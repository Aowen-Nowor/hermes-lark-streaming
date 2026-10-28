"""hermes-feishu-streaming-platform — 飞书 CardKit 流式卡片平台插件（V2，零钩子）.

架构：继承 hermes 内置 FeishuAdapter，出站帧经 FrameComposer 重组为
CardKit 流式卡片。平台名 ``feishu-streaming``，与内置 ``feishu`` 平台并存、
随时可切回。V2.0.0 起为纯 platform 形态：无任何运行时 monkey-patch。
"""

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
    from .plugin.adapter import FeishuStreamingAdapter, register
except Exception as _adapter_err:  # noqa: BLE001 — hermes 不在环境：包导入不炸，register 调用时 fail-fast
    _adapter_import_error = _adapter_err

    def register(ctx):  # type: ignore[misc]
        raise RuntimeError(
            "feishu-streaming: plugin modules unavailable in this environment "
            f"(original import error: {_adapter_import_error!r})"
        )

    FeishuStreamingAdapter = None  # type: ignore[assignment,misc]

__all__ = ["FeishuStreamingAdapter", "__version__", "register"]
