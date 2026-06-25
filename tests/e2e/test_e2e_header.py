"""v1.2.0 E2E — header 颜色切换验证（mock + 真飞书）.

验证 H6 方案B：开了 header 后，
- 创建占位卡时 header.template == "blue"（streaming 处理中）
- 封卡全量重建后 header.template == "green"（completed 已完成）
- 出错时 header.template == "red"（error）

真飞书模式下无法回查卡片内容，通过 wrap client.cardkit_create /
cardkit_update 捕获传入的 card JSON 来验证 header 字段。
"""

from __future__ import annotations

import asyncio
import pytest


class TestHeaderColorSwitch:
    """v1.2.0 H6: 开 header 后卡片头部颜色随状态切换."""

    @pytest.mark.asyncio
    async def test_header_blue_on_create(self, runner):
        """创建占位卡时 header 是蓝色（streaming 处理中）."""
        runner.enable_header()
        session = await runner.start_message("hello header")
        await asyncio.sleep(0.3)

        create_card = runner.get_create_card_json()
        assert create_card is not None, "占位卡未创建"
        assert "header" in create_card, f"开启 header 后占位卡应有 header 字段: {create_card.keys()}"
        assert create_card["header"]["template"] == "blue", \
            f"占位卡 header 应为 blue(streaming), 实际: {create_card['header']['template']}"

        # 清理：完成会话
        await runner.feed_answer(session, "test answer")
        await runner.complete(session, answer="test answer")

    @pytest.mark.asyncio
    async def test_header_green_on_complete(self, runner):
        """正常完成封卡后 header 变绿色（completed）."""
        runner.enable_header()
        session = await runner.start_message("hello complete")
        await runner.feed_answer(session, "The final answer.")
        await runner.complete(session, answer="The final answer.")
        await runner.assert_card_sealed(session)

        # 全量重建走 cardkit_update，应捕获到带 green header 的 card
        update_cards = runner.get_update_card_jsons()
        assert len(update_cards) >= 1, \
            f"开 header 后封卡应走全量重建(cardkit_update), 但捕获到 {len(update_cards)} 次更新"
        final_card = update_cards[-1]
        assert "header" in final_card, f"全量重建卡片应有 header: {final_card.keys()}"
        assert final_card["header"]["template"] == "green", \
            f"完成态 header 应为 green(completed), 实际: {final_card['header']['template']}"

    @pytest.mark.asyncio
    async def test_header_red_on_stopped(self, runner):
        """中断封卡后 header 变红色（stopped 与 error 同为 red template）."""
        runner.enable_header()
        session = await runner.start_message("trigger stop")
        await runner.feed_answer(session, "partial answer")
        # 模拟用户 /stop：设 _was_aborted，_do_linear_complete 据此用 stopped(red) header
        session._was_aborted = True
        await runner.complete(session, answer="partial answer")
        await runner.assert_card_sealed(session)

        update_cards = runner.get_update_card_jsons()
        assert len(update_cards) >= 1, "中断封卡应走全量重建"
        final_card = update_cards[-1]
        assert "header" in final_card, "中断全量重建卡片应有 header"
        assert final_card["header"]["template"] == "red", \
            f"中断 header 应为 red(stopped), 实际: {final_card['header']['template']}"

    @pytest.mark.asyncio
    async def test_header_disabled_no_header(self, runner):
        """关 header（默认）时卡片无 header 字段."""
        # 不调 enable_header，默认 header_enabled=False
        session = await runner.start_message("no header test")
        await runner.feed_answer(session, "answer without header")
        await runner.complete(session, answer="answer without header")
        await runner.assert_card_sealed(session)

        create_card = runner.get_create_card_json()
        assert create_card is not None
        assert "header" not in create_card, \
            f"关 header 时占位卡不应有 header 字段: {create_card.keys()}"
