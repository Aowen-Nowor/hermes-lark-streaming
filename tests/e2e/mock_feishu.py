"""Mock Feishu API server — simulates CardKit v2 + IM API endpoints.

Records all API calls for later assertion. Does NOT actually serve
cards to real users — just returns success responses and stores
the card/element data for test verification.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any
from dataclasses import dataclass, field

_logger = logging.getLogger("hermes_lark_streaming")


@dataclass
class MockCardState:
    """Tracks the state of a single mock card."""
    card_id: str
    card_json: dict[str, Any] = field(default_factory=dict)
    elements: dict[str, dict[str, Any]] = field(default_factory=dict)  # element_id → element data
    streaming_mode: bool = True
    sequence: int = 0
    summary: str = ""
    created_at: float = field(default_factory=time.time)
    closed_at: float | None = None
    actions_log: list[dict[str, Any]] = field(default_factory=list)  # all batch_update/stream_element calls


class MockFeishuServer:
    """In-memory mock of the Feishu CardKit + IM API.

    No actual HTTP server — the MockFeishuClient calls methods directly.
    This avoids port conflicts and network overhead in tests.
    """

    def __init__(self) -> None:
        self._cards: dict[str, MockCardState] = {}  # card_id → state
        self._next_card_id: int = 1
        self._next_msg_id: int = 1
        self._call_log: list[dict[str, Any]] = []

    def reset(self) -> None:
        """Clear all state — call between tests."""
        self._cards.clear()
        self._call_log.clear()

    @property
    def call_log(self) -> list[dict[str, Any]]:
        """Return all API calls made, in order."""
        return self._call_log

    def get_card(self, card_id: str) -> MockCardState | None:
        return self._cards.get(card_id)

    def get_cards(self) -> dict[str, MockCardState]:
        return self._cards

    # ── CardKit API simulation ──

    async def cardkit_create(self, card: dict[str, Any]) -> str:
        card_id = f"mock_card_{self._next_card_id}"
        self._next_card_id += 1
        state = MockCardState(card_id=card_id, card_json=card)
        # Extract elements from card JSON — elements are in card["body"]["elements"]
        # (CardKit v2 card structure) or card["elements"] (IM fallback card)
        body = card.get("body", {})
        elements = body.get("elements", []) if isinstance(body, dict) else []
        if not elements:
            elements = card.get("elements", [])
        for element in elements:
            eid = element.get("element_id", "")
            if eid:
                state.elements[eid] = element
        self._cards[card_id] = state
        self._call_log.append({"op": "cardkit_create", "card_id": card_id, "card": card})
        _logger.debug("MockFeishu: cardkit_create → %s", card_id)
        return card_id

    async def reply_card_by_id(self, message_id: str, card_id: str) -> str:
        msg_id = f"om_mock_{self._next_msg_id}"
        self._next_msg_id += 1
        self._call_log.append({"op": "reply_card_by_id", "message_id": message_id, "card_id": card_id, "msg_id": msg_id})
        return msg_id

    async def reply_card(self, message_id: str, card: dict[str, Any]) -> str:
        msg_id = f"om_mock_{self._next_msg_id}"
        self._next_msg_id += 1
        self._call_log.append({"op": "reply_card", "message_id": message_id, "msg_id": msg_id})
        return msg_id

    async def cardkit_stream_element(
        self, card_id: str, element_id: str, content: str, *, sequence: int = 0,
    ) -> None:
        state = self._cards.get(card_id)
        if state is None:
            raise RuntimeError(f"cardkit_stream_element: card {card_id} not found")
        if not state.streaming_mode:
            raise RuntimeError("cardkit_stream_element: streaming mode closed (300309)")
        if element_id not in state.elements:
            raise RuntimeError(f"cardkit_stream_element: element {element_id} not found (300313)")
        # Update element content
        state.elements[element_id]["content"] = content
        state.sequence = sequence
        state.actions_log.append({"op": "stream_element", "element_id": element_id, "content": content, "seq": sequence})
        self._call_log.append({"op": "cardkit_stream_element", "card_id": card_id, "element_id": element_id, "content_len": len(content), "seq": sequence})

    async def cardkit_batch_update(
        self, card_id: str, actions: list[dict[str, Any]], *, sequence: int = 0,
    ) -> None:
        state = self._cards.get(card_id)
        if state is None:
            raise RuntimeError(f"cardkit_batch_update: card {card_id} not found")
        if not state.streaming_mode and any(a["action"] != "partial_update_element" for a in actions):
            # Allow partial_update after close, but not add/delete
            pass  # Be lenient for testing
        for action in actions:
            op = action.get("action")
            params = action.get("params", {})
            if op == "add_elements":
                elements = params.get("elements", [])
                for el in elements:
                    eid = el.get("element_id", "")
                    if eid:
                        state.elements[eid] = el
            elif op == "delete_elements":
                for eid in params.get("element_ids", []):
                    state.elements.pop(eid, None)
            elif op == "partial_update_element":
                eid = params.get("element_id")
                partial = params.get("partial_element", {})
                if eid and eid in state.elements:
                    state.elements[eid].update(partial)
            state.actions_log.append({"op": op, "params": params, "seq": sequence})
        state.sequence = sequence
        self._call_log.append({"op": "cardkit_batch_update", "card_id": card_id, "actions": actions, "seq": sequence})

    async def cardkit_close_streaming(
        self, card_id: str, *, sequence: int = 0, summary: str = "",
    ) -> None:
        state = self._cards.get(card_id)
        if state is None:
            raise RuntimeError(f"cardkit_close_streaming: card {card_id} not found")
        state.streaming_mode = False
        state.closed_at = time.time()
        state.summary = summary
        state.sequence = sequence
        self._call_log.append({"op": "cardkit_close_streaming", "card_id": card_id, "seq": sequence, "summary": summary})

    async def cardkit_update_summary(self, card_id: str, summary: str, *, sequence: int = 0) -> None:
        state = self._cards.get(card_id)
        if state:
            state.summary = summary
        self._call_log.append({"op": "cardkit_update_summary", "card_id": card_id, "summary": summary})

    async def cardkit_update(self, card_id: str, card: dict[str, Any], *, sequence: int = 0) -> None:
        state = self._cards.get(card_id)
        if state:
            state.card_json = card
            body = card.get("body", {})
            elements = body.get("elements", []) if isinstance(body, dict) else []
            if not elements:
                elements = card.get("elements", [])
            for element in elements:
                eid = element.get("element_id", "")
                if eid:
                    state.elements[eid] = element
        self._call_log.append({"op": "cardkit_update", "card_id": card_id, "seq": sequence})

    async def update_card(self, message_id: str, card: dict[str, Any]) -> None:
        self._call_log.append({"op": "update_card", "message_id": message_id})

    async def send_card_to_chat(self, chat_id: str, card: dict[str, Any]) -> str:
        msg_id = f"om_mock_{self._next_msg_id}"
        self._next_msg_id += 1
        self._call_log.append({"op": "send_card_to_chat", "chat_id": chat_id, "msg_id": msg_id})
        return msg_id

    async def reply_text(self, message_id: str, text: str) -> str:
        msg_id = f"om_mock_{self._next_msg_id}"
        self._next_msg_id += 1
        self._call_log.append({"op": "reply_text", "message_id": message_id, "msg_id": msg_id, "text": text[:50]})
        return msg_id
