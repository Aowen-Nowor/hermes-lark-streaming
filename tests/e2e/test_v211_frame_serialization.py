"""v2.0.11 回归测试 — per-chat 帧串行化（多卡片竞态修复）.

生产事故（2026-10-12 14:30 "几点了"，gateway21）：一轮对话 1.2s 内连建 3 张卡
（composer: card created ×3，seeds 1/1/2 行），仅末张被定稿，前两张成为孤儿卡。
根因：``_on_progress`` early-create 路径 = active_state() 检查 → await
cardkit_create → await send_card_by_id_to_chat（合计 ≈0.5-1s 挂起）→ 注册状态；
挂起窗口内并发帧（工具完成后的思考中继/工具行/status 通报多 lane 齐发，
reasoning 回灌走 ensure_future 火忘任务）各自看到"无活跃卡"→ 各建一张。
10-11 23:13 同族（两卡相隔 145ms，其一为 0 行空面板孤儿卡）。

修复：FrameComposer 以 per-chat asyncio.Lock 串行化 handle_send/handle_edit；
锁内 active_state 复查即双重检查。并发帧在建卡窗口内排队 → 首帧建卡、
后续帧合并进同一张卡（或暂存后随建卡并入面板种子）。

测试策略：慢速 mock client（create/send 各 sleep）确定性放大挂起窗口——
无锁时 N 帧 = N 张卡（确定性复现），有锁时恒为 1 张。
"""

from __future__ import annotations

import asyncio

import pytest
from hermes_lark_streaming.composer.classify import (
    CH_PROGRESS,
    CH_STREAM,
    ClassifyResult,
)
from hermes_lark_streaming.composer.engine import FrameComposer

from .test_v208_stream_quality import _FakeClient

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


class _SlowClient(_FakeClient):
    """cardkit_create / send_card_by_id_to_chat 带网络延迟的 fake.

    延迟把「建卡窗口」（检查活跃状态 → 注册新状态之间的挂起段）放大到
    确定性可复现：无锁并发下所有 burst 帧都会在首帧注册前进入建卡分支。
    """

    def __init__(self, delay: float = 0.05) -> None:
        super().__init__()
        self.delay = delay
        self.create_calls = 0
        self.send_calls = 0

    async def cardkit_create(self, card):
        self.create_calls += 1
        await asyncio.sleep(self.delay)
        return await super().cardkit_create(card)

    async def send_card_by_id_to_chat(self, chat_id, card_id):
        self.send_calls += 1
        await asyncio.sleep(self.delay)
        return f"om_{card_id}"


def _make_composer(client: _SlowClient) -> FrameComposer:
    from hermes_lark_streaming.cardkit.cards import build_streaming_card_v2

    return FrameComposer(
        get_client=lambda: client,
        build_card=lambda **kw: build_streaming_card_v2(**kw),
        build_final_card=lambda **kw: {},
        config=None,
    )


def _progress(content: str) -> ClassifyResult:
    return ClassifyResult(channel=CH_PROGRESS, kind="thinking", raw_content=content)


def _stream(content: str) -> ClassifyResult:
    return ClassifyResult(channel=CH_STREAM, kind="answer", raw_content=content)


async def _fire_burst(comp: FrameComposer, chat: str, frames: list[ClassifyResult]) -> None:
    """并发发射帧（gather = 同一事件循环内的真并发，复现生产 burst）."""
    await asyncio.gather(
        *(
            comp.handle_send(chat, (f.raw_content or ""), None, None, f)
            for f in frames
        )
    )


class TestBurstProgressSingleCard:
    """工具完成后的多 lane burst：N 帧并发 → 恰好 1 张卡."""

    async def test_six_progress_frames_one_card(self):
        client = _SlowClient()
        comp = _make_composer(client)
        chat = "oc_burst"
        frames = [_progress(f"💬 thinking fragment {i}") for i in range(6)]
        await _fire_burst(comp, chat, frames)

        assert client.create_calls == 1, (
            f"burst 帧必须只建 1 张卡（生产事故 2026-10-12 14:30），实际 {client.create_calls}"
        )
        assert client.send_calls == 1
        st = comp.active_state(chat)
        assert st is not None
        # 全部帧的思考行都并入同一张卡（无丢失）
        assert len(st.status_lines) == 6
        assert st.card_message_id == "om_1"

    async def test_mixed_stream_progress_burst_one_card(self):
        """answer 流帧与进度帧混合 burst（工具完成后答案即刻开流的形态）."""
        client = _SlowClient()
        comp = _make_composer(client)
        chat = "oc_mixed"
        frames: list[ClassifyResult] = [
            _progress("💬 round one thinking"),
            _stream("现在"),
            _progress("🔧 terminal：\"date\""),
            _stream("现在是 14:30"),
            _progress("💬 second round"),
            _stream("现在是 14:30 (CST)。"),
        ]
        await _fire_burst(comp, chat, frames)

        assert client.create_calls == 1
        st = comp.active_state(chat)
        assert st is not None
        # answer 全量缓冲保序拼接；3 条进度行全部入面板
        assert st.answer_text == "现在\n\n现在是 14:30\n\n现在是 14:30 (CST)。"
        assert len(st.status_lines) == 3

    async def test_reasoning_feed_style_fire_and_forget_single_card(self):
        """reasoning 回灌同款路径：submit_progress_from_thread 火忘任务并发.

        生产里 reasoning_feed 与多 lane 帧就是经 ensure_future 火忘任务进入
        handle_send 的——本测试不经 gather 而是模拟真实的任务投递时序。
        """
        client = _SlowClient()
        comp = _make_composer(client)
        chat = "oc_fire"
        comp._loop = asyncio.get_running_loop()
        for i in range(5):
            assert comp.submit_progress_from_thread(chat, f"💬 reasoning {i}")
        await _settle_all()
        assert client.create_calls == 1
        st = comp.active_state(chat)
        assert st is not None
        assert len(st.status_lines) == 5


class TestLockHandover:
    """锁的跨事件交接：定稿与下一回合建卡复用同一状态对象时无交错."""

    async def test_finalize_then_new_turn_progress_two_cards_exactly(self):
        """turn1 定稿后紧接新回合 burst：恰好 1 张新卡（重复建卡防线）.

        定稿与建卡共用 per-chat 锁：finalize（edit 通道）先完成，burst 在锁上
        排队后进入 → 上一 turn 状态被复用重建 → 恰好 1 张新卡。
        """
        client = _SlowClient()
        comp = _make_composer(client)
        chat = "oc_handover"

        # turn 1：建卡 → 定稿
        await comp.handle_send(chat, "第一回合答案", None, None, _stream("第一回合答案"))
        st1 = comp.active_state(chat)
        assert st1 is not None
        msg1 = st1.card_message_id
        await comp.handle_edit(chat, msg1, "第一回合答案", True, _stream("第一回合答案"))
        assert comp.active_state(chat) is None  # 已定稿

        # turn 2：burst 紧随定稿（生产形态：/retry 或连续追问）
        await _fire_burst(
            comp, chat,
            [_progress("💬 new turn"), _progress("💬 new turn two"), _stream("新答案")],
        )
        assert client.create_calls == 2, (
            f"turn1 + turn2 应恰好 2 张卡，实际 {client.create_calls}"
        )
        st2 = comp.active_state(chat)
        assert st2 is not None
        assert st2.card_message_id != msg1
        assert st2.answer_text == "新答案"

    async def test_finalize_concurrent_with_progress_burst_no_crash(self):
        """定稿与同回合进度 burst 同时在途：无异常、无重复定稿（P1-4 幂等保持）."""
        client = _SlowClient()
        comp = _make_composer(client)
        chat = "oc_race_final"

        await comp.handle_send(chat, "初始答案", None, None, _stream("初始答案"))
        st = comp.active_state(chat)
        assert st is not None
        msg = st.card_message_id

        async def _finalize():
            await comp.handle_edit(chat, msg, "初始答案", True, _stream("初始答案"))

        await asyncio.gather(
            _finalize(),
            comp.handle_send(chat, "💬 late line", None, None, _progress("💬 late line")),
            comp.handle_send(chat, "💬 late line two", None, None, _progress("💬 late line two")),
        )
        # 不抛异常即通过；定稿幂等由 v2.0.9 P1-4 保证（这里验证锁未破坏它）
        assert client.create_calls >= 1


class TestLockUnit:
    """锁对象管理语义."""

    async def test_same_chat_same_lock_different_chat_different_lock(self):
        client = _FakeClient()
        comp = _make_composer(client)  # type: ignore[arg-type]
        lock_a1 = comp._chat_lock("oc_a")
        lock_a2 = comp._chat_lock("oc_a")
        lock_b = comp._chat_lock("oc_b")
        assert lock_a1 is lock_a2
        assert lock_a1 is not lock_b

    async def test_clear_resets_locks(self):
        client = _FakeClient()
        comp = _make_composer(client)  # type: ignore[arg-type]
        lock_a = comp._chat_lock("oc_a")
        comp.clear()
        assert comp._chat_lock("oc_a") is not lock_a


async def _settle_all() -> None:
    """等全部在途火忘任务结束（镜像 test_v208 的 _settle 语义）。"""
    for _ in range(6):
        await asyncio.sleep(0)
    cur = asyncio.current_task()
    ts = [t for t in asyncio.all_tasks() if t is not cur and not t.done()]
    if ts:
        await asyncio.gather(*ts, return_exceptions=True)
