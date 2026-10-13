"""v1.8.1 regression tests — five confirmed fixes from the GitHub issue audit.

Covers (all validated against hermes-agent v0.21.0 source before fixing):

Re-validated on hermes-agent v0.21.1 (tag v2026.9.7, v1.8.2): all 21 tests
plus the full suite (940 unit + 22 e2e + 30 integration) pass unchanged —
the v1.8.1 fixes are version-insensitive (hermes 0.21.1's file split moved
internal symbol addresses but preserved every production surface this
plugin patches).

  * P1 thread-scope isolation: the concurrency seal key drops from
    ``chat_id`` to ``thread_id or chat_id`` — different topics in one Feishu
    chat are independent hermes sessions (session key includes thread_id,
    gateway/session.py:1198) and must not seal each other's cards.
  * P1 continuation-route race: TTL prune of a terminal session no longer
    severs its continuation route while the continuation target is still
    active (previously: long task > TTL → route popped → on_completed(old)
    landed nowhere → continuation card spun forever + session leak).
  * P1 error-string seal: hermes _run_agent converts agent exceptions into
    error *strings* (run.py except branch) — the COMPLETE wrapper now seals
    the streaming card with that error instead of leaving it spinning.
  * P1 START-hook platform guard: non-Feishu inbound messages no longer
    create streaming-card sessions (reply on a non-om_ id always failed).
  * P2 /bg reply anchor: the /bg wrapper forwards hermes' event_message_id
    (om_ anchor) as the card reply anchor — background cards could never
    be created since v1.4.0 because the synthetic task_id was used.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from hermes_lark_streaming.controller import CardSession, StreamCardController
from hermes_lark_streaming.controller.mixin import (
    COMPLETED,
    STREAMING,
)
from hermes_lark_streaming.state.linear import UnifiedLinearState

# Reuse helpers/loop-cleanup registry from the shared controller tests.
from tests.test_controller import _make_session, _setup_ctrl

# ══════════════════════════════════════════════════════════════════════
# P1 — thread-scope isolation for the concurrency seal
# ══════════════════════════════════════════════════════════════════════

class TestThreadScopeIsolation:
    """v1.8.1: concurrency seal compares (thread_id or chat_id), not chat_id."""

    @staticmethod
    def _ctrl_with_active_session(msg_id: str, thread_id: str | None) -> StreamCardController:
        ctrl = _setup_ctrl()
        session = _make_session(msg_id)
        session.thread_id = thread_id
        ctrl._sessions[msg_id] = session
        return ctrl

    def test_seal_skipped_for_other_topic_same_chat(self) -> None:
        """Different topics share chat_id but are independent hermes
        sessions — a new message in topic B must NOT seal topic A's card."""
        ctrl = self._ctrl_with_active_session("om_topic_a", thread_id="ot_a")
        seal = MagicMock()
        ctrl.on_interrupted = seal  # instance attr wins over the method

        ctrl.on_message_started(
            message_id="om_topic_b",
            chat_id="chat_456",
            anchor_id=None,
            thread_id="ot_b",
        )

        seal.assert_not_called()
        # topic A's session untouched
        assert ctrl._sess_get("om_topic_a") is not None

    def test_seal_fires_within_same_topic(self) -> None:
        """Same topic = same hermes session — a new message still interrupts
        the old card (unchanged semantics inside one topic)."""
        ctrl = self._ctrl_with_active_session("om_t1", thread_id="ot_1")
        seal = MagicMock()
        ctrl.on_interrupted = seal

        ctrl.on_message_started(
            message_id="om_t2",
            chat_id="chat_456",
            anchor_id=None,
            thread_id="ot_1",
        )

        seal.assert_called_once()

    def test_seal_fires_without_thread_falls_back_to_chat(self) -> None:
        """Plain group chats / DMs (thread_id=None) keep the legacy
        chat-wide interrupt behaviour."""
        ctrl = self._ctrl_with_active_session("om_plain_a", thread_id=None)
        seal = MagicMock()
        ctrl.on_interrupted = seal

        ctrl.on_message_started(
            message_id="om_plain_b",
            chat_id="chat_456",
            anchor_id=None,
            thread_id=None,
        )

        seal.assert_called_once()

    def test_topic_session_not_disturbed_by_plain_chat_message(self) -> None:
        """A topic-internal active card must not be sealed by a message
        sent to the parent chat outside any topic (different hermes
        sessions both ways)."""
        ctrl = self._ctrl_with_active_session("om_topic_a", thread_id="ot_a")
        seal = MagicMock()
        ctrl.on_interrupted = seal

        ctrl.on_message_started(
            message_id="om_parent_chat",
            chat_id="chat_456",
            anchor_id=None,
            thread_id=None,  # outside any topic
        )

        seal.assert_not_called()

    def test_started_session_records_thread_id(self) -> None:
        ctrl = _setup_ctrl()
        ctrl.on_message_started(
            message_id="om_new",
            chat_id="chat_456",
            anchor_id=None,
            thread_id="ot_rec",
        )
        session = ctrl._sess_get("om_new")
        assert session is not None
        assert session.thread_id == "ot_rec"

    def test_on_interrupted_new_session_inherits_thread_id(self) -> None:
        ctrl = _setup_ctrl()
        ctrl.on_interrupted(
            old_message_id="om_old",
            new_message_id="om_new",
            chat_id="chat_456",
            anchor_id=None,
            thread_id="ot_inherit",
        )
        session = ctrl._sess_get("om_new")
        assert session is not None
        assert session.thread_id == "ot_inherit"

    def test_reactivation_continuation_inherits_thread_id(self) -> None:
        """The continuation card stays in the original topic's scope."""
        ctrl = _setup_ctrl(linear=True)
        stale = _make_session("om_stale", linear=True)
        stale.anchor_id = "om_stale"
        stale.state = STREAMING
        stale._streaming_closed = True
        stale.thread_id = "ot_stale"
        ctrl._sessions["om_stale"] = stale

        new_id = ctrl._maybe_reactivate_for_continuation("om_stale")
        assert new_id is not None
        new_session = ctrl._sess_get(new_id)
        assert new_session is not None
        assert new_session.thread_id == "ot_stale"


# ══════════════════════════════════════════════════════════════════════
# P1 — TTL prune preserves an in-flight continuation route
# ══════════════════════════════════════════════════════════════════════

class TestPruneDefersContinuationRoute:
    """v1.8.1: prune no longer unconditionally pops the continuation map."""

    @staticmethod
    def _expired(session: CardSession) -> CardSession:
        session.created_at = time.time() - 10_000.0
        return session

    def test_prune_defers_while_continuation_active(self) -> None:
        """Terminal old session whose continuation target is still active
        (streaming) is NOT reclaimed — its route must survive for the
        upcoming on_completed(old) redirect."""
        ctrl = _setup_ctrl()
        ctrl._session_ttl = 1.0

        old = self._expired(_make_session("om_old"))
        old.state = COMPLETED
        ctrl._sessions["om_old"] = old

        cont = self._expired(_make_session("om_old-cont-1"))
        cont.state = STREAMING
        cont.linear = True
        cont.unified_state = UnifiedLinearState()
        ctrl._sessions["om_old-cont-1"] = cont

        ctrl._register_continuation("om_old", "om_old-cont-1")

        ctrl._prune_stale_sessions()

        assert ctrl._sess_get("om_old") is not None
        assert ctrl._resolve_continuation_id("om_old") == "om_old-cont-1"

    def test_prune_reclaims_once_continuation_terminal(self) -> None:
        """Full lifecycle closure: continuation reaches a terminal state →
        prune reclaims both sessions and the route is gone."""
        ctrl = _setup_ctrl()
        ctrl._session_ttl = 1.0

        old = self._expired(_make_session("om_old"))
        old.state = COMPLETED
        ctrl._sessions["om_old"] = old

        cont = self._expired(_make_session("om_old-cont-1"))
        cont.state = COMPLETED
        ctrl._sessions["om_old-cont-1"] = cont

        ctrl._register_continuation("om_old", "om_old-cont-1")

        ctrl._prune_stale_sessions()

        assert ctrl._sess_get("om_old") is None
        assert ctrl._sess_get("om_old-cont-1") is None
        assert ctrl._resolve_continuation_id("om_old") is None

    def test_two_round_closure_after_continuation_finishes_late(self) -> None:
        """Round 1 defers (continuation still streaming); the continuation
        finishes; round 2 reclaims everything — no permanent leak."""
        ctrl = _setup_ctrl()
        ctrl._session_ttl = 1.0

        old = self._expired(_make_session("om_old"))
        old.state = COMPLETED
        ctrl._sessions["om_old"] = old

        cont = self._expired(_make_session("om_old-cont-1"))
        cont.state = STREAMING
        cont.linear = True
        cont.unified_state = UnifiedLinearState()
        ctrl._sessions["om_old-cont-1"] = cont
        ctrl._register_continuation("om_old", "om_old-cont-1")

        ctrl._prune_stale_sessions()  # defers old
        assert ctrl._sess_get("om_old") is not None

        # continuation finishes + expires
        cont.state = COMPLETED
        ctrl._prune_stale_sessions()  # reclaims old (route dead) + cont

        assert ctrl._sess_get("om_old") is None
        assert ctrl._sess_get("om_old-cont-1") is None
        assert ctrl._resolve_continuation_id("om_old") is None

    def test_continuation_map_capped(self) -> None:
        """Extreme case (targets never reaching terminal state): the map
        is bounded at _CONTINUATION_MAP_MAX like _interrupt_map."""
        from hermes_lark_streaming.controller.core import _CONTINUATION_MAP_MAX

        ctrl = _setup_ctrl()
        for i in range(_CONTINUATION_MAP_MAX + 25):
            ctrl._register_continuation(f"om_{i}", f"om_{i}-cont")
        assert len(ctrl._continuation_map) == _CONTINUATION_MAP_MAX
        # oldest entries evicted, newest kept
        assert ctrl._resolve_continuation_id("om_0") is None
        assert ctrl._resolve_continuation_id(
            f"om_{_CONTINUATION_MAP_MAX + 24}"
        ) == f"om_{_CONTINUATION_MAP_MAX + 24}-cont"


# ══════════════════════════════════════════════════════════════════════
# P1 — hermes error-string results seal the streaming card
# ══════════════════════════════════════════════════════════════════════

class TestErrorStringSeal:
    """v1.8.1: _wrap_run_agent handles str results (hermes except branch)."""

    @staticmethod
    def _run_wrapper(result, *, with_ctx: bool = True):
        from hermes_lark_streaming.patching import _msg_ctx
        from hermes_lark_streaming.patching.gateway import _wrap_run_agent

        orig = AsyncMock(return_value=result)
        wrapped = _wrap_run_agent(orig)
        self_mock = MagicMock()
        source = SimpleNamespace(
            platform=SimpleNamespace(value="feishu"),
            thread_id=None,
            chat_id="oc_1",
        )
        ctx = {
            "message_id": "om_err",
            "chat_id": "oc_1",
            "anchor_id": "om_err",
            "event_message_id": "",
            "card_sent": False,
            "_msg_start_time": time.monotonic(),
        }
        if with_ctx:
            _msg_ctx.set(dict(ctx))
        try:
            coro = wrapped(
                self_mock, "user text", "ctx prompt", [], source, "sess_1"
            )
            return asyncio.get_event_loop().run_until_complete(coro), orig
        finally:
            _msg_ctx.set(None)

    def test_string_result_seals_card_with_error(self) -> None:
        """Agent exception → hermes returns an error string → the wrapper
        fires on_message_completed with error_message so the card seals."""
        err_text = "Sorry, I encountered an unexpected error. Try again."
        with patch(
            "hermes_lark_streaming.patching.hooks.on_message_completed",
            return_value=True,
        ) as completed:
            result, _ = self._run_wrapper(err_text)

        assert result == err_text  # returned to hermes unchanged
        completed.assert_called_once()
        kwargs = completed.call_args.kwargs
        assert kwargs["message_id"] == "om_err"
        assert kwargs["error_message"] == err_text

    def test_string_result_without_ctx_is_passthrough(self) -> None:
        """No message context (e.g. non-card flows) → nothing sealed, the
        string flows back to hermes untouched."""
        with patch(
            "hermes_lark_streaming.patching.hooks.on_message_completed",
            return_value=True,
        ) as completed:
            result, _ = self._run_wrapper("err", with_ctx=False)

        assert result == "err"
        completed.assert_not_called()

    def test_dict_result_still_uses_full_completion(self) -> None:
        """Regression guard: normal dict results keep the legacy path
        (final_response answer, footer metrics)."""
        dict_result = {
            "final_response": "the answer",
            "model": "gpt-x",
            "input_tokens": 10,
            "output_tokens": 20,
            "interrupted": False,
        }
        with patch(
            "hermes_lark_streaming.patching.hooks.on_message_completed",
            return_value=True,
        ) as completed:
            result, _ = self._run_wrapper(dict_result)

        assert result is dict_result
        completed.assert_called_once()
        kwargs = completed.call_args.kwargs
        assert kwargs["answer"] == "the answer"
        assert kwargs["error_message"] == ""


# ══════════════════════════════════════════════════════════════════════
# P1 — START hook only fires for Feishu channels
# ══════════════════════════════════════════════════════════════════════

class TestStartHookPlatformGuard:
    """v1.8.1: non-Feishu messages no longer create card sessions."""

    @staticmethod
    def _run_wrapper(platform_value: str):
        from hermes_lark_streaming.patching.gateway import (
            _wrap_handle_message_with_agent,
        )

        orig = AsyncMock(return_value=None)
        wrapped = _wrap_handle_message_with_agent(orig)
        event = SimpleNamespace(message_id="om_g", text="hi")
        source = SimpleNamespace(
            platform=SimpleNamespace(value=platform_value),
            chat_id="oc_1",
            thread_id="ot_g",
        )
        self_mock = MagicMock()
        self_mock._reply_anchor_for_event = MagicMock(return_value="om_g")
        coro = wrapped(self_mock, event, source)
        return asyncio.get_event_loop().run_until_complete(coro), orig

    def test_start_hook_fires_for_feishu_with_thread_id(self) -> None:
        with patch(
            "hermes_lark_streaming.patching.hooks.on_message_started"
        ) as started:
            self._run_wrapper("feishu")

        started.assert_called_once()
        kwargs = started.call_args.kwargs
        assert kwargs["message_id"] == "om_g"
        assert kwargs["thread_id"] == "ot_g"

    def test_start_hook_skipped_for_telegram(self) -> None:
        with patch(
            "hermes_lark_streaming.patching.hooks.on_message_started"
        ) as started:
            self._run_wrapper("telegram")

        started.assert_not_called()

    def test_start_hook_skipped_for_qq(self) -> None:
        with patch(
            "hermes_lark_streaming.patching.hooks.on_message_started"
        ) as started:
            self._run_wrapper("qq")

        started.assert_not_called()


# ══════════════════════════════════════════════════════════════════════
# P2 — /bg tasks reply to the user's message (om_ anchor)
# ══════════════════════════════════════════════════════════════════════

class TestBackgroundAnchor:
    """v1.8.1: /bg wrapper forwards event_message_id as the card anchor."""

    @staticmethod
    def _run_bg_wrapper(*, event_message_id: str | None = "om_user_1"):
        from hermes_lark_streaming.patching.gateway import _wrap_run_background_task

        orig = AsyncMock(return_value={"final_response": "bg done", "model": "m"})
        wrapped = _wrap_run_background_task(orig)
        source = SimpleNamespace(
            platform=SimpleNamespace(value="feishu"),
            chat_id="oc_1",
            thread_id="ot_bg",
        )
        self_mock = MagicMock()
        self_mock.adapters = {}
        kwargs = (
            {"event_message_id": event_message_id} if event_message_id else {}
        )
        coro = wrapped(self_mock, "bg prompt", source, "bg_1234_ab", **kwargs)
        return (
            asyncio.get_event_loop().run_until_complete(coro),
            orig,
        )

    def test_start_hook_receives_user_message_anchor(self) -> None:
        with patch(
            "hermes_lark_streaming.patching.hooks.on_message_started"
        ) as started, patch(
            "hermes_lark_streaming.patching.hooks.on_message_completed",
            return_value=True,
        ):
            self._run_bg_wrapper()

        started.assert_called_once()
        kwargs = started.call_args.kwargs
        assert kwargs["message_id"] == "bg_1234_ab"
        assert kwargs["anchor_id"] == "om_user_1"
        assert kwargs["thread_id"] == "ot_bg"

    def test_orig_receives_event_message_id_unchanged(self) -> None:
        """The anchor must still reach hermes' own delivery logic
        (thread metadata routing) — we consume it, we don't swallow it."""
        with patch(
            "hermes_lark_streaming.patching.hooks.on_message_started"
        ), patch(
            "hermes_lark_streaming.patching.hooks.on_message_completed",
            return_value=True,
        ):
            _, orig = self._run_bg_wrapper()

        orig_kwargs = orig.call_args.kwargs
        assert orig_kwargs["event_message_id"] == "om_user_1"

    def test_legacy_hermes_without_anchor_keeps_old_behaviour(self) -> None:
        """Older hermes versions that don't pass event_message_id fall back
        to the previous anchor-less behaviour (no regression)."""
        with patch(
            "hermes_lark_streaming.patching.hooks.on_message_started"
        ) as started, patch(
            "hermes_lark_streaming.patching.hooks.on_message_completed",
            return_value=True,
        ):
            self._run_bg_wrapper(event_message_id=None)

        started.assert_called_once()
        assert started.call_args.kwargs["anchor_id"] is None

    def test_non_feishu_bg_passthrough_forwards_anchor(self) -> None:
        """Non-Feishu /bg tasks skip card hooks entirely; the anchor still
        reaches orig untouched."""
        from hermes_lark_streaming.patching.gateway import _wrap_run_background_task

        orig = AsyncMock(return_value=None)
        wrapped = _wrap_run_background_task(orig)
        source = SimpleNamespace(
            platform=SimpleNamespace(value="telegram"),
            chat_id="oc_1",
            thread_id=None,
        )
        self_mock = MagicMock()
        coro = wrapped(
            self_mock, "bg prompt", source, "bg_1", event_message_id="om_tg"
        )
        asyncio.get_event_loop().run_until_complete(coro)

        assert orig.call_args.kwargs["event_message_id"] == "om_tg"
