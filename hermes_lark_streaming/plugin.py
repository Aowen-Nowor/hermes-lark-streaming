"""Plugin entry point — register(ctx) for Hermes plugin system.

On registration:
- Ensures ``config.yaml`` has a clean top-level ``streaming`` section
  with the minimal required defaults so streaming cards work out of the box.
- Ensures ``hermes-lark-streaming`` is listed in ``plugins.enabled``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from hermes_cli.plugins import PluginContext

_logger = logging.getLogger("hermes_lark_streaming")

_HERMES_CONFIG_PATH = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "config.yaml"

# Default streaming config injected into config.yaml on first load
_DEFAULT_STREAMING_CONFIG: dict[str, Any] = {
    "enabled": True,
    "linear": True,
    "panel_expanded": False,
    "card_ttl_sec": 600,
    "footer": {
        "fields": [
            ["status", "elapsed", "model"],
            ["tokens", "context"],
        ],
        "show_label": True,
    },
}


def _prepare_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Pre-process config dict: flatten ``footer.fields`` before YAML dump.

    The plugin internally uses a 2D array for footer field layout (rows),
    but the documented YAML format is a flat list. This function flattens
    the 2D array so the dumped YAML matches user expectations.
    """
    result: dict[str, Any] = {}
    for k, v in cfg.items():
        if k == "footer" and isinstance(v, dict):
            footer = dict(v)
            flds = footer.get("fields", [])
            if flds and isinstance(flds[0], list):
                footer["fields"] = [item for sub in flds for item in sub]
            result[k] = footer
        elif isinstance(v, dict):
            result[k] = _prepare_config(v)
        else:
            result[k] = v
    return result


def _ensure_streaming_config() -> None:
    """Ensure ``config.yaml`` has a clean top-level ``streaming`` section."""
    if not _HERMES_CONFIG_PATH.exists():
        _logger.warning("config.yaml not found at %s, skipping config injection", _HERMES_CONFIG_PATH)
        return

    try:
        text = _HERMES_CONFIG_PATH.read_text(encoding="utf-8")
        raw = yaml.safe_load(text) or {}
        changed = False

        # Ensure streaming section exists
        if "streaming" not in raw:
            raw["streaming"] = dict(_DEFAULT_STREAMING_CONFIG)
            changed = True
            _logger.info("Injected top-level streaming config into %s", _HERMES_CONFIG_PATH)

        # Ensure plugins.enabled includes this plugin
        plugins = raw.get("plugins")
        if isinstance(plugins, dict):
            enabled = plugins.get("enabled")
            if isinstance(enabled, list) and "hermes-lark-streaming" not in enabled:
                enabled.append("hermes-lark-streaming")
                changed = True
                _logger.info("Added hermes-lark-streaming to plugins.enabled")

        if changed:
            # Prepare config (flatten footer.fields) and dump
            prepped = _prepare_config(raw)
            with open(_HERMES_CONFIG_PATH, "w", encoding="utf-8") as f:
                yaml.dump(prepped, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    except Exception:
        _logger.exception("Failed to ensure streaming config in config.yaml")


def _cleanup_config() -> None:
    """Remove ``streaming`` section and ``plugins.enabled`` entry.

    Called via ``unregister()`` when Hermes plugin system supports it.
    """
    if not _HERMES_CONFIG_PATH.exists():
        return

    try:
        text = _HERMES_CONFIG_PATH.read_text(encoding="utf-8")
        raw = yaml.safe_load(text) or {}
        changed = False

        if "streaming" in raw:
            del raw["streaming"]
            changed = True
            _logger.info("Removed top-level streaming config from %s", _HERMES_CONFIG_PATH)

        plugins = raw.get("plugins")
        if isinstance(plugins, dict):
            enabled = plugins.get("enabled")
            if isinstance(enabled, list) and "hermes-lark-streaming" in enabled:
                enabled.remove("hermes-lark-streaming")
                changed = True
                _logger.info("Removed hermes-lark-streaming from plugins.enabled")

        if changed:
            with open(_HERMES_CONFIG_PATH, "w", encoding="utf-8") as f:
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
        _logger.info("hermes-lark-streaming: patches applied successfully")
    except Exception:
        _logger.exception("hermes-lark-streaming: failed to apply patches")


def unregister(ctx: "PluginContext") -> None:
    """Unregister hermes-lark-streaming.

    Cleans up the injected streaming config from config.yaml.
    """
    _cleanup_config()
    _logger.info("hermes-lark-streaming: unregistered")
