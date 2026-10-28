"""CardKit v2.0.4/2.0.5 — 工具名 → 飞书官方 standard_icon / 展示标题映射.

V1 插件 ``state/tooluse.py::_TOOL_DESCRIPTORS`` 的移植（v2.0.4 仅图标表；
v2.0.5 补齐 V1 的标题机制）：

- ``resolve_tool_icon(name)``：别名归一化后匹配官方图标 token（未命中
  ``DEFAULT_TOOL_ICON``；v2.0.10 起为官方枚举 token ``code_outlined``）
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
    "DEFAULT_NOTICE_ICON",
    "resolve_tool_icon",
    "resolve_notice_icon",
    "split_notice_lead",
    "humanize_tool_name",
    "tool_display_title",
]

# v2.0.10：默认工具图标从 tool_02 换为官方枚举 token（用户验收要求“面板一律
# 飞书官方图标”，而 tool_02 不在官方 enumerations-for-icons 清单内）。
# v2.0.16：terminal 从 setting_outlined 并入 code_outlined（用户验收：shell 行
# 齿轮图标语义不符，统一代码图标；默认与 shell 行同 token 为预期结果）。
DEFAULT_TOOL_ICON = "code_outlined"

# v2.0.10：非工具类通报行（限流/警告/后台通知等 emoji 开头且解析不出工具结构
# 的行）→ 官方图标映射。emoji token 全部取自官方 enumerations-for-icons 清单
# （2026-10 抓取存档：time/warning/info/storage/refresh 均 _outlined 线性风格）。
# 键为 emoji 首字符（剥 U+FE0F 变体选择符后），覆盖同族常见变体。
DEFAULT_NOTICE_ICON = "info_outlined"
_NOTICE_ICONS: dict[str, str] = {
    "\u23f1": "time_outlined",      # ⏱（限流/耗时提示，含 U+FE0F 变体）
    "\u23f2": "time_outlined",      # ⏲
    "\u23f0": "time_outlined",      # ⏰
    "\u23f3": "time_outlined",      # ⏳（Working / 等待）
    "\u26a0": "warning_outlined",   # ⚠（provider 错误/警告）
    "\U0001f6a8": "warning_outlined",  # 🚨
    "\u2139": "info_outlined",      # ℹ
    "\U0001f4a1": "info_outlined",  # 💡
    "\U0001f4be": "storage_outlined",  # 💾（Self-improvement review）
    "\U0001f504": "refresh_outlined",  # 🔄
    "\u267b": "refresh_outlined",   # ♻
}

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
    # v2.0.4 按生产工具名补充，保证 shell 行命中官方图标；
    # v2.0.16 用户验收：setting_outlined 齿轮 → code_outlined）
    (("exec", "bash", "command", "run", "terminal"), "code_outlined"),
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


def resolve_notice_icon(lead_token: str | None) -> str:
    """通报行 emoji 首字符 → 官方图标 token；未命中 ``info_outlined``.

    ``lead_token`` 允许带 U+FE0F 变体选择符（如 "⏱️"）；剥除后查表。
    永不抛异常。
    """
    try:
        key = (lead_token or "").replace("\ufe0f", "").strip()
        return _NOTICE_ICONS.get(key, DEFAULT_NOTICE_ICON)
    except Exception:  # noqa: BLE001 — 图标解析绝不影响面板渲染
        return DEFAULT_NOTICE_ICON


def split_notice_lead(text: str) -> tuple[str, str] | None:
    """emoji 开头的通报行 → ``(官方图标 token, 剥 emoji 后正文)``；非 emoji 行 None.

    仅对“解析不出工具结构的 emoji 行”由渲染层兑底调用（工具行/动词行在
    ``parse_progress_line`` 已被结构化吸收，不进本函数）。首 token 必须
    整体是 emoji（剥 U+FE0F/ZWJ 后逐字校验，或命中通报图标表——覆盖
    U+2139 等不在 _EMOJI_RE 区间的符号），否则维持裸 markdown 兑底。
    永不抛异常。
    """
    try:
        t = (text or "").strip()
        if not t:
            return None
        from .progline import _EMOJI_LEAD_RE, _EMOJI_RE

        m = _EMOJI_LEAD_RE.match(t)
        if not m:
            return None
        lead, rest = m.group(1), m.group(2).strip()
        core = lead.replace("\ufe0f", "").replace("\u200d", "")
        if not core or not rest:
            return None
        if core not in _NOTICE_ICONS and not all(_EMOJI_RE.match(ch) for ch in core):
            return None
        return resolve_notice_icon(lead), rest
    except Exception:  # noqa: BLE001 — 兑底解析绝不影响面板渲染
        return None


def resolve_tool_icon(name: str | None) -> str:
    """按工具名解析官方图标 token；未命中返回 ``DEFAULT_TOOL_ICON``."""
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
