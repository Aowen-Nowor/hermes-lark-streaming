"""本地修复 审计修复回归（2026-11-17）.

对应修复：

- P1-1 纯文本回合（无工具调用/无流式帧）到达 final 时无活跃卡——
  此前 engine._on_final_send 返回 None → adapter 走 oneshot final 路径
  （build_gateway_card），show_reasoning=true 时推理块暴露在正文。
  修复：adapter 按入站事件类型区分普通回合 final 与命令 final；普通回合
  final 交 composer 建 turn 卡并走 _finalize，推理经 _extract_reasoning_block
  进折叠面板；命令回复与无锚点回合外通知保持 oneshot。
- P1-2 compat_probe S4 清单修正：移除插件自身 FeishuClient 方法
  （send_stream_frame / send_card_to_chat / reply_card_by_id /
  cardkit_create / cardkit_update / cardkit_stream_element），
  避免 0.23.2+ 误报不兼容。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_lark_streaming.composer.classify import CH_FINAL, ClassifyResult, classify_send
from hermes_lark_streaming.composer.engine import FrameComposer
from hermes_lark_streaming.plugin.adapter import FeishuStreamingAdapter

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


class _TurnFakeClient:
    """turn 卡建卡 + finalize 四步所需的最小 fake。"""

    def __init__(self):
        self.cards: dict[str, dict] = {}
        self.streamed: list[str] = []
        self.batches: list[list[dict]] = []
        self.close_calls = 0

    async def cardkit_create(self, card):
        card_id = "card_v236"
        self.cards[card_id] = {"card": card}
        return card_id

    async def send_card_by_id_to_chat(self, chat_id, card_id):
        return "om_v236"

    async def reply_card_by_id(self, anchor, card_id):
        return "om_v236"

    async def cardkit_stream_element(self, card_id, element_id, content, *, sequence=0):
        self.streamed.append(content)

    async def cardkit_update(self, card_id, card, *, sequence=0):
        self.cards[card_id]["card"] = card

    async def cardkit_batch_update(self, card_id, actions, *, sequence=0):
        self.batches.append(actions)

    async def cardkit_close_streaming(self, card_id, sequence=0, *, summary=""):
        self.close_calls += 1


def _composer(client):
    return FrameComposer(
        get_client=lambda: client,
        build_card=lambda **kwargs: {},
        build_final_card=lambda **kwargs: {},
    )


class TestToollessFinalTurnCard:
    async def test_toolless_final_creates_turn_card_and_relocates_reasoning(self):
        client = _TurnFakeClient()
        composer = _composer(client)
        content = "💭 **Reasoning:**\n```\n先分析再回答\n```\n\n正文答案"

        result = await composer.handle_send(
            "oc_v236_toolless", content, "om_user", {"notify": True},
            ClassifyResult(CH_FINAL, kind="final", raw_content=content),
        )

        assert result is not None and result.success
        state = composer._states["oc_v236_toolless"]
        assert state.card_id == "card_v236"
        assert state.finalized is True
        assert state.answer_text == "正文答案"
        assert any("先分析再回答" in line for line in state.status_lines)
        assert client.close_calls == 1
        # 纯文本回合建卡时 panel_on_card=False → 定稿只 add 面板，不得 delete
        reseal_batches = [b for b in client.batches if any(a.get("action") == "add_elements" for a in b)]
        assert reseal_batches, "定稿应补插面板"
        for batch in reseal_batches:
            assert not any(a.get("action") == "delete_elements" for a in batch), (
                "面板不存在时不得 delete（300314）"
            )
        assert state.panel_on_card is True

    async def test_toolless_final_without_reasoning_still_creates_turn_card(self):
        client = _TurnFakeClient()
        composer = _composer(client)
        content = "普通正文，无推理块"

        result = await composer.handle_send(
            "oc_v236_plain", content, "om_user", {"notify": True},
            ClassifyResult(CH_FINAL, kind="final", raw_content=content),
        )

        assert result is not None and result.success
        state = composer._states["oc_v236_plain"]
        assert state.card_id == "card_v236"
        assert state.answer_text == content


class _OneshotFakeClient:
    def __init__(self):
        self.sent_cards: list[dict] = []

    async def send_card_to_chat(self, chat_id, card):
        self.sent_cards.append(card)
        return "om_oneshot"

    async def reply_card(self, anchor, card):
        self.sent_cards.append(card)
        return "om_oneshot"


def _bare_adapter(client, composer):
    adapter = FeishuStreamingAdapter.__new__(FeishuStreamingAdapter)
    adapter._card_config = SimpleNamespace(oneshot_cards=True)
    adapter._card_client = client
    adapter._composer = composer
    adapter._inbound_event_kind = {}
    return adapter


class TestFinalRouting:
    def test_message_kind_accepts_enum_and_string(self):
        from hermes_lark_streaming.plugin.adapter import _message_kind

        class _CommandType:
            value = "COMMAND"

        class _TextType:
            value = "TEXT"

        assert _message_kind(SimpleNamespace(message_type=_CommandType())) == "command"
        assert _message_kind(SimpleNamespace(message_type=_TextType())) == "message"
        assert _message_kind(SimpleNamespace(message_type="command")) == "command"
        assert _message_kind(SimpleNamespace(message_type="text")) == "message"
        assert _message_kind(SimpleNamespace()) == "message"

    async def test_final_with_message_anchor_not_oneshot(self):
        """普通文本回合 final（锚点来源为 message）不转 oneshot，交 composer。"""
        adapter = _bare_adapter(_OneshotFakeClient(), SimpleNamespace(active_state=lambda chat: None))
        adapter._inbound_event_kind = {"om_user": ("message", 0.0)}
        content = "正文"
        meta = {"notify": True}
        result = await adapter._send_oneshot_card(
            "oc_v236", content, meta, classify_send(content, meta), reply_to="om_user",
        )
        assert result is None

    async def test_final_with_command_anchor_stays_oneshot(self):
        """命令回复 final（锚点来源为 command）保持 oneshot 静态卡。"""
        client = _OneshotFakeClient()
        adapter = _bare_adapter(client, SimpleNamespace(active_state=lambda chat: None))
        adapter._inbound_event_kind = {"om_cmd": ("command", 0.0)}
        content = "命令输出"
        meta = {"notify": True}
        result = await adapter._send_oneshot_card(
            "oc_v236", content, meta, classify_send(content, meta), reply_to="om_cmd",
        )
        assert result is not None and result.success
        assert len(client.sent_cards) == 1

    async def test_final_without_anchor_stays_oneshot(self):
        """无锚点回合外 final 通知保持 oneshot。"""
        client = _OneshotFakeClient()
        adapter = _bare_adapter(client, SimpleNamespace(active_state=lambda chat: None))
        content = "out-of-band notice"
        meta = {"notify": True}
        result = await adapter._send_oneshot_card(
            "oc_v236", content, meta, classify_send(content, meta),
        )
        assert result is not None and result.success
        assert len(client.sent_cards) == 1
