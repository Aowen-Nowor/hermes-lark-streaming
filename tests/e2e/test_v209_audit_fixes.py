"""v2.0.9 回归测试 — 审计修复六连（P0×2 / P1×2 / P2×3）.

审计来源（Task 11 五轮审计，用户确认 2026-10-12）与修复对应：

- P0-1 300317 sequence 冲突自愈：``client._retry_transient`` 网络错误重试同
  seq 重发，首次请求实际已达服务端时必撞 300317 → 整回合降级纯文本
  （gateway19.log 23:09:50 生产事故）。真飞书实验定案：重放必现、seq+1 立即
  自愈、跳号允许 → 主流程自愈（cardkit/seq.py）。
- P0-2 GC 运算符优先级：``(finalized and idle>60) or (created_at>ttl)`` 使
  活跃回合建卡满 TTL 即被回收（长回合断卡）。重写为空闲回收。
- P1-3 幽灵卡竞态：agent 层 on_stream_end 先于网关 finalize edit，reasoning
  尾 flush 入队守卫通过、执行时回合已终态 → 占位分支重建带跑马灯幽灵卡。
  final flush 执行时复查。
- P1-4 finalize 幂等：网关重试/重复 finalize 事件重复插页脚。finalized
  先行置位短路。
- P2-5 元素上限安全网接线：``_enforce_card_element_limit`` 此前定义+导出但
  从未调用（test_cardkit.py 已覆盖，此处不重复）。
- P2-8 工具耗时：尾反查任意未挂行 → 钩子丢失时误挂更早调用；只认尾数第一
  个同工具行 + pending→preview 取代时标签迁移 + 建卡重置耗时表。
- P2-9 可观测性：插件 logger 显式 INFO（register 内联，依赖 hermes 环境，
  由部署探针验证，此处不重复）。
"""

from __future__ import annotations

import time

import pytest
from hermes_lark_streaming.cardkit.seq import (
    SEQ_CONFLICT,
    call_with_seq_recovery,
)
from hermes_lark_streaming.composer.engine import (
    CardTurnState,
    FrameComposer,
)

from .test_v208_stream_quality import _FakeClient, _settle


class _FakeAPIError(Exception):
    """duck-type 飞书 API 错误（.code 属性），避免测试依赖 lark_oapi."""

    def __init__(self, message: str, code: int = 0) -> None:
        super().__init__(message)
        self.code = code


class _SeqClient(_FakeClient):
    """可编程失败 fake：``fail_plan[op] = 剩余失败次数``，命中抛 300317."""

    def __init__(self, fail_plan: dict[str, int] | None = None):
        super().__init__()
        self.fail_plan = dict(fail_plan or {})
        self.calls: list[tuple[str, int]] = []
        self.batch_update_calls: list[int] = []

    def _hit(self, op: str, seq: int) -> None:
        self.calls.append((op, seq))
        left = self.fail_plan.get(op, 0)
        if left > 0:
            self.fail_plan[op] = left - 1
            raise _FakeAPIError("sequence compare failed", code=SEQ_CONFLICT)

    async def cardkit_stream_element(self, card_id, element_id, content, sequence=0):
        self._hit("stream", sequence)
        self.cards[card_id]["seq"] = sequence

    async def cardkit_update(self, card_id, card, sequence=0):
        self._hit("update", sequence)
        self.cards[card_id]["card"] = card
        self.cards[card_id]["seq"] = sequence

    async def cardkit_batch_update(self, card_id, actions, sequence=0):
        self._hit("batch", sequence)
        self.batch_update_calls.append(sequence)

    async def cardkit_close_streaming(self, card_id, sequence=0, summary=None):
        self._hit("close", sequence)
        self.cards[card_id]["closed"] = True


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
    yield comp


async def _seed_active(comp, chat: str, client: _FakeClient | None = None):
    """预建 fake 卡片并播种活跃状态."""
    client = client or comp._get_client()
    card_id = await client.cardkit_create({})
    comp._states[chat] = CardTurnState(chat_id=chat, card_id=card_id,
                                       card_message_id=f"om_{card_id}")
    return comp._states[chat]


def _stream_cls(text: str):
    from hermes_lark_streaming.composer.classify import CH_STREAM, ClassifyResult

    return ClassifyResult(CH_STREAM, raw_content=text)


def _progress_cls(text: str):
    from hermes_lark_streaming.composer.classify import CH_PROGRESS, ClassifyResult

    return ClassifyResult(CH_PROGRESS, kind="tool", raw_content=text)

# ---------------------------------------------------------------------------
# P0-1：300317 sequence 冲突主流程自愈
# ---------------------------------------------------------------------------


class TestSeqSelfHeal:
    async def test_conflict_recovers_with_seq_plus_one(self):
        """首发撞 300317 → 本地 seq+1 重发同内容 → 成功."""
        st = CardTurnState(chat_id="c1")
        st.seq = 5
        calls: list[int] = []

        async def send(seq: int) -> None:
            calls.append(seq)
            if seq == 5:
                raise _FakeAPIError("sequence compare failed", code=SEQ_CONFLICT)

        await call_with_seq_recovery(st, send, what="test")
        assert calls == [5, 6]
        assert st.seq == 6

    async def test_conflict_raises_after_max_recoveries(self):
        """连续 300317 超过上限 → 上抛（交降级红线）."""
        st = CardTurnState(chat_id="c1")
        st.seq = 1
        calls: list[int] = []

        async def send(seq: int) -> None:
            calls.append(seq)
            raise _FakeAPIError("always conflict", code=SEQ_CONFLICT)

        with pytest.raises(_FakeAPIError):
            await call_with_seq_recovery(st, send, what="test")
        assert len(calls) == 3  # 1 首发 + 2 自愈

    async def test_non_conflict_error_raised_immediately(self):
        """非 300317 异常不重试直接上抛."""
        st = CardTurnState(chat_id="c1")
        st.seq = 1
        calls: list[int] = []

        async def send(seq: int) -> None:
            calls.append(seq)
            raise _FakeAPIError("element limit", code=300305)

        with pytest.raises(_FakeAPIError):
            await call_with_seq_recovery(st, send, what="test")
        assert calls == [1]

    async def test_engine_stream_answer_self_heals(self):
        """engine 答案流撞 300317 → seq+1 重发 → 回合不降级."""
        client = _SeqClient(fail_plan={"stream": 1})
        comp = FrameComposer(
            get_client=lambda: client,
            build_card=lambda **kw: {},
            build_final_card=lambda **kw: {},
        )
        chat = "oc_selfheal"
        await _seed_active(comp, chat, client)
        st = comp._states[chat]
        st.seq = 3
        result = await comp.handle_send(chat, "answer text", None, None, _stream_cls("answer text"))
        assert result is not None and result.success
        stream_calls = [s for op, s in client.calls if op == "stream"]
        assert stream_calls == [4, 5]  # 首发 seq=4 撞 → 自愈重发 seq=5
        assert not st.degraded

    async def test_finalize_self_heals(self):
        """定稿三步撞 300317 → 自愈 → 卡片完成终态."""
        client = _SeqClient(fail_plan={"stream": 1})
        comp = FrameComposer(
            get_client=lambda: client,
            build_card=lambda **kw: {},
            build_final_card=lambda **kw: {},
        )
        chat = "oc_finalheal"
        await _seed_active(comp, chat, client)
        st = comp._states[chat]
        await comp.handle_send(chat, "final", None, None,
                               _progress_cls("x"))  # 占位防 None 分支
        comp._states[chat] = st
        result = await comp._finalize(st, "final answer")
        assert result.success
        assert st.finalized
        assert any(op == "close" for op, _s in client.calls)


# ---------------------------------------------------------------------------
# P0-2：GC 空闲回收（活跃回合长回合不再被误杀）
# ---------------------------------------------------------------------------


class TestGcIdleRecycle:
    def _comp(self) -> FrameComposer:
        comp = FrameComposer(
            get_client=lambda: None, build_card=lambda **kw: {},
            build_final_card=lambda **kw: {})
        return comp

    def test_active_turn_survives_beyond_ttl(self):
        """核心回归：created_at 超 TTL 但 last_activity 新鲜 → 保留.

        旧实现 ``(finalized and idle>60) or (created_at>ttl)`` 会把活跃
        回合在满 TTL 时弹出（长回合断卡根因）。
        """
        comp = self._comp()
        st = CardTurnState(chat_id="c1")
        st.created_at = time.monotonic() - 700  # > 默认 TTL 600
        st.last_activity = time.monotonic() - 1  # 活跃
        comp._states["c1"] = st
        comp._gc()
        assert "c1" in comp._states

    def test_finalized_recycled_after_cooldown(self):
        comp = self._comp()
        st = CardTurnState(chat_id="c1")
        st.finalized = True
        st.last_activity = time.monotonic() - 61
        comp._states["c1"] = st
        comp._by_message["om_x"] = "c1"
        st.card_message_id = "om_x"
        comp._gc()
        assert "c1" not in comp._states
        assert "om_x" not in comp._by_message

    def test_finalized_recent_kept(self):
        comp = self._comp()
        st = CardTurnState(chat_id="c1")
        st.finalized = True
        st.last_activity = time.monotonic() - 10
        comp._states["c1"] = st
        comp._gc()
        assert "c1" in comp._states

    def test_idle_orphan_recycled(self):
        """未定稿但空闲超 TTL（中断孤儿回合）→ 回收."""
        comp = self._comp()
        st = CardTurnState(chat_id="c1")
        st.last_activity = time.monotonic() - 700
        comp._states["c1"] = st
        comp._gc()
        assert "c1" not in comp._states

    async def test_placeholder_renewed_by_staging(self):
        """占位状态在暂存帧到达时续命（last_activity 刷新）→ 不被 GC."""
        comp = FrameComposer(
            get_client=lambda: None, build_card=lambda **kw: {},
            build_final_card=lambda **kw: {})
        # 建卡失败（client None）→ 占位保留（v2.0.5 语义）
        await comp.handle_send("c1", "🖥️ terminal：ls", None, None,
                               _progress_cls("🖥️ terminal：ls"))
        ph = comp._states.get("c1")
        assert ph is not None and ph.card_id == ""
        assert time.monotonic() - ph.last_activity < 1.0  # 暂存帧已续命
        ph.last_activity = time.monotonic() - 700  # 但若持续无帧 → TTL 后回收
        comp._gc()
        assert "c1" not in comp._states


# ---------------------------------------------------------------------------
# P1-3：幽灵卡竞态（final flush 执行时复查）
# ---------------------------------------------------------------------------


class TestFinalFlushGhostCard:
    async def test_final_flush_after_finalize_dropped(self):
        """入队时活跃 → 执行时已终态 → 丢弃（不建卡不占位）."""
        client = _FakeClient()
        comp = FrameComposer(
            get_client=lambda: client,
            build_card=lambda **kw: {},
            build_final_card=lambda **kw: {},
        )
        chat = "oc_ghost"
        st = await _seed_active(comp, chat, client)
        cards_before = len(client.cards)
        ok = comp.submit_progress_from_thread(chat, "💬 迟到的尾思考", final=True)
        assert ok  # 入队时未终态
        st.finalized = True  # 竞态：网关 finalize 先执行
        await _settle()
        assert len(client.cards) == cards_before  # 无幽灵卡
        assert st.status_lines == []  # 未入面板

    async def test_final_flush_without_state_rejected(self):
        """无任何状态（无卡无占位）→ 入队即拒（False）."""
        comp = FrameComposer(
            get_client=lambda: _FakeClient(),
            build_card=lambda **kw: {},
            build_final_card=lambda **kw: {},
        )
        assert comp.submit_progress_from_thread("oc_none", "💬 x", final=True) is False

    async def test_final_flush_joins_active_panel(self):
        """活跃卡片 → 尾思考正常进面板（幽灵卡防线不误伤正常路径）."""
        client = _FakeClient()
        comp = FrameComposer(
            get_client=lambda: client,
            build_card=lambda **kw: {},
            build_final_card=lambda **kw: {},
        )
        chat = "oc_join"
        st = await _seed_active(comp, chat, client)
        assert comp.submit_progress_from_thread(chat, "💬 尾思考", final=True) is True
        await _settle()
        assert any("尾思考" in ln for ln in st.status_lines)

    async def test_final_flush_degraded_dropped(self):
        """降级回合 → final flush 丢弃."""
        client = _FakeClient()
        comp = FrameComposer(
            get_client=lambda: client,
            build_card=lambda **kw: {},
            build_final_card=lambda **kw: {},
        )
        chat = "oc_deg"
        st = await _seed_active(comp, chat, client)
        st.degraded = True
        assert comp.submit_progress_from_thread(chat, "💬 x", final=True) is False

    async def test_nonfinal_flush_finalized_state_rejected_at_enqueue(self):
        """非 final 的流中 flush 对终态状态在入队时拒绝（v2.0.7 既有守卫语义）.

        新回合 delta 的实际路径：prev 终态时入队被拒 → buf["sent"] 不推进
        → 1.5s 节流后下一次 flush 时新卡已建（answer 首帧）→ 正常进面板。
        暂存丢失仅存在于建卡前窗口（v2.0.7 既有边界，不扩大本轮修复面）。
        """
        client = _FakeClient()
        comp = FrameComposer(
            get_client=lambda: client,
            build_card=lambda **kw: {},
            build_final_card=lambda **kw: {},
        )
        chat = "oc_stage"
        st = await _seed_active(comp, chat, client)
        st.finalized = True  # 上一回合终态
        assert comp.submit_progress_from_thread(chat, "💬 新回合思考", final=False) is False


# ---------------------------------------------------------------------------
# P1-4：finalize 幂等守卫
# ---------------------------------------------------------------------------


class TestFinalizeIdempotent:
    async def test_double_finalize_single_footer(self):
        """双 finalize → 页脚 batch_update 只执行一次."""
        client = _SeqClient()
        comp = FrameComposer(
            get_client=lambda: client,
            build_card=lambda **kw: {},
            build_final_card=lambda **kw: {},
        )
        chat = "oc_idem"
        await _seed_active(comp, chat, client)
        st = comp._states[chat]
        first = await comp._finalize(st, "answer")
        second = await comp._finalize(st, "answer")
        assert first.success and second.success
        footer_adds = client.batch_update_calls  # add+del spinner = ≤2 次调用
        assert len(footer_adds) == 2  # 一次 finalize：add_elements + delete_elements
        assert st.finalized

    async def test_finalize_via_handle_edit_idempotent(self):
        """handle_edit(finalize=True) 重试路径同样幂等."""
        client = _SeqClient()
        comp = FrameComposer(
            get_client=lambda: client,
            build_card=lambda **kw: {},
            build_final_card=lambda **kw: {},
        )
        chat = "oc_edit"
        st = await _seed_active(comp, chat, client)
        comp._by_message[st.card_message_id] = chat  # state_by_message 反查注册
        from hermes_lark_streaming.composer.classify import CH_FINAL, ClassifyResult

        cls = ClassifyResult(CH_FINAL, raw_content="x")
        await comp.handle_edit(chat, st.card_message_id, "answer", True, cls)
        await comp.handle_edit(chat, st.card_message_id, "answer", True, cls)
        assert len(client.batch_update_calls) == 2


# ---------------------------------------------------------------------------
# P2-8：工具耗时配对（尾行优先 + 标签迁移 + 跨回合重置）
# ---------------------------------------------------------------------------


class TestToolDurationPairing:
    async def test_latest_unlabeled_row_gets_hook(self):
        """尾数第一个同工具行未挂 → 挂上（跨思考行定位；主路径）.

        正常时序：pending 行随快照先插入 → 工具执行完钩子到达 → 尾行
        （本调用的 pending 行）挂耗时 → preview 快照随后取代（标签迁移）。
        """
        client = _FakeClient()
        comp = FrameComposer(
            get_client=lambda: client,
            build_card=lambda **kw: {},
            build_final_card=lambda **kw: {},
        )
        chat = "oc_pair2"
        st = await _seed_active(comp, chat, client)
        st.status_lines = [
            "🖥️ terminal：echo first-call",
            "💬 thinking text",
            "🖥️ terminal：echo second-call",
        ]
        await comp._feed_tool_duration(chat, "terminal", 2300.0)
        assert st.tool_durations.get("🖥️ terminal：echo second-call") == "2.3 s"
        assert "🖥️ terminal：echo first-call" not in st.tool_durations

    async def test_labeled_tail_row_goes_pending(self):
        """尾行已挂 → 本钩子属更新的调用（行未到）→ pending，不再向前误挂.

        旧实现 continue 跳过已挂尾行向前反查：钩子重复/丢失场景会把耗时
        挂到更早调用的行上。新实现 break→pending 等新行认领。
        """
        client = _FakeClient()
        comp = FrameComposer(
            get_client=lambda: client,
            build_card=lambda **kw: {},
            build_final_card=lambda **kw: {},
        )
        chat = "oc_pair"
        st = await _seed_active(comp, chat, client)
        st.status_lines = [
            "🖥️ terminal：echo first-call",     # 更早调用（未挂，钩子丢失）
            "🖥️ terminal：echo second-call",    # 尾行（已挂）
        ]
        st.tool_durations["🖥️ terminal：echo second-call"] = "1.0 s"
        await comp._feed_tool_duration(chat, "terminal", 2500.0)
        # 不向前误挂 first-call；入 pending 等新行认领
        assert "🖥️ terminal：echo first-call" not in st.tool_durations
        assert st.pending_tool_durations.get("terminal") == ["2.5 s"]

    async def test_pending_to_preview_label_migration(self):
        """pending 行挂耗时 → preview 取代 → 标签迁移到新 key（渲染层可查）."""
        client = _FakeClient()
        comp = FrameComposer(
            get_client=lambda: client,
            build_card=lambda **kw: {},
            build_final_card=lambda **kw: {},
        )
        chat = "oc_mig"
        st = await _seed_active(comp, chat, client)
        pending_line = "🖥️ terminal：uname -r..."
        st.status_lines = [pending_line]
        st.tool_durations[pending_line] = "850 ms"  # 钩子在 pending 期挂上
        # preview 快照到达 → 原地取代（raw_content 必须是快照全文）
        await comp.handle_send(
            chat, "🖥️ terminal：uname -r && echo ok",
            None, None, _progress_cls("🖥️ terminal：uname -r && echo ok"))
        assert pending_line not in st.tool_durations
        assert st.tool_durations.get("🖥️ terminal：uname -r && echo ok") == "850 ms"

    async def test_create_card_resets_durations(self):
        """新回合建卡重置耗时表（同命令重跑不继承旧耗时）."""
        client = _FakeClient()
        comp = FrameComposer(
            get_client=lambda: client,
            build_card=lambda **kw: {},
            build_final_card=lambda **kw: {},
        )
        chat = "oc_reset"
        st = await _seed_active(comp, chat, client)
        st.tool_durations["🖥️ terminal：uname -r"] = "2.3 s"
        st.finalized = True  # 回合 1 结束
        # 回合 2：answer 首帧建卡（复用终态状态对象）
        await comp.handle_send(chat, "answer v2", None, None, _stream_cls("answer v2"))
        st2 = comp._states[chat]
        assert st2.finalized is False
        assert st2.tool_durations == {}


# ---------------------------------------------------------------------------
# P2-9 可观测性：register 内联 setLevel —— 依赖 hermes 环境，由部署探针
# s99 验证（探针断言 register 后 hermes_lark_streaming logger 级别 = INFO）。
# ---------------------------------------------------------------------------
