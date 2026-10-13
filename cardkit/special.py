"""CardKit v2.0 — Specialized card types: cron, gateway, clarify."""

from __future__ import annotations

import ast
import re
from typing import Any

from .i18n import _LOCALES, _T, _i18n, _t
from .elements import _escape_md
from .cards import _build_notice_row
from .md import (
    _MAX_CRON_TABLES,
    _downgrade_tables,
    _split_long_text,
    optimize_markdown_style,
)

__all__ = [
    'build_cron_card',
    'build_gateway_card',
    'parse_cron_delivery',
    'build_clarify_card',
    'build_clarify_submitted_card',
    'build_clarify_confirmed_card',
    'normalize_clarify_choices',
]

_CLARIFY_DICT_FIELD_PRIORITY = (
    "label", "description", "text", "title",
    "name", "path", "value", "id",
)

_CLARIFY_MAX_CHOICE_LEN = 80

def _normalize_choice(choice: Any) -> str:
    """Normalize clarify choice to readable string. Handles: plain string,
    dict-repr string (parsed via ast.literal_eval), real dict. Never raises."""
    if choice is None:
        return ""
    if not isinstance(choice, str):
        if isinstance(choice, dict):
            return _extract_readable_from_dict(choice)
        if isinstance(choice, (list, tuple)):
            parts = [_normalize_choice(x) for x in choice]
            return " ".join(p for p in parts if p)[:_CLARIFY_MAX_CHOICE_LEN]
        choice = str(choice)

    text = choice.strip()
    if not text:
        return ""

    # Parse dict-repr strings: starts with { ends with }.
    if text.startswith("{") and text.endswith("}"):
        try:
            parsed = ast.literal_eval(text)
        except (ValueError, SyntaxError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            extracted = _extract_readable_from_dict(parsed)
            if extracted:
                text = extracted

    if len(text) > _CLARIFY_MAX_CHOICE_LEN:
        text = text[: _CLARIFY_MAX_CHOICE_LEN - 1] + "…"

    return text

def _extract_readable_from_dict(d: dict) -> str:
    """Extract readable string field from dict (priority order, strings only)."""
    for field in _CLARIFY_DICT_FIELD_PRIORITY:
        val = d.get(field)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""

def normalize_clarify_choices(choices: list[str] | None) -> list[str]:
    """Normalize choices for display + AI resolution. Filters empty."""
    if not choices:
        return []
    normalized = []
    for c in choices:
        n = _normalize_choice(c)
        if n:
            normalized.append(n)
    return normalized

# v2.0.14：官方 cron 投递包装头（cron/scheduler_delivery.py:2024-2031 逐行镜像）
#   "Cronjob Response: {task_name}\n(job_id: {id})\n-------------\n\n{content}..."
# wrap_response=false 时无包装头 → 原样返回。永不抛异常。
_CRON_HEAD_PREFIX = "Cronjob Response: "
_CRON_JOB_ID_RE = re.compile(r"^\(job_id:\s*(.*?)\)\s*$")


def parse_cron_delivery(content: str) -> tuple[str, str, str]:
    """解析官方 cron 投递包装头 → ``(task_name, job_id, body)``.

    官方格式（scheduler_delivery.py:2025-2031，cron.wrap_response 默认 true）：
    首行 ``Cronjob Response: {name}``，次行 ``(job_id: {id})``，第三行分隔线。
    非包装内容（wrap_response=false / send_message 工具族）原样透传：
    返回 ``("", "", content)``。任何解析异常降级为原样透传（内容永不丢失）。
    """
    try:
        text = (content or "").strip("\n")
        lines = text.split("\n")
        title = job_id = ""
        idx = 0
        if lines and lines[0].startswith(_CRON_HEAD_PREFIX):
            title = lines[0][len(_CRON_HEAD_PREFIX):].strip()
            idx = 1
            if len(lines) > idx:
                m = _CRON_JOB_ID_RE.match(lines[idx].strip())
                if m:
                    job_id = m.group(1).strip()
                    idx += 1
            if len(lines) > idx and lines[idx].strip() and set(lines[idx].strip()) <= {"-"}:
                idx += 1
        body = "\n".join(lines[idx:]).strip()
        return title, job_id, body
    except Exception:  # noqa: BLE001 — 解析失败原样透传（内容永不丢失）
        return "", "", content or ""


def _cron_title_row(title: str) -> dict[str, Any]:
    """cron 卡标题行：官方 standard_icon + lark_md 粗体（与面板标题行同原语）."""
    return {
        "tag": "div",
        "icon": {"tag": "standard_icon", "token": "time_outlined"},
        "text": {"tag": "lark_md", "content": f"**{_escape_md(title)}**"},
    }


def build_cron_card(content: str, *, title: str = "", job_id: str = "") -> dict[str, Any]:
    """Cron 推送用的静态卡片 — schema 2.0，markdown 内容.

    v2.0.14 扩展（向后兼容）：``title`` 非空时在正文前插入标题行
    （time_outlined 图标 + 粗体，V1 极简卡无标题行）；``job_id`` 非空时在
    末尾追加灰色 notation 页脚行（_build_notice_row 同构）——溯源信息
    （原包装头的 ``(job_id: ...)`` 行）从正文上移到页脚，正文不再重复。
    缺省参数下与 V1 行为逐字节一致（既有测试冻结该契约）。
    """
    card: dict[str, Any] = {
        "schema": "2.0",
        "config": {"locales": _LOCALES},
        "body": {"elements": []},
    }
    if title.strip():
        card["body"]["elements"].append(_cron_title_row(title.strip()))
        card["config"]["summary"] = {"content": title.strip()}
    if content.strip():
        if "summary" not in card["config"]:
            summary = content[:120].replace("\n", " ").replace("```", "").strip()
            if summary:
                card["config"]["summary"] = {"content": summary}
        chunks = [
            chunk
            for chunk in _split_long_text(_downgrade_tables(optimize_markdown_style(content), limit=_MAX_CRON_TABLES))
            if chunk.strip()
        ]
        if chunks and title.strip():
            card["body"]["elements"].append({"tag": "hr"})
        card["body"]["elements"].extend({"tag": "markdown", "content": chunk} for chunk in chunks)
    if job_id.strip():
        card["body"]["elements"].append(
            _build_notice_row("time_outlined", f"Cronjob · job_id: {job_id.strip()}")
        )
    # v1.7.0 (R4-10, E2E T7 铁证): body.elements=[] 被飞书 Card 2.0 直接拒绝
    # （230099/200621 "parse card json err"）。空内容的 cron 推送以前必产生
    # 一张非法卡 + 一次必败 API + 静默降级纯文本。单个空格 markdown 与
    # loading hint 占位符同构，schema 合法且渲染为空。
    if not card["body"]["elements"]:
        card["body"]["elements"].append({"tag": "markdown", "content": " "})
    return card

def build_gateway_card(content: str, *, category: str = "", status_label: str = "", status_emoji: str = "") -> dict[str, Any]:
    """Gateway-internal message card — lightweight, static, no streaming. For slash
    command replies, auth, session, errors. category retained for reaction routing."""
    elements: list[dict] = []

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

    # v1.7.0 (R4-10, E2E T7): same empty-body guard as build_cron_card —
    # status-only updates (no status_label, no content) previously produced
    # a schema-invalid card.
    if not card["body"]["elements"]:
        card["body"]["elements"].append({"tag": "markdown", "content": " "})

    return card

def build_clarify_card(*, question: str, choices: list[str] | None = None, clarify_id: str = "") -> dict[str, Any]:
    """构建 Clarify 待选择态卡片 (State 1: Pending). 三态: 标题/选项列表/快速选择下拉/
    自定义输入. choices 经 normalize + escape for lark_md. select_static 用 plain_text."""
    elements: list[dict] = []

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

    # Defense in depth: adapter also normalizes, but card builders must be safe.
    normalized_choices = normalize_clarify_choices(choices)

    if normalized_choices:
        option_lines = []
        for i, choice in enumerate(normalized_choices):
            label = chr(ord("A") + i) if i < 26 else str(i + 1)
            option_lines.append(f"{label}. {_escape_md(choice)}")
        options_md = "\n".join(option_lines)
        elements.append({
            "tag": "markdown",
            "content": options_md,
        })

        # select_static dropdown (plain_text, no markdown).
        options: list[dict] = []
        for i, choice in enumerate(normalized_choices):
            label = chr(ord("A") + i) if i < 26 else str(i + 1)
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

def build_clarify_submitted_card(*, question: str, selected: str, clarify_id: str = "") -> dict[str, Any]:
    """构建 Clarify 已提交态卡片 (State 2: Submitted/Soft Lock). 标题 + 用户选择 +
    "已提交" 提示 + 重试按钮."""
    # Escape selected for lark_md (rendered inside "已选择: {}" template).
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
            # v1.7.0 (E2E T8b→A3 铁证): "action" 容器在 Card 2.0 已废弃
            # （230099/200861 "cards of schema V2 no longer support this
            # capability; unsupported tag action"）— button 必须是顶层元素。
            # 修复后的结构已经真飞书 E2E 验证通过（A3 SUCCESS）。
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

def build_clarify_confirmed_card(*, question: str, selected: str) -> dict[str, Any]:
    """构建 Clarify 已确认态卡片 (State 3: Confirmed/Hard Lock). 标题 + 选择 + "已确认"."""
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
