"""注册期配置注入与依赖门槛检查（V2 platform 形态）.

职责（自 V1 plugin/ 模块收敛）：
- ``check_lark_oapi_floor``：lark-oapi 版本门槛检测（hermes ≥0.19 传
  extra_ua_tags，旧 SDK 必然 WS 断连——2026-07-21 生产中断根因的防御）
- ``ensure_streaming_config``：向 config.yaml 注入默认插件配置节
- ``cleanup_streaming_config``：卸载时清理

V2 不再包含任何运行时补丁逻辑（零 monkey-patch）。
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import yaml

_logger = logging.getLogger("hermes_lark_streaming")

_PLUGIN_NAME = "feishu-streaming-platform"
# V2 配置节名；兼容读取 V1 残留的 hermes_lark_streaming 节
_CONFIG_KEY = "feishu_streaming"
_LEGACY_CONFIG_KEY = "hermes_lark_streaming"

# lark-oapi 最低版本门槛（hermes-agent >= 0.19 需要 extra_ua_tags 支持）
_LARK_OAPI_MIN = (1, 6, 4)

# 默认插件配置（键语义与 V1 保持一致，用户配置零迁移成本；v2.0.17 移除
# V1 遗留死键 linear/panel_expanded/flush_interval_ms/max_tool_steps/
# max_reasoning_rounds——V2 引擎从未消费，详见 Task 24 审计）
_DEFAULT_STREAMING_CONFIG: dict[str, Any] = {
    "streaming_panel_expanded": False,
    "print_strategy": "delay",
    "print_step": 4,
    "card_ttl_sec": 600,
    "footer": {
        "fields": [
            ["status", "elapsed", "model", "cost", "compression_exhausted"],
        ],
        "show_label": False,
    },
}


def _parse_version(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in str(v).split(".")[:3])
    except (ValueError, TypeError):
        return ()


def check_lark_oapi_floor() -> None:
    """启动时检测 lark-oapi 版本，低于门槛留下 ERROR/WARNING 级日志指纹。"""
    import importlib.metadata as _md

    try:
        installed = _md.version("lark-oapi")
    except Exception:  # noqa: BLE001 — metadata 不可用仅降级为警告
        _logger.warning("feishu-streaming: lark-oapi version undetectable — cannot verify >=1.6.4 floor")
        return
    ver = _parse_version(installed)
    if ver and ver >= _LARK_OAPI_MIN:
        return
    hermes_ver = ""
    try:
        hermes_ver = _md.version("hermes-agent")
    except Exception:  # noqa: BLE001 — hermes 版本未知按 0 处理
        _logger.debug("feishu-streaming: hermes-agent version undetectable")
    breaks_ws = bool(_parse_version(hermes_ver)) and _parse_version(hermes_ver) >= (0, 19, 0)
    _logger.log(
        logging.ERROR if breaks_ws else logging.WARNING,
        "feishu-streaming: lark-oapi %s below required >=1.6.4 (hermes-agent=%s). "
        "hermes >= 0.19 passes extra_ua_tags to lark.Client — old SDKs reject it and "
        "the Feishu WebSocket cannot connect. Fix: pip install -U 'lark-oapi>=1.6.4'",
        installed,
        hermes_ver or "unknown",
    )


def _get_hermes_config_path() -> Path:
    from .config.reader import _get_hermes_config_path as _get_path

    return _get_path()


@contextlib.contextmanager
def _config_write_lock(config_path: Path):
    """跨进程保护显式配置迁移；锁文件不含配置或秘密。"""
    lock_path = config_path.with_name(config_path.name + ".migration.lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _atomic_write_text(config_path: Path, text: str) -> None:
    """同目录临时文件 + os.replace，避免中断留下半份 YAML。"""
    fd, temp_name = tempfile.mkstemp(
        prefix=config_path.name + ".", suffix=".tmp", dir=config_path.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, config_path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _migrated_config_text(text: str) -> tuple[str, bool]:
    """文本级迁移，保留其他配置、注释和秘密，不做整份 YAML 重序列化。"""
    updated = text
    changed = False
    has_current = re.search(r"^feishu_streaming\s*:", updated, re.MULTILINE)
    has_legacy = re.search(r"^hermes_lark_streaming\s*:", updated, re.MULTILINE)
    if has_legacy and not has_current:
        updated = re.sub(
            r"^hermes_lark_streaming\s*:", "feishu_streaming:", updated,
            count=1, flags=re.MULTILINE,
        )
        changed = True
        has_current = re.search(r"^feishu_streaming\s*:", updated, re.MULTILINE)
    if not has_current:
        if updated and not updated.endswith("\n"):
            updated += "\n"
        updated += yaml.dump(
            {_CONFIG_KEY: _DEFAULT_STREAMING_CONFIG},
            default_flow_style=False, allow_unicode=True, sort_keys=False,
        )
        changed = True
    return updated, changed


def _without_plugin_sections(text: str) -> tuple[str, bool]:
    """仅移除插件顶层配置节，其他行逐字保留。"""
    kept: list[str] = []
    removing = False
    changed = False
    for line in text.splitlines(keepends=True):
        top_level = re.match(r"^([A-Za-z_][\w-]*)\s*:", line)
        if top_level:
            removing = top_level.group(1) in {_CONFIG_KEY, _LEGACY_CONFIG_KEY}
            if removing:
                changed = True
                continue
        if not removing:
            kept.append(line)
    return "".join(kept), changed


def ensure_streaming_config() -> None:
    """用户显式调用的 V1→V2 配置迁移；注册流程不得调用。"""
    config_path = _get_hermes_config_path()
    if not config_path.exists():
        raise FileNotFoundError(f"Hermes config not found: {config_path}")
    with _config_write_lock(config_path):
        updated, changed = _migrated_config_text(
            config_path.read_text(encoding="utf-8")
        )
        if changed:
            _atomic_write_text(config_path, updated)
            _logger.info("feishu-streaming: migrated %s config section", _CONFIG_KEY)


def cleanup_streaming_config() -> None:
    """用户显式调用的卸载清理：仅删除插件配置节。"""
    config_path = _get_hermes_config_path()
    if not config_path.exists():
        return
    with _config_write_lock(config_path):
        updated, changed = _without_plugin_sections(
            config_path.read_text(encoding="utf-8")
        )
        if changed:
            _atomic_write_text(config_path, updated)
            _logger.info("feishu-streaming: removed plugin config sections")
