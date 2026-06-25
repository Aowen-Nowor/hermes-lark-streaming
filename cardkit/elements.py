"""CardKit v2.0 — Primitive element builders: panels, footers, helpers."""



from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from .i18n import _LOCALES, _T, _i18n, _t
from .md import (
    _downgrade_tables,
    _split_long_text,
    optimize_markdown_style,
)


__all__ = [
    # Element ID constants
    'STREAMING_ELEMENT_ID',
    'ANSWER_ELEMENT_ID',
    'UNIFIED_PANEL_ELEMENT_ID',
    '_LOADING_ELEMENT_ID',
    '_LOADING_HINT_ELEMENT_ID',
    '_LOADING_IMG_KEY',
    '_IMG_MD_PATTERN',
    # Element builders
    '_extract_images_from_markdown',
    '_collapsible_panel',
    '_streaming_element',
    '_loading_element',
    '_loading_hint_element',
    '_build_tool_step_elements',
    '_build_tool_step_title',
    '_build_reasoning_round_title',
    '_build_tool_step_detail',
    '_build_tool_step_output',
    '_tool_status_info',
    '_format_code_block',
    '_longest_backtick_run',
    '_escape_md',
    '_build_error_panel',
    '_build_header',
    '_build_background_review_panel',
    '_build_footer_elements',
    'build_preservative_seal_actions',
    '_render_footer_field',
    '_compact',
    '_format_elapsed',
    # Unified panel builders
    '_build_unified_panel_placeholder',
    'build_unified_panel',
    'build_panel_header',
    'build_panel_children',
    # Element counting
    '_count_tag_objects',
]

# 匹配 markdown 图片语法: ![alt](url)
_HEADER_STATES: dict[str, dict[str, str]] = {
    "streaming": {"template": "blue", "i18n_key": "processing_prefix"},
    "completed": {"template": "green", "i18n_key": "status_completed"},
    "error": {"template": "red", "i18n_key": "status_error"},
    "stopped": {"template": "red", "i18n_key": "status_stopped"},
}


def _build_header(status: str) -> dict[str, Any]:
    """Build card-level header — streaming blue / completed green / stopped red."""
    cfg = _HEADER_STATES.get(status, _HEADER_STATES["completed"])
    en_text, zh_text = _T[cfg["i18n_key"]]
    return {
        "title": {
            "tag": "plain_text",
            "content": en_text,
            "i18n_content": _i18n(en_text, zh_text),
        },
        "template": cfg["template"],
    }


_IMG_MD_PATTERN = re.compile(r"!\[([^\]]*)\]\((img_[^)\s]+)\)")
_RE_MULTI_NEWLINE = re.compile(r"\n{3,}")
_RE_BACKTICK_RUN = re.compile(r"`+")
_RE_MD_SPECIAL = re.compile(r"([`*_{}\[\]<>])")


def _extract_images_from_markdown(text: str) -> tuple[str, list[dict]]:
    """从 markdown 文本中提取已解析的飞书图片，返回 (清理后的文本, img元素列表).

    将 ``![alt](img_v3_xxx)`` 格式的图片从文本中提取为独立的
    Card 2.0 ``tag: "img"`` 元素，图片从文本中移除以避免重复显示。

    仅处理 ``img_`` 前缀的 URL（已上传到飞书的图片 key）。
    """
    images: list[dict] = []

    def _replace(m: re.Match) -> str:
        alt = m.group(1)
        img_key = m.group(2)
        images.append({
            "tag": "img",
            "img_key": img_key,
            "scale_type": "fit_horizontal",
            "alt": {"tag": "plain_text", "content": alt},
            "corner_radius": "8px",
            "preview": True,
        })
        return ""

    cleaned = _IMG_MD_PATTERN.sub(_replace, text)
    # 清理图片移除后可能留下的空行
    cleaned = _RE_MULTI_NEWLINE.sub("\n\n", cleaned).strip()
    return cleaned, images

if TYPE_CHECKING:
    from ..state.linear import ReasoningRound

# ---------------------------------------------------------------------------
# Element ID constants
# ---------------------------------------------------------------------------

STREAMING_ELEMENT_ID = "streaming_content"
ANSWER_ELEMENT_ID = "answer_content"
UNIFIED_PANEL_ELEMENT_ID = "agent_process_panel"
_LOADING_ELEMENT_ID = "loading_icon"
_LOADING_HINT_ELEMENT_ID = "context_loading_hint"
_LOADING_IMG_KEY = "img_v3_02vb_496bec09-4b43-4773-ad6b-0cdd103cd2bg"


def _count_tag_objects(obj: Any) -> int:
    """Recursively count all JSON objects with a ``tag`` key in a card element tree.

    Feishu Card 2.0 counts every nested tag object toward its 200-element
    limit — including ``standard_icon`` inside a ``div``, ``plain_text``
    inside a ``collapsible_panel`` header, etc.
    """
    count = 0
    if isinstance(obj, dict):
        if "tag" in obj:
            count += 1
        for v in obj.values():
            count += _count_tag_objects(v)
    elif isinstance(obj, list):
        for item in obj:
            count += _count_tag_objects(item)
    return count


def _collapsible_panel(
    *,
    expanded: bool,
    title_el: dict,
    elements: list[dict],
    vertical_spacing: str = "4px",
    icon_position: str = "right",
) -> dict:
    icon_el = {
        "tag": "standard_icon",
        "token": "down-small-ccm_outlined",
        "size": "16px 16px",
    }
    if icon_position == "right":
        icon_el["color"] = "grey"
    return {
        "tag": "collapsible_panel",
        "expanded": expanded,
        "header": {
            "title": title_el,
            "vertical_align": "center",
            "icon": icon_el,
            "icon_position": icon_position,
            "icon_expanded_angle": -180,
        },
        "border": {"color": "grey", "corner_radius": "5px"},
        "vertical_spacing": vertical_spacing,
        "padding": "8px 8px 8px 8px",
        "elements": elements,
    }


def _streaming_element(content: str = "", *, element_id: str = STREAMING_ELEMENT_ID) -> dict:
    return {
        "tag": "markdown",
        "content": content,
        "text_align": "left",
        "text_size": "normal_v2",
        "margin": "0px 0px 0px 0px",
        "element_id": element_id,
    }


def _loading_element() -> dict:
    """Loading spinner element — uses div with icon for schema safety.

    CardKit v2.0 ``div`` elements natively support the ``icon`` property,
    while ``markdown`` elements' icon support varies across API versions.
    Using ``div`` guarantees the spinner renders without 300315 errors.
    """
    return {
        "tag": "div",
        "icon": {
            "tag": "custom_icon",
            "img_key": _LOADING_IMG_KEY,
            "size": "16px 16px",
        },
        "text": {
            "tag": "plain_text",
            "content": " ",
        },
        "element_id": _LOADING_ELEMENT_ID,
    }


def _loading_hint_element() -> dict:
    """上下文加载占位元素 — 首卡创建后插入，首字即显时删除."""
    return {
        "tag": "div",
        "icon": {
            "tag": "standard_icon",
            "token": "time_outlined",
            "size": "16px 16px",
        },
        "text": {
            "tag": "lark_md",
            "content": _T["loading_context"][0],
            "i18n_content": _t("loading_context"),
        },
        "element_id": _LOADING_HINT_ELEMENT_ID,
    }


# ---------------------------------------------------------------------------
# Unified panel builders
# ---------------------------------------------------------------------------

def _build_unified_panel_placeholder(*, expanded: bool = False) -> dict:
    """Build empty unified panel placeholder for initial streaming card.

    This creates a collapsible panel with no content — ready for
    streaming updates via ``partial_update_element``.
    """
    en_title, zh_title = _T["agent_process"]
    panel = _collapsible_panel(
        expanded=expanded,
        title_el={
            "tag": "plain_text",
            "content": en_title,
            "i18n_content": _i18n(en_title, zh_title),
            "text_color": "grey",
            "text_size": "notation",
        },
        elements=[{"tag": "markdown", "content": " "}],
    )
    panel["element_id"] = UNIFIED_PANEL_ELEMENT_ID
    return panel


def build_panel_header(
    *,
    reasoning_rounds: list,  # list of ReasoningRound objects
    current_reasoning_text: str = "",  # in-progress reasoning
    tool_steps: list[dict],
    tool_elapsed_ms: float = 0,
    show_reasoning: bool = True,
) -> dict:
    """Build the header dict for the unified panel.

    Computes the panel title from the current state (round count, tool
    count, elapsed time) and wraps it in the full header structure
    expected by Feishu's ``collapsible_panel`` schema:

        {"title": <plain_text>, "vertical_align": "center",
         "icon": <standard_icon>, "icon_position": "right",
         "icon_expanded_angle": -180}

    The returned dict can be passed directly to Feishu's
    ``partial_update_element`` API as ``partial_element.header``.

    Parameters
    ----------
    reasoning_rounds : list[ReasoningRound]
        Finalised reasoning rounds (each has ``.index``, ``.text``, ``.elapsed_ms``).
    current_reasoning_text : str
        In-progress reasoning text that has not yet been finalised into a round.
    tool_steps : list[dict]
        Tool step dicts (length is used for the title count).
    tool_elapsed_ms : float
        Total elapsed time for tool execution in milliseconds.
    show_reasoning : bool
        Whether to render reasoning content.  When ``False`` the title omits
        the rounds count.
    """
    # ── Title computation (uses ORIGINAL, untrimmed counts) ──
    en_title, zh_title = _T["agent_process"]
    en_parts: list[str] = [en_title]
    zh_parts: list[str] = [zh_title]

    has_reasoning = show_reasoning and (
        reasoning_rounds or current_reasoning_text
    )
    num_rounds = len(reasoning_rounds) + (1 if current_reasoning_text else 0)

    if has_reasoning and num_rounds > 0:
        en_rounds, zh_rounds = _T["rounds"]
        en_parts.append(en_rounds.format(num_rounds))
        zh_parts.append(zh_rounds.format(num_rounds))

    if tool_steps:
        en_tools, zh_tools = _T["tools_count"]
        en_parts.append(en_tools.format(len(tool_steps)))
        zh_parts.append(zh_tools.format(len(tool_steps)))

    # Total elapsed = reasoning elapsed + tool elapsed
    reasoning_elapsed_ms = sum(r.elapsed_ms for r in reasoning_rounds)
    total_elapsed_ms = reasoning_elapsed_ms + tool_elapsed_ms
    if total_elapsed_ms > 0 and (has_reasoning or tool_steps):
        elapsed_str = _format_elapsed(total_elapsed_ms)
        en_parts.append(elapsed_str)
        zh_parts.append(elapsed_str)

    en_full = " · ".join(en_parts)
    zh_full = " · ".join(zh_parts)

    title_el = {
        "tag": "plain_text",
        "content": en_full,
        "i18n_content": _i18n(en_full, zh_full),
        "text_color": "grey",
        "text_size": "notation",
    }

    # Full header structure (matches what _collapsible_panel puts under
    # the "header" key, with icon_position="right" → icon gets color="grey").
    icon_el = {
        "tag": "standard_icon",
        "token": "down-small-ccm_outlined",
        "size": "16px 16px",
        "color": "grey",
    }
    return {
        "title": title_el,
        "vertical_align": "center",
        "icon": icon_el,
        "icon_position": "right",
        "icon_expanded_angle": -180,
    }


_REASONING_DISPLAY_LIMIT = 2000  # 单条推理文本最大显示字数


def _truncate_reasoning(text: str) -> str:
    """截断过长推理文本，防止飞书渲染卡顿.

    所有推理文本渲染路径都应调用此函数，确保截断逻辑一致。
    保证返回值总长度不超过 _REASONING_DISPLAY_LIMIT。
    """
    if len(text) <= _REASONING_DISPLAY_LIMIT:
        return text
    suffix = "\n\n... (已截断，共 {} 字)".format(len(text))
    return text[:_REASONING_DISPLAY_LIMIT - len(suffix)] + suffix


def build_panel_children(
    *,
    reasoning_rounds: list,  # list of ReasoningRound objects
    current_reasoning_text: str = "",  # in-progress reasoning
    tool_steps: list[dict],
    show_reasoning: bool = True,
    panel_events: list[tuple[str, int]] | None = None,
    max_tool_steps: int = 20,
    max_reasoning_rounds: int = 20,
) -> list[dict]:
    """Build the list of child elements for the unified panel body.

    Renders reasoning rounds and tool steps in chronological order (when
    ``panel_events`` is provided) or sequentially (fallback).  Applies
    element-limit trimming when there are too many rounds/tools.

    The returned list can be passed directly to Feishu's
    ``partial_update_element`` API as ``partial_element.elements``.

    Parameters
    ----------
    reasoning_rounds : list[ReasoningRound]
        Finalised reasoning rounds.  Will be trimmed to the last
        ``max_reasoning_rounds`` items if longer.
    current_reasoning_text : str
        In-progress reasoning text that has not yet been finalised into a round.
    tool_steps : list[dict]
        Tool step dicts consumed by :func:`_build_tool_step_elements`.
        Will be trimmed to the last ``max_tool_steps`` items if longer.
    show_reasoning : bool
        Whether to render reasoning content.  When ``False`` reasoning
        blocks are hidden (but tools are still rendered).
    panel_events : list[tuple[str, int]] | None
        Chronological timeline from :attr:`UnifiedLinearState.panel_events`.
        When provided, reasoning and tool elements are interleaved in the
        order they actually occurred, instead of grouping all reasoning
        before all tools.
    max_tool_steps : int
        Maximum number of tool steps to render (older steps are trimmed).
    max_reasoning_rounds : int
        Maximum number of reasoning rounds to render (older rounds are trimmed).
    """
    # ── Element limit trimming ──
    # Feishu Card 2.0 has a hard limit of 200 elements/components.
    # When the card has too many tool steps or reasoning rounds,
    # the preservative seal or full rebuild fails with code 300305
    # ("element exceeds the limit"), causing a text fallback that
    # duplicates content already visible on the card.
    # We trim early items and show a collapse hint instead.
    trimmed_rounds = 0
    trimmed_tools = 0

    if len(reasoning_rounds) > max_reasoning_rounds:
        trimmed_rounds = len(reasoning_rounds) - max_reasoning_rounds
        reasoning_rounds = reasoning_rounds[-max_reasoning_rounds:]

    if len(tool_steps) > max_tool_steps:
        trimmed_tools = len(tool_steps) - max_tool_steps
        tool_steps = tool_steps[-max_tool_steps:]

    # Recount after trimming
    num_rounds = len(reasoning_rounds) + (1 if current_reasoning_text else 0)

    # Filter panel_events to match trimmed items
    if panel_events and (trimmed_rounds > 0 or trimmed_tools > 0):
        # panel_events reference original indices; after trimming, we keep only
        # the last N items, so original index i maps to trimmed index i - offset.
        round_offset = trimmed_rounds
        tool_offset = trimmed_tools
        filtered_events: list[tuple[str, int]] = []
        for kind, idx in panel_events:
            if kind == "reasoning":
                if idx >= round_offset:
                    filtered_events.append((kind, idx - round_offset))
            elif kind == "tool":
                if idx >= tool_offset:
                    filtered_events.append((kind, idx - tool_offset))
        panel_events = filtered_events if filtered_events else None

    # ── Internal elements ──
    children: list[dict] = []

    if trimmed_rounds > 0 or trimmed_tools > 0:
        collapse_parts: list[str] = []
        if trimmed_rounds > 0:
            collapse_parts.append(f"{trimmed_rounds} 轮早期推理")
        if trimmed_tools > 0:
            collapse_parts.append(f"{trimmed_tools} 步早期操作")
        collapse_text = "⚡ 还有 " + "、".join(collapse_parts) + "已折叠"
        children.append({
            "tag": "markdown",
            "content": collapse_text,
            "text_size": "notation",
        })

    if panel_events:
        # ── Chronological rendering: interleave reasoning and tools ──
        rendered_tools: set[int] = set()
        for kind, idx in panel_events:
            if kind == "reasoning" and show_reasoning and idx < len(reasoning_rounds):
                round_ = reasoning_rounds[idx]
                children.append(_build_reasoning_round_title(
                    round_.index, round_.elapsed_ms, finalized=True,
                ))
                if round_.text.strip():
                    children.append({
                        "tag": "div",
                        "margin": "0px 0px 0px 22px",
                        "text": {
                            "tag": "lark_md",
                            "content": _truncate_reasoning(round_.text),
                            "text_size": "notation",
                        },
                    })
            elif kind == "tool" and idx < len(tool_steps):
                if idx not in rendered_tools:
                    step = tool_steps[idx]
                    children.extend(_build_tool_step_elements(step))
                    rendered_tools.add(idx)

        # In-progress reasoning (not yet finalised into panel_events)
        if current_reasoning_text and show_reasoning:
            in_progress_idx = num_rounds  # 1-based
            children.append(_build_reasoning_round_title(
                in_progress_idx, 0, finalized=False,
            ))
            if current_reasoning_text.strip():
                children.append({
                    "tag": "div",
                    "margin": "0px 0px 0px 22px",
                    "text": {
                        "tag": "lark_md",
                        "content": _truncate_reasoning(current_reasoning_text),
                        "text_size": "notation",
                    },
                })

        # Remaining tool steps not in panel_events (safety fallback)
        for i, step in enumerate(tool_steps):
            if i not in rendered_tools:
                children.extend(_build_tool_step_elements(step))

    else:
        # ── Fallback: no timeline available, render sequentially ──
        has_reasoning = show_reasoning and (
            reasoning_rounds or current_reasoning_text
        )
        if has_reasoning:
            # Reasoning rounds
            for round_ in reasoning_rounds:
                children.append(_build_reasoning_round_title(
                    round_.index, round_.elapsed_ms, finalized=True,
                ))
                if round_.text.strip():
                    children.append({
                        "tag": "div",
                        "margin": "0px 0px 0px 22px",
                        "text": {
                            "tag": "lark_md",
                            "content": _truncate_reasoning(round_.text),
                            "text_size": "notation",
                        },
                    })

            # In-progress reasoning
            if current_reasoning_text:
                in_progress_idx = num_rounds
                children.append(_build_reasoning_round_title(
                    in_progress_idx, 0, finalized=False,
                ))
                if current_reasoning_text.strip():
                    children.append({
                        "tag": "div",
                        "margin": "0px 0px 0px 22px",
                        "text": {
                            "tag": "lark_md",
                            "content": _truncate_reasoning(current_reasoning_text),
                            "text_size": "notation",
                        },
                    })

        # Tool steps
        for step in tool_steps:
            children.extend(_build_tool_step_elements(step))

    # Fallback: empty content
    if not children:
        children.append({"tag": "markdown", "content": " "})

    return children


def build_unified_panel(
    *,
    reasoning_rounds: list,  # list of ReasoningRound objects
    current_reasoning_text: str = "",  # in-progress reasoning
    tool_steps: list[dict],
    tool_elapsed_ms: float = 0,
    show_reasoning: bool = True,
    expanded: bool = False,
    element_id: str | None = None,
    panel_events: list[tuple[str, int]] | None = None,
    max_tool_steps: int = 20,
    max_reasoning_rounds: int = 20,
) -> dict:
    """Build the full unified panel content for streaming updates and complete cards.

    Combines reasoning rounds and tool steps into a single collapsible panel.
    The panel title dynamically reflects the current state (round count, tool
    count, elapsed time).

    This is a thin assembler over :func:`build_panel_header` and
    :func:`build_panel_children`; callers that only need to update the
    panel's children (e.g. during streaming partial_update_element) can
    call :func:`build_panel_children` directly to skip header rebuild.

    .. note::
       v1.2.0: ``build_panel_header`` / ``build_panel_children`` 的单独
       调用入口当前**仅由本函数内部使用**，生产代码尚未单独调用它们
       （即"只重建 children 跳过 header"的性能优化预留未启用）。保留
       入口供未来在 flush 性能成为瓶颈时实现该优化。

    Parameters
    ----------
    reasoning_rounds : list[ReasoningRound]
        Finalised reasoning rounds (each has ``.index``, ``.text``, ``.elapsed_ms``).
    current_reasoning_text : str
        In-progress reasoning text that has not yet been finalised into a round.
    tool_steps : list[dict]
        Tool step dicts consumed by :func:`_build_tool_step_elements`.
    tool_elapsed_ms : float
        Total elapsed time for tool execution in milliseconds.
    show_reasoning : bool
        Whether to render reasoning content.  When ``False`` the title omits
        the rounds count and reasoning blocks are hidden.
    expanded : bool
        Whether the panel starts expanded.
    element_id : str | None
        Override for the panel element_id.  Defaults to
        :data:`UNIFIED_PANEL_ELEMENT_ID`.
    panel_events : list[tuple[str, int]] | None
        Chronological timeline from :attr:`UnifiedLinearState.panel_events`.
        When provided, reasoning and tool elements are interleaved in the
        order they actually occurred, instead of grouping all reasoning
        before all tools.
    """
    header = build_panel_header(
        reasoning_rounds=reasoning_rounds,
        current_reasoning_text=current_reasoning_text,
        tool_steps=tool_steps,
        tool_elapsed_ms=tool_elapsed_ms,
        show_reasoning=show_reasoning,
    )
    children = build_panel_children(
        reasoning_rounds=reasoning_rounds,
        current_reasoning_text=current_reasoning_text,
        tool_steps=tool_steps,
        show_reasoning=show_reasoning,
        panel_events=panel_events,
        max_tool_steps=max_tool_steps,
        max_reasoning_rounds=max_reasoning_rounds,
    )
    panel = {
        "tag": "collapsible_panel",
        "expanded": expanded,
        "header": header,
        "border": {"color": "grey", "corner_radius": "5px"},
        "vertical_spacing": "4px",
        "padding": "8px 8px 8px 8px",
        "elements": children,
    }
    panel["element_id"] = element_id or UNIFIED_PANEL_ELEMENT_ID
    return panel


def _build_tool_step_elements(step: dict) -> list[dict]:
    elements: list[dict] = [_build_tool_step_title(step)]
    detail = _build_tool_step_detail(step)
    if detail:
        elements.append(detail)
    output = _build_tool_step_output(step)
    if output:
        elements.append(output)
    return elements


def _build_tool_step_title(step: dict) -> dict:
    status = step.get("status", "running")
    status_info = _tool_status_info(status)
    title = step.get("title", step.get("name", "tool"))
    # 仅用颜色区分状态，不显示状态文字；标题加粗整体上色
    content = f"<font color='{status_info['color']}'>**{_escape_md(title)}**</font>"
    return {
        "tag": "div",
        "icon": {
            "tag": "standard_icon",
            "token": step.get("icon", "tool_02"),
            "color": "grey",
        },
        "text": {
            "tag": "lark_md",
            "content": content,
            "text_size": "notation",
        },
    }


def _build_reasoning_round_title(
    round_index: int,
    elapsed_ms: float,
    finalized: bool,
    failed: bool = False,
) -> dict:
    """构建推理轮次标题 div — 图标 robot-add_outlined + 颜色加粗文字.

    颜色规则：
      - 进行中（未结束）: orange-300
      - 已完成（已结束）: green
      - 失败: red
    """
    if failed:
        color = "red"
    elif finalized:
        color = "green"
    else:
        color = "orange-300"

    # 文本格式：「第 N 轮 · Xs」或「第 N 轮」
    en_label, zh_label = _T["round_n"]
    text = zh_label.format(round_index)  # 用中文格式
    elapsed = _format_elapsed(elapsed_ms) if elapsed_ms > 0 else ""
    if elapsed:
        text += f" · {elapsed}"

    content = f"<font color='{color}'>**{text}**</font>"
    return {
        "tag": "div",
        "icon": {
            "tag": "standard_icon",
            "token": "robot-add_outlined",
            "size": "16px 16px",
            "color": "grey",
        },
        "text": {
            "tag": "lark_md",
            "content": content,
            "text_size": "notation",
        },
    }


def _build_tool_step_detail(step: dict) -> dict | None:
    detail = step.get("detail", "").strip()
    if not detail:
        return None
    return {
        "tag": "div",
        "margin": "0px 0px 0px 22px",
        "text": {
            "tag": "plain_text",
            "content": detail,
            "text_color": "grey",
            "text_size": "notation",
        },
    }


def _build_tool_step_output(step: dict) -> dict | None:
    error_block = step.get("error_block")
    result_block = step.get("result_block")

    lines: list[str] = []
    if error_block:
        lines.append("**Error**")
        lines.append(
            error_block.get("fenced")
            or _format_code_block(error_block.get("content", ""), error_block.get("language", "text"))
        )
    elif result_block:
        lines.append("**Result**")
        lines.append(
            result_block.get("fenced")
            or _format_code_block(result_block.get("content", ""), result_block.get("language", "json"))
        )

    if not lines:
        return None

    return {
        "tag": "div",
        "margin": "0px 0px 0px 22px",
        "text": {
            "tag": "lark_md",
            "content": "\n".join(lines),
            "text_size": "notation",
        },
    }


def _tool_status_info(status: str) -> dict[str, str]:
    return {
        "running": {"label": "", "color": "orange-300"},
        "success": {"label": "", "color": "green"},
        "error": {"label": "", "color": "red"},
    }.get(status, {"label": "", "color": "grey"})


def _format_code_block(content: str, language: str) -> str:
    normalized = content.replace("\r\n", "\n").strip()
    fence = "`" * max(3, _longest_backtick_run(normalized) + 1)
    return f"{fence}{language}\n{normalized}\n{fence}"


def _longest_backtick_run(value: str) -> int:
    matches = _RE_BACKTICK_RUN.findall(value)
    return max((len(m) for m in matches), default=0)


def _escape_md(value: str) -> str:
    return _RE_MD_SPECIAL.sub(r"\\\1", value.replace("\\", "\\\\"))


def _build_error_panel(
    error_message: str,
    *,
    is_aborted: bool = False,
    expanded: bool = True,
    card_trace_id: str = "",
) -> dict:
    """Build a collapsible error/interrupt panel — visually consistent with
    reasoning and tool panels.

    v1.1.0: Error messages are now user-friendly. Technical details are
    placed in the collapsible panel body, while the panel title shows
    a concise friendly message. If card_trace_id is provided, it's
    included for log correlation.

    - Error (API failure, tool crash): red border, expanded by default
    - Interrupt (/stop or new message): orange border, expanded by default
    """
    if is_aborted:
        en_label, zh_label = _T["interrupt_panel"]
        border_color = "orange"
        # Interrupt is already user-friendly (e.g. "⚡ 已停止")
        body_content = error_message
        body_i18n = None  # 中断消息无需 i18n（error_message 来自上层，已是用户语言）
    else:
        en_label, zh_label = _T["error_panel"]
        border_color = "red"
        # v1.1.0: Build user-friendly error body
        # Friendly hint + technical details in collapsible section
        friendly_en = "AI encountered an error while replying. Please try again."
        friendly_zh = "AI 回复时出现错误，请重试。"
        if card_trace_id:
            friendly_en += f"\n\nDebug ID: `{card_trace_id}`"
            friendly_zh += f"\n\n调试 ID: `{card_trace_id}`"
            # v1.2.0: 引导用户通过 issue 模板反馈（附调试 ID 帮开发者定位）
            friendly_en += "\n\nIf this keeps happening, report the Debug ID to the developer."
            friendly_zh += "\n\n如果反复出错，请把调试 ID 反馈给开发者。"

        tech_detail = error_message.strip() if error_message else ""
        if tech_detail:
            # v1.2.0: 去掉 <details> HTML 标签（飞书 markdown 不支持 HTML 标签，
            # 会显示成乱码）。外层 collapsible_panel 已提供折叠能力，
            # 技术详情用分隔线 + 标题区分即可。
            body_content = f"{friendly_zh}\n\n---\n**技术详情**\n```\n{tech_detail}\n```"
            body_content_en = f"{friendly_en}\n\n---\n**Technical Details**\n```\n{tech_detail}\n```"
            body_i18n = _i18n(body_content_en, body_content)
        else:
            body_content = friendly_zh
            body_i18n = _i18n(friendly_en, friendly_zh)

    # v1.3.4 fix (P1): markdown 元素添加 i18n_content，英文 locale 用户
    # 看到英文错误消息而非中文（原实现 friendly_en 是死代码从未使用）。
    markdown_el: dict[str, Any] = {
        "tag": "markdown",
        "content": body_content,
        "text_size": "notation",
    }
    if body_i18n is not None:
        markdown_el["i18n_content"] = body_i18n

    panel = _collapsible_panel(
        expanded=expanded,
        title_el={
            "tag": "plain_text",
            "content": en_label,
            "i18n_content": _i18n(en_label, zh_label),
            "text_color": "red" if not is_aborted else "orange",
            "text_size": "notation",
        },
        elements=[markdown_el],
        vertical_spacing="8px",
    )
    # Override border color to red/orange for visual emphasis
    panel["border"]["color"] = border_color
    return panel


def _build_background_review_panel(
    messages: list[str],
    *,
    expanded: bool = True,
    element_id: str | None = None,
) -> dict[str, Any]:
    """构建后台审查进度面板（可折叠）."""
    en_title, zh_title = _T["bg_review_panel"]
    children: list[dict] = []
    for msg in messages:
        children.append({
            "tag": "markdown",
            "content": msg,
        })
    if not messages:
        children.append({"tag": "markdown", "content": " "})
    panel = _collapsible_panel(
        expanded=expanded,
        title_el={
            "tag": "plain_text",
            "content": en_title,
            "i18n_content": _i18n(en_title, zh_title),
            "text_color": "grey",
            "text_size": "notation",
        },
        elements=children,
    )
    if element_id:
        panel["element_id"] = element_id
    return panel


def _build_footer_elements(
    footer_data: dict | None,
    is_error: bool = False,
    is_aborted: bool = False,
    fields: list[list[str]] | None = None,
    show_label: bool = False,
) -> list[dict]:
    if fields is None:
        fields = [["status", "elapsed", "context", "model"]]

    data = footer_data or {}
    en_lines: list[str] = []
    zh_lines: list[str] = []
    for row in fields:
        en_parts: list[str] = []
        zh_parts: list[str] = []
        for field in row:
            en, zh = _render_footer_field(field, data, is_error, is_aborted, show_label)
            if en:
                en_parts.append(en)
                if zh:
                    zh_parts.append(zh)
        if en_parts:
            en_lines.append(" · ".join(en_parts))
            zh_lines.append(" · ".join(zh_parts))

    if not en_lines:
        return []

    en_content = "\n".join(en_lines)
    zh_content = "\n".join(zh_lines)
    if is_error:
        en_content = f"<font color='red'>{en_content}</font>"
        zh_content = f"<font color='red'>{zh_content}</font>"

    return [
        {"tag": "hr"},
        {
            "tag": "markdown",
            "content": en_content,
            "i18n_content": _i18n(en_content, zh_content),
            "text_size": "notation",
        },
    ]


def build_preservative_seal_actions(
    *,
    partial: bool = False,
    footer_data: dict | None = None,
    is_error: bool = False,
    is_aborted: bool = False,
    error_message: str = "",
    footer_fields: list[list[str]] | None = None,
    footer_show_label: bool = False,
    existing_elements: set[str] | None = None,
    card_trace_id: str = "",
) -> list[dict]:
    """构建保留式封卡的 batch_update actions.

    生成增量操作：删除 loading icon + 添加 partial indicator 或 footer。
    不重建整卡，避免 1→N+2M 的元素爆炸。

    操作顺序：
    1. insert_before loading_icon: 添加 error panel（如有）
    2. insert_before loading_icon: 添加 partial indicator 或 footer
    3. delete_element: 删除 loading_hint（如存在）
    4. delete_element: 删除 loading_icon

    所有 add_elements 都用 insert_before loading_icon 定位，
    然后删除 loading_icon，最终效果是新增元素出现在卡片底部。

    Parameters
    ----------
    existing_elements : set[str] | None
        If provided, only include ``delete_elements`` actions for element IDs
        that are actually present in this set.  This avoids 400 errors when
        trying to delete elements that were never created.  When ``None``
        (the default), all deletions are included — preserving backward
        compatibility.
    """
    actions: list[dict] = []

    # Helper: check if an element exists (when tracking is enabled)
    def _elem_exists(eid: str) -> bool:
        return existing_elements is None or eid in existing_elements

    # ── Error/interrupt panel (if any) ──
    if error_message:
        actions.append({
            "action": "add_elements",
            "params": {
                "type": "insert_before",
                "target_element_id": _LOADING_ELEMENT_ID,
                "elements": [_build_error_panel(
                    error_message, is_aborted=is_aborted, expanded=True,
                    card_trace_id=card_trace_id,
                )],
            },
        })

    # ── Background review panel (if any) ──
    # v1.3.4 fix (P1): bg_review_messages 之前只在 build_unified_complete_card
    # （全量重建路径）渲染，preservative seal 路径遗漏。默认配置（header_enabled=False）
    # 走 preservative seal，导致 background review 功能完全失效。
    bg_review_messages = footer_data.get("bg_review_messages") if footer_data else None
    if bg_review_messages:
        actions.append({
            "action": "add_elements",
            "params": {
                "type": "insert_before",
                "target_element_id": _LOADING_ELEMENT_ID,
                "elements": [_build_background_review_panel(
                    bg_review_messages,
                    expanded=True,
                )],
            },
        })

    # ── Partial indicator or footer ──
    if partial:
        en_text, zh_text = _T["partial_continues"]
        partial_elements = [
            {"tag": "hr"},
            {
                "tag": "markdown",
                "content": f"▸ {en_text} ↩",
                "i18n_content": _i18n(f"▸ {en_text} ↩", f"▸ {zh_text} ↩"),
            },
        ]
        actions.append({
            "action": "add_elements",
            "params": {
                "type": "insert_before",
                "target_element_id": _LOADING_ELEMENT_ID,
                "elements": partial_elements,
            },
        })
    else:
        footer_elements = _build_footer_elements(
            footer_data,
            is_error=is_error,
            is_aborted=is_aborted,
            fields=footer_fields,
            show_label=footer_show_label,
        )
        if footer_elements:
            actions.append({
                "action": "add_elements",
                "params": {
                    "type": "insert_before",
                    "target_element_id": _LOADING_ELEMENT_ID,
                    "elements": footer_elements,
                },
            })

    # ── Delete context loading hint (if still present) ──
    # 占位提示在首字即显时通常已被删除，但如果卡片在 answer
    # 到来前就被封（如超限拆卡），占位提示可能仍在，需要兜底删除。
    if _elem_exists(_LOADING_HINT_ELEMENT_ID):
        actions.append({
            "action": "delete_elements",
            "params": {
                "element_ids": [_LOADING_HINT_ELEMENT_ID],
            },
        })

    # ── Delete loading icon ──
    if _elem_exists(_LOADING_ELEMENT_ID):
        actions.append({
            "action": "delete_elements",
            "params": {
                "element_ids": [_LOADING_ELEMENT_ID],
            },
        })

    return actions


def _render_footer_field(
    name: str,
    data: dict,
    is_error: bool,
    is_aborted: bool,
    show_label: bool,
) -> tuple[str | None, str | None]:
    if name == "status":
        if is_error:
            return _T["status_error"]
        if is_aborted:
            return _T["status_stopped"]
        return _T["status_completed"]

    if name == "elapsed":
        duration = data.get("duration", 0)
        if isinstance(duration, (int, float)) and duration > 0:
            val = _format_elapsed(duration * 1000)
            if show_label:
                return _T["elapsed"][0].format(val), _T["elapsed"][1].format(val)
            return val, val
        return None, None

    if name == "model":
        v = data.get("model") or None
        return v, v

    if name == "tokens":
        input_t = data.get("input_tokens", 0) or 0
        output_t = data.get("output_tokens", 0) or 0
        reasoning_t = data.get("reasoning_tokens", 0) or 0
        if input_t or output_t:
            v = f"↑ {_compact(input_t)} ↓ {_compact(output_t)}"
            if reasoning_t:
                v += f" 💭 {_compact(reasoning_t)}"
            return v, v
        return None, None

    if name == "context":
        used = data.get("context_used", 0) or 0
        max_c = data.get("context_max", 0) or 0
        if max_c:
            pct = int(used / max_c * 100)
            val = f"{_compact(used)}/{_compact(max_c)} ({pct}%)"
            if show_label:
                return _T["context"][0].format(val), _T["context"][1].format(val)
            return val, val
        return None, None

    if name == "api_calls":
        v = data.get("api_calls", 0) or 0
        if v:
            en_val, zh_val = _T["api_calls"]
            if show_label:
                return f"{en_val} {v}", f"{zh_val} {v}"
            return str(v), str(v)
        return None, None

    if name == "history_offset":
        v = data.get("history_offset", 0) or 0
        if v:
            en_val, zh_val = _T["history_offset"]
            if show_label:
                return f"{en_val} {v}", f"{zh_val} {v}"
            return str(v), str(v)
        return None, None

    if name == "compression_exhausted":
        v = data.get("compression_exhausted", False)
        if v:
            en_val, zh_val = _T["compression_exhausted"]
            return en_val, zh_val
        return None, None

    if name == "cache":
        cache_read = data.get("cache_read_tokens", 0) or 0
        input_total = data.get("input_tokens", 0) or 0
        if cache_read and input_total:
            hit_pct = int(cache_read / input_total * 100)
            v = f"{_compact(cache_read)}/{_compact(input_total)} ({hit_pct}%)"
            if show_label:
                return _T["cache"][0].format(v), _T["cache"][1].format(v)
            return v, v
        return None, None

    if name == "cost":
        cost_usd = data.get("estimated_cost_usd", 0) or 0
        cost_status = data.get("cost_status", "unknown")
        if cost_status == "included":
            return _T["cost_included"]
        if cost_status in ("actual", "estimated") and cost_usd:
            # Format: $0.023 for small values, $1.50 for larger
            if cost_usd < 0.01:
                val = f"${cost_usd:.4f}"
            elif cost_usd < 1:
                val = f"${cost_usd:.3f}"
            else:
                val = f"${cost_usd:.2f}"
            key = "cost_actual" if cost_status == "actual" else "cost_estimated"
            en_val, zh_val = _T[key]
            if show_label:
                return f"Cost {en_val.format(val.lstrip('$'))}", f"费用 {zh_val.format(val.lstrip('$'))}"
            return en_val.format(val.lstrip('$')), zh_val.format(val.lstrip('$'))
        return None, None

    return None, None


def _compact(n: int) -> str:
    if n >= 1_000_000:
        m = n / 1_000_000
        return f"{int(m)}M" if m >= 100 else f"{m:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _format_elapsed(ms: float) -> str:
    seconds = ms / 1000
    return f"{seconds:.1f}s" if seconds < 60 else f"{int(seconds // 60)}m {int(seconds % 60)}s"
