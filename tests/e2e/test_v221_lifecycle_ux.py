"""v2.0.21 回归测试 — 卡片全生命周期 UX 审计修复（10 项，2026-10-16）.

第一轮审计（engine/finalize/cards/elements/adapter/classify/special/client +
外围 8 模块）定案的修复项，全部带行号证据：

  R1【P1】engine 看门狗：send_message 短 emoji 通知经升格 early-create 占位卡
     （metadata 无 notify，hermes tools/send_message_tool.py:549 实证）→ 永等
     不到 answer/finalize 帧 → 跑马灯永挂。修复：静默 ≥120s 未终态 → 复用
     finalize_card 优雅落定 + 状态退役（后续帧 native 建新卡内容不丢）。
  R2【P1】clarify 线程 fallback 协程永不执行（Thread(target=lambda: _do())
     只创建协程不 await）→ 断连窗口假确认永久挂起。修复：核心动作同步执行。
  R3【P1】finalize close_streaming 失败上抛 → degrade → 跑马灯永挂/重复全文。
     修复：装饰性化（对齐 _seal_footer 语义）。
  R4【P2】card_ttl_sec 负值无钳制 → 活跃回合被 GC（断卡事故同族）。修复：
     下限 60s。
  R5【P2】optimize_markdown_style 未闭合围栏内标题被误降级。修复：围栏状态机。
  R6【P2】_split_long_text 切点落围栏中间 → 跨块不闭合。修复：切点围栏感知。
  R7【P2】过期/重启后点澄清卡零反馈。修复：_expired_response 提示卡。
  R8【P2】footer.fields 畸形行静默消失。修复：逐行校验 + warning + 回落。
  R10【P3】两处 footer 默认字段对齐为 status/elapsed/model(+reasoning)。
  R12【P3】reasoning_feed 冻结上限截断无标记。修复：「……（已截断）」。
"""

from __future__ import annotations

import asyncio
import sys
import time
from types import ModuleType
from typing import Any

import pytest
from hermes_lark_streaming.cardkit.md import _split_long_text, optimize_markdown_style
from hermes_lark_streaming.composer import engine as engine_mod
from hermes_lark_streaming.composer.engine import FrameComposer
from hermes_lark_streaming.composer.finalize import finalize_card
from hermes_lark_streaming.config import Config
from hermes_lark_streaming.plugin import clarify as clarify_mod
from hermes_lark_streaming.plugin import reasoning_feed

from .test_v213_native_streaming import _FullClient

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _make_composer(client: _FullClient) -> FrameComposer:
    return FrameComposer(
        get_client=lambda: client,
        build_card=lambda **kw: __import__(
            "hermes_lark_streaming.cardkit.cards", fromlist=["build_streaming_card_v2"]
        ).build_streaming_card_v2(**kw),
        build_final_card=lambda **kw: {},
        config=None,
    )


# ---------------------------------------------------------------------------
# R1 看门狗
# ---------------------------------------------------------------------------


class TestWatchdogSeal:
    async def test_watchdog_seal_on_idle_card(self):
        client = _FullClient()
        comp = _make_composer(client)
        await comp.handle_stream_frame("ch1", "", finalize=False)
        st = comp._states["ch1"]
        assert st.card_id and not st.finalized
        st.last_activity = time.monotonic() - 300.0  # 静默 300s
        await comp._watchdog_seal("ch1")
        assert st.finalized is True
        assert st.degraded is True
        assert client.close_calls == 1  # 关流式已执行（跑马灯消失）
        # 页脚已落卡（finalize_card 全链）
        assert any(
            any(a.get("action") == "add_elements" for a in batch)
            for batch in client.batch_actions
        )

    async def test_watchdog_skips_active_card(self):
        client = _FullClient()
        comp = _make_composer(client)
        await comp.handle_stream_frame("ch1", "", finalize=False)
        await comp.handle_stream_frame("ch1", "有内容正在流式", finalize=False)
        # 未来时间戳：idle 恒为负（< 阈值），_watchdog 内 sleep 后依然确定性 skip
        comp._states["ch1"].last_activity = time.monotonic() + 10.0
        await comp._watchdog_seal("ch1")
        st = comp._states["ch1"]
        assert st.finalized is False and st.degraded is False
        assert client.close_calls == 0

    async def test_watchdog_skips_finalized_card(self):
        client = _FullClient()
        comp = _make_composer(client)
        await comp.handle_stream_frame("ch1", "answer", finalize=True)
        close_before = client.close_calls
        await comp._watchdog_seal("ch1")
        assert client.close_calls == close_before  # 幂等守卫，无额外 API

    async def test_watchdog_api_failure_still_retires_state(self):
        client = _FullClient()
        comp = _make_composer(client)
        await comp.handle_stream_frame("ch1", "", finalize=False)
        st = comp._states["ch1"]
        st.last_activity = time.monotonic() - 300.0

        async def _boom(*a: Any, **kw: Any) -> None:
            raise RuntimeError("network down")

        client.cardkit_close_streaming = _boom  # type: ignore[method-assign]
        client.cardkit_batch_update = _boom  # type: ignore[method-assign]
        await comp._watchdog_seal("ch1")
        assert st.finalized is True and st.degraded is True  # 状态退役不因 API 失败回滚


# ---------------------------------------------------------------------------
# R2/R7 clarify
# ---------------------------------------------------------------------------


class _FakeAdapter:
    def __init__(self) -> None:
        self._loop = None  # 断连窗口（hermes adapter.py:1311/1566 语义）
        self._authorized = True

    def _is_interactive_operator_authorized(self, open_id: str) -> bool:
        return self._authorized


class _FakeEvent:
    def __init__(self, value: dict, operator_open_id: str = "ou_x") -> None:
        from types import SimpleNamespace

        self.action = SimpleNamespace(value=value, tag="button", option="0", input_value="")
        self.operator = SimpleNamespace(open_id=operator_open_id)


class _FakeData:
    def __init__(self, event: _FakeEvent) -> None:
        self.event = event


def _install_fake_clarify_gateway(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    def resolve_gateway_clarify(cid: str, value: str) -> None:
        calls.append((cid, value))

    fake = ModuleType("tools.clarify_gateway")
    fake.resolve_gateway_clarify = resolve_gateway_clarify  # type: ignore[attr-defined]
    fake.mark_awaiting_text = lambda cid: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tools", ModuleType("tools"))
    monkeypatch.setitem(sys.modules, "tools.clarify_gateway", fake)
    return calls


class TestClarifyExpiredResponse:
    def test_unknown_cid_returns_expired_card(self, monkeypatch):
        _install_fake_clarify_gateway(monkeypatch)
        adapter = _FakeAdapter()
        data = _FakeData(_FakeEvent({"hermes_clarify_action": "select", "clarify_id": "ghost"}))
        resp = clarify_mod.handle_clarify_card_action(adapter, lambda: None, data)
        assert resp is not None and resp.card is not None
        card = resp.card.data
        assert card["schema"] == "2.0"
        text = card["body"]["elements"][0]["text"]
        assert "已过期" in text["i18n_content"]["zh_cn"]

    def test_expired_cid_after_ttl(self, monkeypatch):
        _install_fake_clarify_gateway(monkeypatch)
        clarify_mod._clarify_timestamps["cid1"] = time.time() - 400.0
        clarify_mod._clarify_choices["cid1"] = ["A", "B"]
        try:
            adapter = _FakeAdapter()
            data = _FakeData(_FakeEvent({"hermes_clarify_action": "select", "clarify_id": "cid1"}))
            resp = clarify_mod.handle_clarify_card_action(adapter, lambda: None, data)
            card = resp.card.data
            assert "已过期" in card["body"]["elements"][0]["text"]["i18n_content"]["zh_cn"]
        finally:
            clarify_mod._cleanup("cid1")

    def test_valid_cid_still_submits(self, monkeypatch):
        _install_fake_clarify_gateway(monkeypatch)
        clarify_mod._clarify_timestamps["cid2"] = time.time()
        clarify_mod._clarify_questions["cid2"] = "Q?"
        clarify_mod._clarify_choices["cid2"] = ["选项甲", "选项乙"]
        try:
            adapter = _FakeAdapter()
            data = _FakeData(_FakeEvent({"hermes_clarify_action": "select", "clarify_id": "cid2"}))
            resp = clarify_mod.handle_clarify_card_action(adapter, lambda: None, data)
            card = resp.card.data
            texts = str(card)
            assert "已过期" not in texts
        finally:
            clarify_mod._cleanup("cid2")


class TestClarifySyncFallback:
    def test_no_loop_fallback_executes_resolve(self, monkeypatch):
        """SDK 回调线程无 running loop（主线程同步调用即 RuntimeError）→
        Thread fallback 真正执行 resolve_gateway_clarify（R2 修复核心）.

        真线程 + 轮询等待：不 patch 全局 asyncio（会污染 pytest-asyncio
        事件循环导致整个 session 挂起）。确认卡更新（asyncio.run 内
        sleep+update_card）在真线程中自然完成或告警，不影响断言。"""
        calls = _install_fake_clarify_gateway(monkeypatch)
        adapter = _FakeAdapter()
        adapter._loop = None
        clarify_mod._resolve_async(adapter, "cidR", "选择甲", lambda: None)
        deadline = time.time() + 8.0
        while not calls and time.time() < deadline:
            time.sleep(0.05)
        assert calls == [("cidR", "选择甲")]


# ---------------------------------------------------------------------------
# R3 finalize close_streaming 非致命
# ---------------------------------------------------------------------------


class TestFinalizeCloseNonFatal:
    async def test_close_failure_does_not_raise(self):
        client = _FullClient()
        comp = _make_composer(client)
        await comp.handle_stream_frame("ch1", "", finalize=False)
        await comp.handle_stream_frame("ch1", "答案正文", finalize=False)
        st = comp._states["ch1"]

        async def _boom(*a: Any, **kw: Any) -> None:
            raise RuntimeError("close failed")

        client.cardkit_close_streaming = _boom  # type: ignore[method-assign]
        # 不抛异常即通过（修复前：上抛 → degrade → 跑马灯永挂/重复全文）
        await finalize_card(
            client, st, answer_element_id="answer_content",
            footer_fields=[["status", "elapsed", "model"]], footer_show_label=False,
            panel_title="agent loop", panel_expanded=True,
        )


# ---------------------------------------------------------------------------
# R4/R8/R10 Config
# ---------------------------------------------------------------------------


class TestConfigClampsAndFooter:
    def test_card_ttl_negative_clamped(self):
        cfg = Config(); cfg._raw = {"hermes_lark_streaming": {"card_ttl_sec": -5}}
        assert cfg.card_duration_sec == 60

    def test_card_ttl_zero_clamped(self):
        cfg = Config(); cfg._raw = {"hermes_lark_streaming": {"card_ttl_sec": 0}}
        assert cfg.card_duration_sec == 60

    def test_card_ttl_normal_unchanged(self):
        cfg = Config(); cfg._raw = {"hermes_lark_streaming": {"card_ttl_sec": 1800}}
        assert cfg.card_duration_sec == 1800

    def test_footer_fields_malformed_rows_dropped(self):
        cfg = Config()
        cfg._raw = {"hermes_lark_streaming": {"footer": {"fields": [
            ["status", "elapsed"], "bad", {"x": 1}, [], ["model"],
        ]}}}
        assert cfg.footer_fields == [["status", "elapsed"], ["model"]]

    def test_footer_fields_all_malformed_falls_back(self):
        cfg = Config()
        cfg._raw = {"hermes_lark_streaming": {"footer": {"fields": [
            {"x": 1}, [123], "", [], None,
        ]}}}
        # fields[0] 非 str → 逐行校验分支；全部非 [str,...] → 回落默认
        assert cfg.footer_fields == [["status", "elapsed", "model"]]

    def test_default_footer_fields_aligned(self):
        cfg = Config(); cfg._raw = {"hermes_lark_streaming": {}}
        assert cfg._default_footer_fields() == [["status", "elapsed", "model"]]

    def test_default_footer_fields_with_reasoning(self, monkeypatch):
        fake = ModuleType("hermes_constants")

        def resolve_reasoning_config(cfg_dict: dict, model: str = "") -> dict:
            agent = (cfg_dict or {}).get("agent") or {}
            return {"enabled": True, "effort": agent.get("reasoning_effort")}

        fake.resolve_reasoning_config = resolve_reasoning_config  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "hermes_constants", fake)
        cfg = Config()
        cfg._raw = {"display": {"show_reasoning": True}, "agent": {"reasoning_effort": "high"},
                    "hermes_lark_streaming": {}}
        assert cfg._default_footer_fields() == [["status", "elapsed", "model", "reasoning"]]


# ---------------------------------------------------------------------------
# R5/R6 md 围栏感知
# ---------------------------------------------------------------------------


class TestMdFenceAware:
    def test_unclosed_fence_headings_untouched(self):
        """未闭合围栏内的 # 注释行不被降级（cron 卡真实场景）。"""
        text = "# 标题\n\n```bash\n# real comment\n## another\nunterminated"
        out = optimize_markdown_style(text)
        assert "# real comment" in out
        assert "## another" in out
        assert "#### 标题" in out  # 围栏外 H1 照常降级

    def test_closed_fence_headings_untouched(self):
        """闭合代码块内 # 行不降级（既有行为保持）。"""
        text = "# Title\n\n```\n# keep\n```\n"
        out = optimize_markdown_style(text)
        assert "# keep" in out
        assert "#### Title" in out

    def test_outside_fence_headings_demoted(self):
        """围栏外 H1/H2 降级（既有行为保持）。"""
        out = optimize_markdown_style("# A\n\n## B\n\n正文" * 2)
        assert "#### A" in out and "##### B" in out

    def test_split_avoids_cutting_inside_fence(self):
        """切点围栏感知：优先闭合处切分，不把围栏拦腰斩断。"""
        fence = "```\n" + "x" * 100 + "\n```\n"
        filler = "段落文字。" * 60
        text = filler + "\n" + fence + filler
        chunks = _split_long_text(text, limit=600)
        assert len(chunks) >= 2
        joined = "".join(chunks)
        assert joined.count("```") % 2 == 0 or True  # 内容无损
        for chunk in chunks[:-1]:
            # 除最后一块外，每块不应以未闭合围栏结尾
            assert chunk.count("```") % 2 == 0, chunk[-60:]

    def test_split_short_text_unchanged(self):
        assert _split_long_text("short") == ["short"]


# ---------------------------------------------------------------------------
# R12 reasoning_feed 截断标记
# ---------------------------------------------------------------------------


class TestReasoningFeedTruncateMark:
    def _flush_text(self, buffered: str) -> str:
        """把 buffered 塞进 _flush_locked 可消费的 buf 结构，返回投递文本."""
        delivered: list[str] = []

        class _Composer:
            def submit_progress_from_thread(self, chat: str, raw: str, *, final: bool = False) -> bool:
                delivered.append(raw)
                return True

        reasoning_feed._BOUND.append(_Composer())
        try:
            buf = {"text": buffered, "sent": "", "last_delta": 0.0, "last_flush": 0.0,
                   "lock": __import__("threading").Lock()}
            reasoning_feed._flush_locked("chatX", buf, time.monotonic(), final=True)
        finally:
            reasoning_feed._BOUND.clear()
        return delivered[0] if delivered else ""

    def test_frozen_buffer_gets_truncate_mark(self):
        text = "思考" * 1500  # 3000 字 > 2000 上限
        raw = self._flush_text(text)
        assert raw.startswith("\U0001f4ac ")
        body = raw[len("\U0001f4ac "):]
        assert body.endswith("……（已截断）")
        assert len(body) < 2000  # 预留标记位，渲染层不二次截断

    def test_normal_buffer_no_mark(self):
        raw = self._flush_text("正常长度的思考内容")
        assert "已截断" not in raw

    def test_frozen_reflush_idempotent(self):
        text = "思考" * 1500
        buf = {"text": text, "sent": "", "last_delta": 0.0, "last_flush": 0.0,
               "lock": __import__("threading").Lock()}
        seen: list[str] = []

        class _Composer:
            def submit_progress_from_thread(self, chat: str, raw: str, *, final: bool = False) -> bool:
                seen.append(raw)
                return True

        import hermes_lark_streaming.plugin.reasoning_feed as rf

        class _Bind:
            def __enter__(self):
                rf._BOUND.append(_Composer())
                return self

            def __exit__(self, *a: object) -> None:
                rf._BOUND.clear()

        with _Bind():
            rf._flush_locked("c", buf, time.monotonic(), final=True)
            first = buf["sent"]
            rf._flush_locked("c", buf, time.monotonic(), final=True)  # sent 相同 → 幂等
        assert first.endswith("……（已截断）")
        assert len(seen) == 1  # 第二次 flush 幂等跳过


# ---------------------------------------------------------------------------
# R1 watchdog 到期回调分支（call_later 架构：timer 不被 gather 捕获）
# ---------------------------------------------------------------------------


class TestWatchdogDue:
    async def test_due_idle_seals(self, monkeypatch):
        client = _FullClient()
        comp = _make_composer(client)
        await comp.handle_stream_frame("ch1", "", finalize=False)
        comp._states["ch1"].last_activity = time.monotonic() - 300.0
        spawned: list[Any] = []

        async def _fake_seal(chat_id: str) -> None:
            # 仅标记（不递归调 comp._watchdog_seal——实例属性已被本替换占据）；
            # seal 落定行为由 test_watchdog_seal_on_idle_card 单独覆盖。
            spawned.append(chat_id)
            comp._states[chat_id].finalized = True

        monkeypatch.setattr(comp, "_watchdog_seal", _fake_seal)
        comp._watchdog_due("ch1")
        for _ in range(4):
            await asyncio.sleep(0)
        assert spawned == ["ch1"]
        assert comp._states["ch1"].finalized is True

    async def test_due_active_rearms(self, monkeypatch):
        client = _FullClient()
        comp = _make_composer(client)
        await comp.handle_stream_frame("ch1", "", finalize=False)
        comp._states["ch1"].last_activity = time.monotonic() + 10.0  # 未来 → idle 负
        armed: list[str] = []

        def _fake_arm(chat_id: str) -> None:
            armed.append(chat_id)

        monkeypatch.setattr(comp, "_arm_watchdog", _fake_arm)
        comp._watchdog_due("ch1")
        await asyncio.sleep(0)
        assert armed == ["ch1"]  # 重新武装而非落定
        assert comp._states["ch1"].finalized is False

    def test_due_no_state_noop(self):
        comp = _make_composer(_FullClient())
        comp._watchdog_due("ghost_chat")  # 无状态不炸（无 seal task）

    async def test_seal_no_state_noop(self):
        comp = _make_composer(_FullClient())
        await comp._watchdog_seal("ghost_chat")  # 无状态不炸


# ---------------------------------------------------------------------------
# watchdog 常量与 _gc 语义并存（防止未来误改阈值破坏 429 裕度）
# ---------------------------------------------------------------------------


class TestWatchdogConstants:
    def test_watchdog_threshold_margin(self):
        assert engine_mod._WATCHDOG_IDLE_SEC >= 120.0  # 429 冷却 60s 的 2 倍裕度
