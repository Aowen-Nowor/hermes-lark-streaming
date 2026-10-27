"""v2.0.26 回归测试 — clarify 暂停/恢复（同卡续流）.

用户验收（2026-10-27）：「在用户发消息后，创建一个流式输出卡片，创建
Clarify 交互卡片时，流式输出卡片应该是要等待创建 Clarify 交互卡片的回调后，
继续进行流式输出，直到卡片状态变成已完成」——此前行为是「流式卡先已完成 →
clarify 卡出现 → 回调后另开一张新卡续流」。

根因（hermes 平台语义，非插件缺陷）：hermes 在 clarify 交互前强制关闭当前流
（gateway/run_turn_runner.py:1404 ``_close_native_stream_boundary("Clarify",
placeholder, reopen=True)``，WeCom 气泡模型语义），回调后 Eager re-seed 重开
新流。实测序列（agent.log 13:20:35→13:21:29）：card created → Clarify boundary:
finalized stream → tool clarify completed → Eager re-seed → card created（第二张）。

修复：finalize 帧先进暂停窗口（不 split/不落正文面板/不关流式）；窗口内
send_clarify 到达（adapter.suspend_for_clarify → engine.suspend_for_clarify）
确认边界 → 卡保持流式态 + 面板暂停行；回调后 seed/delta 帧续写同一张卡
（段感知拼接）；窗口到期无 send_clarify → _pause_expire 真落定（普通回合
仅延迟 clarify_suspend_sec）。

2026-10-27 13:20 实测回合在本套件中的等价序列 = test_full_clarify_turn_one_card
（两次 clarify + 三段流 + 一张卡）。
"""

from __future__ import annotations

import asyncio

import pytest
from hermes_lark_streaming.cardkit.cards import build_streaming_card_v2
from hermes_lark_streaming.composer import engine as _engine
from hermes_lark_streaming.composer.engine import (
    FrameComposer,
    _clarify_wait_row,
    _is_clarify_placeholder,
    _merge_resumed_answer,
)

from .test_v208_stream_quality import _FakeClient  # noqa: F401 — _FullClient 依赖其导入链
from .test_v224_lazy_panel import _FullClient

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_WAIT_ROW = _clarify_wait_row()


def _make_composer(client: _FullClient, suspend_sec: float | None = 1.2) -> FrameComposer:
    return FrameComposer(
        get_client=lambda: client,
        build_card=lambda **kw: build_streaming_card_v2(**kw),
        build_final_card=lambda **kw: {},
        config=None,
        clarify_suspend_sec=suspend_sec,
    )


class TestHelpers:
    def test_placeholder_shapes(self):
        assert _is_clarify_placeholder("💬 等待你的选择...")
        assert _is_clarify_placeholder("💬 Waiting for your choice...")
        assert not _is_clarify_placeholder("")
        assert not _is_clarify_placeholder("普通回答")
        # 多行 / 超长 / 非💬开头：不是占位
        assert not _is_clarify_placeholder("💬 第一行\n第二行")
        assert not _is_clarify_placeholder("💬 " + "x" * 60)
        assert not _is_clarify_placeholder("✅ 定时任务已创建")

    def test_merge_resumed_answer(self):
        assert _merge_resumed_answer("", "新段") == "新段"
        assert _merge_resumed_answer("前段", "") == "前段"
        assert _merge_resumed_answer("", "") == ""
        assert _merge_resumed_answer("前段", "新段") == "前段\n\n新段"

    def test_wait_row_i18n_en(self):
        assert _WAIT_ROW.startswith("⏸")
        from hermes_lark_streaming.cardkit.i18n import _T

        assert _T["clarify_wait"][1].startswith("⏸")


class TestPauseWindow:
    async def test_finalize_frame_holds_card_open(self):
        """finalize 帧进暂停窗口：不落定、不关流式，等 send_clarify 确认."""
        client = _FullClient()
        comp = _make_composer(client)
        chat = "oc_pause_hold"
        await comp.handle_stream_frame(chat, "我先确认一下", finalize=False, turn_id="t1")
        ok = await comp.handle_stream_frame(
            chat, "💬 等待你的选择...", finalize=True, turn_id="t1")
        assert ok is True
        st = comp._states[chat]
        assert st.pause_pending is True
        assert st.finalized is False, "窗口期不得落定（用户验收：卡片不能先变已完成）"
        assert client.close_calls == 0, "窗口期不得关流式"
        assert st.turn_id == "t1"

    async def test_suspend_confirms_boundary_and_marks_card(self):
        """send_clarify 到达：取消窗口 → paused + 暂停行 + 占位文本清空."""
        client = _FullClient()
        comp = _make_composer(client)
        chat = "oc_pause_suspend"
        await comp.handle_stream_frame(chat, "", finalize=False, turn_id="t1")  # seed 建卡
        await comp.handle_stream_frame(
            chat, "💬 等待你的选择...", finalize=True, turn_id="t1")
        ok = await comp.suspend_for_clarify(chat, "选哪种方式？")
        assert ok is True
        st = comp._states[chat]
        assert st.pause_pending is False
        assert st.paused is True
        assert st.finalized is False
        assert client.close_calls == 0
        assert st.answer_text == "", "hermes 占位文案不落正文"
        assert st.resume_base == ""
        assert _WAIT_ROW in st.status_lines
        assert st.panel_on_card is True, "暂停行经 _patch_status 补插面板"

    async def test_suspend_without_pause_pending_still_pauses(self):
        """窗口外（edit 灰度流 / 直接调用）：活跃卡同样可暂停."""
        client = _FullClient()
        comp = _make_composer(client)
        chat = "oc_pause_direct"
        await comp.handle_stream_frame(chat, "答案中", finalize=False)
        ok = await comp.suspend_for_clarify(chat, "q")
        assert ok is True
        st = comp._states[chat]
        assert st.paused and not st.finalized
        assert st.resume_base == "答案中"
        assert client.close_calls == 0

    async def test_suspend_no_card_returns_false(self):
        client = _FullClient()
        comp = _make_composer(client)
        assert await comp.suspend_for_clarify("oc_pause_nocard", "q") is False

    async def test_expire_finalizes_normally(self):
        """窗口到期无 send_clarify → 普通回合真落定（footer/close 全链）."""
        client = _FullClient()
        comp = _make_composer(client)
        chat = "oc_pause_expire"
        await comp.handle_stream_frame(chat, "正文", finalize=False)
        await comp.handle_stream_frame(chat, "正文定稿", finalize=True, turn_id="t9")
        st = comp._states[chat]
        assert st.pause_pending is True and not st.finalized
        await comp._pause_expire(chat)
        assert st.finalized is True
        assert st.pause_pending is False
        assert client.close_calls == 1
        assert st.answer_text == "正文定稿"

    async def test_grace_zero_keeps_v225_semantics(self):
        """clarify_suspend_sec=0（测试/关闭）：finalize 立即落定（v2.0.25 语义）."""
        client = _FullClient()
        comp = _make_composer(client, suspend_sec=0.0)
        chat = "oc_pause_off"
        await comp.handle_stream_frame(chat, "正文", finalize=False)
        await comp.handle_stream_frame(chat, "定稿", finalize=True)
        st = comp._states[chat]
        assert st.finalized is True and client.close_calls == 1

    async def test_composer_config_none_means_zero(self):
        """config=None（存量测试语境）→ 窗口关闭，finalize 立即落定."""
        client = _FullClient()
        comp = FrameComposer(
            get_client=lambda: client,
            build_card=lambda **kw: build_streaming_card_v2(**kw),
            build_final_card=lambda **kw: {},
            config=None,
        )
        assert comp._clarify_suspend_sec == 0.0


class TestResumeSameCard:
    async def test_seed_then_delta_continues_same_card(self):
        """回调后 seed→delta 续写同一张卡：card_id 不变、建卡仅一次."""
        client = _FullClient()
        comp = _make_composer(client)
        chat = "oc_resume_same"
        await comp.handle_stream_frame(chat, "", finalize=False, turn_id="t1")  # seed
        st = comp._states[chat]
        card_id = st.card_id
        await comp.handle_stream_frame(
            chat, "💬 等待你的选择...", finalize=True, turn_id="t1")
        await comp.suspend_for_clarify(chat, "q")
        # 回调后：Eager re-seed（空 seed 帧）→ delta 快照（新段内累积）
        await comp.handle_stream_frame(chat, "", finalize=False, turn_id="t1")
        assert st.paused is False
        assert st.card_id == card_id, "同一张卡续流"
        assert _WAIT_ROW not in st.status_lines
        assert client.close_calls == 0
        await comp.handle_stream_frame(chat, "继续输出的内容", finalize=False, turn_id="t1")
        assert st.answer_text == "继续输出的内容"
        assert st.card_id == card_id
        # 真回合终态 → 正常落定
        await comp.handle_stream_frame(chat, "继续输出的内容", finalize=True, turn_id="t1")
        await comp._pause_expire(chat)
        assert st.finalized is True and client.close_calls == 1
        assert client._n == 1, "全回合仅建卡一次（用户验收核心）"

    async def test_resume_merges_segment_after_pre_pause_body(self):
        """暂停前已有正文 + 新段快照 → 段拼接（hermes reopen 快照不含前段）."""
        client = _FullClient()
        comp = _make_composer(client)
        chat = "oc_resume_merge"
        await comp.handle_stream_frame(chat, "暂停前的结论", finalize=False, turn_id="t1")
        await comp.handle_stream_frame(chat, "暂停前的结论", finalize=True, turn_id="t1")
        await comp.suspend_for_clarify(chat, "q")
        await comp.handle_stream_frame(chat, "", finalize=False, turn_id="t1")
        await comp.handle_stream_frame(chat, "澄清后的补充", finalize=False, turn_id="t1")
        st = comp._states[chat]
        assert st.answer_text == "暂停前的结论\n\n澄清后的补充"
        assert st.resume_base == "暂停前的结论\n\n澄清后的补充"

    async def test_double_clarify_accumulates(self):
        """同一回合两次 clarify（实测 13:20 回合）：两次暂停/恢复递进累积."""
        client = _FullClient()
        comp = _make_composer(client)
        chat = "oc_resume_double"
        await comp.handle_stream_frame(chat, "", finalize=False, turn_id="t1")
        st = comp._states[chat]
        # 第一次 clarify
        await comp.handle_stream_frame(
            chat, "💬 等待你的选择...", finalize=True, turn_id="t1")
        await comp.suspend_for_clarify(chat, "q1")
        await comp.handle_stream_frame(chat, "", finalize=False, turn_id="t1")
        await comp.handle_stream_frame(chat, "第一问之后的话", finalize=False, turn_id="t1")
        assert st.answer_text == "第一问之后的话"
        # 第二次 clarify
        await comp.handle_stream_frame(chat, "第一问之后的话", finalize=True, turn_id="t1")
        await comp.suspend_for_clarify(chat, "q2")
        assert st.paused is True and client.close_calls == 0
        await comp.handle_stream_frame(chat, "", finalize=False, turn_id="t1")
        await comp.handle_stream_frame(chat, "第二问之后的正文", finalize=False, turn_id="t1")
        assert st.answer_text == "第一问之后的话\n\n第二问之后的正文"
        assert st.card_id == comp._states[chat].card_id
        # 真终态落定
        await comp.handle_stream_frame(
            chat, "第一问之后的话\n\n第二问之后的正文", finalize=True, turn_id="t1")
        await comp._pause_expire(chat)
        assert st.finalized and client.close_calls == 1
        assert client._n == 1


class TestTurnGuardAndExemptions:
    async def test_new_turn_frame_seals_suspended_card(self):
        """新回合帧到达（turn_id 变化）：暂停卡立即落定，新回合建新卡.

        注：_create_card 复用 prev 状态对象（既有设计）——旧卡的落定证据是
        close_streaming 调用与新建卡 card_id 递增，而非对象字段快照。
        """
        client = _FullClient()
        comp = _make_composer(client)
        chat = "oc_turn_guard"
        await comp.handle_stream_frame(chat, "", finalize=False, turn_id="t-old")
        old_card_id = comp._states[chat].card_id
        await comp.handle_stream_frame(chat, "旧回合正文", finalize=True, turn_id="t-old")
        await comp.suspend_for_clarify(chat, "q")
        assert comp._states[chat].paused is True
        # 用户不等回答，直接发新消息 → 新 turn 的 seed 帧
        await comp.handle_stream_frame(chat, "", finalize=False, turn_id="t-new")
        assert client.close_calls == 1, "暂停卡被新回合帧触达时立即真落定"
        st = comp._states[chat]
        assert st.card_id != old_card_id, "新回合建新卡"
        assert st.turn_id == "t-new"
        assert st.paused is False and st.finalized is False

    async def test_watchdog_spares_suspended_card(self):
        """暂停/窗口期看门狗让位：静默达标也不 seal（等待是预期非孤儿）."""
        client = _FullClient()
        comp = _make_composer(client)
        chat = "oc_watchdog_pause"
        await comp.handle_stream_frame(chat, "", finalize=False)
        st = comp._states[chat]
        await comp.handle_stream_frame(chat, "💬 等待你的选择...", finalize=True)
        await comp.suspend_for_clarify(chat, "q")
        st.last_activity -= (_engine._WATCHDOG_IDLE_SEC + 1)
        comp._watchdog_due(chat)  # due 时 paused → rearm + return，不 seal
        await asyncio.sleep(0.05)
        assert st.finalized is False and not st.degraded
        assert client.close_calls == 0

    async def test_gc_spares_suspended_card(self):
        """暂停卡豁免 idle GC：状态保留到回调/超时（TTL 600s < hermes 3600s）."""
        client = _FullClient()
        comp = _make_composer(client)
        chat = "oc_gc_pause"
        await comp.handle_stream_frame(chat, "", finalize=False)
        st = comp._states[chat]
        await comp.handle_stream_frame(chat, "💬 等待你的选择...", finalize=True)
        await comp.suspend_for_clarify(chat, "q")
        st.last_activity -= 10_000.0  # 远超 TTL
        comp._gc()
        assert comp._states.get(chat) is st, "暂停卡不得被 GC 回收"

    async def test_finalize_clears_pause_timer_on_forget(self):
        """forget（用户删卡）清理暂停定时器，不泄漏."""
        client = _FullClient()
        comp = _make_composer(client)
        chat = "oc_forget_pause"
        await comp.handle_stream_frame(chat, "", finalize=False)
        await comp.handle_stream_frame(chat, "定稿", finalize=True)  # 挂起窗口
        assert chat in comp._pause_timers
        comp.forget(chat)
        assert chat not in comp._pause_timers
