"""CardKit v2.0 — Card assemblers: streaming, complete, and linear cards."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .elements import (
    ANSWER_ELEMENT_ID,
    STREAMING_ELEMENT_ID,
    UNIFIED_PANEL_ELEMENT_ID,
    _LOADING_ELEMENT_ID,
    _LOADING_HINT_ELEMENT_ID,
    _build_footer_elements,
    _build_unified_panel_placeholder,
    _collapsible_panel,
    _count_tag_objects,
    _loading_element,
    _loading_hint_element,
    _streaming_element,
    build_unified_panel,
)
from .i18n import _LOCALES, _T, _i18n, _t

if TYPE_CHECKING:
    from ..state.linear import ReasoningRound

__all__ = [
    'build_streaming_card_v2',
    '_enforce_card_element_limit',
]

# Feishu Card 2.0 element limit — every JSON object with a ``tag`` key
# counts toward this limit at all nesting levels.
_FEISHU_ELEMENT_LIMIT = 200

_ELEMENT_LIMIT_MARGIN = 5

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
    include_loading_hint: bool = True,
    include_answer_element: bool = True,
) -> dict[str, Any]:
    """Card lifecycle (v1.0.2+):"""
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
    return card
