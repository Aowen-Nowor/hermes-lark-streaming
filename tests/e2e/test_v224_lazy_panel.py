"""v2.0.24 回归测试 — Agent Loop 面板懒渲染（用户反馈：空面板体验差）.

用户观察：卡片出现时 "agent loop" 折叠面板即出现，即使该回合没有任何
思考推理或工具调用——空面板从建卡挂到定稿（v2.0.13 只在 finalize 删除）。

v2.0.24 懒渲染语义：
- cards.py：仅 status_lines 非空时渲染面板（纯 answer 回合建卡无面板）；
- engine.py：panel_on_card 旗标以建/更卡产物实测赋值（_card_has_panel），
  首个状态行到达时 _patch_status 全量更新补插面板（cardkit_update =
  card_json 全量替换，s172 真飞书探针实证更新时新增元素合法）；
- finalize.py：空面板删除分支加 panel_on_card 门控——正常路径不再对
  从未有过面板的卡发起 delete_elements，防御性保险保留。

降级红线不变：任何面板相关 API 失败仅 debug/warning，内容永不丢。
"""

from __future__ import annotations

import pytest
from hermes_lark_streaming.cardkit.cards import build_streaming_card_v2
from hermes_lark_streaming.cardkit.elements import UNIFIED_PANEL_ELEMENT_ID
from hermes_lark_streaming.composer import engine as _engine
from hermes_lark_streaming.composer.classify import CH_PROGRESS, ClassifyResult
from hermes_lark_streaming.composer.engine import FrameComposer, _card_has_panel

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


def _panel_in(card: dict) -> bool:
    return _card_has_panel(card)


def _panel_delete_calls(client: _FullClient) -> list[list[dict]]:
    return [
        actions for actions in client.batch_actions
        if any(
            a.get("action") == "delete_elements"
            and UNIFIED_PANEL_ELEMENT_ID in (a.get("params", {}).get("element_ids", []))
            for a in actions
        )
    ]


class TestCardHasPanelHelper:
    def test_body_elements(self):
        card = build_streaming_card_v2(status_lines=["🔍 Searching..."])
        assert _card_has_panel(card) is True

    def test_no_panel(self):
        card = build_streaming_card_v2(answer_text="纯答案")
        assert _card_has_panel(card) is False

    def test_malformed_never_raises(self):
        assert _card_has_panel({}) is False
        assert _card_has_panel({"body": None}) is False
        assert _card_has_panel({"body": {"elements": ["not-a-dict", 42]}}) is False

    def test_legacy_flat_elements(self):
        """非 body 嵌套的扁平 elements（防御性兼容）也能识别."""
        card = {"elements": [{"element_id": UNIFIED_PANEL_ELEMENT_ID, "tag": "collapsible_panel"}]}
        assert _card_has_panel(card) is True


class TestEngineLazyPanel:
    async def test_send_path_pure_answer_no_panel(self):
        """send 路径纯 answer 回合：建卡无面板、finalize 无删除调用."""
        client = _FullClient()
        comp = _make_composer(client)
        r = await comp._on_stream("oc_send_lazy", "直接回答")
        assert r is not None and r.success
        st = comp._states["oc_send_lazy"]
        assert st.panel_on_card is False
        assert not _panel_in(client.cards["1"]["card"])
        await comp._on_final_send("oc_send_lazy", "直接回答定稿")
        assert st.finalized and st.panel_on_card is False
        assert _panel_delete_calls(client) == []
        assert not _panel_in(client.cards["1"]["card"])

    async def test_seed_then_tool_line_adds_panel(self):
        """seed 无面板 → 首个状态行到达时全量更新补插面板（懒渲染核心）."""
        client = _FullClient()
        comp = _make_composer(client)
        chat = "oc_seed_then_tool"
        await comp.handle_stream_frame(chat, "开始回答", finalize=False)
        st = comp._states[chat]
        assert st.panel_on_card is False
        assert not _panel_in(client.cards["1"]["card"])
        # 复合快照帧携带首个进度行 → _absorb_status_lines → _patch_status
        await comp.handle_stream_frame(
            chat, f"开始回答{_SEP}🔍 Searching...", finalize=False)
        assert st.panel_on_card is True
        assert _panel_in(client.cards["1"]["card"]), "全量更新必须补插面板元素"
        # 定稿保留面板（有过程记录不删）
        await comp.handle_stream_frame(chat, "答案定稿", finalize=True)
        assert _panel_delete_calls(client) == []
        assert _panel_in(client.cards["1"]["card"])

    async def test_early_create_with_pending_has_panel(self):
        """early-create（卡片先现）：暂存行非空 → 首卡即带面板（有货不空）."""
        client = _FullClient()
        comp = _make_composer(client)
        chat = "oc_early_create"
        cls = ClassifyResult(
            CH_PROGRESS, kind="tool", tool_name="terminal",
            raw_content="💻 terminal：ls -la")
        r = await comp._on_progress(chat, cls)
        assert r is not None and r.success
        st = comp._states[chat]
        assert st.panel_on_card is True
        assert _panel_in(client.cards["1"]["card"]), "early-create 面板必须有状态行"

    async def test_watchdog_seal_pure_answer_no_delete(self):
        """看门狗 seal 纯 answer 孤儿卡：finalize_card 经旗标门控不删面板."""
        client = _FullClient()
        comp = _make_composer(client)
        chat = "oc_watchdog_lazy"
        await comp.handle_stream_frame(chat, "孤儿回答", finalize=False)
        st = comp._states[chat]
        assert st.panel_on_card is False
        # 回拨 last_activity 越过静默阈值（不真睡 120s）
        st.last_activity -= (_engine._WATCHDOG_IDLE_SEC + 1)
        await comp._watchdog_seal(chat)
        assert st.degraded and st.finalized
        assert _panel_delete_calls(client) == []


class TestFlagInvariant:
    async def test_flag_tracks_built_card(self):
        """旗标真值 = 每次 build_card 产物实测（不靠调用参数推断）."""
        client = _FullClient()
        comp = _make_composer(client)
        chat = "oc_flag_track"
        await comp._on_stream(chat, "答案")
        st = comp._states[chat]
        assert st.panel_on_card is False
        assert _panel_in(client.cards["1"]["card"]) is False
        await comp._on_progress(
            chat, ClassifyResult(CH_PROGRESS, kind="thinking", raw_content="💬 想一想"))
        assert st.panel_on_card is True
        assert _panel_in(client.cards["1"]["card"]) is True
