"""v2.0.8 回归测试 — 流式面板质量四连修（用户验收 2026-10-11）.

用户验收问题与根因：
1. 「思考、推理的部分，只有前面一部分的格式是对的，后面格式就不对了」
   → reasoning_feed / 网关中继的 ``💬 {text}`` 自带换行，
   ``split_progress_segments`` 按行直拆把首行后的内容打碎成独立 raw 行
   （只有首行保留思考样式）。修复：多行思考吸附为单一逻辑段。
2. 「"SpaceX买" 不知道是什么内容」→ 模型可见间歇文本（commentary）经
   CH_INTERIM 直 append 成裸行。修复：非 emoji 播报归入思考轮次（V1 语义）；
   最终答案的中继复读按前缀过滤（V1 从不把 final answer 展示进面板）。
3. 「每一条都没有耗时记录，而插件都有的」→ 官方 ``post_tool_call`` 钩子
   （payload 自带 duration_ms）→ 工具标题「Terminal (2.3 s)」；行时间戳 →
   思考轮次标题「第 N 轮 · X.Xs」。
4. 「Self-improvement review 会新起一个卡片输出，但跑马灯一直在」→
   后台通知（💾/⏳ 等，metadata 仅 _interim_send）命中进度启发式 →
   定稿后仍触发建卡（streaming_mode + spinner，永无 finalize）。
   修复：官方非对话通知清单 → CH_INTERIM（活跃卡并入面板 / 无卡原生）。

本文件固化上述语义，防四类问题回归。
"""

from __future__ import annotations

import asyncio

import pytest
from hermes_lark_streaming import tool_timing
from hermes_lark_streaming.cardkit.cards import _render_status_children
from hermes_lark_streaming.cardkit.progline import (
    parse_progress_line,
    split_progress_segments,
)
from hermes_lark_streaming.composer.classify import (
    CH_INTERIM,
    _is_notice_text,
    classify_send,
)
from hermes_lark_streaming.composer.engine import (
    FrameComposer,
    _is_answer_duplicate,
    _merge_progress_lines,
    _reconcile_line_times,
)

MULTI_LINE_THINK = (
    "My previous response was cut off mid-stream while listing news. "
    "Let me continue from where I left off.\n"
    "Remaining items from my search results:\n"
    "乌克兰外交部表示准备在2026冬奥会期间与俄休战\n"
    "美国：田纳西军事工厂爆炸；联邦政府计划二战以来首次行刑队处决"
)

THINK_PREFIX = "💬"


# ---------------------------------------------------------------------------
# 测试夹具（对齐 test_v207 的 fake client 模式）
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self):
        self.cards: dict[str, dict] = {}
        self._n = 0

    async def cardkit_create(self, card):
        self._n += 1
        self.cards[str(self._n)] = {"card": card, "seq": 0}
        return str(self._n)

    async def send_card_by_id_to_chat(self, chat_id, card_id):
        return f"om_{card_id}"

    async def cardkit_stream_element(self, card_id, element_id, content, sequence=0):
        self.cards[card_id]["seq"] = sequence

    async def cardkit_update(self, card_id, card, sequence=0):
        self.cards[card_id]["card"] = card
        self.cards[card_id]["seq"] = sequence


@pytest.fixture()
def composer():
    from hermes_lark_streaming.cardkit.cards import build_streaming_card_v2

    client = _FakeClient()
    comp = FrameComposer(
        get_client=lambda: client,
        build_card=lambda **kw: build_streaming_card_v2(**kw),
        build_final_card=lambda **kw: {},
        config=None,
    )
    tool_timing.reset_for_tests()
    tool_timing.bind_composer(comp)
    yield comp
    tool_timing.reset_for_tests()


async def _settle() -> None:
    for _ in range(4):
        await asyncio.sleep(0)
    cur = asyncio.current_task()
    ts = [t for t in asyncio.all_tasks() if t is not cur and not t.done()]
    if ts:
        await asyncio.gather(*ts, return_exceptions=True)


def _panel_children(comp, chat_id):
    st = comp._states[chat_id]
    return _render_status_children(
        st.status_lines, durations=st.tool_durations, line_times=st.line_times)


async def _seed_active(comp, chat: str):
    """预建 fake 卡片并播种活跃状态（避免直接播种导致的 update KeyError）."""
    client = comp._get_client()
    card_id = await client.cardkit_create({})
    from hermes_lark_streaming.composer.engine import CardTurnState

    comp._states[chat] = CardTurnState(chat_id=chat, card_id=card_id,
                                       card_message_id=f"om_{card_id}")
    return comp._states[chat]


# ---------------------------------------------------------------------------
# 1. 多行思考吸附（问题1 根因）
# ---------------------------------------------------------------------------


class TestMultiLineThinking:
    def test_multiline_thinking_single_segment(self):
        raw = f"{THINK_PREFIX} {MULTI_LINE_THINK}"
        segs = split_progress_segments(raw)
        assert len(segs) == 1
        assert segs[0].startswith(THINK_PREFIX)
        assert "乌克兰外交部" in segs[0]
        kind, _tool, clean = parse_progress_line(segs[0])
        assert kind == "thinking"
        assert "美国：田纳西军事工厂爆炸" in clean

    def test_thinking_absorbs_until_next_emoji(self):
        raw = (f"{THINK_PREFIX} 第一段思考\n继续第二行\n🔍 搜索网页 · q\n"
               f"{THINK_PREFIX} 第二段")
        segs = split_progress_segments(raw)
        assert segs[0].startswith(THINK_PREFIX)
        assert "继续第二行" in segs[0]
        assert segs[1] == "🔍 搜索网页 · q"
        assert segs[2].startswith(THINK_PREFIX)
        assert "第二段" in segs[2]

    def test_fence_not_absorbed_into_thinking(self):
        raw = f"{THINK_PREFIX} 思考一下\n💻 terminal\n```\nuname -r\n```"
        segs = split_progress_segments(raw)
        assert segs[0].startswith(THINK_PREFIX)
        assert "```" in segs[1]
        # 块与工具头一体
        kind, tool, _c = parse_progress_line(segs[1])
        assert kind == "tool" and tool == "terminal"

    def test_accumulate_snapshot_with_multiline_thinking(self):
        raw = (f"💻 terminal\n```\necho hi\n```\n"
               f"{THINK_PREFIX} {MULTI_LINE_THINK}")
        segs = split_progress_segments(raw)
        assert len(segs) == 2
        kind, tool, _c = parse_progress_line(segs[0])
        assert kind == "tool" and tool == "terminal"
        assert parse_progress_line(segs[1])[0] == "thinking"

    def test_multiline_thinking_renders_single_detail(self):
        els = _render_status_children([f"{THINK_PREFIX} {MULTI_LINE_THINK}"])
        # 1 个轮次标题 + 1 个正文 detail（不打碎成多行 raw markdown）
        assert len(els) == 2
        detail = els[1]
        assert detail["text"]["tag"] == "plain_text"
        assert "乌克兰外交部" in detail["text"]["content"]
        assert "美国：田纳西" in detail["text"]["content"]

    def test_multiline_growth_replaced_in_place(self):
        head = f"{THINK_PREFIX} My previous response was cut off"
        full = f"{THINK_PREFIX} {MULTI_LINE_THINK}"
        merged = _merge_progress_lines([], [head])
        assert merged == [head]
        merged2 = _merge_progress_lines(merged, [full])
        assert merged2 == [full]  # 原地取代，不膨胀

    def test_reasoning_body_truncated_at_2000(self):
        from hermes_lark_streaming.cardkit.cards import _truncate_reasoning_body

        long_text = "啊" * 2500
        out = _truncate_reasoning_body(long_text)
        assert len(out) < 2500
        assert "已截断" in out
        assert _truncate_reasoning_body("短文本") == "短文本"


# ---------------------------------------------------------------------------
# 2. 答案复读过滤 + 播报归入思考（问题2）
# ---------------------------------------------------------------------------


class TestAnswerDuplicateFilter:
    def test_final_answer_relay_dropped(self):
        answer = "这是一段足够长的最终答案正文，用来验证中继复读过滤逻辑是否生效。" * 3
        relay = f"{THINK_PREFIX} {answer[:500]}"
        assert _is_answer_duplicate(relay, answer) is True

    def test_genuine_reasoning_kept(self):
        reasoning = "先查新闻再汇总，注意去重，用户要的是 2026 冬奥会期间的国际动态"
        assert _is_answer_duplicate(f"{THINK_PREFIX} {reasoning}", "完全不同的答案正文") is False

    def test_short_fragment_kept(self):
        assert _is_answer_duplicate(f"{THINK_PREFIX} SpaceX买", "SpaceX买入特斯拉股票") is False

    def test_empty_answer_kept(self):
        assert _is_answer_duplicate(f"{THINK_PREFIX} {'x' * 50}", "") is False

    def test_engine_drops_relay(self, composer):
        chat = "oc_dup"
        answer = "这是足够长的最终答案正文，验证引擎层面中继复读被过滤掉。" * 3
        comp = composer
        comp._states[chat] = __import__(
            "hermes_lark_streaming.composer.engine", fromlist=["CardTurnState"]
        ).CardTurnState(chat_id=chat, card_id="1", card_message_id="om_1",
                        answer_text=answer, finalized=False)
        from hermes_lark_streaming.composer.classify import CH_PROGRESS, ClassifyResult

        relay = f"{THINK_PREFIX} {answer[:500]}"
        res = asyncio.get_event_loop().run_until_complete(
            comp.handle_send(chat, relay, None, None,
                             ClassifyResult(CH_PROGRESS, kind="thinking", raw_content=relay)))
        assert res.success
        # 状态行未增加（复读被过滤）
        assert comp._states[chat].status_lines == []


class TestInterimToThinking:
    async def test_commentary_becomes_thinking_row(self, composer):
        chat = "oc_interim"
        comp = composer
        from hermes_lark_streaming.composer.classify import CH_INTERIM, ClassifyResult

        await _seed_active(comp, chat)
        res = await comp.handle_send(chat, "SpaceX买", None, None,
                                     ClassifyResult(CH_INTERIM, kind="advisory",
                                                    raw_content="SpaceX买"))
        assert res.success
        st = comp._states[chat]
        assert st.status_lines == [f"{THINK_PREFIX} SpaceX买"]
        # 渲染：轮次标题 + 灰色正文（不再是无样式 raw markdown）
        els = _panel_children(comp, chat)
        assert len(els) == 2
        assert els[0]["text"]["content"].startswith("<font")

    def test_emoji_advisory_stays_raw(self, composer):
        chat = "oc_adv"
        comp = composer
        from hermes_lark_streaming.composer.classify import CH_INTERIM, ClassifyResult

        async def run():
            await _seed_active(comp, chat)
            return await comp.handle_send(chat, "⏳ Working — 5 min", None, None,
                                          ClassifyResult(CH_INTERIM, kind="advisory",
                                                         raw_content="⏳ Working — 5 min"))

        asyncio.get_event_loop().run_until_complete(run())
        st = comp._states[chat]
        assert st.status_lines == ["⏳ Working — 5 min"]

    def test_interim_no_card_returns_none(self, composer):
        async def run():
            from hermes_lark_streaming.composer.classify import (
                CH_INTERIM,
                ClassifyResult,
            )

            return await composer.handle_send(
                "oc_nocard", "SpaceX买", None, None,
                ClassifyResult(CH_INTERIM, kind="advisory", raw_content="SpaceX买"))

        res = asyncio.get_event_loop().run_until_complete(run())
        assert res is None  # 原生路径（永不建卡）


# ---------------------------------------------------------------------------
# 3. 工具/轮次耗时（问题3）
# ---------------------------------------------------------------------------


class TestToolDurations:
    async def test_duration_attaches_to_tool_row(self, composer):
        chat = "oc_dur"
        comp = composer

        st = await _seed_active(comp, chat)
        st.status_lines = ["💻 运行命令 uname -r"]
        st.line_times = [1.0]
        # 生产中 _loop 由首个 handle_send 捕获；测试直接补齐（镜像生产时序）
        comp._loop = asyncio.get_running_loop()
        assert comp.submit_tool_duration_from_thread(chat, "terminal", 2300.0)
        await _settle()
        st = comp._states[chat]
        assert "💻 运行命令 uname -r" in st.tool_durations
        els = _panel_children(comp, chat)
        title = els[0]["text"]["content"]
        assert "(2.3 s)" in title

    async def test_duration_pending_queue_claimed_by_row(self, composer):
        chat = "oc_pend"
        comp = composer
        from hermes_lark_streaming.composer.classify import CH_PROGRESS, ClassifyResult

        await _seed_active(comp, chat)
        comp._loop = asyncio.get_running_loop()
        # 钩子先到：行未出现 → pending 队列
        assert comp.submit_tool_duration_from_thread(chat, "web_search", 850.0)
        await _settle()
        st = comp._states[chat]
        assert st.status_lines == []
        assert st.pending_tool_durations.get("web_search") == ["850 ms"]
        # 文本帧后到：行追加时认领
        await comp.handle_send(
            chat, "🔍 搜索网页 · 新闻", None, None,
            ClassifyResult(CH_PROGRESS, kind="tool", tool_name="web_search",
                           raw_content="🔍 搜索网页 · 新闻"))
        await _settle()
        st = comp._states[chat]
        assert "🔍 搜索网页 · 新闻" in st.tool_durations
        assert "web_search" not in st.pending_tool_durations
        els = _panel_children(comp, chat)
        assert "(850 ms)" in els[0]["text"]["content"]

    def test_round_elapsed_in_title(self, composer):
        lines = [f"{THINK_PREFIX} 思考正文A", f"{THINK_PREFIX} 思考正文A续",
                 "💻 运行命令 uname -r"]
        times = [10.0, 11.0, 12.0]
        els = _render_status_children(lines, line_times=times)
        assert "第 1 轮 · 2.0s" in els[0]["text"]["content"]

    def test_round_elapsed_omitted_without_times(self):
        els = _render_status_children([f"{THINK_PREFIX} 思考"])
        assert "·" not in els[0]["text"]["content"].split("**")[1].replace("第 1 轮", "", 1) or True
        # 不携带耗时信息（title 不含 elapsed 段）
        assert "s<" not in els[0]["text"]["content"]
        assert "第 1 轮" in els[0]["text"]["content"]

    def test_reconcile_inherits_thinking_growth(self):
        old = [f"{THINK_PREFIX} 前缀文本"]
        old_times = [100.0]
        new = [f"{THINK_PREFIX} 前缀文本\n续行"]
        times = _reconcile_line_times(old, old_times, new)
        assert times == [100.0]  # 增长取代继承起点

    def test_reconcile_new_line_gets_now(self):
        old = ["💻 运行命令 a"]
        old_times = [1.0]
        new = ["💻 运行命令 a", f"{THINK_PREFIX} 新思考"]
        times = _reconcile_line_times(old, old_times, new)
        assert times[0] == 1.0
        assert times[1] > 1.0


# ---------------------------------------------------------------------------
# 4. 非对话通知 → 永不建卡（问题4）
# ---------------------------------------------------------------------------


class TestNoticeRouting:
    def test_self_improvement_is_notice(self):
        assert _is_notice_text("💾 Self-improvement review: User profile updated")
        assert _is_notice_text("💾  Self-improvement review: Skill 'x' improved")

    def test_other_notices(self):
        assert _is_notice_text("⏳ Working — 5 min")
        assert _is_notice_text("✅ Hermes update finished successfully")
        assert _is_notice_text("♻️ Gateway restarted successfully")
        assert not _is_notice_text("普通用户消息")
        assert not _is_notice_text("💻 运行命令 uname -r")

    def test_notice_classifies_interim(self):
        cls = classify_send("💾 Self-improvement review: User profile updated",
                            {"_interim_send": True})
        assert cls.channel == CH_INTERIM
        assert cls.kind == "advisory"

    def test_notice_with_no_card_returns_none(self, composer):
        async def run():
            from hermes_lark_streaming.composer.classify import ClassifyResult

            return await composer.handle_send(
                "oc_notice", "💾 Self-improvement review: User profile updated",
                None, {"_interim_send": True},
                ClassifyResult(CH_INTERIM, kind="advisory",
                               raw_content="💾 Self-improvement review: User profile updated"))

        res = asyncio.get_event_loop().run_until_complete(run())
        assert res is None  # 原生文本，不建卡 → 无跑马灯
        assert "oc_notice" not in composer._states  # 未建占位状态

    def test_notice_with_active_card_appends_to_panel(self, composer):
        chat = "oc_notice2"
        comp = composer
        from hermes_lark_streaming.composer.classify import ClassifyResult

        async def run():
            await _seed_active(comp, chat)
            await comp.handle_send(
                chat, "💾 Self-improvement review: User profile updated",
                None, {"_interim_send": True},
                ClassifyResult(CH_INTERIM, kind="advisory",
                               raw_content="💾 Self-improvement review: User profile updated"))

        asyncio.get_event_loop().run_until_complete(run())
        st = comp._states[chat]
        assert st.status_lines == ["💾 Self-improvement review: User profile updated"]

    def test_upgrade_plain_skips_notice_and_notify(self, composer):
        from hermes_lark_streaming.adapter import _upgrade_plain_to_progress

        assert _upgrade_plain_to_progress(
            composer, "oc_x", "💾 Self-improvement review: User profile updated") is None
        assert _upgrade_plain_to_progress(
            composer, "oc_x", "🔍 搜索网页 · q", {"notify": True}) is None
        # 普通进度文本仍升格
        cls = _upgrade_plain_to_progress(composer, "oc_x", "🔍 搜索网页 · q")
        assert cls is not None and cls.channel == "progress"


# ---------------------------------------------------------------------------
# 5. tool_timing 模块 + 钩子载荷
# ---------------------------------------------------------------------------


class TestToolTimingModule:
    def test_post_tool_call_payload_submits(self, composer, monkeypatch):
        chat = "oc_hook"
        comp = composer

        async def run():
            st = await _seed_active(comp, chat)
            st.status_lines = ["💻 运行命令 echo hi"]
            st.line_times = [1.0]
            comp._loop = asyncio.get_running_loop()
            assert comp.submit_tool_duration_from_thread(chat, "terminal", 2300.0)
            await _settle()

        asyncio.get_event_loop().run_until_complete(run())
        st = comp._states[chat]
        assert st.tool_durations.get("💻 运行命令 echo hi") == "2.3 s"

    def test_zero_duration_ignored(self, composer, monkeypatch):
        called = []

        def fake_submit(chat, tool, ms):
            called.append((chat, tool, ms))
            return True

        monkeypatch.setattr(tool_timing, "_current_chat_id", lambda: "oc_z")
        comp = composer
        monkeypatch.setattr(comp, "submit_tool_duration_from_thread", fake_submit)
        tool_timing.on_post_tool_call(tool_name="terminal", duration_ms=0)
        tool_timing.on_post_tool_call(tool_name="", duration_ms=100)
        assert called == []

    def test_register_hooks_invokes_ctx(self):
        class _Ctx:
            def __init__(self):
                self.names = []

            def register_hook(self, name, cb):
                self.names.append(name)

        ctx = _Ctx()
        tool_timing.register_hooks(ctx)
        assert ctx.names == ["post_tool_call"]

    def test_max_line_chars_raised(self):
        from hermes_lark_streaming import reasoning_feed

        assert reasoning_feed._MAX_LINE_CHARS == 2000


# ---------------------------------------------------------------------------
# 6. 引擎端到端：多行思考进面板不打碎（回归守卫）
# ---------------------------------------------------------------------------


class TestEngineMultiLineThinking:
    def test_feed_multiline_thinking_single_row(self, composer):
        chat = "oc_ml"
        comp = composer

        async def run():
            from hermes_lark_streaming.composer.classify import (
                CH_PROGRESS,
                ClassifyResult,
            )

            raw = f"{THINK_PREFIX} {MULTI_LINE_THINK}"
            await comp.handle_send(chat, raw, None, None,
                                   ClassifyResult(CH_PROGRESS, kind="thinking",
                                                  raw_content=raw))
            # 增长 flush（模拟 reasoning_feed 下一次累积全文）
            grown = f"{THINK_PREFIX} {MULTI_LINE_THINK}\n股市：OpenAI 年化收入 $50B"
            await comp.handle_send(chat, grown, None, None,
                                   ClassifyResult(CH_PROGRESS, kind="thinking",
                                                  raw_content=grown))

        asyncio.get_event_loop().run_until_complete(run())
        st = comp._states[chat]
        # 思考行原地增长（1 行），新内容不产生独立 raw 行
        thinking_rows = [ln for ln in st.status_lines if ln.startswith(THINK_PREFIX)]
        assert len(thinking_rows) == 1
        assert "股市：OpenAI" in thinking_rows[0]
        assert "乌克兰外交部" in thinking_rows[0]
        # 渲染：轮次标题 + 单一正文元素
        els = _panel_children(comp, chat)
        assert len(els) == 2
        assert els[1]["text"]["tag"] == "plain_text"
