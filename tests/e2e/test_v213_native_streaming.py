"""v2.0.13 回归测试 — native streaming 迁移（P1-B）+ 空面板壳清除（P1-A）.

Task 18 取证定案的两个修复：

P1-B（通道级根治「同段话一次进正文一次进面板」/ 多卡片同族）：
  适配器声明 ``SUPPORTS_NATIVE_STREAMING`` + ``send_stream_frame``，答案流
  帧改走官方 native 帧通道。官方 native 路径吞掉 segment-break finalize
  （stream_consumer_transport.py:389 ``finalize = finalize and is_turn_final``）
  且 native = cumulative transport（stream_consumer.py:906 ``_end_segment``
  直接 return）→ 工具边界不再封卡，单卡全回合。历史缺陷链：
  - v2.0.11 修的是「并发窗口重复建卡」（per-chat 锁）；
  - 本版修的是「hermes 官方 edit-based 分段断卡」（16:54 轮次 call#1 前奏
    被工具边界 seal 成独立卡——Task 18 报告）。

P1-A：纯 answer 回合（status_lines=0）finalize 时删除 collapsible_panel
  空壳（"agent loop" 头部无内容留存）。

降级红线（官方契约）：任何 native 帧 False/异常 → consumer 禁用 native 并
回落 edit/send 现有链路（transport:399-426），帧永不丢。
"""

from __future__ import annotations

import pytest
from hermes_lark_streaming.cardkit.cards import build_streaming_card_v2
from hermes_lark_streaming.cardkit.elements import UNIFIED_PANEL_ELEMENT_ID
from hermes_lark_streaming.composer.engine import (
    FrameComposer,
    _split_native_composite,
)

from .test_v208_stream_quality import _FakeClient

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_SEP = "\n\n---\n"


def _make_composer(client: _FakeClient) -> FrameComposer:
    return FrameComposer(
        get_client=lambda: client,
        build_card=lambda **kw: build_streaming_card_v2(**kw),
        build_final_card=lambda **kw: {},
        config=None,
    )


class _FullClient(_FakeClient):
    """补齐 finalize 全链（batch_update / close_streaming）的 fake."""

    def __init__(self):
        super().__init__()
        self.batch_actions: list[list[dict]] = []
        self.close_calls = 0

    async def cardkit_batch_update(self, card_id, actions, sequence=0):
        self.batch_actions.append(actions)
        self.cards[card_id]["seq"] = sequence

    async def cardkit_close_streaming(self, card_id, sequence=0, *, summary=""):
        self.close_calls += 1
        self.cards[card_id]["seq"] = sequence


# ---------------------------------------------------------------------------
# 1. 复合帧拆分器（官方 _compose_frame_content 的逆变换）
# ---------------------------------------------------------------------------


class TestSplitNativeComposite:
    def test_plain_text(self):
        assert _split_native_composite("hello world") == ("hello world", [])

    def test_seed_frame(self):
        assert _split_native_composite("") == ("", [])

    def test_composite_tool_line(self):
        a, p = _split_native_composite(f"前奏文本{_SEP}🔍 Searching the web")
        assert a == "前奏文本"
        assert p == ["🔍 Searching the web"]

    def test_composite_multiple_lines(self):
        a, p = _split_native_composite(f"body{_SEP}🔍 Searching...\n💻 terminal")
        assert a == "body"
        assert p == ["🔍 Searching...", "💻 terminal"]

    def test_body_contains_hr_not_split(self):
        """正文自身含 markdown 分隔线且尾部非进度行 → 不拆."""
        text = "章节一\n\n---\n章节二"
        assert _split_native_composite(text) == (text, [])

    def test_body_trailing_hr_with_text(self):
        text = "para\n\n---\n\npara2 continues"
        assert _split_native_composite(text) == (text, [])

    def test_progress_only_frame(self):
        """accumulated 为空时官方 join 只留 progress（无分隔符）."""
        a, p = _split_native_composite("💬 thinking...")
        assert a == ""
        assert len(p) == 1

    def test_fenced_block_progress(self):
        """围栏命令块作为进度行整体拆出（split_progress_segments 保一体）."""
        block = "💻 运行命令\n```\nls -la\n```"
        a, p = _split_native_composite(f"answer{_SEP}{block}")
        assert a == "answer"
        assert p == [block]

    def test_short_answer_not_misrouted(self):
        """短正文非 emoji 开头 → 不误判为进度行."""
        assert _split_native_composite("好的") == ("好的", [])

    def test_never_raises(self):
        assert _split_native_composite(None) == ("", [])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 2. handle_stream_frame：种子 / 快照 / 单卡全回合 / 幂等定稿
# ---------------------------------------------------------------------------


class TestHandleStreamFrame:
    async def test_seed_frame_creates_card_without_answer(self):
        """种子帧（text=""）→ 建卡骨架，不写 answer 元素."""
        client = _FullClient()
        comp = _make_composer(client)
        chat = "oc_seed"
        ok = await comp.handle_stream_frame(chat, "", finalize=False)
        assert ok is True
        st = comp.active_state(chat)
        assert st is not None and st.answer_text == ""
        assert client._n == 1
        assert st.card_message_id

    async def test_snapshot_semantics_not_append(self):
        """native 帧是全量快照（cumulative）——直接赋值而非追加段落."""
        client = _FullClient()
        comp = _make_composer(client)
        chat = "oc_snap"
        await comp.handle_stream_frame(chat, "", finalize=False)
        await comp.handle_stream_frame(chat, "第一段", finalize=False)
        await comp.handle_stream_frame(chat, "第一段\n\n第二段（快照含前文）", finalize=False)
        st = comp.active_state(chat)
        assert st.answer_text == "第一段\n\n第二段（快照含前文）"
        assert client._n == 1, "快照帧不得建第二张卡"

    async def test_tool_boundary_keeps_single_card(self):
        """工具边界（官方吞掉 segment-break finalize）→ 同一张卡继续."""
        client = _FullClient()
        comp = _make_composer(client)
        chat = "oc_boundary"
        await comp.handle_stream_frame(chat, "", finalize=False)          # 种子
        await comp.handle_stream_frame(chat, "前奏文本", finalize=False)
        # 工具边界：官方对 edit-based 平台发 finalize=True（断卡根因），
        # native 下帧为 finalize=False 的复合/纯文本快照
        await comp.handle_stream_frame(
            chat, f"前奏文本{_SEP}🔍 Searching...", finalize=False)
        await comp.handle_stream_frame(
            chat, "前奏文本，现在开始检索", finalize=False)              # 工具后续流
        assert client._n == 1, "工具边界不得封卡/建新卡（单卡全回合）"
        st = comp.active_state(chat)
        assert st is not None and not st.finalized
        assert st.answer_text == "前奏文本，现在开始检索"
        assert st.status_lines == ["🔍 Searching..."], "复合帧工具行进面板"

    async def test_finalize_seals_and_is_idempotent(self):
        client = _FullClient()
        comp = _make_composer(client)
        chat = "oc_fin"
        await comp.handle_stream_frame(chat, "", finalize=False)
        await comp.handle_stream_frame(chat, "最终答案", finalize=True)
        st = comp._states[chat]
        assert st.finalized
        again = await comp.handle_stream_frame(chat, "最终答案", finalize=True)
        assert again is True  # 幂等短路（_finalize 守卫）

    async def test_finalize_then_new_frame_creates_new_card(self):
        """封卡后的下一内容帧＝新 turn → 新卡（clarify reopen 语义）."""
        client = _FullClient()
        comp = _make_composer(client)
        chat = "oc_turn2"
        await comp.handle_stream_frame(chat, "", finalize=False)
        await comp.handle_stream_frame(chat, "第一轮答案", finalize=True)
        ok = await comp.handle_stream_frame(chat, "", finalize=False)
        assert ok is True
        assert client._n == 2, "封卡后的新种子帧必须建新卡"
        st2 = comp.active_state(chat)
        assert st2 is not None and st2 is not comp._states.get(chat + "_old")

    async def test_finalize_without_active_card_creates_and_seals(self):
        """终帧无活跃卡（状态被清理/上轮封卡）→ 建卡再封（内容永不丢红线）."""
        client = _FullClient()
        comp = _make_composer(client)
        chat = "oc_orphan_fin"
        ok = await comp.handle_stream_frame(chat, "孤儿终值", finalize=True)
        assert ok is True
        st = comp._states[chat]
        assert st.finalized
        assert st.answer_text == "孤儿终值"

    async def test_progress_before_seed_staged_into_first_card(self):
        """进度行早于种子帧 → 暂存，建卡时并入首卡面板."""
        client = _FullClient()
        comp = _make_composer(client)
        chat = "oc_stage"
        await comp.handle_stream_frame(chat, "🔍 early tool line", finalize=False)
        await comp.handle_stream_frame(chat, "", finalize=False)  # 种子
        st = comp.active_state(chat)
        assert st is not None
        assert st.status_lines == ["🔍 early tool line"]
        assert client._n == 1

    async def test_degrade_on_client_failure_returns_false(self):
        """活跃卡更新失败 → handle_stream_frame False + state 降级（官方回落）。"""
        client = _FullClient()
        comp = _make_composer(client)
        chat = "oc_deg"
        await comp.handle_stream_frame(chat, "", finalize=False)  # 种子建卡成功
        st = comp.active_state(chat)
        assert st is not None

        async def _boom(*a, **kw):
            raise RuntimeError("cardkit down")

        client.cardkit_stream_element = _boom  # type: ignore[method-assign]
        ok = await comp.handle_stream_frame(chat, "快照", finalize=False)
        assert ok is False, "帧失败必须返回 False（官方禁 native 回落 edit/send）"
        st = comp._states.get(chat)
        assert st is not None and st.degraded and st.finalized

    async def test_create_failure_is_frame_level_not_permanent(self):
        """建卡失败（无 state）→ 帧级回落；下一帧可重试建卡（既有语义）。"""
        client = _FullClient()

        async def _boom(*a, **kw):
            raise RuntimeError("cardkit down")

        client.cardkit_create = _boom  # type: ignore[method-assign]
        comp = _make_composer(client)
        chat = "oc_deg2"
        assert await comp.handle_stream_frame(chat, "x", finalize=False) is False
        assert comp._states.get(chat) is None or not comp._states[chat].degraded
        # 恢复后下一帧重试建卡成功（实例属性函数不自动绑定 self）
        client.cardkit_create = lambda card: _FullClient.cardkit_create(client, card)
        assert await comp.handle_stream_frame(chat, "y", finalize=False) is True
        assert comp.active_state(chat) is not None


# ---------------------------------------------------------------------------
# 3. 适配器：能力声明 / probe / send_stream_frame 委托
# ---------------------------------------------------------------------------


class TestAdapterNativeSurface:
    def test_class_declares_native_streaming(self):
        from hermes_lark_streaming.plugin.adapter import FeishuStreamingAdapter

        assert getattr(FeishuStreamingAdapter, "SUPPORTS_NATIVE_STREAMING", False) is True
        assert hasattr(FeishuStreamingAdapter, "supports_native_streaming")
        assert hasattr(FeishuStreamingAdapter, "send_stream_frame")

    def _bare_adapter(self):
        """绕过 __init__（无 hermes 环境 fail-fast）的最小实例."""
        from hermes_lark_streaming.plugin.adapter import FeishuStreamingAdapter

        adapter = FeishuStreamingAdapter.__new__(FeishuStreamingAdapter)
        return adapter

    def test_probe_false_without_client(self):
        adapter = self._bare_adapter()
        adapter._card_config = None
        adapter._card_client = None
        adapter._app_id = ""
        adapter._app_secret = ""
        assert adapter.supports_native_streaming() is False

    def test_probe_true_with_client(self):
        adapter = self._bare_adapter()
        adapter._card_config = None
        adapter._card_client = object()  # 已初始化的 client
        assert adapter.supports_native_streaming(chat_type="dm", metadata={}) is True

    def test_probe_respects_config_switch(self):
        """feishu_streaming.native_streaming: false → probe False（灰度回退）."""
        adapter = self._bare_adapter()

        class _Cfg:
            native_streaming = False

        adapter._card_config = _Cfg()
        adapter._card_client = object()
        assert adapter.supports_native_streaming() is False

    async def test_send_stream_frame_delegates_to_composer(self):
        """adapter.send_stream_frame → composer.handle_stream_frame 端到端."""
        client = _FullClient()
        comp = _make_composer(client)
        adapter = self._bare_adapter()
        adapter._composer = comp
        chat = "oc_e2e"
        assert await adapter.send_stream_frame("", chat_id=chat, turn_id="t1") is True
        assert await adapter.send_stream_frame("正文", chat_id=chat, turn_id="t1") is True
        assert await adapter.send_stream_frame(
            f"正文{_SEP}🔍 tool", chat_id=chat, turn_id="t1") is True
        assert await adapter.send_stream_frame(
            "正文定稿", chat_id=chat, finalize=True, turn_id="t1") is True
        assert client._n == 1
        st = comp._states[chat]
        assert st.finalized and st.answer_text == "正文定稿"
        assert st.status_lines == ["🔍 tool"]

    async def test_send_stream_frame_requires_chat(self):
        adapter = self._bare_adapter()
        adapter._composer = None
        assert await adapter.send_stream_frame("x") is False

    async def test_send_stream_frame_swallows_exception(self):
        """composer 抛异常 → False（官方回落），永不向 consumer 上抛."""
        adapter = self._bare_adapter()

        class _Boom:
            async def handle_stream_frame(self, *a, **kw):
                raise RuntimeError("boom")

        adapter._composer = _Boom()
        assert await adapter.send_stream_frame("x", chat_id="oc_c") is False


# ---------------------------------------------------------------------------
# 4. P1-A：finalize 空面板壳删除
# ---------------------------------------------------------------------------


class _RecordingClient(_FullClient):
    """记录 batch_update actions 的 fake（P1-A 断言用；别名保留语义清晰）."""


class TestEmptyPanelRemoval:
    async def test_finalize_without_status_lines_deletes_panel(self):
        client = _RecordingClient()
        comp = _make_composer(client)
        chat = "oc_empty_panel"
        await comp.handle_stream_frame(chat, "", finalize=False)
        await comp.handle_stream_frame(chat, "纯答案", finalize=True)
        st = comp._states[chat]
        assert st.status_lines == []
        delete_calls = [
            actions for actions in client.batch_actions
            if any(
                a.get("action") == "delete_elements"
                and UNIFIED_PANEL_ELEMENT_ID in (a.get("params", {}).get("element_ids", []))
                for a in actions
            )
        ]
        assert delete_calls, "空面板必须在 finalize 时被删除"

    async def test_finalize_with_status_lines_keeps_panel(self):
        client = _RecordingClient()
        comp = _make_composer(client)
        chat = "oc_full_panel"
        await comp.handle_stream_frame(
            chat, f"答案前奏{_SEP}🔍 Searching...", finalize=False)
        await comp.handle_stream_frame(chat, "答案定稿", finalize=True)
        st = comp._states[chat]
        assert st.status_lines == ["🔍 Searching..."]
        panel_deletes = [
            actions for actions in client.batch_actions
            if any(
                a.get("action") == "delete_elements"
                and UNIFIED_PANEL_ELEMENT_ID in (a.get("params", {}).get("element_ids", []))
                for a in actions
            )
        ]
        assert not panel_deletes, "有过程记录的面板必须保留"
