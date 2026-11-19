"""Feishu CardKit 2.0 form cards for multi-select and open-ended clarify prompts.

Official components:
- multi_select_static: only inside a form container; submits an array of option values.
- input: only inside a form container; submits a string.
- form submit button: form_action_type="submit", callback carries action.form_value.
"""

from __future__ import annotations

import json
from typing import Any


def build_clarify_form_card(
    question: str,
    choices: list[str] | None,
    clarify_id: str,
    *,
    multi_select: bool,
) -> dict[str, Any]:
    """Build a schema 2.0 form card for a multi-select or open-ended clarify."""
    if multi_select and choices:
        component = {
            "tag": "multi_select_static",
            "element_id": "clarify_multi",
            "name": "clarify_multi",
            "placeholder": {"tag": "plain_text", "content": "请选择（可多选）"},
            "width": "fill",
            "required": True,
            "behaviors": [
                {
                    "type": "callback",
                    "value": {
                        "hermes_clarify_action": "form_component",
                        "clarify_id": clarify_id,
                    },
                }
            ],
            "options": [
                {
                    "text": {"tag": "plain_text", "content": str(choice)},
                    "value": str(index),
                }
                for index, choice in enumerate(choices)
            ],
        }
        form_kind = "multi"
    else:
        component = {
            "tag": "input",
            "element_id": "clarify_text",
            "name": "clarify_text",
            "placeholder": {"tag": "plain_text", "content": "请输入回答"},
            "width": "fill",
            "required": True,
            "input_type": "multiline_text",
            "rows": 3,
            "auto_resize": True,
            "max_rows": 6,
            "label": {"tag": "plain_text", "content": str(question)},
        }
        form_kind = "text"

    submit = {
        "tag": "button",
        "text": {"tag": "plain_text", "content": "提交"},
        "type": "primary",
        "form_action_type": "submit",
        "name": "clarify_submit",
        "behaviors": [
            {
                "type": "callback",
                "value": {
                    "hermes_clarify_action": "form_submit",
                    "clarify_id": clarify_id,
                    "form_kind": form_kind,
                },
            }
        ],
    }
    reset = {
        "tag": "button",
        "text": {"tag": "plain_text", "content": "取消"},
        "type": "default",
        "form_action_type": "reset",
        "name": "clarify_reset",
    }

    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": "❓ Hermes needs your input"},
            "template": "blue",
        },
        "body": {
            "elements": [
                {"tag": "markdown", "content": str(question)},
                {
                    "tag": "form",
                    "name": "clarify_form",
                    "elements": [
                        component,
                        {
                            "tag": "column_set",
                            "columns": [
                                {
                                    "tag": "column",
                                    "width": "auto",
                                    "elements": [submit],
                                },
                                {
                                    "tag": "column",
                                    "width": "auto",
                                    "elements": [reset],
                                },
                            ],
                        },
                    ],
                },
            ]
        },
    }


def build_settled_form_card(question: str, line: str, template: str = "green") -> dict[str, Any]:
    """Build a schema 2.0 settled card for a form card (200830-safe).

    Feishu rejects updating a JSON 2.0 card with a JSON 1.0 card, so the
    submission/expiry response must stay in the 2.0 structure.
    """
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": "❓ Hermes needs your input"},
            "template": template,
        },
        "body": {
            "elements": [
                {"tag": "markdown", "content": f"{question}\n\n{line}"},
            ]
        },
    }


def extract_form_value(event: Any) -> dict[str, Any]:
    """Return ``event.action.form_value`` across SDK objects and plain dicts."""
    action = getattr(event, "action", None)
    if action is None and isinstance(event, dict):
        action = event.get("action")
    value = getattr(action, "form_value", None)
    if value is None and isinstance(action, dict):
        value = action.get("form_value")
    return value if isinstance(value, dict) else {}


def extract_form_answer(
    form_kind: str,
    form_value: dict[str, Any],
    choices: list[str] | None,
) -> str | None:
    """Map a Feishu form submission to the string clarify_gateway expects.

    Multi-select values are option indices from the card; resolve them back to
    the original choice labels and return a JSON array string (Hermes'
    multi-select wire format). Open-ended input is returned as trimmed text.
    """
    if form_kind == "multi":
        raw = form_value.get("clarify_multi")
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            return None
        labels: list[str] = []
        source = list(choices or [])
        for token in raw:
            try:
                index = int(str(token))
            except (TypeError, ValueError):
                continue
            if 0 <= index < len(source):
                labels.append(source[index])
        return json.dumps(labels, ensure_ascii=False) if labels else None
    if form_kind == "text":
        text = str(form_value.get("clarify_text") or "").strip()
        return text or None
    return None
