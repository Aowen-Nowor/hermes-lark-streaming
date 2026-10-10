"""CardKit v2.0.4/2.0.5 — 工具名 → 飞书官方 standard_icon / 展示标题映射.

V1 插件 ``state/tooluse.py::_TOOL_DESCRIPTORS`` 的移植（v2.0.4 仅图标表；
v2.0.5 补齐 V1 的标题机制）：

- ``resolve_tool_icon(name)``：别名归一化后匹配官方图标 token（未命中 ``tool_02``）
- ``tool_display_title(name)``：描述符标题（V1: read→"Read"、grep→"Search text"…），
  未命中回退 ``humanize_tool_name``（``-``/``_``→空格 + 首字母大写，如
  hermes 的 ``terminal`` → "Terminal"——与 V1 截图中的工具行标题完全一致）
- ``humanize_tool_name(name)``：V1 state/tooluse.py 同名函数移植

注意：``terminal`` 只进图标表（v2.0.4 生产补充），**不进标题表**——V1 的
描述符表没有 terminal，实际显示的是 humanize 产物 "Terminal"，为对齐用户
看到的 V1 效果保持一致。匹配永不抛异常。
"""

from __future__ import annotations

import logging

_logger = logging.getLogger("hermes_lark_streaming.cardkit.tool_icons")

__all__ = [
    "DEFAULT_TOOL_ICON",
    "resolve_tool_icon",
    "humanize_tool_name",
    "tool_display_title",
]

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


# V1 state/tooluse.py::_TOOL_DESCRIPTORS 标题表（aliases → 展示标题）。
# 与图标表同构但不含 terminal（V1 无 terminal 条目 → humanize → "Terminal"）。
_TOOL_TITLES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("skill",), "Load skill"),
    (("read", "open"), "Read"),
    (("write", "edit"), "Edit"),
    (("web_search", "web-search", "search"), "Search"),
    (("web_fetch", "web-fetch", "fetch"), "Fetch web page"),
    (("grep",), "Search text"),
    (("glob",), "Search files"),
    (("exec", "bash", "command", "run"), "Run command"),
    (("browser", "playwright", "navigate"), "Browser"),
    (("agent", "task", "spawn"), "Run sub-agent"),
    (("check", "determine", "verify"), "Check"),
    (("summarize", "analyze", "prepare"), "Analyze"),
)


def humanize_tool_name(name: str | None) -> str:
    """V1 state/tooluse.py 同款：``-``/``_``→空格、首字母大写（terminal→Terminal）."""
    try:
        cleaned = (name or "").replace("-", " ").replace("_", " ").strip()
        if not cleaned:
            return "Tool"
        return cleaned[0].upper() + cleaned[1:]
    except Exception:  # noqa: BLE001 — 标题解析绝不影响面板渲染
        return "Tool"


def tool_display_title(name: str | None) -> str:
    """V1 build_display_steps 同款：描述符标题优先，未命中 humanize 回退。"""
    try:
        normalized = (name or "").strip().lower().replace("-", "_")
        if normalized:
            for aliases, title in _TOOL_TITLES:
                for alias in aliases:
                    if normalized == alias or normalized.startswith(f"{alias}_"):
                        return title
    except Exception:  # noqa: BLE001
        pass
    return humanize_tool_name(name)


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
