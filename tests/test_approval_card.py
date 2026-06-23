"""Tests for approval CardKit cards."""

from __future__ import annotations

from hermes_lark_streaming.cardkit import (
    build_approval_card,
    build_approval_resolved_card,
)


class TestBuildApprovalCard:
    def test_schema_2_and_summary(self) -> None:
        card = build_approval_card(
            command="rm -rf /tmp/demo",
            description="危险命令",
            approval_id=42,
        )
        assert card["schema"] == "2.0"
        assert card["config"]["streaming_mode"] is False
        assert card["config"]["summary"]["content"] == "命令审批确认"

    def test_buttons_keep_hermes_callback_values(self) -> None:
        card = build_approval_card(
            command="rm -rf /tmp/demo",
            description="危险命令",
            approval_id=42,
        )
        action = next(e for e in card["body"]["elements"] if e.get("tag") == "action")
        values = [button["behaviors"][0]["value"] for button in action["actions"]]
        assert values == [
            {"hermes_action": "approve_once", "approval_id": 42},
            {"hermes_action": "approve_session", "approval_id": 42},
            {"hermes_action": "approve_always", "approval_id": 42},
            {"hermes_action": "deny", "approval_id": 42},
        ]

    def test_chinese_content(self) -> None:
        card = build_approval_card(
            command="rm -rf /tmp/demo",
            description="delete in root path",
            approval_id=1,
        )
        markdown = next(e for e in card["body"]["elements"] if e.get("tag") == "markdown")
        labels = [button["text"]["content"] for button in next(e for e in card["body"]["elements"] if e.get("tag") == "action")["actions"]]
        assert "**原因：** delete in root path" in markdown["content"]
        assert labels == ["允许一次", "本次会话", "始终允许", "拒绝"]


class TestBuildApprovalResolvedCard:
    def test_approved_card_is_green(self) -> None:
        card = build_approval_resolved_card(choice="session", user_name="刘芸洋")
        assert card["schema"] == "2.0"
        assert card["header"]["template"] == "green"
        assert card["header"]["title"]["content"] == "已允许本次会话"
        assert "由 刘芸洋 操作" in card["body"]["elements"][0]["text"]["content"]

    def test_denied_card_is_red(self) -> None:
        card = build_approval_resolved_card(choice="deny", user_name="刘芸洋")
        assert card["header"]["template"] == "red"
        assert card["header"]["title"]["content"] == "已拒绝"
