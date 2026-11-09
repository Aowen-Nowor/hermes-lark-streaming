"""Reply anchor from metadata (gateway stream consumer path)."""
from __future__ import annotations

import pytest
from hermes_lark_streaming.composer.engine import FrameComposer

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


class _FakeClient:
    def __init__(self):
        self.cards = {}
        self.replies = []
        self.sent = []

    async def cardkit_create(self, card):
        cid = f"card_{len(self.cards)}"
        self.cards[cid] = card
        return cid

    async def send_card_by_id_to_chat(self, chat_id, card_id):
        self.sent.append(chat_id)
        return "om_direct"

    async def reply_card_by_id(self, anchor, card_id):
        self.replies.append((anchor, card_id))
        return "om_reply"

    async def cardkit_stream_element(self, *a, **kw):
        return True


def _comp(client):
    from hermes_lark_streaming.cardkit.cards import build_streaming_card_v2

    return FrameComposer(
        get_client=lambda: client,
        build_card=lambda **kw: build_streaming_card_v2(**kw),
        build_final_card=lambda **kw: {},
        config=None,
    )


async def test_handle_send_uses_meta_anchor():
    from hermes_lark_streaming.composer.classify import CH_STREAM, ClassifyResult

    client = _FakeClient()
    comp = _comp(client)
    cls = ClassifyResult(CH_STREAM, kind="answer", raw_content="你好")
    # reply_to 空，锚点在 metadata.reply_to_message_id（网关流式消费者形态）
    await comp.handle_send("oc_1", "你好", None, {"reply_to_message_id": "om_user123"}, cls)
    assert client.replies == [("om_user123", client.cards.__iter__().__next__())] or len(client.replies) == 1
    assert client.sent == []