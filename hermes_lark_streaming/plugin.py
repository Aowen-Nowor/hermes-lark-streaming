"""Plugin entry point — register(ctx) for Hermes plugin system.

On registration:
- Backs up ``config.yaml`` before first modification (format: config.yaml.YYYYMMDD_HHMMSS.hermes-lark-streaming)
- Ensures ``config.yaml`` has a clean top-level ``streaming`` section
  with the minimal required defaults so streaming cards work out of the box.
- Ensures ``hermes-lark-streaming`` is listed in ``plugins.enabled``.

On unregistration:
- Removes the ``streaming`` section and ``plugins.enabled`` entry from config.yaml
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

if TYPE_CHECKING:
    from hermes_cli.plugins import PluginContext

_logger = logging.getLogger("hermes_lark_streaming")


def _get_hermes_config_path() -> Path:
    """动态获取 Hermes 配置文件路径。
    
    在多 Profile 场景下，HERMES_HOME 环境变量会在 Gateway 启动时
    通过 _apply_profile_override() 设置。如果在模块导入时就读取
    该变量，可能会读到错误的路径。
    
    此函数每次调用时都重新读取环境变量，确保始终使用正确的路径。
    """
    return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "config.yaml"


# 向后兼容：保留常量名，但改为使用 getter 函数
# 注意：这仍然在模块导入时求值，但大多数场景下是正确的
# 对于多 Profile 场景，请使用 _get_hermes_config_PATH()
_HERMES_CONFIG_PATH = _get_hermes_config_path()

_PLUGIN_NAME = "hermes-lark-streaming"

# Default streaming config injected into config.yaml on first load
_DEFAULT_STREAMING_CONFIG: dict[str, Any] = {
    "enabled": True,
    "linear": True,
    "panel_expanded": False,
    "card_ttl_sec": 600,
    "inject_time": False,
    "footer": {
        "fields": [
            ["status", "elapsed", "model", "api_calls"],
            ["tokens", "context", "history_offset", "compression_exhausted"],
        ],
        "show_label": True,
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
    """Ensure ``config.yaml`` has a clean top-level ``streaming`` section."""
    # 每次都动态读取 HERMES_HOME，支持多 Profile 场景
    config_path = _get_hermes_config_path()
    if not config_path.exists():
        _logger.warning("config.yaml not found at %s, skipping config injection", config_path)
        return

    try:
        text = config_path.read_text(encoding="utf-8")
        raw = yaml.safe_load(text) or {}
        changed = False

        # Ensure streaming section exists
        if "streaming" not in raw:
            # Back up config.yaml before first modification
            _backup_config()

            raw["streaming"] = dict(_DEFAULT_STREAMING_CONFIG)
            changed = True
            _logger.info("Injected top-level streaming config into %s", config_path)

        # Ensure plugins.enabled includes this plugin
        plugins = raw.get("plugins")
        if isinstance(plugins, dict):
            enabled = plugins.get("enabled")
            if isinstance(enabled, list) and _PLUGIN_NAME not in enabled:
                # Back up if not already done (e.g. streaming section existed but plugin wasn't listed)
                _backup_config()

                enabled.append(_PLUGIN_NAME)
                changed = True
                _logger.info("Added %s to plugins.enabled", _PLUGIN_NAME)

        if changed:
            prepped = _prepare_config(raw)
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(prepped, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    except Exception:
        _logger.exception("Failed to ensure streaming config in config.yaml")


def _cleanup_config() -> None:
    """Remove ``streaming`` section and ``plugins.enabled`` entry.

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

        if "streaming" in raw:
            del raw["streaming"]
            changed = True
            _logger.info("Removed top-level streaming config from %s", config_path)

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
        _logger.exception("Failed to clean up streaming config / plugins.enabled")


def register(ctx: "PluginContext") -> None:
    """Register hermes-lark-streaming as a Hermes plugin.

    Applies runtime monkey patches to GatewayRunner, AIAgent, and
    Scheduler so that streaming CardKit v2.0 cards are sent during
    Feishu conversations — no source file modification required.
    """
    _ensure_streaming_config()

    _logger.info("hermes-lark-streaming: applying runtime patches...")
    try:
        from .monkey_patch import apply_patches

        apply_patches()
        _logger.info("hermes-lark-streaming: patches applied (check logs for per-module status)")
    except Exception:
        _logger.exception("hermes-lark-streaming: failed to apply patches")


def unregister(ctx: "PluginContext") -> None:
    """Unregister hermes-lark-streaming.

    Cleans up the injected streaming config from config.yaml.
    """
    _cleanup_config()
    _logger.info("hermes-lark-streaming: unregistered")
