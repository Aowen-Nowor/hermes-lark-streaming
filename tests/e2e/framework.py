"""E2E test framework — orchestrates full message → card pipeline tests.

v1.1.0: Single runner with automatic mock/real switching.

When FEISHU_E2E_APP_ID + FEISHU_E2E_APP_SECRET + FEISHU_E2E_CHAT_ID
are all set, the runner uses a REAL FeishuClient and creates actual
cards in your Feishu app. Otherwise, it uses the in-memory
MockFeishuServer.

The runner is FULLY AUTOMATIC in real mode — no manual message_id
setup needed. It sends a text message to the test chat to obtain an
anchor message_id, then creates cards as replies to that anchor.

Test code is IDENTICAL in both modes — the only difference is whether
API calls hit the mock or real Feishu servers.

Environment variables (3 required for real mode):
  FEISHU_E2E_APP_ID       — Real Feishu app_id
  FEISHU_E2E_APP_SECRET   — Real Feishu app_secret
  FEISHU_E2E_CHAT_ID      — Real chat_id (test group where cards appear)

Optional:
  FEISHU_E2E_BASE_URL     — Feishu API base URL (default: https://open.feishu.cn/open-apis)

Usage:
    from tests.e2e.framework import E2ETestRunner

    async def test_simple_answer():
        runner = E2ETestRunner()
        await runner.setup()
        try:
            session = await runner.start_message("hello")
            await runner.feed_answer(session, "Hello world!")
            await runner.complete(session)
            assert "Hello world!" in runner.get_answer_text(session)
        finally:
            await runner.teardown()

    # Check mode:
    if runner.is_real_mode:
        print("Testing against real Feishu API")
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from .mock_feishu import MockFeishuServer, MockCardState

_logger = logging.getLogger("hermes_lark_streaming")


def _has_real_feishu_creds() -> bool:
    """Check if real Feishu credentials are available in environment.

    v1.1.1: 4 variables required: app_id, app_secret, chat_id, open_id.
    chat_id 用于群聊测试，open_id 用于私聊测试，两个都需要。
    """
    return bool(
        os.environ.get("FEISHU_E2E_APP_ID")
        and os.environ.get("FEISHU_E2E_APP_SECRET")
        and os.environ.get("FEISHU_E2E_CHAT_ID")
        and os.environ.get("FEISHU_E2E_OPEN_ID")
    )


class E2ETestRunner:
    """Orchestrates end-to-end tests.

    Automatically selects mock or real Feishu mode based on environment:
    - FEISHU_E2E_APP_ID + FEISHU_E2E_APP_SECRET + FEISHU_E2E_CHAT_ID all set
      → real Feishu API (FULLY AUTOMATIC, no manual message_id needed)
    - Otherwise → in-memory MockFeishuServer

    In real mode, the runner sends a text message to the test chat to
    obtain an anchor message_id, then creates cards as replies. Tests
    create ACTUAL cards visible in your Feishu app. Use a dedicated
    test bot and test group, NOT your production bot.

    How to get the 3 required values (see tests/e2e/.env.example for details):
      1. FEISHU_E2E_APP_ID / FEISHU_E2E_APP_SECRET:
         Feishu open platform → your app → Credentials & Basic Info
      2. FEISHU_E2E_CHAT_ID:
         Add bot to a test group, then use the bot's webhook event
         payload, or call GET /open-apis/im/v1/chats to list groups
    """

    def __init__(self) -> None:
        self.mock_server = MockFeishuServer()
        self._patches: list[Any] = []
        self._controller: Any = None
        self._sessions: dict[str, Any] = {}
        self._use_real_feishu: bool = _has_real_feishu_creds()
        # Real mode: capture card states from API responses (can't inspect server-side)
        self._real_card_states: dict[str, MockCardState] = {}
        # Real mode: anchor message_id obtained by sending a text message
        self._real_anchor_message_id: str = ""
        # Real mode: counter for unique message_ids (avoids reusing the same anchor)
        self._real_msg_counter: int = 0
        # v1.1.1: 封卡前缓存数据（封卡后 unified_state 被释放）
        self._test_cache: dict[str, dict] = {}
        # v1.2.0: 真飞书模式下捕获传入飞书的 card JSON（验证 header 等字段）
        # mock 模式下用 mock_server.call_log 即可，不需要此捕获
        self._captured_create_card: dict[str, Any] | None = None
        self._captured_update_cards: list[dict[str, Any]] = []

    @property
    def is_real_mode(self) -> bool:
        """True if running against real Feishu API."""
        return self._use_real_feishu

    async def setup(self) -> None:
        """Set up test environment — mock or real, decided by env vars."""
        from hermes_lark_streaming.controller import StreamCardController
        from hermes_lark_streaming.config import Config

        self._controller = StreamCardController()

        # Force-enable with mock config object
        self._controller._cfg = MagicMock(spec=Config)
        cfg = self._controller._cfg
        cfg.enabled = True
        cfg.linear = True
        cfg.gateway_cards = True
        cfg.flush_interval_ms = 50  # Fast for tests
        cfg.flush_interval_sec = 0.05
        cfg.card_duration_sec = 600
        cfg.print_strategy = "fast"
        cfg.print_step = 4
        cfg.show_reasoning = True
        cfg.panel_expanded = False
        cfg.streaming_panel_expanded = False
        cfg.max_tool_steps = 20
        cfg.max_reasoning_rounds = 20
        cfg.footer_fields = [["status", "elapsed", "model", "cost"]]
        cfg.footer_show_label = False

        if self._use_real_feishu:
            await self._setup_real_client(cfg)
        else:
            self._setup_mock_client(cfg)

        self._controller._ensure_init = AsyncMock()

    def _setup_mock_client(self, cfg: Any) -> None:
        """Configure controller with mock FeishuClient."""
        cfg.feishu_app_id = "mock_app_id"
        cfg.feishu_app_secret = "mock_app_secret"
        cfg.feishu_base_url = "https://mock.feishu.cn"
        cfg.env_app_id = ""
        cfg.env_app_secret = ""

        self._controller._client = self._create_mock_client()
        self._controller._initialized = True
        _logger.info("HLS e2e: using MOCK Feishu server")

    async def _setup_real_client(self, cfg: Any) -> None:
        """Configure controller with REAL FeishuClient.

        v1.1.1: 同时支持 chat_id（群聊）和 open_id（私聊）。
        自动发 anchor 消息到群聊获取 message_id。

        Automatically sends a text message to the test chat to obtain
        an anchor message_id. Cards created during tests will be replies
        to this anchor message.
        """
        from hermes_lark_streaming.feishu import FeishuClient, FeishuClientConfig

        app_id = os.environ["FEISHU_E2E_APP_ID"]
        app_secret = os.environ["FEISHU_E2E_APP_SECRET"]
        chat_id = os.environ["FEISHU_E2E_CHAT_ID"]
        open_id = os.environ.get("FEISHU_E2E_OPEN_ID", "")
        base_url = os.environ.get(
            "FEISHU_E2E_BASE_URL", "https://open.feishu.cn/open-apis"
        )

        cfg.feishu_app_id = app_id
        cfg.feishu_app_secret = app_secret
        cfg.feishu_base_url = base_url
        cfg.env_app_id = app_id
        cfg.env_app_secret = app_secret

        # 保存 chat_id 和 open_id 供测试使用
        self._real_chat_id = chat_id
        self._real_open_id = open_id

        # Create real FeishuClient
        client = FeishuClient(FeishuClientConfig(
            app_id=app_id,
            app_secret=app_secret,
            base_url=base_url,
        ))
        self._controller._client = client
        self._controller._initialized = True

        # ── Auto-obtain anchor message_id ──
        # Send a text message to the test chat. The plugin's card creation
        # uses reply_card_by_id(anchor_message_id, card_id), so we need a
        # real message to reply to.
        try:
            self._real_anchor_message_id = await self._send_anchor_message(
                client, chat_id
            )
            _logger.info(
                "HLS e2e: REAL Feishu API ready — anchor message sent "
                "(app_id=%s..., chat=%s, open_id=%s, anchor_msg=%s)",
                app_id[:8], chat_id[:12], open_id[:12] if open_id else "(none)",
                self._real_anchor_message_id[:12],
            )
        except Exception as e:
            raise RuntimeError(
                f"E2E: 发送群聊 anchor 消息失败 chat={chat_id[:12]}。"
                f"请检查 bot 是否在群内且有发送权限。"
                f"错误: {e}"
            ) from e

        # v1.2.0: wrap cardkit_create / cardkit_update 捕获传入的 card JSON
        # 用于真飞书模式下验证 header 等字段（真飞书无法回查卡片内容）
        self._wrap_client_to_capture_cards(client)

    async def _send_anchor_message(self, client: Any, chat_id: str) -> str:
        """Send a text message to the test chat, return its message_id.

        This message serves as the reply anchor for test cards.
        Uses the IM create message API (not reply).
        """
        import json
        import time
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest,
            CreateMessageRequestBody,
        )

        text = f"[e2e test anchor {time.strftime('%H:%M:%S')}]"
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("text")
                .content(json.dumps({"text": text}))
                .build()
            )
            .build()
        )
        resp = await client._client.im.v1.message.acreate(request)
        if not resp.success():
            raise RuntimeError(
                f"发送 anchor 消息失败: code={resp.code} msg={resp.msg}"
            )
        if not resp.data or not resp.data.message_id:
            raise RuntimeError("发送 anchor 消息失败: 响应缺少 message_id")
        return str(resp.data.message_id)

    async def _send_private_anchor(self, client: Any, open_id: str) -> str:
        """v1.1.1: 发送私聊 anchor 消息，返回 message_id.

        和群聊 anchor 不同，私聊用 receive_id_type=open_id。
        """
        import json
        import time
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest,
            CreateMessageRequestBody,
        )

        text = f"[e2e private anchor {time.strftime('%H:%M:%S')}]"
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("open_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(open_id)
                .msg_type("text")
                .content(json.dumps({"text": text}))
                .build()
            )
            .build()
        )
        resp = await client._client.im.v1.message.acreate(request)
        if not resp.success():
            raise RuntimeError(
                f"发送私聊 anchor 消息失败: code={resp.code} msg={resp.msg}"
            )
        if not resp.data or not resp.data.message_id:
            raise RuntimeError("发送私聊 anchor 消息失败: 响应缺少 message_id")
        return str(resp.data.message_id)

    def _create_mock_client(self) -> Any:
        """Create a mock FeishuClient that delegates to MockFeishuServer."""
        mock_client = MagicMock()
        srv = self.mock_server

        async def _cardkit_create(card):
            return await srv.cardkit_create(card)
        async def _reply_card_by_id(mid, cid):
            return await srv.reply_card_by_id(mid, cid)
        async def _reply_card(mid, card):
            return await srv.reply_card(mid, card)
        async def _cardkit_stream_element(cid, eid, content, *, sequence=0):
            return await srv.cardkit_stream_element(cid, eid, content, sequence=sequence)
        async def _cardkit_batch_update(cid, actions, *, sequence=0):
            return await srv.cardkit_batch_update(cid, actions, sequence=sequence)
        async def _cardkit_close_streaming(cid, *, sequence=0, summary=""):
            return await srv.cardkit_close_streaming(cid, sequence=sequence, summary=summary)
        async def _cardkit_update_summary(cid, summary, *, sequence=0):
            return await srv.cardkit_update_summary(cid, summary, sequence=sequence)
        async def _cardkit_update(cid, card, *, sequence=0):
            return await srv.cardkit_update(cid, card, sequence=sequence)
        async def _update_card(mid, card):
            return await srv.update_card(mid, card)
        async def _send_card_to_chat(chat_id, card):
            return await srv.send_card_to_chat(chat_id, card)
        async def _reply_text(mid, text):
            return await srv.reply_text(mid, text)

        mock_client.cardkit_create = _cardkit_create
        mock_client.reply_card_by_id = _reply_card_by_id
        mock_client.reply_card = _reply_card
        mock_client.cardkit_stream_element = _cardkit_stream_element
        mock_client.cardkit_batch_update = _cardkit_batch_update
        mock_client.cardkit_close_streaming = _cardkit_close_streaming
        mock_client.cardkit_update_summary = _cardkit_update_summary
        mock_client.cardkit_update = _cardkit_update
        mock_client.update_card = _update_card
        mock_client.send_card_to_chat = _send_card_to_chat
        mock_client.reply_text = _reply_text

        return mock_client

    def _wrap_client_to_capture_cards(self, client: Any) -> None:
        """v1.2.0: 真飞书模式下 wrap cardkit_create / cardkit_update.

        捕获传入飞书的 card JSON，用于验证 header 等字段。
        真飞书无法回查卡片内容，只能靠捕获"我们发了什么"来验证。
        """
        import copy
        orig_create = client.cardkit_create
        orig_update = client.cardkit_update

        async def _wrapped_create(card):
            self._captured_create_card = copy.deepcopy(card)
            return await orig_create(card)

        async def _wrapped_update(card_id, card, *, sequence=0):
            self._captured_update_cards.append(copy.deepcopy(card))
            return await orig_update(card_id, card, sequence=sequence)

        client.cardkit_create = _wrapped_create
        client.cardkit_update = _wrapped_update

    def get_create_card_json(self) -> dict[str, Any] | None:
        """v1.2.0: 获取 cardkit_create 传入的 card JSON.

        mock 模式: 从 mock_server 取第一张卡的 card_json
        真飞书模式: 取捕获的 _captured_create_card
        """
        if self._use_real_feishu:
            return self._captured_create_card
        # mock 模式: 取第一张卡
        cards = self.mock_server.get_cards()
        if not cards:
            return None
        first_card_id = next(iter(cards))
        return cards[first_card_id].card_json

    def get_update_card_jsons(self) -> list[dict[str, Any]]:
        """v1.2.0: 获取 cardkit_update（全量重建）传入的 card JSON 列表.

        mock 模式: 从 mock_server.call_log 取所有 cardkit_update 调用对应的 card
        真飞书模式: 取捕获的 _captured_update_cards
        """
        if self._use_real_feishu:
            return list(self._captured_update_cards)
        # mock 模式: cardkit_update 会更新 card_json，取最后一张卡的当前 card_json
        cards = self.mock_server.get_cards()
        if not cards:
            return []
        # 统计 cardkit_update 调用次数
        update_calls = [c for c in self.mock_server.call_log if c["op"] == "cardkit_update"]
        if not update_calls:
            return []
        # mock 只保留最终 card_json，无法还原历史；返回最终状态
        first_card_id = next(iter(cards))
        return [cards[first_card_id].card_json]

    async def teardown(self) -> None:
        """Clean up patches and state."""
        for p in self._patches:
            p.stop()
        self.mock_server.reset()
        self._sessions.clear()
        self._real_card_states.clear()

    @property
    def controller(self) -> Any:
        return self._controller

    # ── v1.1.1: 时间模拟工具 ──

    def simulate_session_age(self, message_id: str, age_seconds: float) -> bool:
        """模拟 session 已存活 age_seconds 秒（不改真实时间）.

        用于测试 TTL 超时、_prune_stale_sessions 等场景，
        避免真等 600 秒。

        修改 session.created_at 和 card_created_at 为 (now - age_seconds)，
        让 _prune_stale_sessions 和 TTL 延长逻辑认为 session 已超时。

        Returns True if session found and modified, False otherwise.
        """
        session = self._controller._sessions.get(message_id)
        if session is None:
            return False
        import time as _time
        past = _time.time() - age_seconds
        session.created_at = past
        if hasattr(session, "card_created_at") and session.card_created_at:
            session.card_created_at = past
        return True

    # ── Test flow helpers ──

    async def start_message(
        self,
        message_text: str = "test message",
        *,
        message_id: str = "",
        chat_id: str = "",
        use_open_id: bool = False,
    ) -> Any:
        """Start a new message session — creates placeholder card.

        v1.1.1: 新增 use_open_id 参数，True 时用 open_id 发私聊（测试私聊场景）。
        默认 False，用 chat_id 发群聊。
        v1.1.1: 私聊模式下动态发私聊 anchor 消息（群聊 anchor 不能用于私聊 reply）。
        """
        if self._use_real_feishu:
            # v1.1.1: 私聊模式用 open_id，群聊模式用 chat_id
            if use_open_id:
                chat_id = chat_id or os.environ.get("FEISHU_E2E_OPEN_ID", "")
                if not chat_id:
                    raise RuntimeError("真飞书模式需要 FEISHU_E2E_OPEN_ID 环境变量（私聊测试）")
            else:
                chat_id = chat_id or os.environ.get("FEISHU_E2E_CHAT_ID", "")
                if not chat_id:
                    raise RuntimeError("真飞书模式需要 FEISHU_E2E_CHAT_ID 环境变量（群聊测试）")
            if not self._real_anchor_message_id:
                raise RuntimeError(
                    "真飞书模式: setup 时未获取到 anchor message_id。"
                    "请先调用 await runner.setup()。"
                )
            if not message_id:
                self._real_msg_counter += 1
                message_id = f"om_e2e_{int(time.time())}_{self._real_msg_counter}"

            # v1.1.1: 私聊模式需要私聊 anchor（群聊 anchor 不能 reply 到私聊）
            if use_open_id:
                # 检查是否已有此 open_id 的私聊 anchor
                if not hasattr(self, "_private_anchors"):
                    self._private_anchors: dict[str, str] = {}
                if chat_id not in self._private_anchors:
                    # 发一条私聊 anchor 消息
                    self._private_anchors[chat_id] = await self._send_private_anchor(
                        self._controller._client, chat_id
                    )
                anchor_id = self._private_anchors[chat_id]
            else:
                anchor_id = self._real_anchor_message_id
        else:
            if not message_id:
                message_id = f"om_test_{int(time.time()*1000)}"
            if not chat_id:
                chat_id = "oc_test_chat" if not use_open_id else "ou_test_user"
            anchor_id = message_id

        self._controller.on_message_started(
            message_id=message_id,
            chat_id=chat_id,
            anchor_id=anchor_id,
        )

        # Wait for card creation
        session = self._controller._sessions.get(message_id)
        if session:
            await asyncio.wait_for(session._card_ready.wait(), timeout=10.0)
        self._sessions[message_id] = session

        # In real mode, track the card state
        if self._use_real_feishu and session and session.card_id:
            self._real_card_states[session.card_id] = MockCardState(
                card_id=session.card_id,
            )

        return session

    async def feed_answer(self, session: Any, text: str) -> None:
        """Feed an answer text delta to the session."""
        self._controller.on_answer(
            message_id=session.message_id,
            text=text,
        )
        await asyncio.sleep(0.15)

    async def feed_reasoning(self, session: Any, text: str) -> None:
        """Feed a reasoning delta to the session."""
        self._controller.on_reasoning(
            message_id=session.message_id,
            text=text,
        )
        await asyncio.sleep(0.15)

    async def feed_tool_update(
        self,
        session: Any,
        tool_name: str,
        status: str,
        detail: str = "",
    ) -> None:
        """Feed a tool update to the session."""
        self._controller.on_tool_update(
            message_id=session.message_id,
            tool_name=tool_name,
            status=status,
            detail=detail,
        )
        await asyncio.sleep(0.15)

    async def complete(
        self,
        session: Any,
        answer: str = "",
        *,
        error_message: str = "",
    ) -> None:
        """Complete the message — triggers seal.

        v1.1.1: 真飞书模式等待封卡完成（轮询 _streaming_closed，最多 15 秒）。
        v1.1.1: 封卡前缓存 answer_text 和 panel 信息（封卡后 unified_state 被释放）。
        """
        # v1.1.1: 封卡前缓存数据到 runner（不用 session，因为 __slots__）
        mid = session.message_id
        if session.unified_state:
            self._test_cache[mid] = {
                "answer_text": session.unified_state.answer_text or answer,
                "has_reasoning": bool(session.unified_state.reasoning_rounds),
                "tool_count": len(session.tool_use.build_display_steps()),
            }

        self._controller.on_completed(
            message_id=session.message_id,
            answer=answer,
            error_message=error_message,
        )
        # Wait for seal to complete
        if self._use_real_feishu:
            max_wait = 15.0
            for _ in range(int(max_wait / 0.5)):
                await asyncio.sleep(0.5)
                if hasattr(session, "_streaming_closed") and session._streaming_closed:
                    return
                if hasattr(session, "is_terminal_phase") and session.is_terminal_phase:
                    return
        else:
            await asyncio.sleep(0.5)

    # ── Verification helpers ──

    def get_card(self, session: Any) -> MockCardState | None:
        """Get the card state for a session.

        In mock mode: returns the MockCardState from the in-memory server.
        In real mode: returns a MockCardState reconstructed from session
        metadata (limited — we can't query the real Feishu server for
        card content, so only card_id/summary/streaming_mode are available).
        """
        if not session.card_id:
            return None

        if self._use_real_feishu:
            # In real mode, we can't inspect the card content directly.
            # Return a minimal state with what we know.
            state = self._real_card_states.get(session.card_id)
            if state is None:
                state = MockCardState(card_id=session.card_id)
                self._real_card_states[session.card_id] = state
            state.streaming_mode = not session._streaming_closed if hasattr(session, "_streaming_closed") else False
            return state
        else:
            return self.mock_server.get_card(session.card_id)

    def get_answer_text(self, session: Any) -> str:
        """Extract answer text from the card.

        v1.1.1: 真飞书模式下封卡后 unified_state 被释放，
        从 _test_cache 或 session.text 读取。
        """
        if self._use_real_feishu:
            # v1.1.1: 优先从缓存读
            cached = self._test_cache.get(session.message_id, {})
            if cached.get("answer_text"):
                return cached["answer_text"]
            if session.unified_state:
                return session.unified_state.answer_text or ""
            if session.text:
                return session.text.display_text or ""
            return ""
        else:
            card = self.get_card(session)
            if card is None:
                return ""
            from hermes_lark_streaming.cardkit.elements import ANSWER_ELEMENT_ID
            answer_el = card.elements.get(ANSWER_ELEMENT_ID, {})
            return answer_el.get("content", "")

    def get_panel_elements(self, session: Any) -> list[dict[str, Any]]:
        """Get the panel children from the card.

        v1.1.1: 真飞书模式下从缓存读取是否有推理和工具。
        """
        if self._use_real_feishu:
            cached = self._test_cache.get(session.message_id, {})
            elements: list[dict] = []
            if cached.get("has_reasoning"):
                elements.append({"type": "reasoning", "text": "cached"})
            for i in range(cached.get("tool_count", 0)):
                elements.append({"type": "tool", "name": f"tool_{i}"})
            return elements
        card = self.get_card(session)
        if card is None:
            return []
        from hermes_lark_streaming.cardkit.elements import UNIFIED_PANEL_ELEMENT_ID
        panel = card.elements.get(UNIFIED_PANEL_ELEMENT_ID, {})
        return panel.get("elements", [])

    def get_call_count(self, operation: str) -> int:
        """Count how many times a specific API operation was called.

        Mock mode: counts from mock_server.call_log.
        Real mode: always returns 0 (we don't intercept real API calls).
        """
        if self._use_real_feishu:
            return 0  # Can't count real API calls without intercepting
        return sum(1 for call in self.mock_server.call_log if call["op"] == operation)

    def get_total_api_calls(self) -> int:
        """Total API calls made during the test."""
        if self._use_real_feishu:
            return 0  # Can't count real API calls without intercepting
        return len(self.mock_server.call_log)

    def assert_card_created(self, session: Any) -> None:
        """Assert that a card was created for this session."""
        assert session.card_id is not None, "Card was not created"
        if not self._use_real_feishu:
            assert self.mock_server.get_card(session.card_id) is not None, \
                "Card not found in mock server"

    async def assert_card_sealed(self, session: Any) -> None:
        """Assert that the card was sealed (streaming closed).

        v1.1.1: 真飞书模式下轮询等待封卡完成（最多 10 秒）。
        """
        if self._use_real_feishu:
            # 轮询等待 _streaming_closed 或 is_terminal_phase
            max_wait = 10.0
            for _ in range(int(max_wait / 0.5)):
                if hasattr(session, "_streaming_closed") and session._streaming_closed:
                    return
                if hasattr(session, "is_terminal_phase") and session.is_terminal_phase:
                    return
                await asyncio.sleep(0.5)
            assert hasattr(session, "_streaming_closed") and session._streaming_closed, \
                f"Card {session.card_id} was not sealed after {max_wait}s wait"
        else:
            card = self.get_card(session)
            assert card is not None, "Card not found"
            assert not card.streaming_mode, \
                f"Card {session.card_id} was not sealed (streaming_mode still True)"

    def assert_no_errors(self) -> None:
        """Assert no error-level API calls were made."""
        if self._use_real_feishu:
            return  # Can't detect errors without intercepting
        errors = [c for c in self.mock_server.call_log if "error" in str(c).lower()]
        assert not errors, f"API errors occurred: {errors}"
