"""CardKit v2.0 — Card assemblers: streaming, complete, and linear cards."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .elements import (
    _LOADING_ELEMENT_ID,
    _LOADING_HINT_ELEMENT_ID,
    ANSWER_ELEMENT_ID,
    STREAMING_ELEMENT_ID,
    UNIFIED_PANEL_ELEMENT_ID,
    _build_footer_elements,
    _build_reasoning_round_title,
    _build_tool_step_detail,
    _build_tool_step_title,
    _build_unified_panel_placeholder,
    _collapsible_panel,
    _count_tag_objects,
    _loading_element,
    _loading_hint_element,
    _streaming_element,
    build_unified_panel,
)
from .i18n import _LOCALES, _T, _i18n, _t
from .progline import _THINKING_PREFIX, parse_progress_line, preview_text
from .tool_icons import resolve_tool_icon, tool_display_title

if TYPE_CHECKING:
    from ..state.linear import ReasoningRound

__all__ = [
    '_enforce_card_element_limit',
    'build_streaming_card_v2',
]

# Feishu Card 2.0 element limit — every JSON object with a ``tag`` key
# counts toward this limit at all nesting levels.
_FEISHU_ELEMENT_LIMIT = 200

_ELEMENT_LIMIT_MARGIN = 5

# ── v2.0.5 状态面板 V1(1.8.1) 同款渲染 ────────────────────────────────────────
# gateway progress 模板行 → V1 unified panel 子元素：
#   💬 思考行 → 分组为推理轮次：robot-add 图标 + 绿色粗体「第 N 轮」+ 缩进灰字正文
#   工具行   → official standard_icon + 彩色粗体工具标题（pending=橙 running /
#              已执行=绿 success）+ 缩进灰字命令摘要（仅已执行行）
#   其余     → 原样 markdown（降级路径）
# 标题/图标解析见 tool_icons.py（V1 state/tooluse.py 移植）。


def _strip_md_quotes(text: str) -> str:
    return text.strip().strip('"“”').strip()


def _render_status_children(lines: list[str]) -> list[dict[str, Any]]:
    """status_lines → V1 风格面板子元素（解析失败行降级为原始 markdown）.

    v2.0.5 对齐 V1(1.8.1)：
    - 连续 💬 行分组为一轮推理（第 N 轮标题绿色 + robot-add_outlined；V1 的
      橙色仅用于流式中未定稿轮次，V2 的 💬 行到达即完整，恒绿不伪造状态）
    - 工具行标题 humanize/描述符化（terminal→Terminal、grep→Search text）
    - pending 行（尾部 ``...``）橙色 running；已执行行绿色 success
    - 预览文本取引号内命令摘要渲染为缩进灰字（不再是含引号原文）
    """
    children: list[dict[str, Any]] = []
    round_no = 0
    prev_thinking = False
    for line in lines:
        text = (line or "").strip()
        if not text:
            continue
        kind, tool, clean = parse_progress_line(text)
        if kind == "thinking":
            if not prev_thinking:
                round_no += 1
                children.append(
                    _build_reasoning_round_title(round_no, 0, finalized=True)
                )
            prev_thinking = True
            body = clean or text[len(_THINKING_PREFIX):].strip() or text
            detail = _build_tool_step_detail({"detail": body})
            if detail is not None:
                children.append(detail)
            continue
        prev_thinking = False
        if kind == "tool" and tool:
            pending = text.endswith("...")
            children.append(_build_tool_step_title(
                {"title": tool_display_title(tool),
                 "status": "running" if pending else "success",
                 "icon": resolve_tool_icon(tool)},
            ))
            if not pending:
                preview = _strip_md_quotes(preview_text(text))
                if preview:
                    detail = _build_tool_step_detail({"detail": preview})
                    if detail is not None:
                        children.append(detail)
            continue
        children.append({"tag": "markdown", "content": text})
    return children or [{"tag": "markdown", "content": " "}]  # 飞书面板拒绝空 elements


def _apply_panel_title(panel: dict[str, Any], title: str) -> None:
    """覆写 collapsible_panel header 标题（V1 风格："agent loop · N 个工具"）."""
    if not title:
        return
    header = panel.get("header") or {}
    title_el = header.get("title") or {}
    title_el["content"] = title
    title_el["i18n_content"] = {"zh_cn": title, "en_us": title}
    header["title"] = title_el
    panel["header"] = header


def _enforce_card_element_limit(
    card: dict[str, Any],
    *,
    panel_element_id: str = UNIFIED_PANEL_ELEMENT_ID,
) -> dict[str, Any]:
    """Counts **all** tag objects in the card (including nested ones)."""
    threshold = _FEISHU_ELEMENT_LIMIT - _ELEMENT_LIMIT_MARGIN
    total = _count_tag_objects(card)
    if total <= threshold:
        return card

    # ── Find the unified panel element in card body ──
    body = card.get("body", {})
    elements = body.get("elements", [])
    panel = None
    for elem in elements:
        if elem.get("element_id") == panel_element_id and elem.get("tag") == "collapsible_panel":
            panel = elem
            break

    if panel is None:
        # No panel found — nothing to trim (answer/footer must not be trimmed)
        return card

    children: list[dict] = panel.get("elements", [])

    # ── Check if a collapse hint already exists ──
    hint_idx = None
    for i, child in enumerate(children):
        if isinstance(child.get("content"), str) and "已折叠" in child["content"]:
            hint_idx = i
            break
    _HINT_TEMPLATE = {"tag": "markdown", "content": "⚡ 还有 0 项已折叠", "text_size": "notation"}
    _HINT_TAG_COUNT = _count_tag_objects(_HINT_TEMPLATE)  # typically 1
    if hint_idx is None:
        total += _HINT_TAG_COUNT  # Reserve exact space for the new collapse hint

    # ── Trim oldest items from panel children until under threshold ──
    trimmed_count = 0
    while total > threshold and len(children) > 1:
        # Skip the collapse hint (first child if it ends with "已折叠")
        first_content = children[0].get("content", "")
        remove_idx = 1 if isinstance(first_content, str) and first_content.endswith("已折叠") else 0
        removed = children.pop(remove_idx)
        total -= _count_tag_objects([removed])
        trimmed_count += 1

    if trimmed_count > 0:
        # Update or add collapse hint
        # Re-find hint_idx (may have shifted due to removals)
        hint_idx = None
        for i, child in enumerate(children):
            if isinstance(child.get("content"), str) and "已折叠" in child["content"]:
                hint_idx = i
                break
        if hint_idx is not None:
            # Parse existing trimmed count from hint, then add new trimmed count
            # e.g. "⚡ 还有 5 项已折叠" → existing_count=5, trimmed_count=3 → "⚡ 还有 8 项已折叠"
            old_hint = children[hint_idx]["content"]
            # Extract the number before "项" — simple string parsing, no regex needed
            existing_count = 0
            _idx = old_hint.find("项")
            if _idx > 0:
                # Walk backwards skipping whitespace, then collect digits
                _end = _idx
                while _end > 0 and old_hint[_end - 1] == ' ':
                    _end -= 1
                _start = _end
                while _start > 0 and old_hint[_start - 1].isdigit():
                    _start -= 1
                if _start < _end:
                    existing_count = int(old_hint[_start:_end])
            total_trimmed = existing_count + trimmed_count
            children[hint_idx]["content"] = f"⚡ 还有 {total_trimmed} 项已折叠"
        else:
            children.insert(0, {
                "tag": "markdown",
                "content": f"⚡ 还有 {trimmed_count} 项已折叠",
                "text_size": "notation",
            })

    # Update panel children in the card
    panel["elements"] = children
    return card

def _build_summary(text: str) -> dict[str, Any]:
    """Feishu CardKit 2.0 displays ``i18n_content.<locale>`` for users"""
    truncated = text[:120].replace("\n", " ").replace("```", "").strip()
    return {
        "content": truncated,
        "i18n_content": _i18n(truncated, truncated),
    }

def build_streaming_card_v2(
    *,
    tool_steps: list[dict] | None = None,
    elapsed_ms: float = 0,
    show_tool_use: bool = True,
    show_reasoning: bool = False,
    show_streaming_element: bool = True,
    streaming_panel_expanded: bool = True,
    print_strategy: str = "delay",
    print_step: int = 4,
    include_unified_panel: bool = True,
    include_loading_hint: bool = False,  # v2.0.4: 已废弃，保留签名兼容（不再渲染）
    include_answer_element: bool = True,
    status_lines: list[str] | None = None,
    answer_text: str = "",
    panel_title: str = "",
) -> dict[str, Any]:
    """Card lifecycle (v1.0.2+):

    ``status_lines``：V2 帧重组引擎的工具/思考/interim 状态行，
    v2.0.3 起按 V1 插件风格渲染（图标 + 彩色工具名 + 缩进灰色摘要）。
    ``answer_text``：已流出的 answer 缓冲——cardkit_update 全量更新时
    回填 answer 元素，防止工具帧刷新面板把已流内容重置为空。
    ``panel_title``：面板动态标题（V1: "agent loop · N 个工具"；空 → 静态 "agent loop"）。
    """
    elements: list[dict] = []

    # ── Unified panel placeholder (linear mode — single panel for reasoning+tools) ──
    if include_unified_panel:
        panel = _build_unified_panel_placeholder(expanded=streaming_panel_expanded)
        _apply_panel_title(panel, panel_title)
        if status_lines:
            panel["elements"] = _render_status_children(status_lines)
        elements.append(panel)

    # ── Streaming answer element ──
    if show_streaming_element and include_answer_element:
        elements.append(_streaming_element(content=answer_text or "", element_id=ANSWER_ELEMENT_ID))

    # ── Loading hint: v2.0.4 移除（用户确认走平台后无需"正在加载上下文..."占位；
    #    v2.0.3 部署时曾从服务器热补丁回退，本版起仓库内永久移除）。
    #    spinner 保留作为封卡 insert_before 锚点 + 首卡 loading 指示。
    # ── Loading spinner ──
    elements.append(_loading_element())

    card: dict[str, Any] = {
        "schema": "2.0",
        "config": {
            "streaming_mode": True,
            "streaming_config": {
                "print_frequency_ms": {"default": 70},
                "print_step": {"default": print_step},
                "print_strategy": print_strategy,
            },
            "locales": _LOCALES,
            "summary": {
                "content": _T["processing"][0],
                "i18n_content": _t("processing"),
            },
        },
        "body": {"elements": elements},
    }
    return card
