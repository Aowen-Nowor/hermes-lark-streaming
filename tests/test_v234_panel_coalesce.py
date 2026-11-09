"""OPT-1 面板合并护栏回归测试（尾沿合并 + 30KB 字节预算 + 200860 分类）.

审计来源：性能五项审计 OPT-1（2026-11-09），生产取证逐条对齐：

- 吞吐证据：499s 回合 35 次工具完成 ≈70 次全量快照上传 ≈1.3MB 上行。思考
  增长/interim tick/耗时回填均为「行数不变的原地改写」→ 0.2s 尾沿合并单发
  （每 chat 单挂起 timer，_queue_panel_flush）；新增行（结构性进展）保持
  同步上传，首块面板补插即时（v2.0.24 懒渲染，test_v224 契约）。
- 体积证据：官方单卡 30KB 上限（200860），34 行状态实测 19.3KB/update +
  ~12KB 答案即撞线 → 回合中段整通道降级。_patch_status 上传前过
  cards._enforce_card_byte_budget（27KB 安全线，最旧面板行先裁、trim hint
  「⚡ N earlier steps omitted / 前 N 项已省略」计数累加，answer 永不裁）。
- 200860 证据：确定性拒收——旧分类缺席时 _retry_transient 白撞 3 次
  （0.1/0.3/0.6s）拖慢降级红线 ~1s。client 侧显式不可重试（含 230099
  通用码的 ErrCode: 200860 子码包裹形态）。

测试沿 test_v233_audit_fixes.py 的 fake client 直驱模式（call_log 保序，
供「先冲刷面板再封卡」顺序断言）；timer 兑现用运行中 loop 轮询等待。
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from hermes_lark_streaming.cardkit.cards import (
    _CARD_BYTE_BUDGET,
    _card_serialized_size,
    build_streaming_card_v2,
)
from hermes_lark_streaming.cardkit.elements import (
    ANSWER_ELEMENT_ID,
    UNIFIED_PANEL_ELEMENT_ID,
)
from hermes_lark_streaming.composer.classify import CH_PROGRESS, ClassifyResult
from hermes_lark_streaming.composer.engine import (
    CardTurnState,
    FrameComposer,
)
from hermes_lark_streaming.feishu.client import (
    FeishuAPIError,
    FeishuClient,
    FeishuClientConfig,
    _is_transient_error,
    is_card_size_error,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_SIZE_200860 = 200860  # 官方单卡 30KB 上限直报码（feishu/client.CARDKIT_CARD_SIZE）


# ---------------------------------------------------------------------------
# 夹具（test_v233_audit_fixes._BaseFakeClient 直驱模式 + 保序 call_log）
# ---------------------------------------------------------------------------


class _PanelClient:
    """全链 fake：API 调用按序记入 call_log；cardkit_update 可编程失败.

    update_attempts 记**尝试次数**（含失败）——200860 fail-fast 断言口径；
    update_fail_from=N 表示第 N 次起 cardkit_update 抛 200860（前 N-1 次
    成功），模拟生产「面板成功落卡后答案增长撞 30KB」的时序。
    """

    def __init__(self, *, update_fail_from: int | None = None) -> None:
        self.cards: dict[str, dict] = {}
        self._n = 0
        self.call_log: list[str] = []
        self.update_attempts = 0
        self.update_fail_from = update_fail_from

    async def cardkit_create(self, card):
        self._n += 1
        cid = str(self._n)
        self.cards[cid] = {"card": card, "seq": 0}
        self.call_log.append("cardkit_create")
        return cid

    async def send_card_by_id_to_chat(self, chat_id, card_id):
        self.call_log.append("send_card_by_id_to_chat")
        return f"om_{card_id}"

    async def cardkit_stream_element(self, card_id, element_id, content, sequence=0):
        self.cards[card_id]["seq"] = sequence
        self.call_log.append("cardkit_stream_element")

    async def cardkit_update(self, card_id, card, sequence=0):
        self.update_attempts += 1
        self.call_log.append("cardkit_update")
        if self.update_fail_from is not None and self.update_attempts >= self.update_fail_from:
            raise FeishuAPIError(
                "cardkit_update: code=200860, msg=cards exceed max size limit", _SIZE_200860)
        self.cards[card_id]["card"] = card
        self.cards[card_id]["seq"] = sequence

    async def cardkit_batch_update(self, card_id, actions, sequence=0):
        self.cards[card_id]["seq"] = sequence
        self.call_log.append("cardkit_batch_update")

    async def cardkit_close_streaming(self, card_id, sequence=0, *, summary=""):
        self.cards[card_id]["seq"] = sequence
        self.call_log.append("cardkit_close_streaming")

    async def cardkit_reopen_streaming(self, card_id, sequence=0):
        self.cards[card_id]["seq"] = sequence
        self.call_log.append("cardkit_reopen_streaming")


def _make_composer(client: _PanelClient, *, suspend_sec: float = 0.0) -> FrameComposer:
    return FrameComposer(
        get_client=lambda: client,
        build_card=lambda **kw: build_streaming_card_v2(**kw),
        build_final_card=lambda **kw: {},
        config=None,
        clarify_suspend_sec=suspend_sec,
    )


def _progress_cls(text: str) -> ClassifyResult:
    """进度帧分类（_feed_progress 同款构造；面板合并语义在引擎侧重解析）."""
    return ClassifyResult(CH_PROGRESS, raw_content=text)


async def _seed_card(comp: FrameComposer, client: _PanelClient, chat: str) -> CardTurnState:
    """建一张纯 answer 卡（无面板：panel_on_card=False，v2.0.24 懒渲染起点）."""
    await comp.handle_stream_frame(chat, "首帧正文", finalize=False)
    st = comp._states[chat]
    assert st.panel_on_card is False
    assert client.update_attempts == 0
    return st


async def _absorb(comp: FrameComposer, chat: str, line: str) -> None:
    """驱动一次进度帧 absorb（handle_send → _on_progress → _absorb_status_lines）."""
    await comp.handle_send(chat, line, None, None, _progress_cls(line))


async def _wait_until(cond, *, timeout: float = 2.0) -> bool:
    """运行中 loop 轮询等待（timer 兑现用；比裸 sleep 更抗 CI 抖动）."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        await asyncio.sleep(0.01)
    return cond()


def _panel_children(card: dict) -> list[dict]:
    for el in card["body"]["elements"]:
        if el.get("element_id") == UNIFIED_PANEL_ELEMENT_ID:
            return el.get("elements", [])
    return []


# ---------------------------------------------------------------------------
# (a) 尾沿合并：0.2s 窗口内的原地改写突发 → 恰好 1 次 cardkit_update
# ---------------------------------------------------------------------------


class TestBurstCoalesce:
    async def test_five_rapid_absorbs_single_update(self):
        """5 次快速 absorb（思考行增长原地改写）合并为 1 次上传.

        生产画像：499s 回合 ≈70 次全量快照 ≈1.3MB 上行——interim tick/
        思考增长都是行数不变改写，0.2s 尾沿窗口内必须只发一次
        cardkit_update（每 chat 单挂起 timer，重复调度是 no-op）。
        """
        client = _PanelClient()
        comp = _make_composer(client)
        chat = "oc_coalesce_burst"
        await _seed_card(comp, client, chat)

        # 首块面板（首条思考行）：懒渲染补插必须即时（update #1，测试 2 契约）
        await _absorb(comp, chat, "💬 第一轮起点")
        assert client.update_attempts == 1
        st = comp._states[chat]
        assert st.panel_on_card is True

        # 窗口内 5 次原地改写（同轮思考行增长 → merged 行数不变 → 不 urgent）
        for i in range(1, 6):
            await _absorb(comp, chat, f"💬 第一轮起点{'·' * i}")
        assert client.update_attempts == 1, "窗口内的原地改写不得即时上传"
        assert chat in comp._panel_flush_timers, "应恰好挂起一个合并 timer"
        assert st.panel_dirty is True

        # 等 timer 兑现（0.2s 窗口 + 轮询裕量）：5 次 absorb → 恰好 1 次上传
        assert await _wait_until(lambda: client.update_attempts >= 2)
        assert client.update_attempts == 2, "突发合并后必须恰好单发一次"
        assert chat not in comp._panel_flush_timers, "冲刷后挂起 timer 必须清位"
        assert st.panel_dirty is False
        # timer 冲刷取的是最新状态：最终上场面板含第 5 次增长后的行
        card = client.cards[st.card_id]["card"]
        assert "·" * 5 in json.dumps(_panel_children(card), ensure_ascii=False)

    async def test_eviction_paths_cancel_pending_flush_timer(self):
        """forget/_gc 驱逐状态时挂起 timer 一并取消（无 dangling timer）.

        挂起 timer 若随状态残留，到期只会空转一次冲刷任务并对已弹出的
        chat 重建锁对象——清理必须骑既有驱逐路径（forget/GC 冷却回收）。
        """
        client = _PanelClient()
        comp = _make_composer(client)
        loop = asyncio.get_running_loop()

        # forget 路径：活跃卡 + 挂起 timer → forget 后 timer 必须被取消
        chat = "oc_coalesce_forget"
        await _seed_card(comp, client, chat)
        await _absorb(comp, chat, "💬 思考")
        await _absorb(comp, chat, "💬 思考·增长")  # 原地改写 → 挂起 timer
        assert chat in comp._panel_flush_timers
        comp.forget(chat)
        assert chat not in comp._panel_flush_timers
        assert comp._states.get(chat) is None

        # GC 冷却回收路径：已定稿 + idle>60s → 弹出状态时 timer 同步清位
        chat2 = "oc_coalesce_gc"
        st2 = CardTurnState(chat_id=chat2, card_id="card_gc", card_message_id="om_gc")
        st2.finalized = True
        st2.last_activity = time.monotonic() - 61
        comp._states[chat2] = st2
        comp._by_message["om_gc"] = chat2
        fired: list[bool] = []
        comp._panel_flush_timers[chat2] = loop.call_later(999, lambda: fired.append(True))
        comp._gc()
        assert chat2 not in comp._states
        assert chat2 not in comp._panel_flush_timers
        await asyncio.sleep(0.02)
        assert fired == [], "被驱逐状态的 timer 必须已取消（不得到期空转）"

    async def test_flush_after_state_evicted_skips_lock_recreation(self):
        """状态被驱逐后到期的无主冲刷：直接放弃，不为死 chat 重建锁对象.

        _coalesced_flush 先查 _states 再取锁——_chat_lock 是惰性创建，
        先取锁会让已 forget 的 chat 在注册表里留下永久空锁。
        """
        client = _PanelClient()
        comp = _make_composer(client)
        chat = "oc_coalesce_ghost"
        await _seed_card(comp, client, chat)
        await _absorb(comp, chat, "💬 思考")
        await _absorb(comp, chat, "💬 思考·增长")
        assert chat in comp._panel_flush_timers
        comp.forget(chat)  # 状态与 timer 均清；再手工模拟「已到期未执行」竞态
        comp._panel_flush_due(chat)
        for _ in range(4):
            await asyncio.sleep(0)
        assert chat not in comp._panel_flush_timers
        assert chat not in comp._chat_locks, "无主冲刷不得重建 chat 锁"


# ---------------------------------------------------------------------------
# (a) 懒首面板：首个状态行补插必须即时（v2.0.24 语义不回退）
# ---------------------------------------------------------------------------


class TestLazyFirstPanel:
    async def test_first_panel_insert_uploads_immediately(self):
        """卡片无面板时的首个状态行 → 同步 cardkit_update，不进 0.2s 窗口."""
        client = _PanelClient()
        comp = _make_composer(client)
        chat = "oc_coalesce_first"
        st = await _seed_card(comp, client, chat)

        await _absorb(comp, chat, '🔧 terminal："ls -la"')

        assert client.update_attempts == 1, "首块面板补插必须即时（lazy first-panel）"
        assert chat not in comp._panel_flush_timers, "即时路径不得另挂 timer"
        assert st.panel_on_card is True
        assert st.panel_dirty is False
        # 面板真的落到了上传的卡上（而非只改了内存缓冲）
        card = client.cards[st.card_id]["card"]
        assert "Terminal" in json.dumps(_panel_children(card), ensure_ascii=False)


# ---------------------------------------------------------------------------
# (a) 红线：finalize 入口强制冲刷——最后面板状态先于封卡 API 落卡
# ---------------------------------------------------------------------------


class TestFinalizeFlushOrder:
    async def test_finalize_flushes_dirty_panel_before_seal_apis(self):
        """挂起脏面板时定稿：先同步冲刷 cardkit_update，再进 finalize 四步.

        流式正确性红线（audit 取证）：终态面板若晚于 close_streaming，
        用户看到的状态面板缺最后一拍。call_log 保序断言冲刷在全部封卡
        API 之前，且冲刷后不残留 timer/dirty。
        """
        client = _PanelClient()
        comp = _make_composer(client)
        chat = "oc_coalesce_final"
        st = await _seed_card(comp, client, chat)

        await _absorb(comp, chat, "💬 第一轮起点")          # 即时补插（#1）
        await _absorb(comp, chat, "💬 第一轮起点·增长")     # 原地改写 → 挂起
        assert client.update_attempts == 1
        assert chat in comp._panel_flush_timers

        await comp.handle_stream_frame(chat, "首帧正文定稿", finalize=True, turn_id="t1")

        assert st.finalized is True
        assert st.panel_dirty is False, "finalize 入口必须清掉脏旗标"
        assert chat not in comp._panel_flush_timers, "finalize 必须取消合并 timer"
        assert client.update_attempts == 2, "挂起面板恰好冲刷一次"
        # 顺序红线：最后一次 cardkit_update 早于任何封卡 API
        log = client.call_log
        last_update = max(i for i, op in enumerate(log) if op == "cardkit_update")
        first_seal = min(
            i for i, op in enumerate(log) if op in ("cardkit_batch_update", "cardkit_close_streaming")
        )
        assert last_update < first_seal, f"面板冲刷必须先于封卡 API: {log}"
        # 冲刷带走的是最新面板状态（增长后的行）
        assert "·增长" in json.dumps(_panel_children(client.cards[st.card_id]["card"]), ensure_ascii=False)


# ---------------------------------------------------------------------------
# (b) 字节预算：_patch_status 上传前裁最旧面板行（answer 永不裁）
# ---------------------------------------------------------------------------


class TestByteBudgetTrim:
    async def test_oversized_panel_trimmed_under_budget_oldest_first(self):
        """超预算面板：序列化体积回落 27KB，最旧行先裁，折叠提示更新.

        生产实证：34 行状态 19.3KB/update + ~12KB 答案即撞官方 30KB
        （200860）→ 中途降级。裁剪只作用于本次上传视图（st.status_lines
        恒存全量），answer 元素内容原样保留（正文红线）。
        """
        client = _PanelClient()
        comp = _make_composer(client)
        chat = "oc_coalesce_budget"
        st = await _seed_card(comp, client, chat)

        # ~12KB 答案 + 34 行大体积状态（生产撞线形态：远超 27KB 预算）
        st.answer_text = "答" * 4096
        st.status_lines = [
            f'🔧 terminal："cmd-{i:02d} ' + "x" * 800 + '"' for i in range(34)
        ]
        st.line_times = [time.monotonic()] * len(st.status_lines)
        oversized = build_streaming_card_v2(
            status_lines=list(st.status_lines), answer_text=st.answer_text)
        assert _card_serialized_size(oversized) > _CARD_BYTE_BUDGET, "前置：夹具须超预算"

        await comp._patch_status(st)

        card = client.cards[st.card_id]["card"]
        assert _card_serialized_size(card) <= _CARD_BYTE_BUDGET, "上传视图必须回落预算内"
        children = _panel_children(card)
        blob = json.dumps(children, ensure_ascii=False)
        # 最旧行先裁：marker-00（最老）出局、marker-33（最新）保留
        assert "cmd-00" not in blob, "裁剪必须从最旧行开始"
        assert "cmd-33" in blob, "最新行必须保留"
        # trim hint 提示行（UX-1a 诚实双语措辞）：保留在最前且计数 ≥1
        # （content=en 兑底，zh 落 i18n_content.zh_cn——措辞由 cards.py 提供）
        hints = [ch for ch in children if "earlier steps omitted" in str(ch.get("content", ""))]
        assert hints, "裁剪后必须更新省略提示"
        assert hints[0]["content"].startswith("⚡ "), f"提示机制措辞不变: {hints[0]}"
        assert "项已省略" in hints[0]["i18n_content"]["zh_cn"]
        assert children[0] is hints[0], "提示行必须钉在面板最前（不被裁）"
        # answer 永不裁：正文元素完整保留
        answer_el = next(
            el for el in card["body"]["elements"] if el.get("element_id") == ANSWER_ELEMENT_ID)
        assert answer_el["content"] == "答" * 4096
        # 状态缓冲恒存全量（裁剪只作用于上传视图）
        assert len(st.status_lines) == 34

    async def test_budget_respected_across_repeated_patches(self):
        """连续多帧超预算更新：每帧独立守卫，折叠计数跨帧累加.

        流式期每次 flush 都重建全量卡片——字节预算必须每帧生效而非只
        保护第一帧；提示计数不随帧重置（与元素上限版同款语义）。
        """
        client = _PanelClient()
        comp = _make_composer(client)
        chat = "oc_coalesce_budget2"
        st = await _seed_card(comp, client, chat)
        st.answer_text = "答" * 4096
        st.status_lines = [
            f'🔧 terminal："cmd-{i:02d} ' + "x" * 800 + '"' for i in range(34)
        ]
        st.line_times = [time.monotonic()] * len(st.status_lines)

        await comp._patch_status(st)
        first_hint = next(
            ch for ch in _panel_children(client.cards[st.card_id]["card"])
            if "earlier steps omitted" in str(ch.get("content", "")))
        first_count = int(first_hint["content"].split("⚡ ")[1].split(" ")[0])

        await comp._patch_status(st)  # 第二帧同体量 → 再次触发裁剪
        card2 = client.cards[st.card_id]["card"]
        assert _card_serialized_size(card2) <= _CARD_BYTE_BUDGET
        second_hint = next(
            ch for ch in _panel_children(card2)
            if "earlier steps omitted" in str(ch.get("content", "")))
        second_count = int(second_hint["content"].split("⚡ ")[1].split(" ")[0])
        assert second_count >= first_count, "省略计数跨帧累加，不重置"


# ---------------------------------------------------------------------------
# (c) 200860 分类：确定性拒收必须 fail-fast（不重试）
# ---------------------------------------------------------------------------


class Test200860FailFast:
    def test_200860_direct_and_wrapped_non_retryable(self):
        """直报码与 230099 通用码包裹形态（ErrCode: 200860）都不可重试.

        _retry_transient 白名单本就未收录 200860；显式短路钉死语义并覆盖
        通用码子码形态（旧实现对 230099+200860 子码会误判可重试白撞 3 次）。
        """
        direct = FeishuAPIError("cardkit_update: code=200860, msg=size limit", _SIZE_200860)
        assert _is_transient_error(direct) is False
        assert is_card_size_error(direct) is True
        wrapped = FeishuAPIError(
            "cardkit_update: code=230099, msg=Card Content Failed ... ErrCode: 200860", 230099)
        assert wrapped.extract_sub_code() == _SIZE_200860
        assert _is_transient_error(wrapped) is False
        # 对照：白名单瞬态码（2200 CardKit 内部超时）语义不回退
        assert _is_transient_error(FeishuAPIError("x", 2200)) is True

    async def test_client_retry_helper_fails_fast_on_200860(self):
        """FeishuClient._retry_transient 对 200860 首试即抛（0 次重试）."""
        client = FeishuClient(FeishuClientConfig(app_id="cli_test", app_secret="secret_test"))
        attempts = 0

        def _factory():
            nonlocal attempts
            attempts += 1
            raise FeishuAPIError(
                "cardkit_update: code=200860, msg=cards exceed max size limit", _SIZE_200860)

        with pytest.raises(FeishuAPIError):
            await client._retry_transient("cardkit_update", _factory)
        assert attempts == 1, "确定性拒收必须 fail-fast（3× 重试白撞拖慢降级 ~1s）"

    async def test_panel_flush_200860_single_attempt_then_degrade(self):
        """composer 冲刷撞 200860：恰好 1 次上传尝试 → 降级（无重试风暴）.

        mock 按生产时序编程：首块面板 update 成功后，下一次 cardkit_update
        恒抛 200860——timer 冲刷必须一击即穿（call_with_seq_recovery 只自愈
        300317/200850），随后走 degrade+seal 红线，绝不二次尝试。
        """
        client = _PanelClient(update_fail_from=2)
        comp = _make_composer(client)
        chat = "oc_coalesce_200860"
        st = await _seed_card(comp, client, chat)

        await _absorb(comp, chat, "💬 第一轮起点")       # update #1 成功
        assert client.update_attempts == 1
        await _absorb(comp, chat, "💬 第一轮起点·增长")  # 原地改写 → 挂起 timer

        assert await _wait_until(lambda: st.degraded)
        assert client.update_attempts == 2, "200860 后不得出现第二次上传尝试"
        assert st.degraded and st.finalized, "失败必须走降级红线（后续帧退原生）"
        assert "cardkit_close_streaming" in client.call_log, "降级收口照常执行"
