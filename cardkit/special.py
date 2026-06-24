"""CardKit v2.0 — Specialized card types: cron, gateway, clarify."""



from __future__ import annotations

import ast
from typing import Any

from .i18n import _LOCALES, _T, _i18n, _t
from .elements import _build_header, _escape_md
from .md import (
    _MAX_CRON_TABLES,
    _downgrade_tables,
    _split_long_text,
    optimize_markdown_style,
)



__all__ = [
    'build_cron_card',
    'build_gateway_card',
    'build_clarify_card',
    'build_clarify_submitted_card',
    'build_clarify_confirmed_card',
    'normalize_clarify_choices',
]


# ── Clarify choice normalization (v1.3.0 P0-01) ──────────────────────
#
# LLMs sometimes emit dict-shaped choices (e.g. {"id": 1, "path": "/mnt/nas/backup1"})
# that get str()-serialized to "{'id': 1, 'path': '/mnt/nas/backup1'}" before reaching
# the plugin.  Hermes core's _flatten_choice() only handles *real* dicts (not dict-repr
# strings), so the garbage string leaks through to the card renderer, where Feishu's
# lark_md mangles {' into template syntax → {id':1) garbled display.
#
# normalize_clarify_choices() parses dict-repr strings back to dicts (via the safe
# ast.literal_eval — no code execution), extracts the most human-readable field by
# priority, and returns clean strings.  Card builders then escape them for lark_md.

# Field priority for extracting readable text from a dict-repr choice.
# Ordered by "most likely to be a human-readable label".
_CLARIFY_DICT_FIELD_PRIORITY = (
    "label", "description", "text", "title",
    "name", "path", "value", "id",
)

# Maximum display length for a single choice (truncated with ellipsis if exceeded).
# Keeps the option list and dropdown readable when LLMs pass very long strings.
_CLARIFY_MAX_CHOICE_LEN = 80


def _normalize_choice(choice: Any) -> str:
    """Normalize a single clarify choice into a readable display string.

    Handles three input shapes:
      1. Plain string → returned as-is (stripped, truncated).
      2. Dict-repr string (``"{'id': 1, 'path': '/x'}"``) → parsed with
         :func:`ast.literal_eval`, most readable field extracted.
      3. Real dict (defensive — in case a caller bypasses the adapter) →
         same field extraction as (2).

    Always returns a clean string (never raises).  If parsing fails or no
    readable field is found, falls back to the original string so the user
    at least sees *something* (escaped later by the card builder).
    """
    if choice is None:
        return ""
    if not isinstance(choice, str):
        # Defensive: handle real dict/list inputs directly.
        if isinstance(choice, dict):
            return _extract_readable_from_dict(choice)
        if isinstance(choice, (list, tuple)):
            parts = [_normalize_choice(x) for x in choice]
            return " ".join(p for p in parts if p)[:_CLARIFY_MAX_CHOICE_LEN]
        choice = str(choice)

    text = choice.strip()
    if not text:
        return ""

    # Try to parse dict-repr strings: starts with { ends with }
    if text.startswith("{") and text.endswith("}"):
        try:
            parsed = ast.literal_eval(text)
        except (ValueError, SyntaxError, TypeError):
            # TypeError covers unhashable keys like "{{}: {}}" (a dict with
            # a dict key). ValueError/SyntaxError cover malformed literals.
            parsed = None  # Not a valid literal — keep original text
        if isinstance(parsed, dict):
            extracted = _extract_readable_from_dict(parsed)
            if extracted:
                text = extracted

    # Truncate long text with a single ellipsis character
    if len(text) > _CLARIFY_MAX_CHOICE_LEN:
        text = text[: _CLARIFY_MAX_CHOICE_LEN - 1] + "…"

    return text


def _extract_readable_from_dict(d: dict) -> str:
    """Extract the most human-readable string field from a dict.

    Tries fields in :data:`_CLARIFY_DICT_FIELD_PRIORITY` order.  Only string
    values are used (bare ints like ``id: 1`` are not helpful as a label).
    Returns ``""`` if no usable string field is found.
    """
    for field in _CLARIFY_DICT_FIELD_PRIORITY:
        val = d.get(field)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def normalize_clarify_choices(choices: list[str] | None) -> list[str]:
    """Normalize a list of clarify choices for both display and AI resolution.

    Called by the adapter (:func:`_wrap_feishu_adapter_send_clarify`) BEFORE
    storing choices in the registry, so that:
      - The card displays readable text (not dict-repr garbage).
      - The user's selection (sent back to the AI via ``resolve_gateway_clarify``)
        is the readable text, not the raw dict-repr.

    Empty results are filtered out (an empty choice is useless to the user).
    """
    if not choices:
        return []
    normalized = []
    for c in choices:
        n = _normalize_choice(c)
        if n:
            normalized.append(n)
    return normalized

def build_cron_card(content: str) -> dict[str, Any]:
    """Cron 推送用的极简静态卡片 — schema 2.0，仅 markdown 内容."""
    card: dict[str, Any] = {
        "schema": "2.0",
        "config": {"locales": _LOCALES},
        "body": {"elements": []},
    }
    if not content.strip():
        return card
    summary = content[:120].replace("\n", " ").replace("```", "").strip()
    if summary:
        card["config"]["summary"] = {"content": summary}
    for chunk in _split_long_text(_downgrade_tables(optimize_markdown_style(content), limit=_MAX_CRON_TABLES)):
        if chunk.strip():
            card["body"]["elements"].append({"tag": "markdown", "content": chunk})
    return card


def build_gateway_card(
    content: str,
    *,
    category: str = "",
    status_label: str = "",
    status_emoji: str = "",
    header_enabled: bool = False,
    header_status: str = "",
) -> dict[str, Any]:
    """Gateway-internal message card — lightweight, static, no streaming.

    Used for slash command replies, auth messages, session lifecycle
    notifications, error messages, and all non-AI, non-interactive text
    that Hermes sends to the Feishu user.

    Displays the Hermes native message content in a clean card without
    any extra emoji or icon prefix.

    Args:
        content: The text content to display in the card.
        category: Retained for reaction interception routing; no longer
            affects card visual appearance.
        status_label: Optional status indicator text (e.g. "Reading",
            "Processing"). When set, shows a status line with emoji + label.
        status_emoji: Optional emoji for the status indicator.
        header_enabled: v1.2.0 H7 — 当为 True 且 header_status 非空时，
            添加 card-level header（用于 IM 降级路径与 CardKit 通道视觉一致）。
            网关内部消息调用时不传此参数（默认 False，无 header）。
        header_status: header 状态色，可选 "streaming"(蓝)/"completed"(绿)/
            "error"(红)/"stopped"(红)。仅 header_enabled=True 时生效。
    """
    elements: list[dict] = []

    # ── Status indicator (from reaction interception) ──
    if status_label and status_emoji:
        elements.append({
            "tag": "div",
            "text": {
                "tag": "plain_text",
                "content": f"{status_emoji} {status_label}",
                "text_color": "turquoise",
                "text_size": "notation",
            },
        })

    if content.strip():
        for chunk in _split_long_text(_downgrade_tables(optimize_markdown_style(content), limit=_MAX_CRON_TABLES)):
            if chunk.strip():
                elements.append({"tag": "markdown", "content": chunk})

    card: dict[str, Any] = {
        "schema": "2.0",
        "config": {"locales": _LOCALES},
        "body": {"elements": elements},
    }

    summary = content[:120].replace("\n", " ").replace("```", "").strip() if content.strip() else ""
    if summary:
        card["config"]["summary"] = {"content": summary}

    # v1.2.0 H7: IM 降级路径 header 支持
    if header_enabled and header_status:
        card["header"] = _build_header(header_status)

    return card


def build_clarify_card(
    *,
    question: str,
    choices: list[str] | None = None,
    clarify_id: str = "",
) -> dict[str, Any]:
    """构建 Clarify 待选择态卡片（State 1: Pending）.

    三态卡片设计 — 待选择态:
      - 标题: helpdesk_outlined 图标 + 问题文本
      - 选项列表: markdown 全量展示所有选项（A. B. C.）
      - 快速选择: select_static 下拉框（仅含预定义选项，无 "其他" 选项）
      - 自定义输入: input 文本输入框（支持 Enter + 按钮提交）
      - 无 choices 时仅显示 input 输入框

    v1.3.0 P0-01: choices are normalized (dict-repr → readable) and the
    markdown list is escaped for lark_md safety.  The select_static
    dropdown uses plain_text (no markdown processing) so it receives
    normalized but unescaped text.

    Args:
        question: 问题文本
        choices: 选项列表，None/空表示开放式问题
        clarify_id: 唯一标识，用于回调路由
    """
    elements: list[dict] = []

    # ── 问题标题 (helpdesk_outlined icon) ──
    elements.append({
        "tag": "div",
        "icon": {
            "tag": "standard_icon",
            "token": "info_outlined",
            "size": "20px 20px",
            "color": "blue",
        },
        "text": {
            "tag": "lark_md",
            "content": f"**{_escape_md(question)}**",
        },
    })

    # v1.3.0 P0-01: normalize choices (dict-repr → readable) — defense in
    # depth.  The adapter also normalizes before storing, but card builders
    # must be safe even if called directly with raw inputs.
    normalized_choices = normalize_clarify_choices(choices)

    if normalized_choices:
        # ── Markdown 全量展示选项列表（转义 lark_md 特殊字符） ──
        option_lines = []
        for i, choice in enumerate(normalized_choices):
            label = chr(ord("A") + i) if i < 26 else str(i + 1)  # A-Z, then 27, 28...
            # Escape for lark_md: { } [ ] < > ` * _ would otherwise be
            # interpreted as markdown/template syntax and garble the display.
            option_lines.append(f"{label}. {_escape_md(choice)}")
        options_md = "\n".join(option_lines)
        elements.append({
            "tag": "markdown",
            "content": options_md,
        })

        # ── 快速选择: select_static 下拉框（无 "其他" 选项） ──
        # plain_text 不做 markdown 渲染，无需转义；但需用 normalized 文本
        options: list[dict] = []
        for i, choice in enumerate(normalized_choices):
            label = chr(ord("A") + i) if i < 26 else str(i + 1)  # A-Z, then 27, 28...
            options.append({
                "text": {"tag": "plain_text", "content": f"{label}. {choice}"},
                "value": str(i),
            })

        en_placeholder, zh_placeholder = _T["clarify_select_placeholder"]
        select_el: dict[str, Any] = {
            "tag": "select_static",
            "element_id": "clarify_select",
            "placeholder": {
                "tag": "plain_text",
                "content": en_placeholder,
                "i18n_content": _i18n(en_placeholder, zh_placeholder),
            },
            "options": options,
            "behaviors": [{
                "type": "callback",
                "value": {
                    "hermes_clarify_action": "select",
                    "clarify_id": clarify_id,
                },
            }],
        }
        elements.append(select_el)

    # ── 自定义输入: input 文本输入框（始终显示） ──
    en_input_ph, zh_input_ph = _T["clarify_input_placeholder"]
    input_el: dict[str, Any] = {
        "tag": "input",
        "element_id": "clarify_input",
        "placeholder": {
            "tag": "plain_text",
            "content": en_input_ph,
            "i18n_content": _i18n(en_input_ph, zh_input_ph),
        },
        "max_length": 500,
        "name": "clarify_input",
        "behaviors": [{
            "type": "callback",
            "value": {
                "hermes_clarify_action": "input_submit",
                "clarify_id": clarify_id,
            },
        }],
    }
    elements.append(input_el)

    card: dict[str, Any] = {
        "schema": "2.0",
        "config": {
            "streaming_mode": False,
            "locales": _LOCALES,
        },
        "body": {"elements": elements},
    }
    return card


def build_clarify_submitted_card(
    *,
    question: str,
    selected: str,
    clarify_id: str = "",
) -> dict[str, Any]:
    """构建 Clarify 已提交态卡片（State 2: Submitted / Soft Lock）.

    三态卡片设计 — 已提交态（软锁定）:
      - 标题: lock_outlined 图标 + 问题文本
      - 用户选择内容
      - "已提交，等待确认..." 提示
      - 「重试提交」按钮：重新发送同一选择（非重新选择）

    Args:
        question: 原始问题文本
        selected: 用户选择的文本
        clarify_id: 唯一标识，用于重试回调路由
    """
    # v1.3.0 P0-01: escape the selected text for lark_md (it is rendered
    # inside a lark_md element via the "已选择: {}" template).  The selected
    # text arrives from the adapter already normalized, but may still
    # contain { } [ ] < > etc. that lark_md would misinterpret.
    safe_selected = _escape_md(selected)
    en_selected, zh_selected = _T["clarify_selected"]
    en_sel_label = en_selected.format(safe_selected)
    zh_sel_label = zh_selected.format(safe_selected)

    en_submitted, zh_submitted = _T["clarify_submitted"]
    en_retry, zh_retry = _T["clarify_retry"]

    elements: list[dict] = [
        {
            "tag": "div",
            "icon": {
                "tag": "standard_icon",
                "token": "lock_outlined",
                "size": "20px 20px",
                "color": "orange",
            },
            "text": {
                "tag": "lark_md",
                "content": f"**{_escape_md(question)}**",
            },
        },
        {
            "tag": "div",
            "icon": {
                "tag": "standard_icon",
                "token": "lock_outlined",
                "size": "16px 16px",
                "color": "orange",
            },
            "text": {
                "tag": "lark_md",
                "content": en_sel_label,
                "i18n_content": _i18n(en_sel_label, zh_sel_label),
            },
        },
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"*{en_submitted}*",
                "i18n_content": _i18n(f"*{en_submitted}*", f"*{zh_submitted}*"),
            },
        },
        {
            "tag": "action",
            "actions": [{
                "tag": "button",
                "text": {
                    "tag": "plain_text",
                    "content": en_retry,
                    "i18n_content": _i18n(en_retry, zh_retry),
                },
                "type": "primary",
                "behaviors": [{
                    "type": "callback",
                    "value": {
                        "hermes_clarify_action": "retry_submit",
                        "clarify_id": clarify_id,
                    },
                }],
            }],
        },
    ]

    card: dict[str, Any] = {
        "schema": "2.0",
        "config": {
            "streaming_mode": False,
            "locales": _LOCALES,
        },
        "body": {"elements": elements},
    }
    return card


def build_clarify_confirmed_card(
    *,
    question: str,
    selected: str,
) -> dict[str, Any]:
    """构建 Clarify 已确认态卡片（State 3: Confirmed / Hard Lock）.

    三态卡片设计 — 已确认态（硬锁定）:
      - 标题: resolve_filled 图标 + 问题文本
      - 用户选择内容
      - "已确认" 文本
      - 无操作按钮（由服务端更新卡片至此态）

    Args:
        question: 原始问题文本
        selected: 用户选择的文本
    """
    # v1.3.0 P0-01: escape the selected text for lark_md (same rationale
    # as build_clarify_submitted_card).
    safe_selected = _escape_md(selected)
    en_selected, zh_selected = _T["clarify_selected"]
    en_sel_label = en_selected.format(safe_selected)
    zh_sel_label = zh_selected.format(safe_selected)

    en_confirmed, zh_confirmed = _T["clarify_confirmed"]

    elements: list[dict] = [
        {
            "tag": "div",
            "icon": {
                "tag": "standard_icon",
                "token": "resolve_filled",
                "size": "20px 20px",
                "color": "green",
            },
            "text": {
                "tag": "lark_md",
                "content": f"**{_escape_md(question)}**",
            },
        },
        {
            "tag": "div",
            "icon": {
                "tag": "standard_icon",
                "token": "resolve_filled",
                "size": "16px 16px",
                "color": "green",
            },
            "text": {
                "tag": "lark_md",
                "content": en_sel_label,
                "i18n_content": _i18n(en_sel_label, zh_sel_label),
            },
        },
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": en_confirmed,
                "i18n_content": _i18n(en_confirmed, zh_confirmed),
            },
        },
    ]

    card: dict[str, Any] = {
        "schema": "2.0",
        "config": {
            "streaming_mode": False,
            "locales": _LOCALES,
        },
        "body": {"elements": elements},
    }
    return card
