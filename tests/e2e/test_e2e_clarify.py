"""E2E tests for Clarify interactive card — v1.3.0 P0-01 fix.

Verifies that the Clarify card with dict-repr choices (the production bug)
is correctly normalized, escaped, and accepted by the real Feishu API.

The production bug: LLM passed choices as dict-repr strings like
``"{'id': 1, 'path': '/mnt/nas/backup1'}"`` which Feishu's lark_md
mangled into ``{id':1)`` garbled display.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.e2e.framework import E2ETestRunner
from hermes_lark_streaming.cardkit import (
    build_clarify_card,
    build_clarify_submitted_card,
    build_clarify_confirmed_card,
)


@pytest.fixture
async def runner():
    """E2E test runner — auto mock/real switching."""
    r = E2ETestRunner()
    await r.setup()
    yield r
    await r.teardown()
    if r.is_real_mode:
        await asyncio.sleep(2.0)  # Rate limit guard


class TestClarifyCardNormalization:
    """Verify dict-repr choices are normalized before sending to Feishu."""

    async def test_clarify_card_with_dict_repr_choices(self, runner):
        """The production bug scenario: dict-repr choices must be normalized.

        Input: choices = ["{'id': 1, 'path': '/mnt/nas/backup1'}", ...]
        Expected: card shows readable paths, not dict-repr garbage.
        """
        # Exact choices from the production bug log
        raw_choices = [
            "{'id': 1, 'path': '/mnt/nas/backup1'}",
            "{'id': 2, 'path': '/mnt/nas/backup2'}",
            "{'id': 3, 'path': '/mnt/nas/backup3'}",
            "{'id': 4, 'path': '/mnt/nas/backup4'}",
        ]

        card = build_clarify_card(
            question="确认 NAS 访问路径，避免我朝错误方向配置",
            choices=raw_choices,
            clarify_id="e2e_test_clarify_001",
        )

        # ── Verify card JSON is correct BEFORE sending ──
        elements = card["body"]["elements"]

        # Find the markdown element (option list)
        md_el = next(e for e in elements if e.get("tag") == "markdown")
        md_content = md_el["content"]

        # Should contain normalized paths, NOT dict-repr garbage
        assert "/mnt/nas/backup1" in md_content
        assert "/mnt/nas/backup2" in md_content
        assert "/mnt/nas/backup3" in md_content
        assert "/mnt/nas/backup4" in md_content

        # Should NOT contain the bug trigger: unescaped {' which Feishu mangles
        # The escaped form \{' is safe (Feishu treats \{ as literal)
        unescaped = md_content.replace("\\{", "").replace("\\}", "")
        assert "{" not in unescaped  # No unescaped braces in the option list

        # ── Verify select_static dropdown uses plain_text (no escaping needed) ──
        select_el = next(e for e in elements if e.get("tag") == "select_static")
        for opt in select_el["options"]:
            content = opt["text"]["content"]
            # Should contain the readable path
            assert "/mnt/nas/backup" in content

        # ── Send to real Feishu (or mock) and verify acceptance ──
        if runner.is_real_mode:
            client = runner._controller._client
            msg_id = await client.send_card_to_chat(
                runner._real_chat_id, card
            )
            assert msg_id, "Card send failed"
        else:
            # Mock mode: just verify the card was "sent"
            msg_id = await runner._controller._client.send_card_to_chat(
                "mock_chat", card
            )
            assert msg_id

    async def test_clarify_card_with_special_chars_in_question(self, runner):
        """Question with curly braces must be escaped (v1.3.0 Round 2 fix)."""
        card = build_clarify_card(
            question="Confirm {'id': 1} configuration?",
            choices=["Yes", "No"],
            clarify_id="e2e_test_clarify_002",
        )

        elements = card["body"]["elements"]
        question_el = elements[0]  # First element is the question div
        content = question_el["text"]["content"]

        # Should contain escaped braces
        assert "\\{" in content
        # Should NOT contain unescaped { (the bug trigger)
        unescaped = content.replace("\\{", "").replace("\\}", "")
        assert "{" not in unescaped

        # Send to verify API acceptance
        if runner.is_real_mode:
            client = runner._controller._client
            msg_id = await client.send_card_to_chat(runner._real_chat_id, card)
            assert msg_id
        else:
            msg_id = await runner._controller._client.send_card_to_chat(
                "mock_chat", card
            )
            assert msg_id

    async def test_clarify_submitted_card_escaped(self, runner):
        """Submitted card with special chars in selected text must be escaped.

        Note: the submitted card uses an ``action``+``button`` element for the
        retry button. In production, this card is returned as a CallBackCard
        response (card action callback API), NOT sent via ``send_card_to_chat``
        (IM message API). The IM message API rejects ``action`` tags in V2
        schema (code 230099). So this test only verifies the card JSON
        structure (escaping) without sending it via the IM API.
        """
        card = build_clarify_submitted_card(
            question="Which option?",
            selected="/mnt/nas/backup[1]",
            clarify_id="e2e_test_clarify_003",
        )

        elements = card["body"]["elements"]
        # Find the selected text element (second div with lock icon)
        selected_el = elements[1]
        content = selected_el["text"]["content"]

        # Should contain escaped brackets
        assert "\\[" in content
        assert "\\]" in content

        # Verify the question is also escaped (first element)
        question_content = elements[0]["text"]["content"]
        assert "\\[" in question_content or "\\" not in question_content  # question has no special chars here

    async def test_clarify_confirmed_card_escaped(self, runner):
        """Confirmed card with special chars must be escaped."""
        card = build_clarify_confirmed_card(
            question="Which option?",
            selected="path/to/{config}",
        )

        elements = card["body"]["elements"]
        selected_el = elements[1]
        content = selected_el["text"]["content"]

        # Should contain escaped braces
        assert "\\{" in content
        assert "\\}" in content

        # Send to verify API acceptance
        if runner.is_real_mode:
            client = runner._controller._client
            msg_id = await client.send_card_to_chat(runner._real_chat_id, card)
            assert msg_id
        else:
            msg_id = await runner._controller._client.send_card_to_chat(
                "mock_chat", card
            )
            assert msg_id

    async def test_clarify_card_normal_choices_unchanged(self, runner):
        """Normal string choices should work without any escaping artifacts."""
        card = build_clarify_card(
            question="Which deployment target?",
            choices=["staging", "production"],
            clarify_id="e2e_test_clarify_004",
        )

        elements = card["body"]["elements"]
        md_el = next(e for e in elements if e.get("tag") == "markdown")
        md_content = md_el["content"]

        # Normal text should appear as-is (no backslash escapes)
        assert "staging" in md_content
        assert "production" in md_content
        assert "\\" not in md_content  # No escaping for normal text

        # Send to verify API acceptance
        if runner.is_real_mode:
            client = runner._controller._client
            msg_id = await client.send_card_to_chat(runner._real_chat_id, card)
            assert msg_id
        else:
            msg_id = await runner._controller._client.send_card_to_chat(
                "mock_chat", card
            )
            assert msg_id
