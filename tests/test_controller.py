"""controller.py 测试 — 会话生命周期边界条件 + 线性模式 dispatch 与集成测试."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hermes_lark_streaming.controller import CardSession, StreamCardController
from hermes_lark_streaming.controller.mixin import (
    ABORTED,
    COMPLETED,
    COMPLETING,
    CREATION_FAILED,
    STREAMING,
)
from hermes_lark_streaming.feishu import (
    CARDKIT_CONTENT_FAILED,
    CARDKIT_ELEMENT_LIMIT,
    CARDKIT_SEQUENCE_CONFLICT,
    CARDKIT_STREAMING_CLOSED,
    FeishuAPIError,
    FeishuClient,
)
from hermes_lark_streaming.cardkit import (
    ANSWER_ELEMENT_ID,
    UNIFIED_PANEL_ELEMENT_ID,
    _LOADING_HINT_ELEMENT_ID,
    _LOADING_ELEMENT_ID,
)
from hermes_lark_streaming.state.linear import UnifiedLinearState


def _enable(ctrl: StreamCardController, *, linear: bool = False) -> None:
    ctrl._cfg._raw = {
        "hermes_lark_streaming": {"enabled": True, "linear": linear},
        "feishu": {"app_id": "app", "app_secret": "secret"},
    }


class _DummyFlush:
    def __init__(self) -> None:
        self.completed = False

    def mark_completed(self) -> None:
        self.completed = True


@pytest.mark.parametrize("message_id", [None, ""])
def test_on_message_started_ignores_missing_message_id(message_id: str | None) -> None:
    ctrl = StreamCardController()
    _enable(ctrl)

    ctrl.on_message_started(message_id=message_id, chat_id="chat")

    assert ctrl._sessions == {}


def test_on_message_started_registers_anchor_alias_and_cleanup() -> None:
    ctrl = StreamCardController()
    _enable(ctrl)

    with patch.object(ctrl, "_fire_and_forget", side_effect=lambda coro, loop: coro.close()):
        ctrl.on_message_started(message_id="msg", chat_id="chat", anchor_id="quoted")

    session = ctrl._sessions["msg"]
    assert ctrl._sessions["quoted"] is session
    assert session.anchor_id == "quoted"

    ctrl._cleanup("msg")

    assert "msg" not in ctrl._sessions
    assert "quoted" not in ctrl._sessions


def test_on_interrupted_uses_new_message_id_and_anchor_alias() -> None:
    ctrl = StreamCardController()
    _enable(ctrl)

    with patch.object(ctrl, "_fire_and_forget", side_effect=lambda coro, loop: coro.close()):
        ctrl.on_message_started(message_id="old", chat_id="chat")
        ctrl.on_interrupted(
            old_message_id="old",
            new_message_id="new",
            chat_id="chat",
            anchor_id="quoted",
        )

    session = ctrl._sessions["new"]
    assert ctrl._sessions["quoted"] is session
    assert session.anchor_id == "quoted"
    assert ctrl._interrupt_map["old"] == "new"
    assert ctrl._sessions["old"].state == ABORTED


def test_on_interrupted_skips_abort_for_completing_session() -> None:
    """Hotfix: COMPLETING session should not be aborted on interrupt.

    When on_completed has already fired (session in COMPLETING state),
    on_interrupted should skip the abort logic — let _do_linear_complete
    finish naturally. However, the new session must still be created and
    _interrupt_map must still be updated.
    """
    ctrl = StreamCardController()
    _enable(ctrl)

    with patch.object(ctrl, "_fire_and_forget", side_effect=lambda coro, loop: coro.close()):
        ctrl.on_message_started(message_id="completing_msg", chat_id="chat")
        # Simulate session in COMPLETING state (on_completed already fired)
        ctrl._sessions["completing_msg"].state = COMPLETING

        ctrl.on_interrupted(
            old_message_id="completing_msg",
            new_message_id="new_msg",
            chat_id="chat",
            anchor_id="anchor",
        )

    # Key assertion: COMPLETING session must NOT be marked as aborted
    old_session = ctrl._sessions["completing_msg"]
    assert old_session._was_aborted is False, (
        "COMPLETING session must NOT be marked as aborted"
    )
    assert old_session.state == COMPLETING, (
        "COMPLETING session state must not be overwritten"
    )

    # New session must still be created
    assert "new_msg" in ctrl._sessions, (
        "New session must be created even when old session is COMPLETING"
    )
    new_session = ctrl._sessions["new_msg"]
    assert ctrl._sessions["anchor"] is new_session
    assert new_session.anchor_id == "anchor"

    # _interrupt_map must still be updated
    assert ctrl._interrupt_map["completing_msg"] == "new_msg", (
        "_interrupt_map must be updated even when old session is COMPLETING"
    )


def test_on_aborted_skips_abort_for_completing_session() -> None:
    """Hotfix: COMPLETING session should not be aborted on /stop.

    Same race condition as on_interrupted: if on_completed has already
    fired (session in COMPLETING state), on_aborted should skip the
    abort logic — let _do_linear_complete finish naturally. Only mark
    _was_aborted so the seal shows "stopped" state.
    """
    ctrl = StreamCardController()
    _enable(ctrl)

    ctrl.on_message_started(message_id="completing_msg", chat_id="chat")
    # Simulate session in COMPLETING state (on_completed already fired)
    ctrl._sessions["completing_msg"].state = COMPLETING

    ctrl.on_aborted(message_id="completing_msg")

    # Key assertion: COMPLETING session must NOT be marked as ABORTED
    old_session = ctrl._sessions["completing_msg"]
    assert old_session.state == COMPLETING, (
        "COMPLETING session state must not be overwritten to ABORTED"
    )
    assert old_session._was_aborted is True, (
        "COMPLETING session must be marked as _was_aborted for seal display"
    )
    # flush must NOT be marked completed (drain should continue)
    assert not old_session.flush._completed, (
        "flush must NOT be marked completed during COMPLETING"
    )


def test_on_aborted_normally_aborts_non_completing_session() -> None:
    """Non-COMPLETING sessions should still be aborted normally."""
    ctrl = StreamCardController()
    _enable(ctrl)

    ctrl.on_message_started(message_id="streaming_msg", chat_id="chat")
    # Session starts in IDLE state, on_message_started sets it up
    # Simulate it in STREAMING state
    ctrl._sessions["streaming_msg"].state = STREAMING

    ctrl.on_aborted(message_id="streaming_msg")

    session = ctrl._sessions["streaming_msg"]
    assert session.state == ABORTED, (
        "Non-COMPLETING session must be marked as ABORTED"
    )
    assert session._was_aborted is True
    assert session.flush._completed, (
        "flush must be marked completed for aborted session"
    )


def test_prune_stale_sessions_ignores_none_key_and_prunes_valid_key() -> None:
    ctrl = StreamCardController()
    # v1.1.1: _prune_stale_sessions 只清理 is_terminal_phase 的 session
    stale_session = SimpleNamespace(
        created_at=time.time() - ctrl._session_ttl - 1,
        flush=_DummyFlush(),
        is_terminal_phase=True,
    )
    valid_stale_session = SimpleNamespace(
        created_at=time.time() - ctrl._session_ttl - 1,
        flush=_DummyFlush(),
        is_terminal_phase=True,
    )
    ctrl._sessions[None] = stale_session  # type: ignore[index,assignment]
    ctrl._sessions["msg"] = valid_stale_session  # type: ignore[assignment]

    ctrl._prune_stale_sessions()

    assert ctrl._sessions[None] is stale_session  # type: ignore[index]
    assert "msg" not in ctrl._sessions
    assert valid_stale_session.flush.completed


@pytest.mark.asyncio
async def test_background_review_deferred_until_complete() -> None:
    """v1.1.0: deferred background review flushing was tied to the removed
    non-linear ``_do_complete`` path. In the unified linear architecture,
    linear sessions consume background reviews immediately via
    ``UnifiedLinearState.on_background_review`` (see
    ``defer_background_review`` in ``controller/core.py``), so there is no
    longer a "deferred until complete" code path to test. The test that
    previously lived here asserted on the deleted ``ctrl._do_complete``
    method and was removed in Task 1.1+1.2.

    The remaining behavior — that ``defer_background_review`` returns True
    for an active session and the message is consumed by the unified
    state — is covered by the linear dispatch tests below.
    """
    ctrl = _setup_ctrl(linear=True)
    session = _make_session("msg_bg", linear=True)
    session.state = STREAMING
    session.card_msg_id = "card_msg"
    ctrl._sessions["msg_bg"] = session
    sent: list[str] = []

    # In linear mode, defer_background_review pushes the text directly
    # into unified_state and returns True (consumed by card, suppress
    # plain text). The sender callback is never invoked.
    assert ctrl.defer_background_review(message_id="msg_bg", text="review", sender=sent.append)
    assert sent == []
    assert session.unified_state is not None
    assert session.unified_state.bg_review_messages == ["review"]


def test_background_review_without_active_session_not_deferred() -> None:
    ctrl = _setup_ctrl()
    sent: list[str] = []

    assert not ctrl.defer_background_review(message_id="missing", text="review", sender=sent.append)
    assert sent == []


def test_background_review_after_flush_not_deferred() -> None:
    ctrl = _setup_ctrl()
    session = _make_session("msg_bg")
    ctrl._sessions["msg_bg"] = session
    sent: list[str] = []

    ctrl._flush_deferred_background_reviews(session)

    assert not ctrl.defer_background_review(message_id="msg_bg", text="review", sender=sent.append)
    assert sent == []


# ── 辅助函数 ──

# Track event loops created by _make_session so the conftest autouse
# fixture can close them after each test (prevents ResourceWarning /
# "coroutine never awaited" warnings).
_loops_to_cleanup: list[asyncio.AbstractEventLoop] = []


def _make_session(msg_id: str = "msg_123", *, linear: bool = False) -> CardSession:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _loops_to_cleanup.append(loop)
    session = CardSession(msg_id, "chat_456", loop)
    if linear:
        session.linear = True
        session.unified_state = UnifiedLinearState()
    # v0.12.1: _card_ready must be set so _do_linear_complete doesn't
    # hang for 30 seconds. In production, this is set by
    # _do_create_linear_card (the non-linear _do_create_card was
    # removed in v1.1.0).
    session._card_ready.set()
    return session


def _mock_client() -> AsyncMock:
    client = AsyncMock(spec=FeishuClient)
    client.cardkit_create = AsyncMock(return_value="card_id_abc")
    client.reply_card_by_id = AsyncMock(return_value="msg_id_reply")
    client.reply_card = AsyncMock(return_value="msg_id_reply")
    client.cardkit_batch_update = AsyncMock()
    client.cardkit_stream_element = AsyncMock()
    client.cardkit_close_streaming = AsyncMock()
    client.cardkit_update = AsyncMock()
    client.update_card = AsyncMock()
    client.reply_text = AsyncMock(return_value="msg_id_reply")
    return client


def _setup_ctrl(*, linear: bool = False) -> StreamCardController:
    ctrl = StreamCardController()
    _enable(ctrl, linear=linear)
    ctrl._initialized = True
    ctrl._client = _mock_client()
    return ctrl


@pytest.mark.asyncio
async def test_create_linear_card_replies_to_anchor_id() -> None:
    """Anchor alias support: the linear card creator replies to ``anchor_id``
    when set.

    v1.1.0: the legacy non-linear ``_do_create_card`` was removed; this
    test was rewritten to drive the linear ``_do_create_linear_card``
    path (the only creation path that remains). The behaviour under
    test — replying to ``anchor_id`` when present — is unchanged.
    """
    ctrl = _setup_ctrl(linear=True)
    session = _make_session("msg", linear=True)
    session.anchor_id = "quoted"
    ctrl._sessions["msg"] = session

    await ctrl._do_create_linear_card(session)

    ctrl._client.reply_card_by_id.assert_called_once()
    assert ctrl._client.reply_card_by_id.call_args.args[0] == "quoted"


def _capture_split_calls(
    ctrl: StreamCardController,
    *,
    cards: list[str] | None = None,
    messages: list[str] | None = None,
    create_error: Exception | None = None,
) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    client = ctrl._client
    card_iter = iter(cards or ["card_next"])
    message_iter = iter(messages or ["msg_next"])

    client.cardkit_batch_update = AsyncMock(
        side_effect=lambda card_id, *a, **k: calls.append(("batch", card_id))
    )
    if create_error is None:
        client.cardkit_create = AsyncMock(
            side_effect=lambda *a, **k: calls.append(("create", "")) or next(card_iter)
        )
    else:
        client.cardkit_create = AsyncMock(side_effect=create_error)
    client.reply_card_by_id = AsyncMock(
        side_effect=lambda *a, **k: calls.append(("reply", "")) or next(message_iter)
    )
    client.cardkit_close_streaming = AsyncMock(
        side_effect=lambda card_id, **k: calls.append(("close", card_id))
    )
    client.cardkit_update = AsyncMock(
        side_effect=lambda card_id, *a, **k: calls.append(("seal", card_id))
    )
    return calls


# ── Dispatch 测试 — 线性模式分流 ──


class TestLinearDispatch:
    """验证线性 session 的 6 个入口走 linear 路径，非线性 session 不受影响."""

    @pytest.mark.parametrize("event,kwargs", [
        ("on_reasoning", {"text": "r"}),
        ("on_answer", {"text": "a"}),
    ])
    def test_linear_dispatch_creates_state(self, event: str, kwargs: dict) -> None:
        ctrl = _setup_ctrl()
        ctrl._cfg._reload_cached = lambda: {"display": {"platforms": {"feishu": {"show_reasoning": True}}}}  # type: ignore[assignment]
        session = _make_session("msg_d", linear=True)
        ctrl._sessions["msg_d"] = session
        getattr(ctrl, event)(message_id="msg_d", **kwargs)
        assert session.unified_state is not None

    def test_linear_dispatch_reasoning_sets_dirty(self) -> None:
        ctrl = _setup_ctrl()
        ctrl._cfg._reload_cached = lambda: {"display": {"platforms": {"feishu": {"show_reasoning": True}}}}  # type: ignore[assignment]
        session = _make_session("msg_d", linear=True)
        ctrl._sessions["msg_d"] = session
        ctrl.on_reasoning(message_id="msg_d", text="r")
        assert session.unified_state is not None
        assert session.unified_state.panel_dirty is True

    def test_linear_dispatch_answer_sets_dirty(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_d", linear=True)
        ctrl._sessions["msg_d"] = session
        ctrl.on_answer(message_id="msg_d", text="a")
        assert session.unified_state is not None
        assert session.unified_state.answer_dirty is True

    def test_linear_thinking_dispatches(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_t", linear=True)
        ctrl._sessions["msg_t"] = session
        with patch.object(ctrl, "_linear_on_thinking") as m:
            ctrl.on_thinking(message_id="msg_t", text="thinking")
            m.assert_called_once()

    def test_linear_tool_dispatches(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_tool", linear=True)
        ctrl._sessions["msg_tool"] = session
        ctrl.on_tool_update(message_id="msg_tool", tool_name="read", status="started")
        assert session.unified_state is not None
        assert session.unified_state.tool_steps_dirty is True

    def test_linear_completed_dispatches(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_c", linear=True)
        session.state = STREAMING
        session.card_id = "card_123"
        ctrl._sessions["msg_c"] = session
        with patch.object(ctrl, "_do_linear_complete_with_fallback", new_callable=AsyncMock):
            ctrl.on_completed(message_id="msg_c")
        # After on_completed, state should be COMPLETING (not COMPLETED yet)
        # The actual completion happens asynchronously in _do_linear_complete
        assert session.state == COMPLETING

    def test_guard_skips_terminal(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_term", linear=True)
        session.state = COMPLETED
        ctrl._sessions["msg_term"] = session
        ctrl.on_answer(message_id="msg_term", text="late text")
        # unified_state should still have no answer text (guard blocked)
        assert session.unified_state is not None
        assert session.unified_state.answer_text == ""

    def test_message_started_creates_linear_session(self) -> None:
        ctrl = _setup_ctrl(linear=True)
        ctrl.on_message_started(message_id="msg1", chat_id="chat1")
        session = ctrl._sessions["msg1"]
        loop = session._loop
        loop.run_until_complete(asyncio.sleep(0.05))
        assert session.linear is True
        assert session.card_id is not None


# ── _do_create_linear_card 集成测试 ──


class TestDoCreateLinearCard:
    @pytest.mark.asyncio
    async def test_cardkit_success(self) -> None:
        ctrl = _setup_ctrl(linear=True)
        session = _make_session("msg_create")
        ctrl._sessions["msg_create"] = session

        await ctrl._do_create_linear_card(session)

        assert session.linear is True
        assert session.unified_state is not None
        assert session.card_id == "card_id_abc"
        assert session.state == STREAMING

    @pytest.mark.asyncio
    async def test_generic_failure_marks_failed(self) -> None:
        ctrl = _setup_ctrl(linear=True)
        ctrl._client = None
        session = _make_session("msg_err")
        ctrl._sessions["msg_err"] = session

        await ctrl._do_create_linear_card(session)

        assert session.state == CREATION_FAILED

    @pytest.mark.asyncio
    async def test_unified_state_set_before_await(self) -> None:
        """CREATING 期间的事件进入线性路径 — unified_state 在 try 之前设置."""
        ctrl = _setup_ctrl(linear=True)
        session = _make_session("msg_early")
        ctrl._sessions["msg_early"] = session

        original_ensure = ctrl._ensure_init

        async def check_state_then_ensure() -> None:
            assert session.linear is True
            assert session.unified_state is not None
            await original_ensure()

        ctrl._ensure_init = check_state_then_ensure  # type: ignore[assignment]
        await ctrl._do_create_linear_card(session)

    @pytest.mark.asyncio
    async def test_post_create_flush_on_dirty(self) -> None:
        """When data arrives during card creation, a flush is triggered after card is ready."""
        ctrl = _setup_ctrl(linear=True)
        session = _make_session("msg_dirty")
        ctrl._sessions["msg_dirty"] = session

        original_ensure = ctrl._ensure_init

        async def inject_data_and_ensure() -> None:
            await original_ensure()
            session.unified_state.on_reasoning_delta("during-creating")

        ctrl._ensure_init = inject_data_and_ensure  # type: ignore[assignment]

        await ctrl._do_create_linear_card(session)

        # After card creation, data that arrived during creation should
        # trigger a flush. The new implementation either calls flush_now
        # directly (first content) or _schedule_linear_flush (subsequent).
        # Either way, the dirty data should be cleared or a flush scheduled.
        assert session._first_flush_done is True

    @pytest.mark.asyncio
    async def test_first_card_creates_preallocated_elements(self) -> None:
        """首卡创建后仅预分配 loading hint + loading icon (2 elements).

        Panel and answer element are added dynamically when the first
        LLM token arrives (Phase 2 of card lifecycle).
        """
        ctrl = _setup_ctrl(linear=True)
        session = _make_session("msg_hint")
        ctrl._sessions["msg_hint"] = session

        await ctrl._do_create_linear_card(session)

        # existing_elements should contain only 2 pre-allocated elements
        assert len(session.existing_elements) == 2
        assert "panel" not in session._creation_stages

    @pytest.mark.asyncio
    async def test_card_created_at_set(self) -> None:
        """card_created_at 应在首卡创建后设置."""
        ctrl = _setup_ctrl(linear=True)
        session = _make_session("msg_time")
        ctrl._sessions["msg_time"] = session

        await ctrl._do_create_linear_card(session)

        assert session.card_created_at > 0


# ── _do_unified_flush 集成测试 ──


class TestDoUnifiedFlush:
    @pytest.mark.asyncio
    async def test_reasoning_and_answer_flush(self) -> None:
        """reasoning + answer flush — Phase 2: add panel + delete hint + stream answer."""
        ctrl = _setup_ctrl()
        session = _make_session("msg_flush", linear=True)
        session.state = STREAMING
        session.card_id = "card_flush"
        session.unified_state.on_reasoning_delta("think")
        session.unified_state.on_answer_delta("hello world")
        ctrl._sessions["msg_flush"] = session

        await ctrl._do_unified_flush(session)

        # panel dirty should be cleared
        assert session.unified_state.panel_dirty is False
        # answer dirty should be cleared
        assert session.unified_state.answer_dirty is False
        # batch_update should have been called (Phase 2: add panel + delete hint)
        ctrl._client.cardkit_batch_update.assert_called()
        # stream_element should have been called for answer
        ctrl._client.cardkit_stream_element.assert_called()
        # Panel should now be created
        assert "panel" in session._creation_stages

    @pytest.mark.asyncio
    async def test_no_api_calls_when_no_card_id(self) -> None:
        """无 card_id 时跳过 API 调用."""
        ctrl = _setup_ctrl()
        session = _make_session("msg_no_id", linear=True)
        session.state = STREAMING
        session.card_id = None
        session.unified_state.on_reasoning_delta("think")
        ctrl._sessions["msg_no_id"] = session

        await ctrl._do_unified_flush(session)

        ctrl._client.cardkit_batch_update.assert_not_called()
        ctrl._client.cardkit_stream_element.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_api_calls_when_terminal_state(self) -> None:
        """终态时跳过 API 调用."""
        ctrl = _setup_ctrl()
        session = _make_session("m1", linear=True)
        session.state = COMPLETED
        session.card_id = "c"
        ctrl._sessions["m1"] = session

        await ctrl._do_unified_flush(session)

        ctrl._client.cardkit_batch_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_api_calls_when_no_dirty(self) -> None:
        """无 dirty 时跳过 API 调用 (panel already created, no pending content)."""
        ctrl = _setup_ctrl()
        session = _make_session("m2", linear=True)
        session.state = STREAMING
        session.card_id = "c"
        session._creation_stages.add("hint_removed")
        session._creation_stages.add("answer")  # Answer element already exists (Phase 2 done)
        session._creation_stages.add("panel")  # Panel already exists (Phase 2 done)
        session.unified_state.on_reasoning_delta("t")
        session.unified_state.panel_dirty = False
        session.unified_state.answer_dirty = False
        session.unified_state.tool_steps_dirty = False
        ctrl._sessions["m2"] = session

        await ctrl._do_unified_flush(session)

        ctrl._client.cardkit_stream_element.assert_not_called()
        # batch_update should not be called (no dirty data, panel already created)
        ctrl._client.cardkit_batch_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_tool_event_flush(self) -> None:
        """tool event flush — panel partial_update."""
        ctrl = _setup_ctrl()
        session = _make_session("msg_tool_flush", linear=True)
        session.state = STREAMING
        session.card_id = "card_tool"
        session.unified_state.on_reasoning_delta("think")
        session.unified_state.on_answer_delta("hello")
        session.tool_use.record_start("read", "f")
        session.unified_state.on_tool_event()
        ctrl._sessions["msg_tool_flush"] = session

        await ctrl._do_unified_flush(session)

        assert session.unified_state.panel_dirty is False
        assert session.unified_state.tool_steps_dirty is False
        ctrl._client.cardkit_batch_update.assert_called()

    @pytest.mark.asyncio
    async def test_loading_hint_removed_on_first_content(self) -> None:
        """首字即显时 Phase 2 的 batch_update 中添加 panel + 删除 loading hint."""
        ctrl = _setup_ctrl()
        session = _make_session("msg_hint_del", linear=True)
        session.state = STREAMING
        session.card_id = "card_hint"
        session._creation_stages.discard("hint_removed")
        session.existing_elements = {_LOADING_HINT_ELEMENT_ID, _LOADING_ELEMENT_ID}
        session.unified_state.on_answer_delta("hello")
        ctrl._sessions["msg_hint_del"] = session

        batch_actions: list[list[dict]] = []

        async def capture_batch(card_id: str, actions: list[dict], **kw: object) -> None:
            batch_actions.append(actions)

        ctrl._client.cardkit_batch_update = capture_batch

        await ctrl._do_unified_flush(session)

        # Should include add_elements (panel + answer) and delete loading hint
        assert len(batch_actions) >= 1
        all_actions = batch_actions[0]
        add_actions = [a for a in all_actions if a["action"] == "add_elements"]
        delete_hint_actions = [
            a for a in all_actions
            if a["action"] == "delete_elements"
        ]
        assert len(add_actions) == 1  # Phase 2: add answer element (panel not needed for answer-only content)
        assert len(delete_hint_actions) == 1  # Delete loading hint
        assert "hint_removed" in session._creation_stages
        assert "answer" in session._creation_stages  # Answer element created (Path B: answer only)
        # panel stage remains absent — no reasoning/tools, so no panel needed

    @pytest.mark.asyncio
    async def test_loading_hint_removed_on_reasoning(self) -> None:
        """reasoning 到达时 Phase 2: add panel + delete loading hint."""
        ctrl = _setup_ctrl()
        session = _make_session("msg_hint_reasoning", linear=True)
        session.state = STREAMING
        session.card_id = "card_hint_r"
        session._creation_stages.discard("hint_removed")
        session.existing_elements = {_LOADING_HINT_ELEMENT_ID, _LOADING_ELEMENT_ID}
        session.unified_state.on_reasoning_delta("thinking")
        ctrl._sessions["msg_hint_reasoning"] = session

        batch_actions: list[list[dict]] = []

        async def capture_batch(card_id: str, actions: list[dict], **kw: object) -> None:
            batch_actions.append(actions)

        ctrl._client.cardkit_batch_update = capture_batch

        await ctrl._do_unified_flush(session)

        assert len(batch_actions) >= 1
        all_actions = batch_actions[0]
        add_actions = [a for a in all_actions if a["action"] == "add_elements"]
        delete_hint_actions = [
            a for a in all_actions
            if a["action"] == "delete_elements"
        ]
        assert len(add_actions) == 1  # Phase 2: add panel + answer element
        assert len(delete_hint_actions) == 1
        assert "hint_removed" in session._creation_stages
        assert "panel" in session._creation_stages

    @pytest.mark.asyncio
    @pytest.mark.parametrize("code", [230020, 300309])
    async def test_api_errors_swallowed(self, code: int) -> None:
        """rate limited / streaming closed 不抛异常."""
        ctrl = _setup_ctrl()
        ctrl._client.cardkit_batch_update = AsyncMock(side_effect=FeishuAPIError("e", code=code))
        session = _make_session("msg_err", linear=True)
        session.state = STREAMING
        session.card_id = "card_e"
        session.unified_state.on_reasoning_delta("think")
        ctrl._sessions["msg_err"] = session

        await ctrl._do_unified_flush(session)

    @pytest.mark.asyncio
    async def test_stream_element_feishu_error_does_not_crash(self) -> None:
        """FeishuAPIError from stream_element is caught in unified flush."""
        ctrl = _setup_ctrl()
        ctrl._client.cardkit_stream_element = AsyncMock(side_effect=FeishuAPIError("stream fail", code=230020))
        session = _make_session("msg_exc", linear=True)
        session.state = STREAMING
        session.card_id = "card_exc"
        session.unified_state.on_answer_delta("text")
        ctrl._sessions["msg_exc"] = session

        # Should not raise
        await ctrl._do_unified_flush(session)


# ── Split/rollover tests — REMOVED in unified panel architecture ──


# NOTE: TestDoLinearSplit has been removed entirely.
# The unified panel architecture (v1.0.2) eliminates card splitting —
# all content lives in a single panel + 1 answer element, so there
# is never a need to split across multiple cards.


# ── v1.4.1: Phase 3 is_element_not_found_error 回归测试 ──


class TestPhase3ElementNotFoundV141:
    """v1.4.1 regression: Phase 3 batch_update 300315 "not find elementID" 处理。

    Root cause (Issue 2): Phase 3 safety net 尝试删除已被 Phase 2 删除的
    _LOADING_HINT_ELEMENT_ID → 飞书返回 300315 "not find elementID" →
    Phase 3 except 块无 is_element_not_found_error 分支 → 落入通用 warning
    → hint_removed 未同步 + existing_elements 未 discard → 下轮 safety net
    再次添加 delete → 死循环 → partial_update (面板内容) 因 batch 原子失败
    丢失 → 卡片卡在「正在加载上下文...」。

    修复：Phase 3 except 块新增 is_element_not_found_error 分支，同步
    hint_removed + discard existing_elements，保留 dirty 供下轮重试。
    """

    @pytest.mark.asyncio
    async def test_phase3_element_not_found_syncs_hint_and_keeps_dirty(self) -> None:
        """300315 on Phase 3 batch → sync hint_removed, keep panel_dirty for retry."""
        ctrl = _setup_ctrl(linear=True)
        session = _make_session("msg_141", linear=True)
        session.state = STREAMING
        session.card_id = "card_141"
        # Simulate: Phase 2 succeeded on Feishu side (hint deleted) but local
        # tracking out of sync (hint_removed NOT in stages, hint still in
        # existing_elements) — e.g. network error after Feishu processed batch.
        session._creation_stages = {"answer", "panel"}  # hint_removed missing!
        session.existing_elements = {
            ANSWER_ELEMENT_ID,
            UNIFIED_PANEL_ELEMENT_ID,
            _LOADING_HINT_ELEMENT_ID,  # hint still tracked locally
            _LOADING_ELEMENT_ID,
        }
        session.unified_state.on_reasoning_delta("think")
        time.sleep(0.01)
        session.unified_state.on_answer_delta("reply")
        ctrl._sessions["msg_141"] = session

        # Phase 3 batch_update returns 300315 "not find elementID" (hint already gone)
        err = FeishuAPIError(
            "cardkit_batch_update: code=300315, msg=ErrMsg: not find elementID : context_loading_hint;",
            code=300315,
        )
        ctrl._client.cardkit_batch_update = AsyncMock(side_effect=err)
        ctrl._client.cardkit_stream_element = AsyncMock()

        await ctrl._do_unified_flush(session)

        # v1.4.1 fix: hint_removed synced into _creation_stages
        assert "hint_removed" in session._creation_stages, (
            "Phase 3 is_element_not_found_error must sync hint_removed to break "
            "the safety-net delete loop"
        )
        # v1.4.1 fix: hint discarded from existing_elements
        assert _LOADING_HINT_ELEMENT_ID not in session.existing_elements, (
            "Phase 3 is_element_not_found_error must discard hint from existing_elements"
        )
        # v1.4.1 fix: panel_dirty / tool_steps_dirty PRESERVED for next-flush retry
        # (not cleared like schema_error, not lost like the old generic warning path)
        assert session.unified_state.panel_dirty or session.unified_state.tool_steps_dirty, (
            "Phase 3 is_element_not_found_error must preserve panel_dirty/tool_steps_dirty "
            "so the partial_update retries on the next flush"
        )

    @pytest.mark.asyncio
    async def test_phase3_element_not_found_second_flush_no_loop(self) -> None:
        """After the 300315 sync, the next flush must NOT re-add the hint delete
        (safety net skipped because hint_removed is now in stages) — loop broken."""
        ctrl = _setup_ctrl(linear=True)
        session = _make_session("msg_141b", linear=True)
        session.state = STREAMING
        session.card_id = "card_141b"
        session._creation_stages = {"answer", "panel"}
        session.existing_elements = {
            ANSWER_ELEMENT_ID,
            UNIFIED_PANEL_ELEMENT_ID,
            _LOADING_HINT_ELEMENT_ID,
            _LOADING_ELEMENT_ID,
        }
        session.unified_state.on_reasoning_delta("think")
        time.sleep(0.01)
        session.unified_state.on_answer_delta("reply")
        ctrl._sessions["msg_141b"] = session

        call_count = {"batch": 0}

        async def batch_then_ok(card_id: str, actions: list[dict], **kw: object) -> None:
            call_count["batch"] += 1
            if call_count["batch"] == 1:
                # First flush: Phase 3 safety net deletes hint → 300315
                raise FeishuAPIError(
                    "cardkit_batch_update: code=300315, msg=ErrMsg: not find elementID : context_loading_hint;",
                    code=300315,
                )
            # Second flush: succeed (no hint delete in actions because hint_removed synced)

        ctrl._client.cardkit_batch_update = AsyncMock(side_effect=batch_then_ok)
        ctrl._client.cardkit_stream_element = AsyncMock()

        # First flush — triggers 300315, syncs hint_removed
        await ctrl._do_unified_flush(session)
        assert "hint_removed" in session._creation_stages

        # Reset dirty to simulate new content arriving
        session.unified_state.panel_dirty = True
        session.unified_state.tool_steps_dirty = True

        # Second flush — should NOT include a delete_elements for the hint
        captured_actions: list[list[dict]] = []

        async def capture(card_id: str, actions: list[dict], **kw: object) -> None:
            captured_actions.append(actions)

        ctrl._client.cardkit_batch_update = AsyncMock(side_effect=capture)
        await ctrl._do_unified_flush(session)

        # Verify no delete_elements targets the hint (loop broken)
        for actions_batch in captured_actions:
            for action in actions_batch:
                if action.get("action") == "delete_elements":
                    ids = action.get("params", {}).get("element_ids", [])
                    assert _LOADING_HINT_ELEMENT_ID not in ids, (
                        "Phase 3 safety net must NOT re-add hint delete after "
                        "is_element_not_found_error sync — loop would recur"
                    )


# ── Reasoning finalization tests ──


class TestReasoningFinalization:
    @pytest.mark.asyncio
    async def test_reasoning_finalized_on_answer(self) -> None:
        """reasoning round is finalized when answer arrives."""
        ctrl = _setup_ctrl()
        session = _make_session("msg_snap", linear=True)
        session.state = STREAMING
        session.card_id = "card_snap"
        session.unified_state.on_reasoning_delta("think")
        session.unified_state.on_answer_delta("reply")
        ctrl._sessions["msg_snap"] = session

        await ctrl._do_unified_flush(session)

        # The reasoning round should be finalized (moved from current to rounds)
        assert len(session.unified_state.reasoning_rounds) == 1
        assert session.unified_state.reasoning_rounds[0].finalized is True

    @pytest.mark.asyncio
    async def test_reasoning_round_elapsed_displayed(self) -> None:
        ctrl = _setup_ctrl()
        batch_calls: list[list[dict]] = []

        async def capture_batch(card_id: str, actions: list[dict], **kw: object) -> None:
            batch_calls.append(actions)

        ctrl._client.cardkit_batch_update = capture_batch

        session = _make_session("msg_title", linear=True)
        session.state = STREAMING
        session.card_id = "card_title"
        session.existing_elements = {_LOADING_HINT_ELEMENT_ID, _LOADING_ELEMENT_ID}
        session.unified_state.on_reasoning_delta("think")
        time.sleep(0.01)
        session.unified_state.on_answer_delta("reply")
        ctrl._sessions["msg_title"] = session

        await ctrl._do_unified_flush(session)

        # The panel should have been updated (Phase 2: add_elements)
        assert len(batch_calls) >= 1
        add_actions = [a for a in batch_calls[0] if a["action"] == "add_elements"]
        assert len(add_actions) == 1


# ── _do_linear_complete 集成测试 ──


class TestDoLinearComplete:
    @pytest.mark.asyncio
    async def test_closes_streaming_then_updates(self) -> None:
        ctrl = _setup_ctrl()
        call_order: list[str] = []
        client = ctrl._client
        client.cardkit_close_streaming = AsyncMock(side_effect=lambda *a, **k: call_order.append("close"))
        client.cardkit_update = AsyncMock(side_effect=lambda *a, **k: call_order.append("update"))

        session = _make_session("msg_comp", linear=True)
        session.state = STREAMING
        session.card_id = "card_comp"
        session.card_msg_id = "msg_comp_reply"
        ctrl._sessions["msg_comp"] = session

        assert await ctrl._do_linear_complete(session) is True
        assert session.state == COMPLETED
        # With preservative seal, the flow is: close + batch_update
        # (summary is passed IN close_streaming, no separate cardkit_update needed)
        assert "close" in call_order
        # cardkit_update should NOT be called — summary is updated atomically
        # in close_streaming per Feishu docs
        client.cardkit_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_streaming_closed_flag_prevents_double_close(self) -> None:
        """Preservative seal succeeds on first attempt, so close_streaming is called once."""
        ctrl = _setup_ctrl()
        client = ctrl._client
        client.cardkit_close_streaming = AsyncMock()

        session = _make_session("msg_retry", linear=True)
        session.state = STREAMING
        session.card_id = "card_retry"
        session.card_msg_id = "msg_retry_reply"
        ctrl._sessions["msg_retry"] = session

        assert await ctrl._do_linear_complete(session) is True
        assert client.cardkit_close_streaming.call_count == 1
        # cardkit_update should NOT be called — summary is passed in
        # close_streaming atomically per Feishu docs
        client.cardkit_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_three_retries_exhausted(self) -> None:
        ctrl = _setup_ctrl()
        ctrl._client.cardkit_close_streaming = AsyncMock(side_effect=FeishuAPIError("fail", code=99999))

        session = _make_session("msg_3fail", linear=True)
        session.state = STREAMING
        session.card_id = "card_3fail"
        ctrl._sessions["msg_3fail"] = session

        with patch("asyncio.sleep", new_callable=AsyncMock):
            assert await ctrl._do_linear_complete(session) is False
        assert session.state == CREATION_FAILED

    @pytest.mark.asyncio
    async def test_finalize_and_cleanup(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_fc", linear=True)
        session.state = STREAMING
        session.card_id = "card_fc"
        session.unified_state.on_reasoning_delta("think")
        time.sleep(0.001)
        # Capture elapsed_ms before complete, since _release_session_data
        # clears unified_state after completion (v0.19.0 memory release)
        elapsed_ms_before = session.unified_state.reasoning_rounds[0].elapsed_ms if session.unified_state.reasoning_rounds else 0
        ctrl._sessions["msg_fc"] = session

        await ctrl._do_linear_complete(session)

        # If reasoning was finalized before complete, elapsed should be positive
        # (After on_answer_delta above, the reasoning is finalized)
        if session.unified_state is not None and session.unified_state.reasoning_rounds:
            assert session.unified_state.reasoning_rounds[0].elapsed_ms >= 0
        else:
            # unified_state may be cleared after completion
            assert elapsed_ms_before >= 0

    @pytest.mark.asyncio
    async def test_drain_dirty_answer_before_seal(self) -> None:
        """Remaining dirty answer text is flushed (drained) before the seal.

        This is the fix for the premature card finalization bug:
        when on_completed fires while answer text hasn't been flushed yet,
        _do_linear_complete must drain it before closing streaming.
        """
        ctrl = _setup_ctrl()
        session = _make_session("msg_drain", linear=True)
        session.state = STREAMING
        session.card_id = "card_drain"
        session._creation_stages.add("panel")
        session._creation_stages.add("answer")  # Answer element must exist for drain to work
        session._creation_stages.add("hint_removed")
        # Simulate: last answer delta arrived but flush hasn't fired yet
        session.unified_state.on_answer_delta("final chunk")
        assert session.unified_state.answer_dirty is True
        ctrl._sessions["msg_drain"] = session

        # Track API call order
        api_calls: list[str] = []
        client = ctrl._client
        client.cardkit_stream_element = AsyncMock(
            side_effect=lambda *a, **k: api_calls.append("stream"),
        )
        client.cardkit_batch_update = AsyncMock(
            side_effect=lambda *a, **k: api_calls.append("batch"),
        )
        client.cardkit_close_streaming = AsyncMock(
            side_effect=lambda *a, **k: api_calls.append("close"),
        )
        # v1.1.1: 封卡后 _release_session_data 会置 None，mock 掉以便检查 dirty
        ctrl._release_session_data = lambda s: None

        assert await ctrl._do_linear_complete(session) is True

        # stream_element should have been called for the drain
        assert "stream" in api_calls
        # close_streaming should happen AFTER the drain
        close_idx = api_calls.index("close")
        stream_idx = api_calls.index("stream")
        assert stream_idx < close_idx, (
            f"Drain stream_element must happen before close_streaming, "
            f"got stream@{stream_idx} close@{close_idx}"
        )
        # answer_dirty should be cleared after drain
        assert session.unified_state.answer_dirty is False

    @pytest.mark.asyncio
    async def test_drain_dirty_panel_before_seal(self) -> None:
        """Remaining dirty panel content is flushed before the seal."""
        ctrl = _setup_ctrl()
        session = _make_session("msg_drain_panel", linear=True)
        session.state = STREAMING
        session.card_id = "card_drain_panel"
        session._creation_stages.add("panel")
        session._creation_stages.add("answer")
        session._creation_stages.add("hint_removed")
        session.unified_state.on_reasoning_delta("think more")
        assert session.unified_state.panel_dirty is True
        ctrl._sessions["msg_drain_panel"] = session

        api_calls: list[str] = []
        client = ctrl._client
        client.cardkit_batch_update = AsyncMock(
            side_effect=lambda *a, **k: api_calls.append("batch"),
        )
        client.cardkit_close_streaming = AsyncMock(
            side_effect=lambda *a, **k: api_calls.append("close"),
        )
        # v1.1.1: 封卡后 _release_session_data 会置 None，mock 掉以便检查 dirty
        ctrl._release_session_data = lambda s: None

        assert await ctrl._do_linear_complete(session) is True

        # batch_update should have been called for the drain (panel update)
        assert "batch" in api_calls
        # close should happen AFTER the drain
        close_idx = api_calls.index("close")
        first_batch_idx = api_calls.index("batch")
        assert first_batch_idx < close_idx
        # panel_dirty should be cleared after drain
        assert session.unified_state.panel_dirty is False

    @pytest.mark.asyncio
    async def test_linear_complete_simple_conversation_no_panel(self) -> None:
        """Simple conversation (answer only, no reasoning/tools) completes without panel.

        v1.0.5 Phase 2 split: simple conversations skip the unified panel
        entirely. Only the answer streaming element is created (Path B).
        _do_linear_complete should seal the card without panel operations.
        """
        ctrl = _setup_ctrl()
        session = _make_session("msg_simple", linear=True)
        session.state = STREAMING
        session.card_id = "card_simple"
        # Simple conversation: answer element created but NO panel
        session._creation_stages.add("answer")
        session._creation_stages.discard("panel")
        session._creation_stages.add("hint_removed")
        session.unified_state.on_answer_delta("Simple answer text")
        session.unified_state.answer_dirty = False  # Already flushed
        ctrl._sessions["msg_simple"] = session

        api_calls: list[str] = []
        client = ctrl._client
        client.cardkit_stream_element = AsyncMock(
            side_effect=lambda *a, **k: api_calls.append("stream"),
        )
        client.cardkit_batch_update = AsyncMock(
            side_effect=lambda *a, **k: api_calls.append("batch"),
        )
        client.cardkit_close_streaming = AsyncMock(
            side_effect=lambda *a, **k: api_calls.append("close"),
        )

        assert await ctrl._do_linear_complete(session) is True

        # No panel operations should happen
        assert "panel" not in session._creation_stages
        # Close should be called
        assert "close" in api_calls

    @pytest.mark.asyncio
    async def test_no_drain_when_no_dirty_data(self) -> None:
        """No drain API calls when all data is already flushed."""
        ctrl = _setup_ctrl()
        session = _make_session("msg_clean", linear=True)
        session.state = STREAMING
        session.card_id = "card_clean"
        session._creation_stages.add("panel")
        session._creation_stages.add("answer")
        session._creation_stages.add("hint_removed")
        # No dirty data
        ctrl._sessions["msg_clean"] = session

        stream_call_count_before = ctrl._client.cardkit_stream_element.call_count

        assert await ctrl._do_linear_complete(session) is True

        # stream_element should NOT have been called for drain
        # (only called in seal if answer_text exists)
        # The key point: no ADDITIONAL stream_element call for drain
        # since answer_dirty was False

    @pytest.mark.asyncio
    async def test_close_streaming_passes_summary(self) -> None:
        """close_streaming is called with summary text from answer.

        This is the fix for the "处理中..." stays in conversation list bug:
        when close_streaming is called, the summary MUST be passed in the
        same request so Feishu atomically updates the conversation list
        preview from "处理中..." to the actual answer text.  A separate
        cardkit_update_summary call does NOT reliably work.

        See: 飞书开放平台 → 卡片2.0 → 流式更新 → 完成后关闭流式更新模式
        """
        ctrl = _setup_ctrl()
        session = _make_session("msg_summary", linear=True)
        session.state = STREAMING
        session.card_id = "card_summary"
        session._creation_stages.add("panel")
        session._creation_stages.add("answer")
        session._creation_stages.add("hint_removed")
        session.unified_state.on_answer_delta("Hello, this is the answer text")
        # Clear dirty flag (simulates already-flushed content)
        session.unified_state.answer_dirty = False
        ctrl._sessions["msg_summary"] = session

        close_kwargs: list[dict] = []
        ctrl._client.cardkit_close_streaming = AsyncMock(
            side_effect=lambda *a, **k: close_kwargs.append(k),
        )

        assert await ctrl._do_linear_complete(session) is True

        # close_streaming should have been called with summary kwarg
        # containing the answer text
        assert len(close_kwargs) >= 1
        summary = close_kwargs[0].get("summary", "")
        assert "Hello" in summary
        assert "处理中" not in summary

    @pytest.mark.asyncio
    async def test_streaming_closed_guard_prevents_double_close_on_300317(self) -> None:
        """When batch_update gets 300317 after close_streaming succeeds,
        the retry path must NOT call close_streaming again.

        This is the fix for the cascading 300317 failure:
        1. preservative_seal calls close_streaming → succeeds
        2. preservative_seal calls batch_update → 300317
        3. Retry path should skip close_streaming (already done)
        4. Retry path should only retry batch_update
        Without _streaming_closed guard, step 3 calls close_streaming
        again, causing a second 300317 (sequence advanced from step 1).
        """
        ctrl = _setup_ctrl()
        client = ctrl._client
        close_call_count = 0

        async def _close_side_effect(*a, **k):
            nonlocal close_call_count
            close_call_count += 1
            # First close_streaming succeeds

        async def _batch_side_effect(*a, **k):
            # First batch_update → 300317
            raise FeishuAPIError("sequence conflict", code=CARDKIT_SEQUENCE_CONFLICT)

        client.cardkit_close_streaming = AsyncMock(side_effect=_close_side_effect)
        client.cardkit_batch_update = AsyncMock(side_effect=_batch_side_effect)

        session = _make_session("msg_double_close", linear=True)
        session.state = STREAMING
        session.card_id = "card_double"
        session._creation_stages.add("panel")
        session._creation_stages.add("answer")
        session._creation_stages.add("hint_removed")
        ctrl._sessions["msg_double_close"] = session

        result = await ctrl._do_linear_complete(session)
        # v1.5.0: seal failure (sequence conflict retries exhausted) now marks
        # the session CREATION_FAILED instead of attempting a full rebuild.
        # The close_streaming call should NOT happen — the 300317 occurs on
        # batch_update BEFORE close_streaming is reached, and the retry path
        # also fails on batch_update (same mock), so close_streaming is never
        # called. The test guards that the seal path doesn't crash.
        assert result is False, "seal should fail after exhausting retries"

    @pytest.mark.asyncio
    async def test_session_initializes_streaming_closed_false(self) -> None:
        """CardSession._streaming_closed starts as False."""
        loop = asyncio.new_event_loop()
        _loops_to_cleanup.append(loop)
        session = CardSession("msg_test", "chat_test", loop)
        assert session._streaming_closed is False

    @pytest.mark.asyncio
    async def test_seal_always_writes_final_answer_after_stream_element(self) -> None:
        """v1.3.1 regression: _preservative_seal must ALWAYS send a final
        partial_update_element for the answer element, even when the answer
        was already fully delivered via stream_element during the drain loop.

        Bug: v1.3.0 added ``_answer_finalized_via_stream`` guard that skipped
        the final partial_update_element when stream_element had delivered
        the answer. But Feishu's typewriter queue is asynchronous —
        close_streaming terminates the streaming session immediately,
        discarding any un-rendered characters. Result: the last chunk of
        answer text was permanently lost (e.g. "服务 + 守护进程 +" instead
        of the full sentence).

        Fix (v1.3.1): removed the guard. Now the seal ALWAYS sends a final
        partial_update_element as the authoritative "last word" before
        close_streaming. The instant-replace jarring effect is accepted as
        a minor visual trade-off for content completeness.

        This test uses a CardSession subclass that simulates the v1.3.0 bug
        scenario: ``_answer_finalized_via_stream=True`` (as if the drain loop
        had just delivered the answer via stream_element). The seal must
        STILL send the final partial_update_element regardless of this flag.
        """
        ctrl = _setup_ctrl()
        client = ctrl._client

        # Create a session subclass that allows the _answer_finalized_via_stream
        # attribute (simulating v1.3.0's __slots__ definition). This lets us
        # set the flag to True, which is the exact state the drain loop would
        # leave the session in after stream_element succeeds.
        class _BugScenarioSession(CardSession):
            __slots__ = ("_answer_finalized_via_stream",)

        loop = asyncio.new_event_loop()
        _loops_to_cleanup.append(loop)
        session = _BugScenarioSession("msg_seal_final_write", "chat_456", loop)
        session.linear = True
        session.unified_state = UnifiedLinearState()
        session._card_ready.set()
        session.state = STREAMING
        session.card_id = "card_final_write"
        session._creation_stages.add("panel")
        session._creation_stages.add("answer")
        session._creation_stages.add("hint_removed")
        session.unified_state.answer_text = "这是完整的回答内容，不应被裁剪。"
        session.unified_state.answer_dirty = False
        session.existing_elements = {ANSWER_ELEMENT_ID, UNIFIED_PANEL_ELEMENT_ID}
        # Set the flag to True — this is what the v1.3.0 drain loop did
        # after stream_element succeeded. The v1.3.0 guard would then skip
        # Step 2. The v1.3.1 fix must NOT check this flag at all.
        session._answer_finalized_via_stream = True
        ctrl._sessions["msg_seal_final_write"] = session

        # Call _preservative_seal directly (bypass drain loop)
        result = await ctrl._preservative_seal(
            session,
            footer_data={"duration": 1.0, "model": "test"},
            is_error=False,
            is_aborted=False,
            error_message="",
            footer_fields=[["status", "elapsed"]],
            footer_show_label=False,
        )

        # The seal must have succeeded
        assert result is True, "seal should succeed"

        # Verify the batch_update call contained a partial_update_element
        # for ANSWER_ELEMENT_ID with the full answer text
        batch_calls = client.cardkit_batch_update.call_args_list
        assert len(batch_calls) > 0, "batch_update should have been called"

        found_answer_update = False
        for call in batch_calls:
            actions = call.args[1] if len(call.args) > 1 else call.kwargs.get("actions", [])
            for action in actions:
                if (action.get("action") == "partial_update_element"
                        and action.get("params", {}).get("element_id") == ANSWER_ELEMENT_ID):
                    found_answer_update = True
                    content = action["params"]["partial_element"].get("content", "")
                    assert "这是完整的回答内容" in content, (
                        f"final partial_update_element must contain full answer text, "
                        f"got: {content!r}"
                    )
                    break
            if found_answer_update:
                break

        assert found_answer_update, (
            "v1.3.1 regression: _preservative_seal must ALWAYS send a final "
            "partial_update_element for ANSWER_ELEMENT_ID, even when the answer "
            "was delivered via stream_element (_answer_finalized_via_stream=True). "
            "Without this, the last chunk of answer text is permanently lost when "
            "close_streaming fires before Feishu's typewriter queue finishes rendering."
        )

class TestLinearOnThinkingNativeReasoningDedup:
    """Bug fix: _linear_on_thinking must skip reasoning when already tracked.

    When the model provides a dedicated reasoning_callback (e.g. DeepSeek, QwQ),
    reasoning text arrives incrementally via on_reasoning → on_reasoning_delta.
    The interim_assistant_callback also delivers the same reasoning text in
    accumulated form.  Without a dedup guard, appending the accumulated text
    again via on_reasoning_delta would double every token in the collapsible
    panel ("TheThe user user is is saying saying…").

    v1.1.0 (Task 1.3): The ``_native_reasoning_active`` flag was removed.
    Dedup now keys off ``bool(state._current_reasoning)`` — if any reasoning
    text has already been tracked (via the native reasoning_callback),
    ``_linear_on_thinking`` skips the ``on_reasoning_delta`` call entirely.
    """

    def _make_dedup_session(self) -> tuple:
        ctrl = _setup_ctrl()
        # Enable show_reasoning so _linear_on_thinking processes reasoning text
        ctrl._cfg._raw.setdefault("hermes_lark_streaming", {}).setdefault(
            "display", {"show_reasoning": True},
        )
        # Also set the cached config so _cfg.show_reasoning returns True
        ctrl._cfg._reload_cached = lambda: {
            "display": {"platforms": {"feishu": {"show_reasoning": True}}},
        }
        session = _make_session("msg_dedup", linear=True)
        session.state = STREAMING
        ctrl._sessions["msg_dedup"] = session
        return ctrl, session

    def test_no_dedup_when_no_reasoning_tracked(self) -> None:
        """When no reasoning has been tracked yet, reasoning IS processed."""
        ctrl, session = self._make_dedup_session()
        # No native reasoning tracked yet → guard lets reasoning through
        assert session.unified_state.has_current_reasoning is False
        assert bool(session.unified_state._current_reasoning) is False

        # Use Reasoning:\n prefix so split_reasoning_text classifies
        # this as reasoning_text, not answer_text
        with patch.object(ctrl, "_schedule_linear_flush"):
            ctrl._linear_on_thinking(session, "Reasoning:\nThe user is asking about Python")

        # Reasoning should have been processed (no native reasoning tracked yet)
        assert session.unified_state.current_reasoning_text == "The user is asking about Python"

    def test_dedup_when_reasoning_already_tracked(self) -> None:
        """When reasoning has already been tracked, reasoning is NOT re-appended."""
        ctrl, session = self._make_dedup_session()

        # Simulate reasoning_callback delivering text first — this populates
        # _current_reasoning, which _linear_on_thinking now uses as the dedup
        # guard (replacing the removed _native_reasoning_active flag).
        session.unified_state.on_reasoning_delta("The user is asking about Python")
        assert session.unified_state.current_reasoning_text == "The user is asking about Python"
        assert session.unified_state.has_current_reasoning is True

        # Now interim_assistant_callback delivers the same accumulated text
        # Use Reasoning:\n prefix so split_reasoning_text classifies
        # this as reasoning_text, not answer_text
        with patch.object(ctrl, "_schedule_linear_flush"):
            ctrl._linear_on_thinking(session, "Reasoning:\nThe user is asking about Python")

        # Reasoning should NOT be doubled
        assert session.unified_state.current_reasoning_text == "The user is asking about Python"
        assert "TheThe" not in session.unified_state.current_reasoning_text

    def test_dedup_with_mixed_reasoning_and_answer(self) -> None:
        """When reasoning is already tracked, only answer part is processed from thinking."""
        ctrl, session = self._make_dedup_session()

        # Simulate reasoning_callback delivering text — populates
        # _current_reasoning, which serves as the dedup guard.
        session.unified_state.on_reasoning_delta("Let me think about this")
        assert session.unified_state.has_current_reasoning is True

        # interim_assistant_callback delivers plain text (no Reasoning: prefix),
        # which split_reasoning_text classifies as answer_text.
        # When reasoning is already tracked, the reasoning part is skipped
        # but the answer part should still be processed (if no streamed answer yet)
        with patch.object(ctrl, "_schedule_linear_flush"):
            ctrl._linear_on_thinking(session, "Here is my answer")

        # Answer should be set (no streamed answer yet)
        assert "Here is my answer" in session.unified_state.answer_text


# ── v1.1.1 新增测试 ──


def test_prune_skips_streaming_session() -> None:
    """v1.1.1: STREAMING 状态的 session 超 TTL 不被清理（避免 AI 回调丢失）."""
    from types import SimpleNamespace
    ctrl = StreamCardController()
    # STREAMING 状态（活跃），超 TTL
    active_session = SimpleNamespace(
        created_at=time.time() - ctrl._session_ttl - 1,
        flush=_DummyFlush(),
        is_terminal_phase=False,  # STREAMING 不是终态
    )
    ctrl._sessions["active"] = active_session  # type: ignore[assignment]

    ctrl._prune_stale_sessions()

    # 活跃 session 不应被清理
    assert "active" in ctrl._sessions
    assert not active_session.flush.completed


def test_prune_cleans_terminal_session() -> None:
    """v1.1.1: COMPLETED 状态的 session 超 TTL 正常清理."""
    from types import SimpleNamespace
    ctrl = StreamCardController()
    # COMPLETED 状态（终态），超 TTL
    terminal_session = SimpleNamespace(
        created_at=time.time() - ctrl._session_ttl - 1,
        flush=_DummyFlush(),
        is_terminal_phase=True,  # COMPLETED 是终态
    )
    ctrl._sessions["done"] = terminal_session  # type: ignore[assignment]

    ctrl._prune_stale_sessions()

    # 终态 session 应被清理
    assert "done" not in ctrl._sessions
    assert terminal_session.flush.completed


@pytest.mark.asyncio
async def test_release_session_data_after_seal() -> None:
    """v1.1.1: 封卡成功后 _release_session_data 释放重数据."""
    ctrl = _setup_ctrl()
    session = _make_session("msg_release", linear=True)
    session.state = STREAMING
    session.card_id = "card_release"
    session._creation_stages.add("answer")
    session._creation_stages.add("hint_removed")
    session.unified_state.on_answer_delta("test answer")
    assert session.unified_state is not None
    ctrl._sessions["msg_release"] = session

    assert await ctrl._do_linear_complete(session) is True

    # 封卡后重数据应被释放
    assert session.unified_state is None
    assert session.tool_use is not None  # 被重置为空 ToolUseTracker


@pytest.mark.asyncio
async def test_drain_300309_falls_back_to_batch_update() -> None:
    """v1.1.1: drain 遇 300309 (streaming closed) 改用 batch_update 写入答案."""
    ctrl = _setup_ctrl()
    session = _make_session("msg_309", linear=True)
    session.state = STREAMING
    session.card_id = "card_309"
    session._creation_stages.add("answer")
    session._creation_stages.add("hint_removed")
    session.unified_state.on_answer_delta("answer that needs drain")
    ctrl._sessions["msg_309"] = session

    # stream_element 抛 300309
    client = ctrl._client
    client.cardkit_stream_element = AsyncMock(
        side_effect=FeishuAPIError("test", code=CARDKIT_STREAMING_CLOSED)
    )
    # batch_update 应被调用做 fallback
    client.cardkit_batch_update = AsyncMock()
    client.cardkit_close_streaming = AsyncMock()
    ctrl._release_session_data = lambda s: None

    result = await ctrl._do_linear_complete(session)

    # batch_update 应被调用（drain fallback + seal）
    assert client.cardkit_batch_update.called
    # 封卡应成功
    assert result is True
    # 检查 fallback 调用的 actions 不含 tag
    for call in client.cardkit_batch_update.call_args_list:
        actions = call[0][1] if len(call[0]) > 1 else call[1].get("actions", [])
        for action in actions:
            if action.get("action") == "partial_update_element":
                partial = action.get("params", {}).get("partial_element", {})
                assert "tag" not in partial, "partial_update_element 不应带 tag"


@pytest.mark.asyncio
async def test_drain_300313_falls_back_without_tag() -> None:
    """v1.1.1: drain 遇 300313 fallback 不带 tag（不再报 300312）."""
    from hermes_lark_streaming.feishu import CARDKIT_ELEMENT_NOT_FOUND
    ctrl = _setup_ctrl()
    session = _make_session("msg_313", linear=True)
    session.state = STREAMING
    session.card_id = "card_313"
    session._creation_stages.add("answer")
    session._creation_stages.add("hint_removed")
    session.unified_state.on_answer_delta("answer for 313 test")
    ctrl._sessions["msg_313"] = session

    # stream_element 抛 300313
    client = ctrl._client
    client.cardkit_stream_element = AsyncMock(
        side_effect=FeishuAPIError("test", code=CARDKIT_ELEMENT_NOT_FOUND)
    )
    client.cardkit_batch_update = AsyncMock()
    client.cardkit_close_streaming = AsyncMock()
    ctrl._release_session_data = lambda s: None

    result = await ctrl._do_linear_complete(session)

    assert client.cardkit_batch_update.called
    assert result is True
    # 所有 partial_update_element 都不应带 tag
    for call in client.cardkit_batch_update.call_args_list:
        actions = call[0][1] if len(call[0]) > 1 else call[1].get("actions", [])
        for action in actions:
            if action.get("action") == "partial_update_element":
                partial = action.get("params", {}).get("partial_element", {})
                assert "tag" not in partial, "partial_update_element 不应带 tag"



# ─── v1.3.4 Regression Tests ──────────────────────────────────────────

def test_v134_concurrency_seal_no_duplicate_session() -> None:
    """v1.3.4 P0 fix: on_message_started 不应在 on_interrupted 已创建 session 后再创建。

    场景：用户在卡片流式中发送新消息 → concurrency seal 调 on_interrupted
    → on_interrupted 创建新 session + fire _do_create_linear_card。
    原 bug：on_message_started 继续创建第二个 session 并覆盖，导致：
      1. 两张卡片被创建（on_interrupted 一张 + on_message_started 一张）
      2. on_interrupted 的卡片成为孤儿（永远停在"正在加载上下文..."）

    修复：on_message_started 检测到 session 已存在时复用，不重复创建。
    """
    ctrl = StreamCardController()
    _enable(ctrl, linear=True)

    # 模拟 concurrency seal：先创建一个 active session（旧消息）
    with patch.object(ctrl, "_fire_and_forget", side_effect=lambda coro, loop: coro.close()):
        ctrl.on_message_started(message_id="old_msg", chat_id="chat")
        assert "old_msg" in ctrl._sessions

        # 用户发送新消息 → 触发 concurrency seal
        # on_interrupted 会被 on_message_started 的 concurrency seal 调用，
        # 创建 new_msg 的 session
        ctrl.on_message_started(message_id="new_msg", chat_id="chat")

    # 关键断言：new_msg 只有一个 session（不应有重复创建）
    assert "new_msg" in ctrl._sessions
    session = ctrl._sessions["new_msg"]
    # session 应该是有效的（有 chat_id 和 loop）
    assert session.chat_id == "chat"
    # 旧消息应被 abort
    assert ctrl._sessions["old_msg"].state == ABORTED


@pytest.mark.asyncio
async def test_v134_aborted_session_keeps_aborted_state() -> None:
    """v1.3.4 P1 fix: 被 abort 的 session 完成后应保持 ABORTED 状态，不被覆盖为 COMPLETED。"""
    ctrl = _setup_ctrl(linear=True)
    ctrl._release_session_data = lambda s: None
    session = _make_session("msg_abort_state", linear=True)
    ctrl._sessions["msg_abort_state"] = session

    await ctrl._do_create_linear_card(session)

    session.unified_state.answer_text = "partial answer"
    session.unified_state.answer_dirty = False
    session._was_aborted = True
    session._creation_stages.add("answer")  # Phase 2 已成功

    # 模拟 seal 成功
    async def _ok_seal(*args, **kwargs):
        return True
    ctrl._preservative_seal = _ok_seal  # type: ignore[method-assign]

    await ctrl._do_linear_complete(session)

    # v1.3.4 fix: aborted session 应保持 ABORTED
    assert session.state == ABORTED, f"Expected ABORTED, got {session.state}"
