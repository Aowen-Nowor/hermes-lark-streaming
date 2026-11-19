"""CardKit 2.0 form cards for multi-select / open-ended clarify."""

from __future__ import annotations

import json

from hermes_lark_streaming.plugin.clarify_form import (
    build_clarify_form_card,
    build_settled_form_card,
    extract_form_answer,
    extract_form_value,
)


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def test_multi_select_form_uses_official_multi_select_static_component():
    card = build_clarify_form_card(
        "哪些功能需要验证？", ["登录", "支付", "通知"], "cid_multi", multi_select=True,
    )
    tags = [node.get("tag") for node in _walk(card) if isinstance(node, dict)]
    assert card["schema"] == "2.0"
    assert "form" in tags
    assert "multi_select_static" in tags
    assert "input" not in tags

    component = next(node for node in _walk(card) if node.get("tag") == "multi_select_static")
    assert component["name"] == "clarify_multi"
    assert [option["value"] for option in component["options"]] == ["0", "1", "2"]
    assert component["required"] is True
    assert component["behaviors"][0]["type"] == "callback"


def test_open_ended_form_uses_official_input_component():
    card = build_clarify_form_card("随便输入点什么", None, "cid_text", multi_select=False)
    tags = [node.get("tag") for node in _walk(card) if isinstance(node, dict)]
    assert "form" in tags
    assert "input" in tags
    assert "multi_select_static" not in tags

    component = next(node for node in _walk(card) if node.get("tag") == "input")
    assert component["name"] == "clarify_text"
    assert component["required"] is True


def test_form_submit_button_carries_clarify_route_and_form_action_type():
    card = build_clarify_form_card("Q", ["A"], "cid_submit", multi_select=True)
    submit = next(node for node in _walk(card) if node.get("name") == "clarify_submit")
    assert submit["form_action_type"] == "submit"
    value = submit["behaviors"][0]["value"]
    assert value["hermes_clarify_action"] == "form_submit"
    assert value["clarify_id"] == "cid_submit"


def test_extract_form_answer_maps_multi_indices_back_to_labels():
    answer = extract_form_answer("multi", {"clarify_multi": ["0", "2"]}, ["登录", "支付", "通知"])
    assert json.loads(answer) == ["登录", "通知"]


def test_extract_form_answer_returns_trimmed_free_text():
    assert extract_form_answer("text", {"clarify_text": "  这是开放回答  "}, None) == "这是开放回答"


def test_extract_form_answer_rejects_empty_submission():
    assert extract_form_answer("multi", {"clarify_multi": []}, ["A"]) is None
    assert extract_form_answer("text", {"clarify_text": "   "}, None) is None


def test_extract_form_value_reads_dict_event_shape():
    event = {"action": {"form_value": {"clarify_text": "hello"}}}
    assert extract_form_value(event) == {"clarify_text": "hello"}


def test_settled_form_card_stays_schema_2_for_200830():
    card = build_settled_form_card("问题", "**用户:** 回答", "green")
    assert card["schema"] == "2.0"
    assert card["config"]["update_multi"] is True
    assert card["body"]["elements"][0]["tag"] == "markdown"
