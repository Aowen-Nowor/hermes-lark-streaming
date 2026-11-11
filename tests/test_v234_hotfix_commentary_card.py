"""v2.0.34-hotfix 回归测试：对话性 commentary 无活跃卡 → early-create 回合卡.

用户症状①（logs11.11 22:33 轮三方互证）：turn 首段前导文本经 hermes
commentary 通道独立投递（gateway _send_commentary → adapter.send(
reply_to=触发锚, _interim_send)），delta 流式停止的回合里它先于首个工具
进度行到达 → 无活跃卡 → 原样直发 = 聊天里独立的"第一条文本消息"。

修复（主链，无兜底）：adapter._send_impl 在锚点归一前用原始 reply_to 形参
给 CH_INTERIM advisory 打 conversational_commentary 标（官方语义级区分：
commentary 带 reply_to 形参、后台状态通知 _send_status_text 不带）；
engine._on_interim 对带标无卡帧 early-create 回合卡（与 _on_progress
占位建卡同款），commentary 本体按 V1 语义并入面板 💬 思考行，final send
全量覆盖正文收口同一张卡。💾/⏳/⚠️ 生命周期通知（无锚）维持原生直发
——跑马灯红线（v2.0.8/v2.0.34）不动。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from hermes_lark_streaming.composer.classify import (
    CH_INTERIM,
    ClassifyResult,
    classify_send,
)
from hermes_lark_streaming.composer.engine import FrameComposer
from hermes_lark_streaming.plugin.adapter import FeishuStreamingAdapter

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


class _FakeClient:
    def __init__(self):
        self.cards: dict[str, dict] = {}
        self.created = 0
        self.replies: list[str] = []
        self.direct: list[str] = []

    async def cardkit_create(self, card):
        self.created += 1
        cid = str(self.created)
        self.cards[cid] = {"card": card, "seq": 0}
        return cid

    async def reply_card_by_id(self, anchor, card_id):
        self.replies.append(anchor)
        return f"om_r{card_id}"

    async def send_card_by_id_to_chat(self, chat_id, card_id):
        self.direct.append(chat_id)
        return f"om_{card_id}"

    async def cardkit_stream_element(self, card_id, element_id, content, sequence=0):
        self.cards[card_id]["seq"] = sequence

    async def cardkit_update(self, card_id, card, sequence=0):
        self.cards[card_id]["card"] = card
        self.cards[card_id]["seq"] = sequence


def _composer_with(client: _FakeClient) -> FrameComposer:
    return FrameComposer(
        get_client=lambda: client,
        build_card=lambda **kw: __import__(
            "hermes_lark_streaming.cardkit.cards", fromlist=["build_streaming_card_v2"]
        ).build_streaming_card_v2(**kw),
        build_final_card=lambda **kw: {},
        config=None,
    )


def _panel_text(client: _FakeClient, card_id: str) -> str:
    """最新卡片视图里面板全文（含 i18n zh），供 💬 行断言."""
    card = client.cards[card_id]["card"]
    parts: list[str] = []

    def _walk(node):
        if isinstance(node, dict):
            t = node.get("text")
            if isinstance(t, dict):
                parts.append(str(t.get("content") or ""))
                i18n = t.get("i18n_content")
                if isinstance(i18n, dict):
                    parts.append(str(i18n.get("zh_cn") or ""))
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    _walk(card)
    return "\n".join(p for p in parts if p)


def _marked_commentary_cls(text: str) -> ClassifyResult:
    """生产形态：commentary 经 classify → CH_INTERIM advisory，adapter 层已打标."""
    cls = classify_send(text, {"_interim_send": True})
    assert cls.channel == CH_INTERIM and cls.kind == "advisory"
    cls.meta["conversational_commentary"] = True
    return cls


class TestCommentaryEarlyCreate:
    @pytest.mark.asyncio
    async def test_marked_commentary_no_card_creates_card_and_panels_text(self):
        """带标 commentary 无活跃卡 → early-create 建卡，文本并入面板 💬 行."""
        client = _FakeClient()
        composer = _composer_with(client)
        chat = "oc_cm"
        text = "执行。先看 direction map 依赖，再写 pipeline："

        result = await composer.handle_send(
            chat, text, "om_trigger_anchor", {"_interim_send": True},
            _marked_commentary_cls(text))

        assert client.created == 1, "无卡 commentary 必须建回合卡"
        assert client.replies == ["om_trigger_anchor"], "以触发消息为引用锚发卡"
        st = composer.active_state(chat)
        assert st is not None, "建卡后状态必须活跃（后续 final 收口同一张卡）"
        assert st.card_message_id == result.message_id
        assert st.answer_text == "", "正文留空待 final send 全量覆盖"
        panel = _panel_text(client, st.card_id)
        assert text in panel, "commentary 文本必须并入面板（灰色思考行）"
        assert "grey" in panel, "V1 语义：灰色 notation 行"
        assert "Round 1" in panel or "第 1 轮" in panel, "思考轮次分组标题存在"

    @pytest.mark.asyncio
    async def test_unmarked_notice_no_card_stays_native(self):
        """无标记后台通知（💾/⏳ 等无锚 advisory）无卡 → 原生直发不建卡（跑马灯红线）."""
        client = _FakeClient()
        composer = _composer_with(client)
        chat = "oc_nt"
        notice = "💾 Self-improvement review: session updated"
        cls = classify_send(notice, {"_interim_send": True})
        assert cls.channel == CH_INTERIM and cls.kind == "advisory"
        assert not cls.meta.get("conversational_commentary")

        result = await composer.handle_send(chat, notice, None, {"_interim_send": True}, cls)
        assert result is None, "通知类无卡必须 return None（原生直发路径）"
        assert client.created == 0, "后台通知永不建卡"

    @pytest.mark.asyncio
    async def test_marked_commentary_then_progress_then_final_one_card(self):
        """端到端时序：commentary 建卡 → 工具行并入同卡面板 → final 覆盖正文收口."""
        client = _FakeClient()
        composer = _composer_with(client)
        chat = "oc_seq"
        preamble = "执行。先看 direction map 依赖，再写 pipeline："

        await composer.handle_send(
            chat, preamble, "om_t1", {"_interim_send": True}, _marked_commentary_cls(preamble))
        st1 = composer.active_state(chat)
        card_id = st1.card_id

        # 工具进度行（生产为 _on_progress 通道）并入同一张卡
        from hermes_lark_streaming.composer.classify import CH_PROGRESS
        tool_cls = ClassifyResult(CH_PROGRESS, kind="tool", tool_name="terminal",
                                  raw_content='💻 运行命令 pip list')
        await composer.handle_send(chat, '💻 运行命令 pip list', None, None, tool_cls)
        assert composer.active_state(chat).card_id == card_id, "工具行必须并入 commentary 建的同一张卡"

        # final send 全量覆盖正文收口（show_reasoning 预影由 _finalize 剥离）
        final_cls = classify_send("最终回复正文", {"notify": True})
        assert final_cls.channel == "final"
        await composer.handle_send(chat, "最终回复正文", None, {"notify": True}, final_cls)
        st = composer.active_state(chat)
        assert st is None or st.finalized, "final 后卡片收口"
        assert client.created == 1, "整个回合只建一张卡"


class TestAdapterConversationalMark:
    """adapter._send_impl 的原始 reply_to 形参判定（锚点归一前）."""

    def _adapter_with_composer(self, composer):
        adapter = FeishuStreamingAdapter.__new__(FeishuStreamingAdapter)
        adapter._card_config = SimpleNamespace(oneshot_cards=True)
        adapter._card_client = _FakeClient()
        adapter._composer = composer

        sent = []

        async def _native_send(chat_id, content, reply_to=None, metadata=None):
            sent.append((chat_id, content, reply_to, metadata))
            return SimpleNamespace(success=True, message_id="om_native")

        adapter._native_send = _native_send  # type: ignore[attr-defined]
        return adapter, sent

    @pytest.mark.asyncio
    async def test_reply_to_marks_advisory_conversational(self):
        """带原始 reply_to 形参的 advisory → 打标 → _send_impl 走 early-create 建卡."""
        client = _FakeClient()
        composer = _composer_with(client)
        adapter, sent = self._adapter_with_composer(composer)
        text = "先看依赖，再写 pipeline："
        await adapter._send_impl("oc_m1", text, "om_anchor", {"_interim_send": True})
        assert client.created == 1, "带锚 advisory 必须触发 early-create（标记生效）"
        assert sent == [], "建卡成功不得回落原生文本路径"

    @pytest.mark.asyncio
    async def test_no_reply_to_unmarked_goes_native(self):
        """无 reply_to 形参的后台通知（v2.0.8 清单内）→ 不打标 → 不建卡走原生.

        测试环境无 hermes（占位基类无 send），super().send 原生调用不 mock；
        原生路径本身由 composer 层 test_unmarked_notice_no_card_stays_native
        与生产 log（oneshot/panel 均无踪迹）覆盖，此处验证"永不建卡"红线。"""
        client = _FakeClient()
        composer = _composer_with(client)
        adapter, _sent = self._adapter_with_composer(composer)
        text = "💾 Self-improvement review: session summary updated"
        with pytest.raises(AttributeError):
            # 占位基类无 send —— 原生路径的 AttributeError 即"未走 composer"的证据
            await adapter._send_impl("oc_m2", text, None, {"_interim_send": True})
        assert client.created == 0, "清单内后台通知永不建卡"
