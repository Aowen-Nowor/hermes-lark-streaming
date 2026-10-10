"""End-to-end tests — V2 FrameComposer 全场景.

V2.0.0 语义（对应 docs/DESIGN.md 出站四通道）：
- 基本答案流：建卡 → 多段流式（分段 send 追加 / edit 全量替换）→ 两步定稿
- 思考 + 工具状态面板：feed_reasoning / feed_tool_update（建卡前暂存 / 建卡后增量）
- 长答案 20 段 delta（全量语义 edit）
- 建卡前 progress 暂存并入首卡（V2 关键行为：pending_status → status_lines）
- plain 帧不进卡片通道（handle_send 返回 None → 原生）
- 降级：client 缺失 → handle_send 返回 None（原生兜底红线：永不丢消息）
- chat_id / open_id 形态（V2 引擎不区分，均为不透明字符串）

mock 模式断言 mock 卡片状态（answer 元素 / streaming_mode / summary / 调用计数）；
real 模式断言 API 成功与 engine 状态（无法回查卡片内容）。
测试间限速见 conftest._rate_limit_guard（真飞书模式）。
"""

from __future__ import annotations

import os

import pytest


class TestCoreFlow:
    """基本答案流：建卡 → 多段流式 → 两步定稿."""

    async def test_basic_answer_full_cycle(self, runner):
        """建卡 → 分段 send 追加 → edit 全量替换 → 定稿落终值."""
        seg1 = "你好，我先起个头。"
        seg2 = "第二段由分段 send 帧追加。"
        streamed = f"{seg1}\n\n{seg2}"
        final = f"{streamed}\n\n定稿全文落盘。"

        session = await runner.start_message(seg1)
        runner.assert_card_created(session)
        assert runner.get_call_count("cardkit_create") == 1
        assert runner.get_call_count("send_card_by_id_to_chat") == 1

        if not runner.is_real_mode:
            # 首帧建卡即流式：answer 元素应带首段文本（全量语义）
            assert runner.get_answer_text(session) == seg1

        # 分段 send 帧：引擎以 "\n\n" 追加段落
        await runner.send_segment(session, seg2)
        st = runner.get_state(session)
        assert st is not None and st.answer_text == streamed, \
            f"分段追加语义不符: {st.answer_text!r}"

        # edit 帧：全量替换
        await runner.feed_answer(session, final)
        st = runner.get_state(session)
        assert st.answer_text == final, f"edit 全量替换语义不符: {st.answer_text!r}"

        await runner.complete(session, answer=final)
        runner.assert_card_created(session)
        await runner.assert_card_sealed(session)

        st = runner.get_state(session)
        assert st.finalized and st.answer_text == final
        if not runner.is_real_mode:
            # answer 元素内容 == 全量文本（stream_element 终值真落盘）
            assert runner.get_answer_text(session) == final

    async def test_complete_without_answer_uses_buffer(self, runner):
        """complete 不带 answer → 以缓冲区当前内容定稿."""
        text = "缓冲区定稿语义验证。"
        session = await runner.start_message(text)
        await runner.complete(session)
        await runner.assert_card_sealed(session)
        st = runner.get_state(session)
        assert st.answer_text == text
        if not runner.is_real_mode:
            assert runner.get_answer_text(session) == text

    async def test_open_id_chat_form(self, runner):
        """V2 引擎不区分 chat_id / open_id 形态（同为不透明字符串）."""
        if runner.is_real_mode and not os.environ.get("FEISHU_E2E_OPEN_ID"):
            pytest.skip("real 模式未提供 FEISHU_E2E_OPEN_ID")
        session = await runner.start_message("私聊形态首帧", use_open_id=True)
        runner.assert_card_created(session)
        assert session.chat_id.startswith("ou_"), f"私聊形态 chat_id 异常: {session.chat_id}"
        await runner.feed_answer(session, "私聊形态首帧")
        await runner.complete(session, answer="私聊形态定稿")
        await runner.assert_card_sealed(session)
        assert runner.get_state(session).answer_text == "私聊形态定稿"


class TestStatusPanel:
    """思考 + 工具状态面板（建卡后增量 → cardkit_update）."""

    async def test_reasoning_and_tool_panel(self, runner):
        """思考行 / 工具行累积进 status_lines，且发生 cardkit_update 调用."""
        session = await runner.start_message("面板场景首帧")
        runner.assert_card_created(session)

        await runner.feed_reasoning(session, "先拆解问题再动手")
        lines = runner.get_status_lines(session)
        assert any("先拆解问题再动手" in ln for ln in lines), f"思考行未入面板: {lines}"

        await runner.feed_tool_update(session, tool_name="web_search", detail="query=feishu")
        lines = runner.get_status_lines(session)
        assert any("web_search" in ln for ln in lines), f"工具行未入面板: {lines}"
        assert runner.get_call_count("cardkit_update") == 2, "两次状态补丁应触发 cardkit_update"

        await runner.complete(session, answer="面板场景定稿")
        await runner.assert_card_sealed(session)
        if not runner.is_real_mode:
            # 定稿后状态面板行保留在卡片 JSON 中
            card = runner.get_card(session)
            assert card is not None
            assert card.card_json, "定稿后卡片 JSON 应保留"

    async def test_duplicate_tool_line_deduped(self, runner):
        """重复工具行去重（引擎幂等：相同行不重复入面板）."""
        session = await runner.start_message("去重场景")
        await runner.feed_tool_update(session, tool_name="bash", detail="echo hi")
        await runner.feed_tool_update(session, tool_name="bash", detail="echo hi")
        assert len(runner.get_status_lines(session)) == 1
        assert runner.get_call_count("cardkit_update") == 1

    async def test_interim_after_card_joins_panel(self, runner):
        """无 emoji 的 interim 播报（建卡后）→ 状态行."""
        session = await runner.start_message("interim 场景首帧")
        result = await runner.send_raw(
            "正在等待外部资源返回（interim 播报）", {"_interim_send": True}, chat_id=session.chat_id,
        )
        assert result is not None and getattr(result, "success", False), \
            "建卡后 interim 帧应被卡片通道消费"
        assert any("interim" in ln for ln in runner.get_status_lines(session))


class TestPendingProgress:
    """V2 关键行为：建卡前 progress 暂存 → 首卡并入 status_lines."""

    async def test_pending_progress_merged_into_first_card(self, runner):
        pend = runner.pending_session()
        await runner.feed_reasoning(pend, "卡片还没建，先暂存这条思考")
        await runner.feed_tool_update(pend, tool_name="terminal", detail="pytest tests/ -q")

        # v2.0.4（s70 回流）：首个进度帧即建占位卡（卡片先现，V1 体验），
        # 后续帧经 _patch_status 合并进面板；不再有裸奔的独立气泡。
        assert runner.get_call_count("cardkit_create") == 1

        session = await runner.start_message("首帧建卡应并入暂存的状态行")
        runner.assert_card_created(session)

        # 首帧并入既有卡片而非重复建卡
        assert runner.get_call_count("cardkit_create") == 1

        st = runner.get_state(session)
        assert len(st.status_lines) == 2, f"暂存行未并入首卡: {st.status_lines}"
        assert st.pending_status == [], "暂存区应已清空"
        assert any("先暂存这条思考" in ln for ln in st.status_lines)
        assert any("terminal" in ln for ln in st.status_lines)

        if not runner.is_real_mode:
            # 首卡卡片 JSON 的状态面板即带两条暂存行（build_card(status_lines=pending)）
            # v2.0.3 起面板行渲染为 V1 风格 div（文本在 el["text"]["content"]），
            # 同时兼容旧 markdown 行（顶层 content）。
            from hermes_lark_streaming.cardkit.elements import UNIFIED_PANEL_ELEMENT_ID

            card = runner.get_card(session)
            assert card is not None
            panel = card.elements.get(UNIFIED_PANEL_ELEMENT_ID, {})

            def _flat(el: dict) -> str:
                text = el.get("text")
                nested = text.get("content", "") if isinstance(text, dict) else ""
                return f"{el.get('content', '')} {nested}"

            contents = [_flat(el) for el in panel.get("elements", [])]
            assert any("先暂存这条思考" in c for c in contents), f"首卡面板缺思考行: {contents}"
            # v2.0.5：工具行标题人性化（terminal → Terminal，V1 截图同款）
            assert any("Terminal" in c for c in contents), f"首卡面板缺工具行: {contents}"
            assert any("第 1 轮" in c for c in contents), f"首卡面板缺思考轮次标题: {contents}"

        await runner.complete(session, answer="暂存场景定稿")
        await runner.assert_card_sealed(session)


class TestAnswerDeltas:
    """长答案：20 段 delta（edit 全量语义）."""

    async def test_long_answer_20_deltas(self, runner):
        chunks = [f"Chunk {i}. " for i in range(20)]
        session = await runner.start_message(chunks[0])
        acc = chunks[0]
        for chunk in chunks[1:]:
            acc += chunk
            await runner.feed_answer(session, acc)  # hermes edit 全量替换

        st = runner.get_state(session)
        assert st is not None and st.answer_text == acc

        await runner.complete(session, answer=acc)
        runner.assert_card_created(session)
        await runner.assert_card_sealed(session)

        st = runner.get_state(session)
        assert "Chunk 0" in st.answer_text, "长答案缺少开头"
        assert "Chunk 19" in st.answer_text, "长答案缺少结尾"
        if not runner.is_real_mode:
            assert runner.get_answer_text(session) == acc, "终值未完整落盘"
            # 建卡 1 + 流式 19 + 定稿终值 1 = 21 次 stream_element
            assert runner.get_call_count("cardkit_stream_element") >= 20


class TestNativeAndDegrade:
    """plain 帧与降级路径（原生兜底红线：永不丢消息）."""

    async def test_plain_frame_not_consumed_by_card_channel(self, runner):
        """无标记 send 帧（cron / 后台投递）→ handle_send 返回 None 走原生."""
        result = await runner.send_raw("cron 后台投递文本，无任何 gateway 标记", None)
        assert result is None, "plain 帧不应进入卡片通道"
        assert runner.get_call_count("cardkit_create") == 0
        assert runner.get_call_count("cardkit_stream_element") == 0

    async def test_interim_without_card_native_bubble(self, runner):
        """无 emoji 的 _interim_send 帧在无卡片时 → None（原生小气泡）."""
        result = await runner.send_raw("无 emoji 的中途播报", {"_interim_send": True})
        assert result is None
        assert runner.get_call_count("cardkit_create") == 0

    async def test_client_unavailable_native_fallback(self, runner):
        """get_client 返回 None → handle_send 返回 None（原生兜底），恢复后重建卡."""
        original = runner.client
        runner.set_client(None)
        try:
            session = await runner.start_message("降级场景首帧")
            assert session.last_result is None, "client 缺失时 handle_send 应返回 None"
            assert not session.message_id, "降级时不应有卡片消息 id"
            assert runner.get_call_count("cardkit_create") == 0

            # 状态表无卡片 → 后续 edit 也走原生
            await runner.feed_answer(session, "降级中的流式文本")
            assert session.last_result is None
        finally:
            runner.set_client(original)

        # 恢复 client 后新 turn 正常建卡
        session2 = await runner.start_message("恢复后重建卡片")
        runner.assert_card_created(session2)
        await runner.complete(session2, answer="恢复后定稿")
        await runner.assert_card_sealed(session2)


class TestLifecycle:
    """终态行为：错误定稿 / 定稿后新 turn."""

    async def test_error_still_finalizes(self, runner):
        """错误恢复：AI 报错 → 卡片仍能定稿（V2 按普通定稿处理）."""
        session = await runner.start_message("错误场景首帧")
        await runner.feed_answer(session, "部分答案已流出")
        await runner.complete(session, answer="部分答案已流出", error_message="模拟 AI 中途报错")
        runner.assert_card_created(session)
        await runner.assert_card_sealed(session)
        assert runner.get_state(session).answer_text == "部分答案已流出"

    async def test_new_turn_after_finalize_creates_fresh_card(self, runner):
        """定稿后同 chat 新 turn → 建新卡并独立定稿."""
        first = await runner.start_message("第一轮首帧")
        await runner.complete(first, answer="第一轮定稿")
        await runner.assert_card_sealed(first)

        second = await runner.start_message("第二轮首帧")
        runner.assert_card_created(second)
        if not runner.is_real_mode:
            assert second.card_id != first.card_id, "新 turn 应建新卡"
        await runner.feed_answer(second, "第二轮流式更新")
        await runner.complete(second, answer="第二轮定稿")
        await runner.assert_card_sealed(second)
        assert runner.get_state(second).answer_text == "第二轮定稿"
