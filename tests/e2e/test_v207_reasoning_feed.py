"""v2.0.7 回归测试 — reasoning 隐藏流 → 面板思考行（第 N 轮）.

用户验收背景（2026-10-11）：「折叠面板里为何没有思考、推理的记录，只有工具的
记录」。根因：hermes 网关 thinking_progress 只中继工具调用之间的**可见间歇文本**；
DeepSeek 等推理模型的思考走 ``reasoning_content`` 隐藏流，网关主代理路径不接
``reasoning_callback``，静默丢弃。v2.0.7 经官方 ``on_stream_delta`` 观察者钩子
（config ``plugins.stream_reasoning_deltas: true``）把 reasoning delta 按会话
累积、节流后回灌 composer 的 thinking 通道。

本文件固化：kind 过滤 / 会话归属 / 节流与断轮 / 累积增长合并幂等 / 面板渲染 /
线程安全入口守卫，防思考行再次静默消失。
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
from hermes_lark_streaming import reasoning_feed
from hermes_lark_streaming.cardkit.cards import _render_status_children
from hermes_lark_streaming.composer.engine import FrameComposer, _panel_title

THINK_A = "正在检查系统状态，先收集内核与运行时长…"
THINK_B = "工具跑完了，正在汇总结果…"


# ---------------------------------------------------------------------------
# 测试夹具
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
    reasoning_feed.reset_for_tests()
    reasoning_feed.bind_composer(comp)
    yield comp
    reasoning_feed.reset_for_tests()


@pytest.fixture()
def chat_ctx(monkeypatch):
    """伪造会话 ContextVar 归属（gateway.session_context 可导入则 patch 其 getter）."""
    chat = "oc_test_reasoning"

    def fake_get_session_env(name: str, default: str = "") -> str:
        return chat if name == "HERMES_SESSION_CHAT_ID" else default

    try:
        import gateway.session_context as sc

        monkeypatch.setattr(sc, "get_session_env", fake_get_session_env)
    except Exception:  # noqa: BLE001 — 无 hermes 环境直接 patch feed 内部
        monkeypatch.setattr(
            "hermes_lark_streaming.reasoning_feed._current_chat_id", lambda: chat, raising=True
        )
    return chat


def _feed(delta: str, kind: str = "reasoning") -> None:
    reasoning_feed.on_stream_delta(delta=delta, kind=kind)


def _seed_no_flush(chat: str) -> None:
    """抑制间隔刷新（模拟刚刷过）：节流触发单独在 interval 用例验证."""
    reasoning_feed._BUFS[chat]["last_flush"] = time.monotonic() + 10_000.0


async def _settle() -> None:
    """让 call_soon_threadsafe 排队回调与 ensure_future 任务全部跑完."""
    for _ in range(4):
        await asyncio.sleep(0)
    cur = asyncio.current_task()
    ts = [t for t in asyncio.all_tasks() if t is not cur and not t.done()]
    if ts:
        await asyncio.gather(*ts, return_exceptions=True)


# ---------------------------------------------------------------------------
# 1. 过滤与会话归属
# ---------------------------------------------------------------------------


class TestFiltering:
    def test_text_kind_ignored(self, composer, chat_ctx):
        _feed("可见答案", kind="text")
        assert reasoning_feed._BUFS.get(chat_ctx) is None

    def test_empty_delta_ignored(self, composer, chat_ctx):
        _feed("")
        assert reasoning_feed._BUFS.get(chat_ctx) is None

    def test_delta_accumulates(self, composer, chat_ctx):
        _feed(THINK_A)  # 首 delta：last_flush=0 → 立即刷新（text 保留，sent 记账）
        _seed_no_flush(chat_ctx)
        _feed(THINK_A)
        _feed(THINK_B)
        assert THINK_A in reasoning_feed._BUFS[chat_ctx]["text"]
        assert THINK_B in reasoning_feed._BUFS[chat_ctx]["text"]

    def test_no_chat_context_drops(self, composer, monkeypatch):
        monkeypatch.setattr(
            "hermes_lark_streaming.reasoning_feed._current_chat_id", lambda: "", raising=True
        )
        _feed(THINK_A)
        assert reasoning_feed._BUFS == {}


# ---------------------------------------------------------------------------
# 2. 节流与断轮
# ---------------------------------------------------------------------------


class TestThrottle:
    def test_interval_flush(self, composer, chat_ctx, monkeypatch):
        clock = {"t": 100.0}
        monkeypatch.setattr(time, "monotonic", lambda: clock["t"])
        _feed("x" * 10)  # last_flush=0 → 触发 flush；无存活 loop → 不投递不记账
        buf = reasoning_feed._BUFS[chat_ctx]
        assert buf["text"] == "x" * 10   # 增长语义：text 保留待重发
        assert buf["sent"] == ""        # 未投递未记账（composer 合并层兑底幂等）
        assert buf["last_flush"] == 0.0  # 同上

    def test_gap_resets_buffer(self, composer, chat_ctx, monkeypatch):
        clock = {"t": 200.0}
        monkeypatch.setattr(time, "monotonic", lambda: clock["t"])
        _feed("第一段思考")  # 立即刷新（sent 记账）
        assert reasoning_feed._BUFS[chat_ctx]["text"] == "第一段思考"
        clock["t"] += reasoning_feed._GAP_RESET_SEC + 1
        _feed("第二段思考")  # 间隔超限 → 旧缓冲清空开新行
        assert reasoning_feed._BUFS[chat_ctx]["text"] == "第二段思考"

    def test_stream_end_flushes_and_clears(self, composer, chat_ctx, monkeypatch):
        clock = {"t": 300.0}
        monkeypatch.setattr(time, "monotonic", lambda: clock["t"])
        _feed(THINK_A)  # 立即刷新
        _seed_no_flush(chat_ctx)
        _feed(THINK_A + "补充")  # 增长，缓冲持有
        assert reasoning_feed._BUFS[chat_ctx]["text"] != ""
        reasoning_feed.on_stream_end(final_text="", finished=True, error=None)
        assert reasoning_feed._BUFS[chat_ctx]["text"] == ""
        assert reasoning_feed._BUFS[chat_ctx]["sent"] == ""

    def test_cap_freezes_growth(self, composer, chat_ctx):
        _feed("x")  # 立即刷新
        _seed_no_flush(chat_ctx)
        _feed("y" * (reasoning_feed._MAX_LINE_CHARS + 50))
        _feed("tail-that-must-be-dropped")
        buf = reasoning_feed._BUFS[chat_ctx]
        assert len(buf["text"]) == reasoning_feed._MAX_LINE_CHARS
        assert "tail-that-must-be-dropped" not in buf["text"]


# ---------------------------------------------------------------------------
# 3. 回灌 → 面板语义（累积增长合并 + 第 N 轮渲染）
# ---------------------------------------------------------------------------


class TestPanelIntegration:
    def _open_loop(self):
        return asyncio.new_event_loop()

    def test_growth_replaces_in_place(self, composer, chat_ctx, monkeypatch):
        import hermes_lark_streaming.composer.classify as C

        clock = {"t": 400.0}
        monkeypatch.setattr(time, "monotonic", lambda: clock["t"])
        loop = self._open_loop()
        try:
            async def setup():
                composer._loop = asyncio.get_running_loop()
                await composer.handle_send(
                    chat_ctx, "开始", None, None, C.ClassifyResult("stream", raw_content="开始"))

            loop.run_until_complete(setup())
            _feed(THINK_A)  # last_flush=0 → 立即刷新（loop 存活 → submit 受理）
            loop.run_until_complete(_settle())
            st = composer.active_state(chat_ctx)
            assert st is not None
            assert f"💬 {THINK_A}" in st.status_lines

            reasoning_feed._BUFS[chat_ctx]["last_flush"] = clock["t"]  # 抑制间隔
            _feed("补充")            # 增量 delta：缓冲增长 → 未落盘
            assert not any(l.startswith(f"💬 {THINK_A}补充") for l in st.status_lines)
            clock["t"] += 2.0
            _feed("x")  # 间隔触发刷新：累积全文 THINK_A补充x
            loop.run_until_complete(_settle())
            st = composer.active_state(chat_ctx)
            assert any(l.startswith(f"💬 {THINK_A}补充") for l in st.status_lines)
            # 原地替换：💬 行仍只有一条（未追加膨胀）
            assert sum(1 for l in st.status_lines if l.startswith("💬")) == 1
        finally:
            loop.close()

    def test_rounds_split_after_tool(self, composer, chat_ctx):
        """工具行分隔的两次思考 → 第 1 轮 / 第 2 轮。"""
        import hermes_lark_streaming.composer.classify as C

        async def flow():
            composer._loop = asyncio.get_running_loop()
            await composer.handle_send(chat_ctx, "go", None, None, C.ClassifyResult("stream", raw_content="go"))
            await composer.handle_send(
                chat_ctx, f"💬 {THINK_A}", None, None,
                C.ClassifyResult(C.CH_PROGRESS, kind="thinking", raw_content=f"💬 {THINK_A}"))
            await composer.handle_send(
                chat_ctx, "💻 terminal\n```\nuname -r\n```", None, None,
                C.ClassifyResult(C.CH_PROGRESS, kind="tool", tool_name="terminal",
                                 raw_content="💻 terminal\n```\nuname -r\n```"))
            await composer.handle_send(
                chat_ctx, f"💬 {THINK_B}", None, None,
                C.ClassifyResult(C.CH_PROGRESS, kind="thinking", raw_content=f"💬 {THINK_B}"))

        asyncio.run(flow())
        st = composer.active_state(chat_ctx)
        els = _render_status_children(st.status_lines)
        texts = str(els)
        assert "第 1 轮" in texts and "第 2 轮" in texts
        assert THINK_A in texts and THINK_B in texts

    def test_frozen_reflush_dedupes(self, composer, chat_ctx):
        """冻结上限后重发同文本 → composer 幂等跳过（不追加、不膨胀）."""
        import hermes_lark_streaming.composer.classify as C

        async def flow():
            composer._loop = asyncio.get_running_loop()
            await composer.handle_send(chat_ctx, "go", None, None, C.ClassifyResult("stream", raw_content="go"))

        asyncio.run(flow())
        frozen = "y" * reasoning_feed._MAX_LINE_CHARS

        async def feed_inside_loop():
            # 在存活 loop 内做 flush/重发（submit 需要活 loop 才会受理并记账）
            composer._loop = asyncio.get_running_loop()
            _feed(frozen[: reasoning_feed._FLUSH_CHARS])  # 首 delta：立即刷新
            _feed(frozen)  # 增量至冻结上限
            reasoning_feed.on_stream_end()  # flush 全文（累积语义）
            _feed(frozen)  # 冻结文本不变（截断回上限）
            reasoning_feed.on_stream_end()  # 重发同文本 → 幂等跳过
            await _settle()

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(feed_inside_loop())
        finally:
            loop.close()
        st = composer.active_state(chat_ctx)
        assert sum(1 for l in st.status_lines if l.startswith("💬")) == 1

    def test_panel_title_counts_reasoning_round(self, composer, chat_ctx):
        title = _panel_title([f"💬 {THINK_A}", "💻 terminal\n```\nuname\n```"], elapsed_ms=4000)
        assert title == "agent loop · 1 轮 · 1 个工具 · 4.0s"


# ---------------------------------------------------------------------------
# 4. 线程安全入口守卫
# ---------------------------------------------------------------------------


class TestSubmitGuard:
    def test_submit_from_thread_delivers(self, composer, chat_ctx):
        import hermes_lark_streaming.composer.classify as C

        loop = asyncio.new_event_loop()
        try:
            async def setup():
                composer._loop = asyncio.get_running_loop()
                await composer.handle_send(
                    chat_ctx, "go", None, None, C.ClassifyResult("stream", raw_content="go"))

            loop.run_until_complete(setup())
            assert composer.submit_progress_from_thread(chat_ctx, f"💬 {THINK_A}") is True
            loop.run_until_complete(_settle())
            st = composer.active_state(chat_ctx)
            assert f"💬 {THINK_A}" in st.status_lines
        finally:
            loop.close()

    def test_submit_after_finalize_dropped(self, composer, chat_ctx):
        import hermes_lark_streaming.composer.classify as C

        async def flow():
            composer._loop = asyncio.get_running_loop()
            await composer.handle_send(chat_ctx, "go", None, None, C.ClassifyResult("stream", raw_content="go"))
            await composer.handle_send(chat_ctx, "答", None, None, C.ClassifyResult("final", raw_content="答"))

        asyncio.run(flow())
        assert composer.submit_progress_from_thread(chat_ctx, f"💬 {THINK_A}") is False

    def test_submit_without_loop_safe(self, composer, chat_ctx):
        composer._loop = None
        assert composer.submit_progress_from_thread(chat_ctx, f"💬 {THINK_A}") is False


# ---------------------------------------------------------------------------
# 5. 钩子注册面
# ---------------------------------------------------------------------------


class TestHookRegistration:
    def test_register_hooks_registers_both(self):
        registered: dict[str, object] = {}

        class _Ctx:
            def register_hook(self, name, cb):
                registered[name] = cb

        reasoning_feed.register_hooks(_Ctx())
        assert registered.get("on_stream_delta") is reasoning_feed.on_stream_delta
        assert registered.get("on_stream_end") is reasoning_feed.on_stream_end

    def test_callback_never_raises(self, composer, chat_ctx):
        # 各种异常载荷都不炸（观察者红线）
        reasoning_feed.on_stream_delta()
        reasoning_feed.on_stream_delta(delta=None, kind="reasoning")
        reasoning_feed.on_stream_delta(delta=12345, kind="reasoning")
        reasoning_feed.on_stream_end()
        th = threading.Thread(
            target=reasoning_feed.on_stream_delta, kwargs={"delta": "x", "kind": "reasoning"})
        th.start()
        th.join(timeout=5)
        assert not th.is_alive()
