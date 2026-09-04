"""On registration: backs up ``config.yaml`` (timestamped), injects a clean"""

from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from .. import __version__

if TYPE_CHECKING:
    from hermes_cli.plugins import PluginContext

_logger = logging.getLogger("hermes_lark_streaming")

def _get_hermes_config_path() -> Path:
    """动态获取 Hermes 配置文件路径."""
    from ..config.reader import _get_hermes_config_path as _get_path
    return _get_path()

_PLUGIN_NAME = "hermes-lark-streaming"

# Default config injected into config.yaml on first load.
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

# Hold strong refs to pre-warm tasks (prevent GC per Python docs)
_prewarm_tasks: set = set()

# v1.8.0 (P1-1): lark-oapi 最低版本门槛。hermes-agent >= 0.19 会给
# lark.Client.builder() 传 extra_ua_tags——该参数 1.6.4 才引入（4 个版本
# 的 wheel 源码比对证实，1.5.1 无）。混合安装（hermes 升级、lark-oapi
# 留旧）会让飞书 WS 连接在每次重试时抛错：2026-07-21 生产 22 分 39 秒
# 中断的根因。hermes 自身 pin lark-oapi==1.6.8（feishu extra），此门槛
# 只对手工管理依赖的环境报警。
_LARK_OAPI_MIN = (1, 6, 4)

def _parse_version(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in str(v).split(".")[:3])
    except (ValueError, TypeError):
        return ()

def _check_lark_oapi_floor() -> None:
    """v1.8.0 (P1-1): 启动时检测 lark-oapi 版本，低于门槛立即在日志里留下指纹.

    错误级别按风险定级：hermes >= 0.19（会传 extra_ua_tags）+ 旧 SDK =
    必然 WS 断连 → ERROR；其余组合 → WARNING（低于门槛但暂不会断）。
    """
    import importlib.metadata as _md
    try:
        installed = _md.version("lark-oapi")
    except Exception:
        _logger.warning(
            "hermes-lark-streaming: lark-oapi 版本无法探测（importlib.metadata "
            "失败）——无法校验 >=1.6.4 门槛"
        )
        return
    ver = _parse_version(installed)
    if ver and ver >= _LARK_OAPI_MIN:
        return
    hermes_ver = ""
    try:
        hermes_ver = _md.version("hermes-agent")
    except Exception:
        pass
    breaks_ws = bool(_parse_version(hermes_ver)) and _parse_version(hermes_ver) >= (0, 19, 0)
    _logger.log(
        logging.ERROR if breaks_ws else logging.WARNING,
        "hermes-lark-streaming: lark-oapi %s 低于要求的 >=1.6.4（当前 "
        "hermes-agent=%s）。hermes >= 0.19 会向 lark.Client 传 extra_ua_tags，"
        "旧版 lark-oapi 不认这个参数 → 飞书 WS 无法连接（2026-07-21 生产 "
        "22 分钟中断根因）。修复：pip install -U 'lark-oapi>=1.6.4'",
        installed,
        hermes_ver or "unknown",
    )

def _backup_config() -> None:
    """Back up config.yaml once per install (skips if a backup already exists)."""
    config_path = _get_hermes_config_path()
    if not config_path.exists():
        return

    backup_pattern = f"config.yaml.*.{_PLUGIN_NAME}"
    parent = config_path.parent
    existing_backups = list(parent.glob(backup_pattern))
    if existing_backups:
        _logger.info("Backup already exists: %s, skipping", existing_backups[0].name)
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"config.yaml.{timestamp}.{_PLUGIN_NAME}"
    backup_path = parent / backup_name

    try:
        shutil.copy2(config_path, backup_path)
        _logger.info("Backed up config.yaml to %s", backup_path)
    except Exception:
        _logger.exception("Failed to back up config.yaml to %s", backup_path)

def _prepare_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Recursively pre-process config dict for YAML dump."""
    result: dict[str, Any] = {}
    for k, v in cfg.items():
        if isinstance(v, dict):
            result[k] = _prepare_config(v)
        else:
            result[k] = v
    return result

def _ensure_streaming_config() -> None:
    """Ensure ``config.yaml`` has a clean top-level ``hermes_lark_streaming`` section."""
    config_path = _get_hermes_config_path()
    if not config_path.exists():
        _logger.warning("config.yaml not found at %s, skipping config injection", config_path)
        return

    try:
        text = config_path.read_text(encoding="utf-8")
        raw = yaml.safe_load(text) or {}
        changed = False

        if "hermes_lark_streaming" not in raw:
            _backup_config()
            raw["hermes_lark_streaming"] = dict(_DEFAULT_STREAMING_CONFIG)
            changed = True
            _logger.info("Injected top-level hermes_lark_streaming config into %s", config_path)

        # NOTE: We intentionally do NOT migrate `show_label` to `footer.show_label` —
        # user may have placed `show_label` at top level for other purposes.

        plugins = raw.get("plugins")
        if isinstance(plugins, dict):
            enabled = plugins.get("enabled")
            if isinstance(enabled, list) and _PLUGIN_NAME not in enabled:
                _backup_config()
                enabled.append(_PLUGIN_NAME)
                changed = True
                _logger.info("Added %s to plugins.enabled", _PLUGIN_NAME)

        if changed:
            prepped = _prepare_config(raw)
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(prepped, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    except Exception:
        _logger.exception("Failed to ensure hermes_lark_streaming config in config.yaml")

def _cleanup_config() -> None:
    """Remove ``hermes_lark_streaming`` section and ``plugins.enabled`` entry."""
    config_path = _get_hermes_config_path()
    if not config_path.exists():
        return

    try:
        text = config_path.read_text(encoding="utf-8")
        raw = yaml.safe_load(text) or {}
        changed = False

        if "hermes_lark_streaming" in raw:
            del raw["hermes_lark_streaming"]
            changed = True
            _logger.info("Removed top-level hermes_lark_streaming config from %s", config_path)

        plugins = raw.get("plugins")
        if isinstance(plugins, dict):
            enabled = plugins.get("enabled")
            if isinstance(enabled, list) and "hermes-lark-streaming" in enabled:
                enabled.remove("hermes-lark-streaming")
                changed = True
                _logger.info("Removed hermes-lark-streaming from plugins.enabled")

        if changed:
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(raw, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    except Exception:
        _logger.exception("Failed to clean up hermes_lark_streaming config / plugins.enabled")

def register(ctx: "PluginContext") -> None:
    """Register hermes-lark-streaming as a Hermes plugin (applies runtime patches)."""
    # v1.8.0 (P1-1): 依赖健康检测先于一切补丁——07-21 型断连的日志指纹
    # 必须出现在 patch 输出之前，方便运维按时间线定位。
    try:
        _check_lark_oapi_floor()
    except Exception:
        _logger.debug("lark-oapi floor check failed", exc_info=True)

    _ensure_streaming_config()

    try:
        from ..config import Config
        _diag_cfg = Config()
        _logger.info(
            "hermes-lark-streaming v%s: config diagnostic — "
            "enabled=%s linear=%s gateway_cards=%s "
            "panel_expanded=%s streaming_panel_expanded=%s print_strategy=%s "
            "print_step=%s flush_interval=%sms card_ttl=%ss "
            "footer_fields=%s show_label=%s",
            __version__,
            _diag_cfg.enabled,
            _diag_cfg.linear,
            _diag_cfg.gateway_cards,
            _diag_cfg.panel_expanded,
            _diag_cfg.streaming_panel_expanded,
            _diag_cfg.print_strategy,
            _diag_cfg.print_step,
            _diag_cfg.flush_interval_ms,
            _diag_cfg.card_duration_sec,
            _diag_cfg.footer_fields,
            _diag_cfg.footer_show_label,
        )
    except Exception:
        _logger.debug("config diagnostic log failed", exc_info=True)

    _logger.info("hermes-lark-streaming v%s: applying runtime patches...", __version__)
    try:
        from ..patching import apply_patches

        apply_patches()
        _logger.info("hermes-lark-streaming v%s: patches applied (check logs for per-module status)", __version__)
    except Exception:
        _logger.exception("hermes-lark-streaming v%s: failed to apply patches", __version__)

    # Pre-warm FeishuClient at registration to skip ~50-100ms latency on first card.
    try:
        from ..controller import get_controller
        import asyncio

        ctrl = get_controller()
        if ctrl.enabled:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                _logger.debug("hermes-lark-streaming v%s: no running event loop, skipping pre-warm", __version__)
                loop = None
            if loop is not None and loop.is_running():
                _prewarm_task = loop.create_task(ctrl._ensure_init())
                # Hold strong ref to prevent GC (Python docs: save reference to task)
                _prewarm_tasks.add(_prewarm_task)
                _prewarm_task.add_done_callback(_prewarm_tasks.discard)
                _logger.info("hermes-lark-streaming v%s: FeishuClient pre-warm scheduled", __version__)
            else:
                _logger.debug("hermes-lark-streaming v%s: event loop not running, skipping pre-warm", __version__)
    except Exception:
        _logger.debug("hermes-lark-streaming v%s: FeishuClient pre-warm skipped", __version__, exc_info=True)

    try:
        from ..aowen import handle_pre_gateway_dispatch
        ctx.register_hook("pre_gateway_dispatch", handle_pre_gateway_dispatch)
        _logger.info("hermes-lark-streaming v%s: /aowen commands registered (help, status, monitor)", __version__)
    except Exception:
        _logger.debug("hermes-lark-streaming v%s: /aowen hook registration skipped", __version__, exc_info=True)

def unregister(ctx: "PluginContext") -> None:
    """Unregister — clean up injected config and clear sessions."""
    _cleanup_config()
    try:
        from ..controller import get_controller
        ctrl = get_controller()
        ctrl._sess_clear()
    except Exception:
        pass
    _logger.info("hermes-lark-streaming: unregistered")
