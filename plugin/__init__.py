"""Plugin entry point — register(ctx) for Hermes plugin system.

On registration:
- Backs up ``config.yaml`` before first modification (format: config.yaml.YYYYMMDD_HHMMSS.hermes-lark-streaming)
- Ensures ``config.yaml`` has a clean top-level ``hermes_lark_streaming`` section
  with the minimal required defaults so streaming cards work out of the box.
- Ensures ``hermes-lark-streaming`` is listed in ``plugins.enabled``.

On unregistration:
- Removes the ``hermes_lark_streaming`` section and ``plugins.enabled`` entry from config.yaml
  (does NOT restore the backup — user can manually restore if needed).
"""

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
    """动态获取 Hermes 配置文件路径（从 config/reader.py 复用）."""
    from ..config.reader import _get_hermes_config_path as _get_path
    return _get_path()


_PLUGIN_NAME = "hermes-lark-streaming"

# Default hermes_lark_streaming config injected into config.yaml on first load
_DEFAULT_STREAMING_CONFIG: dict[str, Any] = {
    "panel_expanded": False,
    "streaming_panel_expanded": False,
    "print_strategy": "delay",
    "print_step": 4,
    "flush_interval_ms": 200,
    "card_ttl_sec": 600,
    "max_tool_steps": 20,
    "max_reasoning_rounds": 20,
    # v1.2.0: agent 卡片头部（head）开关。默认关闭。
    # 开启后，agent 回复卡片顶部显示状态头部：
    #   流式中蓝色"处理中" → 完成绿色"已完成" / 出错红色"出错" / 中断红色"已停止"。
    # 注意：开启后封卡走全量重建路径（保证头部颜色切换），关时走增量封卡（性能更优）。
    # 仅影响 agent 流式卡片和完成态卡片；cron/gateway 卡片不受影响。
    "header": {
        "enabled": False,
    },
    "footer": {
        "fields": [
            ["status", "elapsed", "model", "cost", "compression_exhausted"],
        ],
        "show_label": False,
    },
}


def _backup_config() -> None:
    """Back up config.yaml before first modification.

    Backup filename format: config.yaml.YYYYMMDD_HHMMSS.hermes-lark-streaming
    Only creates one backup per plugin installation (skips if a backup already exists).
    """
    # 每次都动态读取 HERMES_HOME，支持多 Profile 场景
    config_path = _get_hermes_config_path()
    if not config_path.exists():
        return

    # Check if a backup for this plugin already exists
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
    """Pre-process config dict before YAML dump.

    Handles nested dicts recursively. Footer fields are kept as-is
    (2D array for row layout) so the YAML output preserves the
    visual row structure.
    """
    result: dict[str, Any] = {}
    for k, v in cfg.items():
        if isinstance(v, dict):
            result[k] = _prepare_config(v)
        else:
            result[k] = v
    return result


def _ensure_streaming_config() -> None:
    """Ensure ``config.yaml`` has a clean top-level ``hermes_lark_streaming`` section."""
    # 每次都动态读取 HERMES_HOME，支持多 Profile 场景
    config_path = _get_hermes_config_path()
    if not config_path.exists():
        _logger.warning("config.yaml not found at %s, skipping config injection", config_path)
        return

    try:
        text = config_path.read_text(encoding="utf-8")
        raw = yaml.safe_load(text) or {}
        changed = False

        # Ensure hermes_lark_streaming section exists
        if "hermes_lark_streaming" not in raw:
            # Back up config.yaml before first modification
            _backup_config()

            raw["hermes_lark_streaming"] = dict(_DEFAULT_STREAMING_CONFIG)
            changed = True
            _logger.info("Injected top-level hermes_lark_streaming config into %s", config_path)

        # NOTE: We intentionally do NOT migrate `hermes_lark_streaming.show_label` to
        # `hermes_lark_streaming.footer.show_label`.  If `show_label` appears at the
        # hermes_lark_streaming top level (or even the config.yaml top level), it may have
        # been placed there by the user for other purposes.  Our plugin only
        # writes `hermes_lark_streaming.footer.show_label` and never touches user-defined
        # keys outside that scope.

        # Ensure plugins.enabled includes this plugin
        plugins = raw.get("plugins")
        if isinstance(plugins, dict):
            enabled = plugins.get("enabled")
            if isinstance(enabled, list) and _PLUGIN_NAME not in enabled:
                # Back up if not already done (e.g. hermes_lark_streaming section existed but plugin wasn't listed)
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
    """Remove ``hermes_lark_streaming`` section and ``plugins.enabled`` entry.

    Called via ``unregister()`` when Hermes plugin system supports it.
    """
    # 每次都动态读取 HERMES_HOME，支持多 Profile 场景
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
    """Register hermes-lark-streaming as a Hermes plugin.

    Applies runtime monkey patches to GatewayRunner, AIAgent, and
    Scheduler so that streaming CardKit v2.0 cards are sent during
    Feishu conversations — no source file modification required.
    """
    _ensure_streaming_config()

    # ── Diagnostic: log key config for troubleshooting ──
    try:
        from ..config import Config
        _diag_cfg = Config()
        _logger.info(
            "hermes-lark-streaming v%s: config diagnostic — "
            "enabled=%s linear=%s gateway_cards=%s "
            "panel_expanded=%s streaming_panel_expanded=%s print_strategy=%s "
            "print_step=%s flush_interval=%sms card_ttl=%ss header_enabled=%s "
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
            _diag_cfg.header_enabled,
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

    # ── Pre-warm FeishuClient ──
    # Initialize the Feishu API client at plugin registration time instead of
    # lazily on the first message.  This eliminates ~50-100ms latency on the
    # first card creation, improving the time-to-first-paint for users.
    # v1.3.6 fix (issue: aowen-unrecognized-bug): 原实现在 except RuntimeError
    # 块里有 return，导致后续 /aowen hook 注册代码从不执行。改为只跳过预热，
    # 不 return——hook 注册必须总能到达。
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
                loop.create_task(ctrl._ensure_init())
                _logger.info("hermes-lark-streaming v%s: FeishuClient pre-warm scheduled", __version__)
            else:
                _logger.debug("hermes-lark-streaming v%s: event loop not running, skipping pre-warm", __version__)
    except Exception:
        _logger.debug("hermes-lark-streaming v%s: FeishuClient pre-warm skipped", __version__, exc_info=True)

    # ── v1.1.0: Register /aowen command hook (Task 3.7) ──
    try:
        from ..aowen import handle_pre_gateway_dispatch
        ctx.register_hook("pre_gateway_dispatch", handle_pre_gateway_dispatch)
        _logger.info("hermes-lark-streaming v%s: /aowen commands registered (help, status, monitor)", __version__)
    except Exception:
        _logger.debug("hermes-lark-streaming v%s: /aowen hook registration skipped", __version__, exc_info=True)


def unregister(ctx: "PluginContext") -> None:
    """Unregister hermes-lark-streaming.

    Cleans up the injected hermes_lark_streaming config from config.yaml.
    """
    _cleanup_config()
    # Clear controller sessions
    try:
        from ..controller import get_controller
        ctrl = get_controller()
        ctrl._sess_clear()
    except Exception:
        pass
    _logger.info("hermes-lark-streaming: unregistered")
