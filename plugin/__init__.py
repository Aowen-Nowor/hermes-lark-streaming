"""注册期配置注入与依赖门槛检查（V2 platform 形态）.

职责（自 V1 plugin/ 模块收敛）：
- ``check_lark_oapi_floor``：lark-oapi 版本门槛检测（hermes ≥0.19 传
  extra_ua_tags，旧 SDK 必然 WS 断连——2026-07-21 生产中断根因的防御）
- ``ensure_streaming_config``：向 config.yaml 注入默认插件配置节
- ``cleanup_streaming_config``：卸载时清理

V2 不再包含任何运行时补丁逻辑（零 monkey-patch）。
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime
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

# 默认插件配置（键语义与 V1 保持一致，用户配置零迁移成本）
_DEFAULT_STREAMING_CONFIG: dict[str, Any] = {
    "panel_expanded": False,
    "streaming_panel_expanded": False,
    "print_strategy": "delay",
    "print_step": 4,
    "flush_interval_ms": 200,
    "card_ttl_sec": 600,
    "max_tool_steps": 20,
    "max_reasoning_rounds": 20,
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


def _backup_config() -> None:
    """首次注入前备份 config.yaml（已有备份则跳过）。"""
    config_path = _get_hermes_config_path()
    if not config_path.exists():
        return
    backup_pattern = f"config.yaml.*.{_PLUGIN_NAME}"
    existing = list(config_path.parent.glob(backup_pattern))
    if existing:
        return
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = config_path.parent / f"config.yaml.{timestamp}.{_PLUGIN_NAME}"
    try:
        shutil.copy2(config_path, backup_path)
        _logger.info("feishu-streaming: backed up config.yaml to %s", backup_path.name)
    except Exception:
        _logger.exception("feishu-streaming: config backup failed")


def _prepare_config(cfg: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for k, v in cfg.items():
        result[k] = _prepare_config(v) if isinstance(v, dict) else v
    return result


def _ensure_legacy_section(raw: dict[str, Any]) -> bool:
    """兼容：把 V1 残留的 hermes_lark_streaming 节迁移到 feishu_streaming."""
    if _LEGACY_CONFIG_KEY in raw and _CONFIG_KEY not in raw:
        raw[_CONFIG_KEY] = raw.pop(_LEGACY_CONFIG_KEY)
        _logger.info("feishu-streaming: migrated config section %s -> %s", _LEGACY_CONFIG_KEY, _CONFIG_KEY)
        return True
    return False


def ensure_streaming_config() -> None:
    """确保 config.yaml 存在插件配置节（V1 节自动迁移）。"""
    config_path = _get_hermes_config_path()
    if not config_path.exists():
        _logger.warning("feishu-streaming: config.yaml not found — config injection skipped")
        return
    try:
        text = config_path.read_text(encoding="utf-8")
        raw = yaml.safe_load(text) or {}
        changed = _ensure_legacy_section(raw)

        if _CONFIG_KEY not in raw:
            _backup_config()
            raw[_CONFIG_KEY] = dict(_DEFAULT_STREAMING_CONFIG)
            changed = True
            _logger.info("feishu-streaming: injected %s config section", _CONFIG_KEY)

        if changed:
            prepped = _prepare_config(raw)
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(prepped, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    except Exception:
        _logger.exception("feishu-streaming: config injection failed")


def cleanup_streaming_config() -> None:
    """卸载：移除插件配置节。"""
    config_path = _get_hermes_config_path()
    if not config_path.exists():
        return
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        changed = False
        for key in (_CONFIG_KEY, _LEGACY_CONFIG_KEY):
            if key in raw:
                del raw[key]
                changed = True
        if changed:
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(raw, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    except Exception:
        _logger.exception("feishu-streaming: config cleanup failed")
