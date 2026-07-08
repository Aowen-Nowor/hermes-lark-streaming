"""Intercepts Hermes message pipeline, renders real-time streaming cards with"""

import logging
from pathlib import Path

_logger = logging.getLogger("hermes_lark_streaming")

_plugin_yaml = Path(__file__).resolve().parent / "plugin.yaml"
if _plugin_yaml.exists():
    for _line in _plugin_yaml.read_text(encoding="utf-8").splitlines():
        if _line.startswith("version:"):
            __version__ = _line.split(":", 1)[1].strip().strip('"').strip("'")
            break
    else:
        __version__ = "unknown"
else:
    __version__ = "unknown"

try:
    from .plugin import register
except ImportError:
    from hermes_lark_streaming.plugin import register  # type: ignore[no-redef]

__all__ = ["register", "__version__"]
