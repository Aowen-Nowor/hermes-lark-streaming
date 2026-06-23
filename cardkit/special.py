"""CardKit v2.0 — Specialized card types: cron, gateway, clarify."""



from __future__ import annotations

from typing import Any

from .i18n import _LOCALES, _T, _i18n, _t
from .elements import _build_header
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
    'build_approval_card',
    'build_approval_resolved_card',
]

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


def build_approval_card(
    *,
    command: str,
    description: str,
    approval_id: int,
) -> dict[str, Any]:
    """构建命令审批卡片 — CardKit JSON 2.0."""
    cmd_preview = command[:3000] + "..." if len(command) > 3000 else command

    def _button(label: str, action_name: str, button_type: str = "default") -> dict[str, Any]:
        return {
            "tag": "button",
            "text": {"tag": "plain_text", "content": label},
            "type": button_type,
            "behaviors": [{
                "type": "callback",
                "value": {"hermes_action": action_name, "approval_id": approval_id},
            }],
        }

    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "icon": {
                "tag": "standard_icon",
                "token": "warning_outlined",
                "size": "20px 20px",
                "color": "orange",
            },
            "text": {"tag": "lark_md", "content": "**命令审批确认**"},
        },
        {
            "tag": "markdown",
            "content": f"```\n{cmd_preview}\n```\n**原因：** {description}",
        },
        {
            "tag": "action",
            "actions": [
                _button("允许一次", "approve_once", "primary"),
                _button("本次会话", "approve_session"),
                _button("始终允许", "approve_always"),
                _button("拒绝", "deny", "danger"),
            ],
        },
    ]
    return {
        "schema": "2.0",
        "config": {
            "streaming_mode": False,
            "locales": _LOCALES,
            "summary": {"content": "命令审批确认"},
        },
        "header": {
            "title": {"content": "命令审批确认", "tag": "plain_text"},
            "template": "orange",
        },
        "body": {"elements": elements},
    }


def build_approval_resolved_card(
    *,
    choice: str,
    user_name: str,
) -> dict[str, Any]:
    """构建审批完成态卡片 — CardKit JSON 2.0."""
    label_map = {
        "once": "已允许一次",
        "session": "已允许本次会话",
        "always": "已始终允许",
        "deny": "已拒绝",
    }
    denied = choice == "deny"
    label = label_map.get(choice, "已处理")
    icon_token = "close_outlined" if denied else "resolve_filled"
    color = "red" if denied else "green"
    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "icon": {
                "tag": "standard_icon",
                "token": icon_token,
                "size": "20px 20px",
                "color": color,
            },
            "text": {"tag": "lark_md", "content": f"**{label}** 由 {user_name} 操作"},
        }
    ]
    return {
        "schema": "2.0",
        "config": {
            "streaming_mode": False,
            "locales": _LOCALES,
            "summary": {"content": label},
        },
        "header": {
            "title": {"content": label, "tag": "plain_text"},
            "template": "red" if denied else "green",
        },
        "body": {"elements": elements},
    }


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
            "content": f"**{question}**",
        },
    })

    if choices:
        # ── Markdown 全量展示选项列表 ──
        option_lines = []
        for i, choice in enumerate(choices):
            label = chr(ord("A") + i)  # A, B, C, ...
            option_lines.append(f"{label}. {choice}")
        options_md = "\n".join(option_lines)
        elements.append({
            "tag": "markdown",
            "content": options_md,
        })

        # ── 快速选择: select_static 下拉框（无 "其他" 选项） ──
        options: list[dict] = []
        for i, choice in enumerate(choices):
            label = chr(ord("A") + i)
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
    en_selected, zh_selected = _T["clarify_selected"]
    en_sel_label = en_selected.format(selected)
    zh_sel_label = zh_selected.format(selected)

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
                "content": f"**{question}**",
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
    en_selected, zh_selected = _T["clarify_selected"]
    en_sel_label = en_selected.format(selected)
    zh_sel_label = zh_selected.format(selected)

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
                "content": f"**{question}**",
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
