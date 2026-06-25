"""CardKit v2.0 — Card assemblers: streaming, complete, and linear cards."""



from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .elements import (
    ANSWER_ELEMENT_ID,
    STREAMING_ELEMENT_ID,
    UNIFIED_PANEL_ELEMENT_ID,
    _LOADING_ELEMENT_ID,
    _LOADING_HINT_ELEMENT_ID,
    _build_background_review_panel,
    _build_error_panel,
    _build_footer_elements,
    _build_header,
    _build_unified_panel_placeholder,
    _collapsible_panel,
    _count_tag_objects,
    _extract_images_from_markdown,
    _loading_element,
    _loading_hint_element,
    _streaming_element,
    build_unified_panel,
)
from .i18n import _LOCALES, _T, _i18n, _t
from .md import (
    _downgrade_tables,
    _split_long_text,
    escape_markdown_asterisks,
    optimize_markdown_style,
)

if TYPE_CHECKING:
    from ..state.linear import ReasoningRound



__all__ = [
    'build_streaming_card_v2',
    'build_im_fallback_card',
    'build_unified_complete_card',
    '_enforce_card_element_limit',
]

# Feishu Card 2.0 element limit — every JSON object with a ``tag`` key
# counts toward this limit at all nesting levels.
_FEISHU_ELEMENT_LIMIT = 200

# Safety margin below the hard limit.  We enforce this threshold
# instead of 200 to leave room for small structural additions that
# the card API may inject internally (e.g. auto-generated wrappers).
_ELEMENT_LIMIT_MARGIN = 5


def _enforce_card_element_limit(
    card: dict[str, Any],
    *,
    panel_element_id: str = UNIFIED_PANEL_ELEMENT_ID,
) -> dict[str, Any]:
    """Enforce Feishu Card 2.0 element limit on a complete card.

    Counts **all** tag objects in the card (including nested ones).
    If the total exceeds ``_FEISHU_ELEMENT_LIMIT - _ELEMENT_LIMIT_MARGIN``
    (default 195), progressively trims the oldest items from the
    unified panel's ``elements`` list until under threshold, adding
    or updating a collapse hint (``⚡ 还有 X 项已折叠``).

    This is the **card-level safety net** — it runs after the entire
    card is assembled (panel + answer + footer + error panel), so it
    knows the exact total and can trim precisely without guessing
    how many elements the answer/footer will consume.

    Parameters
    ----------
    card : dict
        A fully assembled card dict (schema 2.0, with ``body.elements``).
    panel_element_id : str
        Element ID of the unified panel to trim if needed.

    Returns
    -------
    dict
        The same card dict, possibly with trimmed panel children.
    """
    threshold = _FEISHU_ELEMENT_LIMIT - _ELEMENT_LIMIT_MARGIN
    total = _count_tag_objects(card)
    if total <= threshold:
        return card

    # ── Find the unified panel element in card body ──
    body = card.get("body", {})
    elements = body.get("elements", [])
    panel_idx = None
    panel = None
    for i, elem in enumerate(elements):
        if elem.get("element_id") == panel_element_id and elem.get("tag") == "collapsible_panel":
            panel_idx = i
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
    # If no hint exists yet, we'll need to add one, so reserve its exact tag count.
    # Pre-calculate the hint template's tag object count instead of hardcoding,
    # so the reservation stays correct if the hint template changes.
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
    """Build a summary dict with both content and i18n_content.

    Feishu CardKit 2.0 displays ``i18n_content.<locale>`` for users
    whose Feishu language matches that locale, and falls back to
    ``content`` otherwise.  If we only update ``content`` but not
    ``i18n_content``, Chinese users continue seeing the old
    "处理中..." in the conversation list even after close_streaming
    succeeds — the exact bug reported as "会话列表永久显示处理中".
    """
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
    header_enabled: bool = False,
    include_unified_panel: bool = True,
    include_loading_hint: bool = True,
    include_answer_element: bool = True,
) -> dict[str, Any]:
    """CardKit 2.0 流式占位卡片 — placeholder card for streaming mode.

    Card lifecycle (v1.0.2+):
        Phase 1 — User sends message → Create placeholder card with only
        "正在加载上下文..." + loading icon (no panel, no answer element).
        Phase 2 — First LLM token arrives → Delete loading hint, add
        unified panel + answer element via ``add_elements``.
        Phase 3 — Stream reasoning/tool content in panel, stream answer text.
        Phase 4 — Complete → Add footer.

    Parameters
    ----------
    include_unified_panel : bool
        When ``True``, adds the unified panel placeholder element.
        In the new lifecycle (default for linear mode), this is ``False``
        — the panel is added dynamically when the first content arrives.
    include_loading_hint : bool
        When ``True`` (default), adds the context-loading hint element.
        This hint is removed once the first LLM token arrives.
    include_answer_element : bool
        When ``True``, adds the answer streaming element to the initial card.
        In the new lifecycle, this is ``False`` — the answer element is
        added alongside the panel when the first content arrives.
    """
    elements: list[dict] = []

    # ── Unified panel placeholder (linear mode — single panel for reasoning+tools) ──
    if include_unified_panel:
        elements.append(_build_unified_panel_placeholder(expanded=streaming_panel_expanded))

    # ── Streaming answer element ──
    if show_streaming_element and include_answer_element:
        elements.append(_streaming_element(element_id=ANSWER_ELEMENT_ID))

    # ── Loading hint (context loading placeholder, removed on first LLM token) ──
    if include_loading_hint:
        elements.append(_loading_hint_element())

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
    if header_enabled:
        card["header"] = _build_header("streaming")
    return card


def build_im_fallback_card(*, header_enabled: bool = False) -> dict[str, Any]:
    """IM 降级占位卡 — CardKit 创建失败时用 IM 消息卡片通道.

    v1.2.0 H7: 支持 header（与 CardKit 通道视觉一致）。
    开启时显示 streaming(蓝) 状态头部。
    """
    card: dict[str, Any] = {
        "config": {
            "update_multi": True,
            "locales": _LOCALES,
        },
        "elements": [
            {
                "tag": "markdown",
                "content": _T["processing_prefix"][0],
                "i18n_content": _t("processing_prefix"),
            },
        ],
    }
    if header_enabled:
        card["header"] = _build_header("streaming")
    return card


def build_unified_complete_card(
    *,
    reasoning_rounds: list,  # list of ReasoningRound
    current_reasoning_text: str = "",
    tool_steps: list[dict] | None = None,
    tool_elapsed_ms: float = 0,
    answer_text: str = "",
    show_reasoning: bool = True,
    footer_data: dict | None = None,
    is_error: bool = False,
    is_aborted: bool = False,
    error_message: str = "",
    footer_fields: list[list[str]] | None = None,
    footer_show_label: bool = True,
    panel_expanded: bool = False,
    header_enabled: bool = False,
    panel_events: list[tuple[str, int]] | None = None,
    max_tool_steps: int = 20,
    max_reasoning_rounds: int = 20,
    card_trace_id: str = "",
) -> dict[str, Any]:
    """Unified panel complete card — single panel for reasoning+tools, plus answer.

    Builds a complete (non-streaming) card with:

    1. **Unified panel** — all reasoning rounds and tool steps in one
       collapsible panel element (``UNIFIED_PANEL_ELEMENT_ID``).
    2. **Answer text** — markdown content with image extraction and
       long-text splitting.
    3. **Error panel** — if ``error_message`` is provided.
    4. **Background review panel** — if ``bg_review_messages`` is provided.
    5. **Footer** — metadata row(s).

    Parameters
    ----------
    reasoning_rounds : list[ReasoningRound]
        Finalised reasoning rounds.
    current_reasoning_text : str
        In-progress reasoning text not yet finalised into a round.
    tool_steps : list[dict] | None
        Tool step dicts consumed by :func:`_build_tool_step_elements`.
    tool_elapsed_ms : float
        Total elapsed time for tool execution in milliseconds.
    answer_text : str
        The main answer / response text.
    show_reasoning : bool
        Whether to render reasoning content inside the unified panel.
    footer_data : dict | None
        Key-value data for the footer row (model, elapsed, context, etc.).
    is_error : bool
        Whether the response ended with an API error.
    is_aborted : bool
        Whether the response was interrupted by the user.
    error_message : str
        Error or interrupt message to display.
    footer_fields : list[list[str]] | None
        Footer field layout (each inner list is a row of field names).
    footer_show_label : bool
        Whether to show labels alongside footer values.
    panel_expanded : bool
        Whether the unified panel starts expanded.
    header_enabled : bool
        Whether to include a card-level header.
    """
    tool_steps = tool_steps or []
    elements: list[dict] = []

    # ── Unified panel ──
    has_reasoning_or_tools = reasoning_rounds or current_reasoning_text or tool_steps
    if has_reasoning_or_tools:
        elements.append(build_unified_panel(
            reasoning_rounds=reasoning_rounds,
            current_reasoning_text=current_reasoning_text,
            tool_steps=tool_steps,
            tool_elapsed_ms=tool_elapsed_ms,
            show_reasoning=show_reasoning,
            expanded=panel_expanded,
            panel_events=panel_events,
            max_tool_steps=max_tool_steps,
            max_reasoning_rounds=max_reasoning_rounds,
        ))

    # ── Answer text ──
    if answer_text:
        # v1.3.1 fix: apply escape_markdown_asterisks to match _preservative_seal path.
        # Without this, header=true (full rebuild) path would not escape * in
        # expressions like "2*4000+4*3000", causing Feishu markdown to pair
        # *4000+4* as italic — the * disappears and numbers merge.
        content = escape_markdown_asterisks(_downgrade_tables(optimize_markdown_style(answer_text)))
        content, img_elements = _extract_images_from_markdown(content)
        elements.extend(img_elements)
        for chunk in _split_long_text(content):
            if chunk.strip():
                elements.append({"tag": "markdown", "content": chunk})
    else:
        elements.append({"tag": "markdown", "content": _T["done"][0]})

    # ── 错误/中断面板 ──
    if error_message:
        elements.append(_build_error_panel(
            error_message, is_aborted=is_aborted, expanded=panel_expanded,
            card_trace_id=card_trace_id,
        ))

    # ── Background review panel ──
    bg_review_messages = footer_data.get("bg_review_messages") if footer_data else None
    if bg_review_messages:
        elements.append(_build_background_review_panel(
            bg_review_messages,
            expanded=panel_expanded,
        ))

    # ── Footer ──
    elements.extend(
        _build_footer_elements(
            footer_data,
            is_error,
            is_aborted,
            fields=footer_fields,
            show_label=footer_show_label,
        )
    )

    # ── Summary ──
    summary_text = answer_text
    if not summary_text and reasoning_rounds:
        summary_text = reasoning_rounds[-1].text if reasoning_rounds else ""
    summary = summary_text[:120].replace("\n", " ").replace("```", "").strip() if summary_text else ""

    card: dict[str, Any] = {
        "schema": "2.0",
        "config": {
            "update_multi": True,
            "streaming_mode": False,
            "locales": _LOCALES,
        },
    }
    if summary:
        card["config"]["summary"] = _build_summary(summary)
    if header_enabled:
        if is_error:
            card["header"] = _build_header("error")
        elif is_aborted:
            card["header"] = _build_header("stopped")
        else:
            card["header"] = _build_header("completed")
    card["body"] = {"elements": elements}

    # ── Card-level element limit safety net ──
    # After assembling the complete card, count ALL tag objects and
    # trim panel children if the total exceeds 195 (200 - 5 margin).
    # This is more precise than trimming inside build_unified_panel
    # because we now know the exact answer/footer/error overhead.
    _enforce_card_element_limit(card)

    return card



