"""性能五项优化回归测试（OPT-1..5，审计驱动）.

审计来源：性能五项审计（2026-11-09），每项均有生产取证：

- OPT-1 面板更新合并 + 字节预算 + 200860 分类：499s 回合 35 次工具完成
  ≈70 次全量快照上传 ≈1.3MB 上行；34 行状态实测 19.3KB/update，~12KB 答案
  即撞官方 30KB 上限（200860）→ 全通道中途降级。
  修复：(a) 原地改写类事件（思考增长/interim tick/耗时回填）0.2s 尾沿合并
  单发（每 chat 单挂起 timer），新增行保持同步上传（结构性进展即时可见，
  且与存量 e2e「每新增行一次 update」契约兼容——有意的保守切分，见
  engine._queue_panel_flush 注释）；seal/finalize 入口强制冲刷（最后面板
  状态先落卡再封卡，流式正确性红线）；(b) cardkit_update 上传前 27KB 预算
  守卫，超限从最旧面板行裁起（answer 永不裁）；(c) 200860 直报码与
  230099 子码包裹形态均不可重试（feishu/client.py）。
- OPT-2 解析 memo + answer 归一化提升：_patch_status 每次全量渲染对同一批
  行解析 3 次（分组判定/渲染循环/面板标题）→ 模块级 memo 后 1 次；
  _absorb_status_lines 旧实现对每条 incoming 行重复归一化整个 answer 缓冲
  → 每次调用只算一次。
- OPT-3 _chat_locks 驱逐：状态弹出且锁空闲无等待者 → 驱逐锁对象；
  持有中/有等待者/属性不可得一律不驱逐（误驱逐 = 互斥失效，不可逆）。
- OPT-4 finalize 等值跳过：answer 终值与最近一次成功流式上传字节一致 →
  跳过 patch（省一次全量上传）；reasoning 前插改写文本 → 照发。
- OPT-5 流式 markdown 卫生化：optimize_markdown_style + _downgrade_tables(20)
  此前只用于 oneshot 卡（cardkit/special.py）；现应用于流式 answer 与面板
  markdown 视图（单槽身份缓存/逐行精确串缓存，未变快照零重变换；不 chunk）。

测试沿 test_v233_audit_fixes.py 的 fake client 直驱模式。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import hermes_lark_streaming.cardkit.cards as _cards
from hermes_lark_streaming.cardkit.cards import (
    _CARD_BYTE_BUDGET,
    _card_serialized_size,
    _downgrade_tables,
    _enforce_card_byte_budget,
    build_streaming_card_v2,
    md_hygiene_cached,
)
from hermes_lark_streaming.cardkit.elements import ANSWER_ELEMENT_ID
from hermes_lark_streaming.cardkit.md import optimize_markdown_style
from hermes_lark_streaming.composer import engine as _engine
from hermes_lark_streaming.composer.engine import (
    FrameComposer,
    _card_has_panel,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ---------------------------------------------------------------------------
# 测试夹具（对齐 test_v233_audit_fixes.py 的 _BaseFakeClient 模式 + ops 时序）
# ---------------------------------------------------------------------------


class _PerfFakeClient:
    """全链 fake：记录 API 调用时序（ops）与分类计数，供合并/冲刷断言."""

    def __init__(self) -> None:
        self.cards: dict[str, dict] = {}
        self._n = 0
        self.ops: list[str] = []            # API 调用时序（op 名，允许重复）
        self.update_calls = 0
        self.stream_calls = 0
        self.close_calls = 0
        self.stream_contents: list[str] = []  # stream_element content 全录
        self.last_stream_content = ""

    async def cardkit_create(self, card):
        self._n += 1
        cid = str(self._n)
        self.cards[cid] = {"card": card, "seq": 0}
        self.ops.append("cardkit_create")
        return cid

    async def send_card_by_id_to_chat(self, chat_id, card_id):
        self.ops.append("send_card_by_id_to_chat")
        return f"om_{card_id}"

    async def cardkit_stream_element(self, card_id, element_id, content, sequence=0):
        self.ops.append("cardkit_stream_element")
        self.stream_calls += 1
        self.stream_contents.append(content)
        self.last_stream_content = content
        self.cards.setdefault(card_id, {"card": {}, "seq": 0})["seq"] = sequence

    async def cardkit_update(self, card_id, card, sequence=0):
        self.ops.append("cardkit_update")
        self.update_calls += 1
        self.cards.setdefault(card_id, {"card": card, "seq": 0})["card"] = card
        self.cards[card_id]["seq"] = sequence

    async def cardkit_batch_update(self, card_id, actions, sequence=0):
        self.ops.append("cardkit_batch_update")
        self.cards.setdefault(card_id, {"card": {}, "seq": 0})["seq"] = sequence

    async def cardkit_close_streaming(self, card_id, sequence=0, *, summary=""):
        self.ops.append("cardkit_close_streaming")
        self.close_calls += 1
        self.cards.setdefault(card_id, {"card": {}, "seq": 0})["seq"] = sequence

    async def cardkit_reopen_streaming(self, card_id, sequence=0):
        self.ops.append("cardkit_reopen_streaming")
        self.cards[card_id]["seq"] = sequence

    def op_index(self, op: str, occurrence: int = 1) -> int:
        """第 occurrence 次 op 的时序下标（断言「先落卡再封卡」顺序用）."""
        seen = 0
        for i, o in enumerate(self.ops):
            if o == op:
                seen += 1
                if seen == occurrence:
                    return i
        raise AssertionError(f"op {op} #{occurrence} not found in {self.ops}")


def _make_composer(client: _PerfFakeClient) -> FrameComposer:
    return FrameComposer(
        get_client=lambda: client,
        build_card=lambda **kw: build_streaming_card_v2(**kw),
        build_final_card=lambda **kw: {},
        config=None,
        clarify_suspend_sec=0.0,
    )


async def _settle() -> None:
    """让 fire-and-forget 任务（timer 冲刷/孤儿收口）跑完（test_v208 同款）."""
    for _ in range(4):
        await asyncio.sleep(0)
    cur = asyncio.current_task()
    ts = [t for t in asyncio.all_tasks() if t is not cur and not t.done()]
    if ts:
        await asyncio.gather(*ts, return_exceptions=True)


def _answer_content(client: _PerfFakeClient, card_id: str) -> str:
    """卡片上的 answer 当前视图：优先全量快照 JSON；为空回退最近流式元素内容.

    建卡 JSON 的 answer 元素恒为空占位（正文走 stream_element 种子），故空时
    取 stream_contents 尾值。
    """
    state = client.cards.get(card_id) or {}
    for e in (state.get("card") or {}).get("body", {}).get("elements", []):
        if e.get("element_id") == ANSWER_ELEMENT_ID:
            content = str(e.get("content", ""))
            if content:
                return content
            break
    return client.stream_contents[-1] if client.stream_contents else ""


# ---------------------------------------------------------------------------
# OPT-1a：面板更新尾沿合并 + 强制冲刷
# ---------------------------------------------------------------------------


class TestOpt1PanelCoalescing:
    async def test_five_rapid_replace_absorbs_single_update(self):
        """窗口内 5 次原地改写（思考增长）→ 恰好 1 次 cardkit_update（尾沿单发）.

        生产画像：reasoning flush/interim tick 每几百 ms 重发增长行——旧行为
        每次事件一次全量上传，合并后窗口内单发。新增行（工具步骤）不合并，
        保持同步上传（见 _queue_panel_flush 注释的保守切分）。
        """
        client = _PerfFakeClient()
        comp = _make_composer(client)
        chat = "oc_opt1_burst"
        await comp.handle_stream_frame(chat, "seed 正文", finalize=False)
        st = comp._states[chat]
        # 新增行 → urgent 同步（工具行 + 首个思考行，各一次 update）
        await comp._absorb_status_lines(st, ['🔧 terminal："date"'])
        await comp._absorb_status_lines(st, ["💬 开始推理"])
        base = client.update_calls
        assert base == 2, base
        # 5 次连续原地改写（思考行累积增长——merge 原地取代，行数不变）
        for i in range(5):
            await comp._absorb_status_lines(
                st, ["💬 " + "开始推理" + " 续" * (i + 1)])
            assert client.update_calls == base, (
                f"第 {i + 1} 次原地改写不得即时上传（应进尾沿合并窗口）")
            assert st.panel_dirty is True
        assert len(comp._panel_flush_timers) == 1, "每 chat 至多一个挂起合并 timer"
        await asyncio.sleep(0.25)  # 越过 0.2s 合并窗口
        assert client.update_calls == base + 1, (
            "窗口内 5 次原地改写必须合并为恰好 1 次 cardkit_update")
        assert st.panel_dirty is False, "冲刷后脏旗标必须清除"
        # 合并冲刷发的是最新状态（增长后的思考行已落卡）
        card = client.cards[st.card_id]["card"]
        rendered = str(card)
        assert "续 续 续 续 续" in rendered, "尾沿冲刷必须携带最新面板状态"

    async def test_finalize_forces_pending_flush_before_seal(self):
        """finalize 入口强制冲刷挂起面板状态：先落卡、再定稿 API（时序断言）."""
        client = _PerfFakeClient()
        comp = _make_composer(client)
        chat = "oc_opt1_force"
        await comp.handle_stream_frame(chat, "答案", finalize=False)
        st = comp._states[chat]
        await comp._absorb_status_lines(st, ['🔧 terminal："date"'])  # urgent 同步
        await comp._absorb_status_lines(st, ["💬 挂起推理起点"])  # 新增行 → 同步
        await comp._absorb_status_lines(st, ["💬 " + "挂起推理起点" + " 续" * 10])  # 原地改写 → 挂起
        assert client.update_calls == 2 and st.panel_dirty is True
        await comp.handle_stream_frame(chat, "答案定稿", finalize=True)
        assert client.update_calls == 3, "finalize 入口必须强制冲刷挂起面板状态"
        assert st.panel_dirty is False
        # 时序：强制冲刷的 update 必须先于 close_streaming（最后状态先落卡）
        forced_update = client.op_index("cardkit_update", 3)
        close = client.op_index("cardkit_close_streaming")
        assert forced_update < close, client.ops
        assert st.finalized is True

    async def test_watchdog_seal_flushes_pending_panel(self):
        """看门狗 seal 前强制冲刷：挂起的思考行必须先于 finalize_card 落卡."""
        client = _PerfFakeClient()
        comp = _make_composer(client)
        chat = "oc_opt1_watchdog"
        await comp.handle_stream_frame(chat, "孤儿回答", finalize=False)
        st = comp._states[chat]
        await comp._absorb_status_lines(st, ['🔧 terminal："date"'])
        await comp._absorb_status_lines(st, ["💬 挂起推理起点"])
        await comp._absorb_status_lines(st, ["💬 " + "挂起推理起点" + " 续" * 10])
        assert client.update_calls == 2 and st.panel_dirty is True
        st.last_activity -= (_engine._WATCHDOG_IDLE_SEC + 1)
        await comp._watchdog_seal(chat)
        assert client.update_calls == 3, "seal 前必须强制冲刷挂起面板状态"
        assert st.panel_dirty is False
        forced_update = client.op_index("cardkit_update", 3)
        close = client.op_index("cardkit_close_streaming")
        assert forced_update < close, client.ops

    async def test_orphan_gc_seal_flushes_detached_state(self):
        """GC 孤儿收口：状态已脱离注册表，挂起面板状态仍须先落卡再收口."""
        client = _PerfFakeClient()
        comp = _make_composer(client)
        chat = "oc_opt1_orphan"
        await comp.handle_stream_frame(chat, "正文", finalize=False)
        st = comp._states[chat]
        await comp._absorb_status_lines(st, ['🔧 terminal："date"'])
        await comp._absorb_status_lines(st, ["💬 挂起推理起点"])
        await comp._absorb_status_lines(st, ["💬 " + "挂起推理起点" + " 续" * 10])
        assert client.update_calls == 2 and st.panel_dirty is True
        st.last_activity -= 700.0  # 越过 card_ttl（默认 600s，config=None）
        comp._gc()
        assert chat not in comp._states, "空闲超 TTL 的未定稿状态必须被回收"
        await _settle()  # 孤儿 seal 为 fire-and-forget 任务
        assert client.update_calls == 3, "脱管状态的挂起面板必须经 seal 强制冲刷落卡"
        assert st.panel_dirty is False
        card = client.cards[st.card_id]["card"]
        assert _card_has_panel(card), "收口卡必须带面板（挂起行已落卡）"

    async def test_clear_and_forget_cancel_pending_timer(self):
        """forget/clear 清理挂起合并 timer（无主 timer 不得残留到下一回合）."""
        client = _PerfFakeClient()
        comp = _make_composer(client)
        chat = "oc_opt1_cancel"
        await comp.handle_stream_frame(chat, "正文", finalize=False)
        st = comp._states[chat]
        await comp._absorb_status_lines(st, ['🔧 terminal："date"'])
        await comp._absorb_status_lines(st, ["💬 推理起点"])
        await comp._absorb_status_lines(st, ["💬 " + "推理起点" + " 续" * 10])
        assert chat in comp._panel_flush_timers
        comp.forget(chat)
        assert chat not in comp._panel_flush_timers, "forget 必须取消合并 timer"


# ---------------------------------------------------------------------------
# OPT-1b：30KB 字节预算守卫
# ---------------------------------------------------------------------------


class TestOpt1ByteBudget:
    async def test_oversized_panel_trimmed_under_budget(self):
        """超大面板上传前裁到预算内：最旧行先裁、hint 计数、answer 永不裁.

        生产实证：34 行状态 19.3KB/update，~12KB 答案即撞官方 30KB（200860）
        → 全通道降级。裁剪只作用于上传视图，st.status_lines 恒存全量。
        """
        client = _PerfFakeClient()
        comp = _make_composer(client)
        chat = "oc_opt1_budget"
        await comp.handle_stream_frame(chat, "正文红线，不得被裁", finalize=False)
        st = comp._states[chat]
        big = "x" * 1800
        lines = [f'🔧 tool{i}："{big}"' for i in range(20)]
        await comp._absorb_status_lines(st, lines)  # 新增行 → urgent 同步上传
        assert client.update_calls == 1
        card = client.cards[st.card_id]["card"]
        size = _card_serialized_size(card)
        assert size <= _CARD_BYTE_BUDGET + 500, (
            f"上传视图必须裁到预算内：{size} > {_CARD_BYTE_BUDGET}")
        # hint 元素记录裁剪数（沿用 trim hint 机制，UX-1a 起诚实双语措辞）
        panel = next(
            e for e in card["body"]["elements"]
            if e.get("tag") == "collapsible_panel")
        hints = [ch for ch in panel["elements"]
                 if isinstance(ch.get("content"), str) and "earlier steps omitted" in ch["content"]]
        assert hints, "裁剪必须插入省略提示行"
        assert "⚡ 0 " not in hints[0]["content"], hints[0]["content"]
        assert "项已省略" in hints[0]["i18n_content"]["zh_cn"]
        # answer 元素原样保留（正文永不裁）——种子正文经 stream_element 落卡
        assert client.stream_contents[-1] == "正文红线，不得被裁"
        # 状态缓冲恒存全量（过程记录不丢——裁剪只作用于上传视图）
        assert len(st.status_lines) == 20
        assert st.panel_on_card is True

    def test_budget_helper_noop_under_budget_and_without_panel(self):
        """预算内/无面板（纯 answer 超限）→ 原样返回（answer 永不裁）."""
        card = build_streaming_card_v2(answer_text="x" * 40_000)
        out = _enforce_card_byte_budget(card)
        assert out is card, "无面板可裁时必须原样返回（200860 由 client 分类承接）"
        small = build_streaming_card_v2(status_lines=['🔧 t："x"'])
        assert _enforce_card_byte_budget(small) is small


# ---------------------------------------------------------------------------
# OPT-1c：200860 不可重试分类
# ---------------------------------------------------------------------------


class TestOpt1CardSizeErrorClassified:
    async def test_200860_never_retried(self):
        """200860 确定性拒收：_retry_transient 首撞即上抛（不多打一次 API）."""
        from hermes_lark_streaming.feishu.client import (
            CARDKIT_CARD_SIZE,
            FeishuAPIError,
            FeishuClient,
            FeishuClientConfig,
            _is_transient_error,
            is_card_size_error,
        )

        err = FeishuAPIError("card size over limit (200860)", code=CARDKIT_CARD_SIZE)
        assert _is_transient_error(err) is False
        assert is_card_size_error(err) is True

        client = FeishuClient(FeishuClientConfig(app_id="cli_test", app_secret="x"))
        attempts = {"n": 0}

        async def _boom():
            attempts["n"] += 1
            raise FeishuAPIError("card size over limit", code=CARDKIT_CARD_SIZE)

        with pytest.raises(FeishuAPIError):
            await client._retry_transient("cardkit_update", _boom)
        assert attempts["n"] == 1, "体积超限不得重试（3× 白撞 + 拖慢降级红线）"

    def test_200860_wrapped_in_230099_also_non_retryable(self):
        """230099 通用码包裹形态（ErrCode: 200860）同样不可重试（旧实现会误判）."""
        from hermes_lark_streaming.feishu.client import (
            CARDKIT_CONTENT_FAILED,
            FeishuAPIError,
            _is_transient_error,
        )

        wrapped = FeishuAPIError(
            "cardkit_update failed: ErrCode: 200860 card over size limit",
            code=CARDKIT_CONTENT_FAILED,
        )
        assert wrapped.extract_sub_code() == 200860
        assert _is_transient_error(wrapped) is False


# ---------------------------------------------------------------------------
# OPT-2：解析 memo + answer 归一化提升
# ---------------------------------------------------------------------------


class TestOpt2ParseMemoAndNormHoist:
    async def test_second_patch_reuses_parse_results(self, monkeypatch):
        """行未变时第二次 _patch_status 零重解析（memo 命中）.

        旧实现每次全量渲染对同一批行解析 3 次（_last_thinking_group/渲染循环/
        _panel_title）；memo 后每行至多解析 1 次。
        """
        client = _PerfFakeClient()
        comp = _make_composer(client)
        chat = "oc_opt2_memo"
        await comp.handle_stream_frame(chat, "正文", finalize=False)
        st = comp._states[chat]
        line_a = '🔧 terminal："date"'
        line_b = "💬 " + "推理内容 " * 12
        await comp._absorb_status_lines(st, [line_a, line_b])  # urgent，顺带暖 memo
        calls = {"n": 0}
        real = _cards.parse_progress_line

        def _counting(text):
            calls["n"] += 1
            return real(text)

        monkeypatch.setattr(_cards, "parse_progress_line", _counting)
        _cards._PARSE_MEMO.clear()  # 清 memo：让首帧解析真实发生
        await comp._patch_status(st)
        first = calls["n"]
        assert first >= 2, f"memo 清空后首帧应实际解析（got {first}）"
        await comp._patch_status(st)  # 行未变
        assert calls["n"] == first, (
            f"第二次 _patch_status 必须复用解析结果（重解析 {calls['n'] - first} 次）")

    async def test_answer_normalized_once_per_absorb(self, monkeypatch):
        """_absorb_status_lines 每次调用只归一化一次 answer（旧：每行一次）."""
        client = _PerfFakeClient()
        comp = _make_composer(client)
        chat = "oc_opt2_norm"
        await comp.handle_stream_frame(chat, "正文", finalize=False)
        st = comp._states[chat]
        legacy_calls = {"n": 0}
        real = _engine._is_answer_duplicate

        def _spy(line, answer_text, *, answer_norm=None):
            if answer_norm is None:
                legacy_calls["n"] += 1  # 未传预归一化结果 = 旧式调用
            return real(line, answer_text, answer_norm=answer_norm)

        monkeypatch.setattr(_engine, "_is_answer_duplicate", _spy)
        await comp._absorb_status_lines(
            st, ['🔧 a："x"', '🔧 b："y"', '🔧 c："z"'])
        assert legacy_calls["n"] == 0, "absorb 路径必须传预归一化 answer（每调用一次）"

    def test_is_answer_duplicate_default_path_unchanged(self):
        """缺省（不传 answer_norm）就地归一化——直调/存量调用行为逐字等价."""
        answer = "这是一段足够长的已流答案文本，用于触发前缀匹配判定逻辑。" * 3
        dup = "💬 " + answer[:80]
        assert _engine._is_answer_duplicate(dup, answer) is True
        assert _engine._is_answer_duplicate(dup, answer, answer_norm=" ".join(answer.split())) is True
        assert _engine._is_answer_duplicate("💬 完全无关的思考内容" * 5, answer) is False


# ---------------------------------------------------------------------------
# OPT-3：_chat_locks 驱逐
# ---------------------------------------------------------------------------


class TestOpt3ChatLockEviction:
    async def test_gc_evicts_idle_lock_held_lock_survives(self):
        """已弹出且空闲的锁被 gc 驱逐；持有中的锁绝不驱逐."""
        client = _PerfFakeClient()
        comp = _make_composer(client)
        chat = "oc_opt3_idle"
        await comp.handle_stream_frame(chat, "正文", finalize=False)
        await comp.handle_stream_frame(chat, "定稿", finalize=True)
        assert chat in comp._chat_locks
        st = comp._states[chat]
        st.last_activity -= 10_000  # 越过已定稿 60s 冷却
        comp._gc()
        assert chat not in comp._states
        assert chat not in comp._chat_locks, "状态已弹出且锁空闲 → 必须驱逐"

        chat2 = "oc_opt3_held"
        await comp.handle_stream_frame(chat2, "正文", finalize=False)
        await comp.handle_stream_frame(chat2, "定稿", finalize=True)
        lock2 = comp._chat_lock(chat2)
        await lock2.acquire()
        try:
            st2 = comp._states[chat2]
            st2.last_activity -= 10_000
            comp._gc()
            assert chat2 in comp._chat_locks, "持有中的锁绝不驱逐（互斥语义）"
        finally:
            lock2.release()
        comp._evict_chat_lock_if_idle(chat2)
        assert chat2 not in comp._chat_locks, "释放后可驱逐"

    async def test_forget_evicts_lock(self):
        """forget（用户删卡路径，不持 chat 锁）同样驱逐空闲锁."""
        client = _PerfFakeClient()
        comp = _make_composer(client)
        chat = "oc_opt3_forget"
        await comp.handle_stream_frame(chat, "正文", finalize=False)
        assert chat in comp._chat_locks
        comp.forget(chat)
        assert chat not in comp._states
        assert chat not in comp._chat_locks, "forget 必须驱逐空闲锁"

    async def test_locked_lock_never_evicted_by_forget(self):
        """forget 对持有中的锁不驱逐（在途持有者与后来者共用同一把锁）."""
        client = _PerfFakeClient()
        comp = _make_composer(client)
        chat = "oc_opt3_forget_held"
        await comp.handle_stream_frame(chat, "正文", finalize=False)
        lock = comp._chat_lock(chat)
        await lock.acquire()
        try:
            comp.forget(chat)
            assert chat in comp._chat_locks, "持有中不得驱逐"
        finally:
            lock.release()
        comp._evict_chat_lock_if_idle(chat)
        assert chat not in comp._chat_locks


# ---------------------------------------------------------------------------
# OPT-4：finalize 等值跳过 answer 终值 patch
# ---------------------------------------------------------------------------


class TestOpt4FinalizeSkipIdenticalAnswer:
    async def test_finalize_after_identical_stream_skips_patch(self):
        """流式已把同内容落卡 → finalize 零 stream_element（省一次全量上传）."""
        client = _PerfFakeClient()
        comp = _make_composer(client)
        chat = "oc_opt4_skip"
        await comp.handle_stream_frame(chat, "答案正文", finalize=False)
        st = comp._states[chat]
        assert client.stream_calls == 1  # 建卡种子
        assert st.last_streamed_answer == "答案正文"
        await comp.handle_stream_frame(chat, "答案正文", finalize=True)
        assert client.stream_calls == 1, "终值与基线字节一致 → 不得重发 stream_element"
        assert client.close_calls == 1, "关流式照常"
        assert st.finalized is True

    async def test_finalize_with_changed_text_still_patches(self):
        """终值与基线不同（正常流式增长后定稿）→ patch 照发（不误伤）."""
        client = _PerfFakeClient()
        comp = _make_composer(client)
        chat = "oc_opt4_patch"
        await comp.handle_stream_frame(chat, "初稿", finalize=False)
        await comp.handle_stream_frame(chat, "初稿\n\n定稿全文", finalize=True)
        assert client.stream_calls == 2, "终值与基线不等 → 必须补终值 patch"
        assert client.stream_contents[-1] == "初稿\n\n定稿全文"
        assert client.close_calls == 1

    async def test_reasoning_prepend_invalidates_baseline(self):
        """reasoning 前插改写 answer_text → 与基线不等 → patch 照发."""
        client = _PerfFakeClient()
        comp = _make_composer(client)
        chat = "oc_opt4_reasoning"
        await comp.handle_stream_frame(chat, "正文", finalize=False)
        fin = "💭 **推理：**\n```\n内部推理内容\n```\n\n正文终稿"
        await comp.handle_stream_frame(chat, fin, finalize=True)
        # 剥出的 clean answer（"正文终稿"）≠ 基线（"正文"）→ patch 照发
        assert client.stream_calls == 2
        assert client.stream_contents[-1] == "正文终稿"
        # 推理行并入面板（v2.0.9 P1-4 语义不变）
        assert any("内部推理内容" in ln for ln in comp._states[chat].status_lines)

    async def test_never_streamed_answer_still_patched(self):
        """answer 从未流过（基线空）→ finalize 照常 patch（直调 state 场景）."""
        client = _PerfFakeClient()
        comp = _make_composer(client)
        await comp.handle_stream_frame("oc_opt4_never", "", finalize=False)  # 种子无 answer
        st = comp._states["oc_opt4_never"]
        st.answer_text = "只在状态里的终值"
        await comp._finalize(st, "只在状态里的终值")
        assert client.stream_calls == 1, "从未流过的 answer 必须照常补终值"
        assert client.close_calls == 1

    async def test_finalize_card_duck_typed_state_patch_semantics(self):
        """直调 finalize_card：无 last_streamed_answer 字段 → patch；等值 → 跳过."""
        from hermes_lark_streaming.composer.finalize import finalize_card

        client = _PerfFakeClient()

        def _state(answer, streamed=None):
            return SimpleNamespace(
                card_id="c1", seq=0, status_lines=[], tool_durations={},
                line_times=[], answer_text=answer, created_at=0.0,
                last_streamed_answer=streamed,
            )

        await finalize_card(client, _state("终值A"), answer_element_id="answer")
        assert client.stream_calls == 1, "基线缺失（从未流过）→ patch 照发"

        client2 = _PerfFakeClient()
        await finalize_card(client2, _state("终值B", streamed="终值B"), answer_element_id="answer")
        assert client2.stream_calls == 0, "终值与基线字节一致 → 跳过"
        assert client2.close_calls == 1


# ---------------------------------------------------------------------------
# OPT-5：流式 markdown 卫生化
# ---------------------------------------------------------------------------


def _tables(n: int) -> str:
    rows = []
    for i in range(n):
        rows.append(f"| 列A{i} | 列B{i} |\n|---|---|\n| a{i} | b{i} |")
    return "\n\n".join(rows)


class TestOpt5StreamingMarkdownHygiene:
    async def test_answer_with_over_limit_tables_downgraded_like_oneshot(self):
        """>20 张表的流式 answer 与 oneshot 卡同款降级（第 21 张起转代码块）."""
        client = _PerfFakeClient()
        comp = _make_composer(client)
        chat = "oc_opt5_tables"
        answer = _tables(25)
        await comp.handle_stream_frame(chat, answer, finalize=False)
        st = comp._states[chat]
        assert st.answer_text == answer, "状态缓冲恒存原文"
        got = _answer_content(client, st.card_id)
        expected = _downgrade_tables(optimize_markdown_style(answer), limit=20)
        assert got == expected, "流式 answer 必须与 oneshot 卡同一变换视图"
        assert got != answer, "25 表必须实际触发降级（否则断言无意义）"
        assert got.count("```") >= 10, "超限表格应被包进代码块"

    async def test_blank_line_runs_compressed_on_card_only(self):
        """连续空行在卡片视图压缩为单空行；状态缓冲保持原文（oneshot 同款）."""
        client = _PerfFakeClient()
        comp = _make_composer(client)
        chat = "oc_opt5_blank"
        answer = "段落一\n\n\n\n\n段落二"
        await comp.handle_stream_frame(chat, answer, finalize=False)
        assert comp._states[chat].answer_text == answer
        assert _answer_content(client, comp._states[chat].card_id) == "段落一\n\n段落二"

    async def test_panel_markdown_row_hygiene(self):
        """面板 markdown 兜底行同样卫生化（连续空行压缩），状态行保持原文."""
        client = _PerfFakeClient()
        comp = _make_composer(client)
        chat = "oc_opt5_panel"
        await comp.handle_stream_frame(chat, "正文", finalize=False)
        st = comp._states[chat]
        raw = "普通说明行\n\n\n\n续行内容"
        await comp._absorb_status_lines(st, [raw])  # 新增行 → urgent 同步
        assert st.status_lines == [raw], "状态缓冲恒存原文"
        card = client.cards[st.card_id]["card"]
        panel = next(
            e for e in card["body"]["elements"] if e.get("tag") == "collapsible_panel")
        md_rows = [ch["content"] for ch in panel["elements"] if ch.get("tag") == "markdown"]
        assert any(c == "普通说明行\n\n续行内容" for c in md_rows), md_rows

    async def test_transform_runs_once_for_unchanged_answer(self, monkeypatch):
        """未变 answer 的面板-only 帧零重变换（单槽身份缓存命中）."""
        client = _PerfFakeClient()
        comp = _make_composer(client)
        chat = "oc_opt5_cache"
        long_answer = "很长的正文内容，" * 60  # >100 字符，绕过 optimize 快速通道
        await comp.handle_stream_frame(chat, long_answer, finalize=False)
        st = comp._states[chat]
        calls = {"n": 0}
        real = optimize_markdown_style

        def _counting(text):
            calls["n"] += 1
            return real(text)

        monkeypatch.setattr(_cards, "optimize_markdown_style", _counting)
        st.answer_md_cache.clear()  # 清缓存：让首次变换真实发生
        await comp._patch_status(st)
        first = calls["n"]
        assert first >= 1, "清缓存后首次 _patch_status 应实际变换 answer"
        await comp._patch_status(st)  # answer 未变（同一 str 对象）
        assert calls["n"] == first, (
            f"answer 未变 → 不得重变换（多算 {calls['n'] - first} 次）")
        # 缓存键=精确串（==）而非对象身份（is）：native 帧路径每帧把
        # st.answer_text 重绑为内容相等的新 str 对象（handle_stream_frame
        # st.answer_text = answer，JSON 解码产物），缓存必须仍然命中——
        # 否则 30KB answer 的正则变换在 interim tick 上逐帧重复。
        st.answer_text = (st.answer_text + "x")[:-1]  # 内容相等的新对象
        await comp._patch_status(st)
        assert calls["n"] == first, "内容相等的新 str 对象必须命中精确串缓存"

    async def test_unchanged_answer_no_retransform_across_native_frames(self, monkeypatch):
        """native 复合帧端到端：answer 未变（新 str 对象）→ 零重变换.

        handle_stream_frame 每帧 ``st.answer_text = answer`` 重绑新对象——
        精确串（==）键缓存下 interim tick 的 answer 变换只发生一次。
        """
        client = _PerfFakeClient()
        comp = _make_composer(client)
        chat = "oc_opt5_native"
        answer = "很长的正文内容，" * 60  # >100 字符，绕过 optimize 快速通道
        await comp.handle_stream_frame(chat, answer, finalize=False)
        calls = {"n": 0}
        real = optimize_markdown_style

        def _counting(text):
            if text == answer:  # 只计 answer 变换（面板行变换另行计数，不混口径）
                calls["n"] += 1
            return real(text)

        monkeypatch.setattr(_cards, "optimize_markdown_style", _counting)
        st = comp._states[chat]
        st.answer_md_cache.clear()  # 清缓存：让首帧变换真实发生
        await comp.handle_stream_frame(
            chat, f'{answer}\n\n---\n🔧 terminal："date"', finalize=False)
        first = calls["n"]
        assert first >= 1, "清缓存后首个复合帧应实际变换 answer"
        # 第二帧：answer 内容不变（新 str 对象）+ 新工具行 → answer 零重变换
        await comp.handle_stream_frame(
            chat, f'{answer}\n\n---\n🔧 terminal："uname"', finalize=False)
        assert calls["n"] == first, (
            f"answer 未变的 native 帧 → 不得重变换（多算 {calls['n'] - first} 次）")

    def test_md_hygiene_cached_identity_and_none_cache(self):
        """单槽身份缓存直测：同对象命中、新对象重算、cache=None 直算."""
        cache: dict = {}
        a = "标题\n\n正文" * 30  # 绕过 <100 字符快速通道
        out1 = md_hygiene_cached(a, cache)
        out2 = md_hygiene_cached(a, cache)
        assert out1 is out2, "同对象必须直接命中缓存（不重算）"
        b = a + "增长"
        out3 = md_hygiene_cached(b, cache)
        assert out3 == _downgrade_tables(optimize_markdown_style(b), limit=20)
        assert md_hygiene_cached(a, None) == out1, "无缓存直算结果一致"
        assert md_hygiene_cached("", cache) == ""


# ---------------------------------------------------------------------------
# 共存回归：coalescing 不破坏 v2.0.24 懒渲染与新增行即时语义
# ---------------------------------------------------------------------------


class TestCoalescingCompat:
    async def test_first_status_line_still_inserts_panel_immediately(self):
        """v2.0.24 契约：首个状态行（新增行）同步上传补插面板，不经合并窗口."""
        client = _PerfFakeClient()
        comp = _make_composer(client)
        chat = "oc_opt_compat"
        await comp.handle_stream_frame(chat, "开始回答", finalize=False)
        st = comp._states[chat]
        assert st.panel_on_card is False
        await comp.handle_stream_frame(chat, "开始回答\n\n---\n🔍 Searching...", finalize=False)
        assert st.panel_on_card is True
        assert client.update_calls == 1, "首个面板行必须即时补插（不经 0.2s 窗口）"
        assert st.panel_dirty is False

    async def test_duration_attach_goes_through_coalescing(self):
        """耗时回填（原地改写）进合并窗口；timer 冲刷后耗时落卡."""
        client = _PerfFakeClient()
        comp = _make_composer(client)
        chat = "oc_opt_duration"
        await comp.handle_stream_frame(chat, "正文", finalize=False)
        st = comp._states[chat]
        await comp._absorb_status_lines(st, ['🖥️ terminal："uname -r"'])
        base = client.update_calls
        await comp._feed_tool_duration(chat, "terminal", 2300.0)
        assert st.tool_durations.get('🖥️ terminal："uname -r"') == "2.3 s"
        assert client.update_calls == base, "耗时回填不得即时上传（尾沿合并）"
        await asyncio.sleep(0.25)
        assert client.update_calls == base + 1, "合并窗口到期必须单发冲刷"
        card = client.cards[st.card_id]["card"]
        assert "2.3 s" in str(card), "冲刷必须携带最新耗时标签"
