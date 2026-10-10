"""CardKit v2.0.4 — 工具名 → 飞书官方 standard_icon 映射.

V1 插件 ``state/tooluse.py::_TOOL_DESCRIPTORS`` 的图标表移植（仅图标解析，
sanitizer/title 机制不随迁 —— V2 的面板行文本由 gateway progress 模板渲染，
这里只负责把硬编码 tool_02 升级为按工具名匹配的官方图标，对齐 V1 观感）。

匹配规则与 V1 一致：别名归一化（小写、``-``→``_``）后全等或 ``{alias}_`` 前缀
命中（如 ``read_file`` → read 系、``web_search`` → search 系）；未命中回退
``tool_02``（通用工具图标）。永不抛异常。
"""

from __future__ import annotations

import logging

_logger = logging.getLogger("hermes_lark_streaming.cardkit.tool_icons")

__all__ = ["DEFAULT_TOOL_ICON", "resolve_tool_icon"]

DEFAULT_TOOL_ICON = "tool_02"

# V1 state/tooluse.py::_TOOL_DESCRIPTORS 图标表（aliases → standard_icon token）
_TOOL_ICONS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("skill",), "app-default_outlined"),
    (("read", "open"), "file-link-text_outlined"),
    (("write", "edit"), "edit_outlined"),
    (("web_search", "web-search", "search"), "search_outlined"),
    (("web_fetch", "web-fetch", "fetch"), "language_outlined"),
    (("grep",), "doc-search_outlined"),
    (("glob",), "folder_outlined"),
    # "terminal" 为 hermes 实际 shell 工具名（V1 表只有 exec/bash/command/run，
    # v2.0.4 按生产工具名补充，保证 shell 行命中官方图标）
    (("exec", "bash", "command", "run", "terminal"), "setting_outlined"),
    (("browser", "playwright", "navigate"), "browser-mac_outlined"),
    (("agent", "task", "spawn"), "robot_outlined"),
    (("check", "determine", "verify"), "list-check_outlined"),
    (("summarize", "analyze", "prepare"), "report_outlined"),
)


def resolve_tool_icon(name: str | None) -> str:
    """按工具名解析官方图标 token；未命中返回 ``tool_02``."""
    try:
        normalized = (name or "").strip().lower().replace("-", "_")
        if not normalized:
            return DEFAULT_TOOL_ICON
        for aliases, icon in _TOOL_ICONS:
            for alias in aliases:
                if normalized == alias or normalized.startswith(f"{alias}_"):
                    return icon
    except Exception:  # 图标解析绝不影响面板渲染
        _logger.debug("tool icon resolve failed for %r", name, exc_info=True)
    return DEFAULT_TOOL_ICON
