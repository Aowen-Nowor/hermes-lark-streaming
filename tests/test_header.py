"""v1.2.0 H5 — header 功能测试.

覆盖 builder 层：
- build_streaming_card_v2 的 header_enabled 开关 + streaming(蓝) 状态
- build_unified_complete_card 的 header 状态色（completed绿/error红/stopped红）
- build_im_fallback_card 的 header 支持（H7 降级一致性）
- build_gateway_card 的 header 支持（H7 降级一致性）

controller 层的 H6（开了 header 走全量重建封卡）见 test_controller.py。
"""

from __future__ import annotations

from hermes_lark_streaming.cardkit.cards import (
    build_im_fallback_card,
    build_streaming_card_v2,
    build_unified_complete_card,
)
from hermes_lark_streaming.cardkit.special import build_gateway_card


# ── build_streaming_card_v2 ──────────────────────────────────────────


class TestStreamingCardHeader:
    """占位卡 header 开关 + streaming(蓝) 状态."""

    def test_header_disabled_by_default(self) -> None:
        """默认关闭——卡片无 header 字段."""
        card = build_streaming_card_v2()
        assert "header" not in card

    def test_header_disabled_explicit_false(self) -> None:
        card = build_streaming_card_v2(header_enabled=False)
        assert "header" not in card

    def test_header_enabled_adds_blue_header(self) -> None:
        """开启后 header template=blue（streaming 状态）."""
        card = build_streaming_card_v2(header_enabled=True)
        assert "header" in card
        assert card["header"]["template"] == "blue"
        # title 是 plain_text
        assert card["header"]["title"]["tag"] == "plain_text"
        assert "content" in card["header"]["title"]


# ── build_unified_complete_card ──────────────────────────────────────


class TestCompleteCardHeader:
    """完成态卡片 header 状态色：completed(绿)/error(红)/stopped(红)."""

    def test_header_disabled_by_default(self) -> None:
        card = build_unified_complete_card(
            reasoning_rounds=[],
            answer_text="test answer",
        )
        assert "header" not in card

    def test_header_enabled_completed_is_green(self) -> None:
        """正常完成——header template=green."""
        card = build_unified_complete_card(
            reasoning_rounds=[],
            answer_text="test answer",
            header_enabled=True,
        )
        assert card["header"]["template"] == "green"

    def test_header_enabled_error_is_red(self) -> None:
        """出错——header template=red."""
        card = build_unified_complete_card(
            reasoning_rounds=[],
            answer_text="",
            is_error=True,
            error_message="something broke",
            header_enabled=True,
        )
        assert card["header"]["template"] == "red"

    def test_header_enabled_aborted_is_red(self) -> None:
        """中断——header template=red（stopped 状态）."""
        card = build_unified_complete_card(
            reasoning_rounds=[],
            answer_text="",
            is_aborted=True,
            error_message="interrupted",
            header_enabled=True,
        )
        assert card["header"]["template"] == "red"


# ── build_im_fallback_card (H7 降级一致性) ───────────────────────────


class TestIMFallbackCardHeader:
    """IM 降级占位卡 header 支持（H7 方案A）."""

    def test_header_disabled_by_default(self) -> None:
        card = build_im_fallback_card()
        assert "header" not in card

    def test_header_enabled_adds_blue_header(self) -> None:
        """降级卡开启 header——streaming(蓝) 状态（与 CardKit 占位卡一致）."""
        card = build_im_fallback_card(header_enabled=True)
        assert "header" in card
        assert card["header"]["template"] == "blue"


# ── build_gateway_card (H7 降级一致性) ───────────────────────────────


class TestGatewayCardHeader:
    """build_gateway_card header 支持（H7 方案A）.

    网关内部消息调用时不传 header 参数（默认无 header）；
    IM 降级 flush/seal 调用时传 header_enabled + header_status.
    """

    def test_header_disabled_by_default(self) -> None:
        """网关消息默认无 header."""
        card = build_gateway_card("some content")
        assert "header" not in card

    def test_header_enabled_streaming_is_blue(self) -> None:
        """IM 降级 flush——streaming(蓝)."""
        card = build_gateway_card(
            "处理中内容",
            header_enabled=True,
            header_status="streaming",
        )
        assert card["header"]["template"] == "blue"

    def test_header_enabled_completed_is_green(self) -> None:
        """IM 降级 seal 正常完成——completed(绿)."""
        card = build_gateway_card(
            "最终答案",
            header_enabled=True,
            header_status="completed",
        )
        assert card["header"]["template"] == "green"

    def test_header_enabled_error_is_red(self) -> None:
        """IM 降级 seal 出错——error(红)."""
        card = build_gateway_card(
            "出错了",
            header_enabled=True,
            header_status="error",
        )
        assert card["header"]["template"] == "red"

    def test_header_enabled_stopped_is_red(self) -> None:
        """IM 降级 seal 中断——stopped(红)."""
        card = build_gateway_card(
            "已停止",
            header_enabled=True,
            header_status="stopped",
        )
        assert card["header"]["template"] == "red"

    def test_header_enabled_but_no_status_no_header(self) -> None:
        """header_enabled=True 但 header_status 为空——不加 header（防御）."""
        card = build_gateway_card(
            "content",
            header_enabled=True,
            header_status="",
        )
        assert "header" not in card

    def test_gateway_message_unaffected(self) -> None:
        """网关内部消息（不传 header 参数）不受影响，仍无 header."""
        card = build_gateway_card("System notification", category="system")
        assert "header" not in card
