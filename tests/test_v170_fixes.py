"""v1.7.0 regression tests — RELAY mode (P1), R3 bug fixes, R1/R2 robustness+perf.

Covers the fixes shipped in v1.7.0:
  * P1 RELAY: relay-fronted Feishu deployments get cron/gateway cards via the
    RelayAdapter.send / send_for_platform wrappers.
  * R3-01/02/03/04/05/06/08/09: bug-audit fixes (terminal metadata, race
    conditions, clarify robustness, reactivation accounting).
  * R1-03/06: schema-error recovery + GC-safe patch dedupe.
  * R2-01/02/03: escape cache, storage caps, tool-step cap.
  * E2E-verified Card 2.0 schema fixes (empty body / top-level button).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hermes_lark_streaming.controller import CardSession, StreamCardController
from hermes_lark_streaming.controller.mixin import (
    ABORTED,
    COMPLETED,
    COMPLETING,
    CREATION_FAILED,
    IDLE,
    STREAMING,
)
from hermes_lark_streaming.state.linear import UnifiedLinearState
from hermes_lark_streaming.state.phase import TerminalReason


def _enable(ctrl: StreamCardController, *, linear: bool = False) -> None:
    ctrl._cfg._raw = {
        "hermes_lark_streaming": {"enabled": True, "linear": linear},
        "feishu": {"app_id": "app", "app_secret": "secret"},
    }


def _make_ctrl() -> StreamCardController:
    ctrl = StreamCardController()
    _enable(ctrl)
    return ctrl


def _make_session(message_id: str = "msg", chat_id: str = "chat") -> CardSession:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
    return CardSession(message_id, chat_id, loop)


# ══════════════════════════════════════════════════════════════════════
# P1 RELAY — RelayAdapter wrappers
# ══════════════════════════════════════════════════════════════════════

class TestRelayWrappers:
    """v1.7.0 P1: relay-fronted deployments route Feishu sends through
    RelayAdapter.send / send_for_platform — both now dispatch through the
    shared _dispatch_feishu_outbound logic."""

    def _make_relay_cls(self):
        class _RelayAdapter:
            def __init__(self):
                self._platform_by_chat = {"chat_feishu": "feishu", "chat_slack": "slack"}
                self.calls = []

            async def send(self, chat_id, content, reply_to=None, metadata=None, **kw):
                self.calls.append(("send", chat_id, content))
                return SimpleNamespace(success=True)

            async def send_for_platform(self, logical_platform, chat_id, content,
                                        reply_to=None, metadata=None, **kw):
                self.calls.append(("sfp", str(logical_platform), chat_id, content))
                return SimpleNamespace(success=True)

        return _RelayAdapter

    @pytest.mark.asyncio
    async def test_send_for_platform_cron_lane_redirects_to_card(self):
        """Feishu cron deliveries (job_id metadata) become cron cards."""
        from hermes_lark_streaming.patching.adapter import _wrap_relay_adapter_send_for_platform

        cls = self._make_relay_cls()
        orig = cls.send_for_platform
        cls.send_for_platform = _wrap_relay_adapter_send_for_platform(orig)
        relay = cls()

        ctrl = _make_ctrl()
        ctrl._do_cron_deliver = AsyncMock(return_value=None)

        with patch("hermes_lark_streaming.controller.get_controller", return_value=ctrl):
            result = await relay.send_for_platform(
                "feishu", "chat_x", "Cronjob Response: hi",
                metadata={"job_id": "job-123"},
            )

        ctrl._do_cron_deliver.assert_awaited_once_with("chat_x", "Cronjob Response: hi")
        assert relay.calls == []  # never reached the wire (suppressed)

    @pytest.mark.asyncio
    async def test_send_for_platform_non_feishu_passthrough(self):
        """Non-Feishu platforms fronted by the same relay pass through."""
        from hermes_lark_streaming.patching.adapter import _wrap_relay_adapter_send_for_platform

        cls = self._make_relay_cls()
        cls.send_for_platform = _wrap_relay_adapter_send_for_platform(cls.send_for_platform)
        relay = cls()

        ctrl = _make_ctrl()
        with patch("hermes_lark_streaming.controller.get_controller", return_value=ctrl):
            await relay.send_for_platform(
                "slack", "chat_slack", "hello", metadata={"job_id": "j1"},
            )

        assert relay.calls == [("sfp", "slack", "chat_slack", "hello")]

    @pytest.mark.asyncio
    async def test_send_feishu_chat_gateway_card(self):
        """A Feishu chat known from inbound (platform map) gets gateway cards."""
        from hermes_lark_streaming.patching.adapter import _wrap_relay_adapter_send

        cls = self._make_relay_cls()
        cls.send = _wrap_relay_adapter_send(cls.send)
        relay = cls()

        ctrl = _make_ctrl()
        ctrl._do_gateway_deliver = AsyncMock(return_value=("card_msg_1", None))

        with patch("hermes_lark_streaming.controller.get_controller", return_value=ctrl):
            result = await relay.send("chat_feishu", "Session compressed")

        ctrl._do_gateway_deliver.assert_awaited_once()
        assert relay.calls == []  # suppressed — never reached the wire

    @pytest.mark.asyncio
    async def test_send_unknown_platform_passthrough(self):
        """Chats with no learned platform pass through untouched."""
        from hermes_lark_streaming.patching.adapter import _wrap_relay_adapter_send

        cls = self._make_relay_cls()
        cls.send = _wrap_relay_adapter_send(cls.send)
        relay = cls()

        ctrl = _make_ctrl()
        with patch("hermes_lark_streaming.controller.get_controller", return_value=ctrl):
            await relay.send("chat_unknown", "hello")

        assert relay.calls == [("send", "chat_unknown", "hello")]

    @pytest.mark.asyncio
    async def test_send_interim_marker_passthrough(self):
        """Consumer-declared interim frames never become cards."""
        from hermes_lark_streaming.patching.adapter import _wrap_relay_adapter_send

        cls = self._make_relay_cls()
        cls.send = _wrap_relay_adapter_send(cls.send)
        relay = cls()

        ctrl = _make_ctrl()
        with patch("hermes_lark_streaming.controller.get_controller", return_value=ctrl):
            await relay.send(
                "chat_feishu", "tail flush", metadata={"_interim_send": True},
            )

        assert relay.calls == [("send", "chat_feishu", "tail flush")]

    @pytest.mark.asyncio
    async def test_send_for_platform_metadata_logical_platform(self):
        """metadata['_relay_logical_platform'] forces the platform resolution."""
        from hermes_lark_streaming.patching.adapter import _wrap_relay_adapter_send

        cls = self._make_relay_cls()
        cls.send = _wrap_relay_adapter_send(cls.send)
        relay = cls()

        ctrl = _make_ctrl()
        ctrl._do_gateway_deliver = AsyncMock(return_value=("card_msg_2", None))

        with patch("hermes_lark_streaming.controller.get_controller", return_value=ctrl):
            await relay.send(
                "chat_new", "gateway msg",
                metadata={"_relay_logical_platform": "feishu"},
            )

        ctrl._do_gateway_deliver.assert_awaited_once()
        assert relay.calls == []

    @pytest.mark.asyncio
    async def test_cron_card_failure_falls_back_to_wire(self):
        """A failed cron card must still deliver plain text through the relay."""
        from hermes_lark_streaming.patching.adapter import _wrap_relay_adapter_send_for_platform

        cls = self._make_relay_cls()
        cls.send_for_platform = _wrap_relay_adapter_send_for_platform(cls.send_for_platform)
        relay = cls()

        ctrl = _make_ctrl()
        ctrl._do_cron_deliver = AsyncMock(side_effect=RuntimeError("boom"))

        with patch("hermes_lark_streaming.controller.get_controller", return_value=ctrl):
            await relay.send_for_platform(
                "feishu", "chat_x", "cron text", metadata={"job_id": "j1"},
            )

        # fell through to the original send_for_platform
        assert relay.calls == [("sfp", "feishu", "chat_x", "cron text")]


class TestApplyRelayAdapterPatches:
    """v1.7.0: relay class patch application via the shared entry points."""

    def test_patch_and_idempotence(self):
        from hermes_lark_streaming.patching import _apply_relay_adapter_patches

        class RelayAdapter:
            async def send(self, *a, **kw): ...
            async def send_for_platform(self, *a, **kw): ...

        assert _apply_relay_adapter_patches(RelayAdapter) is True
        assert getattr(RelayAdapter, "_hls_relay_patched", False) is True
        wrapped_send = RelayAdapter.send
        wrapped_sfp = RelayAdapter.send_for_platform

        # idempotent — second call does not re-wrap
        assert _apply_relay_adapter_patches(RelayAdapter) is True
        assert RelayAdapter.send is wrapped_send
        assert RelayAdapter.send_for_platform is wrapped_sfp

    def test_none_class(self):
        from hermes_lark_streaming.patching import _apply_relay_adapter_patches
        assert _apply_relay_adapter_patches(None) is False

    def test_create_adapter_hook_patches_relay(self):
        """The v1.6.0 create_adapter hook (extended in v1.7.0) patches relay
        adapters created through platform_registry.create_adapter."""
        from hermes_lark_streaming.patching import _wrap_platform_registry_create_adapter

        created = []

        class RelayAdapter:
            async def send(self, *a, **kw): ...
            async def send_for_platform(self, *a, **kw): ...

        def orig_create_adapter(name, config):
            adapter = RelayAdapter()
            created.append((name, adapter))
            return adapter

        wrapped = _wrap_platform_registry_create_adapter(orig_create_adapter)
        adapter = wrapped("relay", {"url": "ws://x"})

        assert created == [("relay", adapter)]
        assert getattr(type(adapter), "_hls_relay_patched", False) is True

    def test_create_adapter_hook_relay_module_sniff(self):
        """A relay adapter whose class module lives under gateway.relay is
        detected even when the registry name says something else."""
        from hermes_lark_streaming.patching import _wrap_platform_registry_create_adapter

        # Build a class whose __module__ looks like the real relay adapter
        cls_dict = {
            "__module__": "gateway.relay.adapter",
            "send": asyncio.coroutine(lambda self, *a, **kw: None) if hasattr(asyncio, "coroutine") else None,
        }

        async def _send(self, *a, **kw): ...
        async def _sfp(self, *a, **kw): ...
        RelayAdapter = type("RelayAdapter", (), {
            "__module__": "gateway.relay.adapter",
            "send": _send,
            "send_for_platform": _sfp,
        })

        def orig_create_adapter(name, config):
            return RelayAdapter()

        wrapped = _wrap_platform_registry_create_adapter(orig_create_adapter)
        adapter = wrapped("weird-name", None)
        assert getattr(type(adapter), "_hls_relay_patched", False) is True


# ══════════════════════════════════════════════════════════════════════
# R3-01 — terminal metadata on COMPLETED / ABORTED
# ══════════════════════════════════════════════════════════════════════

class TestTerminalMetadata:
    def test_on_aborted_records_reason_and_bumps_epoch(self):
        ctrl = _make_ctrl()
        session = _make_session("m1")
        ctrl._sess_put("m1", session)
        session.state = STREAMING

        with patch.object(ctrl, "_complete_session"):
            ctrl.on_aborted(message_id="m1")

        assert session.state == ABORTED
        assert session.terminal_reason == TerminalReason.ABORT
        assert session.terminal_source == "on_aborted"
        assert session.create_epoch == 1  # bumped → epoch guards activate

    def test_on_interrupted_immediate_records_reason(self):
        ctrl = _make_ctrl()
        old = _make_session("old")
        old.state = STREAMING
        ctrl._sess_put("old", old)

        with patch.object(ctrl, "_complete_session"), patch.object(ctrl, "_fire_and_forget"):
            ctrl.on_interrupted(old_message_id="old", new_message_id="new", chat_id="c")

        assert old.state == ABORTED
        assert old.terminal_reason == TerminalReason.ABORT
        assert old.create_epoch == 1

    def test_seal_success_completed_records_normal_reason(self):
        """_do_linear_complete seal-success path records NORMAL + epoch bump."""
        from hermes_lark_streaming.controller.linear_mixin import UnifiedControllerMixin

        ctrl = _make_ctrl()
        session = _make_session("m2")
        session.linear = True
        session.unified_state = UnifiedLinearState()
        session.state = COMPLETING
        session.card_id = "card_x"
        ctrl._sess_put("m2", session)

        with patch.object(
            UnifiedControllerMixin, "_preservative_seal", new=AsyncMock(return_value=True)
        ), patch.object(
            StreamCardController, "_send_text_fallback", new=AsyncMock()
        ):
            ok = asyncio.get_event_loop().run_until_complete(
                ctrl._do_linear_complete(session)
            )

        assert ok is True
        assert session.state == COMPLETED
        assert session.terminal_reason == TerminalReason.NORMAL
        assert session.create_epoch == 1


# ══════════════════════════════════════════════════════════════════════
# R3-02 — _wait_and_abort skips terminal sessions
# ══════════════════════════════════════════════════════════════════════

class TestWaitAndAbortTerminalGuard:
    @pytest.mark.asyncio
    async def test_abort_skipped_when_seal_already_completed(self):
        """If the seal chain finished during the 3s flush wait, the abort must
        NOT overwrite COMPLETED (illegal transition + double seal)."""
        ctrl = _make_ctrl()
        old = _make_session("old")
        old.state = STREAMING
        old.flush._flush_in_progress = True
        ctrl._sess_put("old", old)

        with patch.object(ctrl, "_complete_session") as complete_mock:
            await ctrl.on_interrupted.__wrapped__ if False else None
            # call on_interrupted synchronously (it schedules _wait_and_abort)
            ctrl.on_interrupted(old_message_id="old", new_message_id="new", chat_id="c")
            # let the fire-and-forget task run
            await asyncio.sleep(0)

            # simulate the seal finishing BEFORE _wait_and_abort resumes
            old.state = COMPLETED
            old.enter_terminal(reason=TerminalReason.NORMAL, source="seal")
            await asyncio.sleep(0.05)
            await asyncio.sleep(0)

        # state not clobbered to ABORTED, no double complete
        assert old.state == COMPLETED
        complete_mock.assert_not_called()


# ══════════════════════════════════════════════════════════════════════
# R3-03 — late on_completed after /stop is swallowed
# ══════════════════════════════════════════════════════════════════════

class TestOnCompletedAbortedSwallow:
    def test_late_completion_after_abort_returns_true(self):
        """A late on_completed for an ABORTED session returns True (swallowed)
        instead of False (which made the gateway re-send the final answer as
        a duplicate plain-text reply)."""
        ctrl = _make_ctrl()
        session = _make_session("m3")
        session.state = ABORTED
        session.enter_terminal(reason=TerminalReason.ABORT, source="on_aborted")
        ctrl._sess_put("m3", session)

        result = ctrl.on_completed(message_id="m3", answer="the answer")
        assert result is True

    def test_normal_idempotency_unchanged(self):
        ctrl = _make_ctrl()
        session = _make_session("m4")
        session.state = COMPLETED
        ctrl._sess_put("m4", session)

        assert ctrl.on_completed(message_id="m4", answer="x") is True


# ══════════════════════════════════════════════════════════════════════
# R3-04 + R3-08 — reactivation epoch bump + success-gated count
# ══════════════════════════════════════════════════════════════════════

class TestReactivationFixes:
    def test_reactivation_bumps_epoch_and_counts_after_success(self):
        """R3-04: epoch bumps at reactivation so late callbacks are rejected.
        R3-08: the reactivation count increments only after the new session
        is registered + dispatched."""
        ctrl = _make_ctrl()
        stale = _make_session("stale")
        stale.state = STREAMING
        stale.anchor_id = "anchor"
        stale._streaming_closed = True
        ctrl._sess_put("stale", stale)

        new_session = ctrl._reactivate_session_for_continuation(stale)
        assert new_session is not None
        assert new_session.message_id == "anchor-cont-1"
        assert stale._continuation_reactivation_count == 1
        # R3-04: epoch bumped → old message_id callbacks now stale
        assert stale.create_epoch == 1
        assert stale.is_stale_create(0) is True

    def test_failed_reactivation_does_not_burn_the_attempt(self):
        """R3-08: an id conflict must not consume the single reactivation
        attempt — the count stays 0 and a retry can proceed."""
        ctrl = _make_ctrl()
        stale = _make_session("stale2")
        stale.state = STREAMING
        stale.anchor_id = "anchor2"
        stale._streaming_closed = True
        ctrl._sess_put("stale2", stale)
        # occupy the would-be continuation id
        ctrl._sess_put("anchor2-cont-1", _make_session("anchor2-cont-1"))

        new_session = ctrl._reactivate_session_for_continuation(stale)
        assert new_session is None
        assert stale._continuation_reactivation_count == 0  # NOT burned

    def test_maybe_reactivate_retry_after_conflict(self):
        """End-to-end: after a conflicted first attempt, the second attempt
        succeeds (previously the early increment blocked it forever)."""
        ctrl = _make_ctrl()
        stale = _make_session("stale3")
        stale.state = STREAMING
        stale.anchor_id = "anchor3"
        stale._streaming_closed = True
        ctrl._sess_put("stale3", stale)
        ctrl._sess_put("anchor3-cont-1", _make_session("anchor3-cont-1"))

        assert ctrl._maybe_reactivate_for_continuation("stale3") is None
        # free the conflicting id
        ctrl._sess_pop("anchor3-cont-1")
        new_id = ctrl._maybe_reactivate_for_continuation("stale3")
        assert new_id == "anchor3-cont-1"


# ══════════════════════════════════════════════════════════════════════
# R3-05 — confirm failure keeps retry data
# ══════════════════════════════════════════════════════════════════════

class TestConfirmCardCleanup:
    @pytest.mark.asyncio
    async def test_cleanup_only_after_successful_update(self):
        from hermes_lark_streaming.patching import adapter as padapter

        cid = "cid_r3_05"
        with padapter._clarify_lock:
            padapter._clarify_card_msg_ids[cid] = "card_msg_9"
            padapter._clarify_questions[cid] = "Q?"
            padapter._clarify_selections[cid] = "my choice"
            padapter._clarify_timestamps[cid] = 1_000_000.0  # ancient

        ctrl = _make_ctrl()
        ctrl._client_ok = lambda: True
        ctrl._client = MagicMock()
        ctrl._client.update_card = AsyncMock(side_effect=RuntimeError("network down"))

        with patch("hermes_lark_streaming.controller.get_controller", return_value=ctrl):
            await padapter._schedule_confirm_card(cid=cid)

        # FAILURE → selection KEPT so the retry button still works
        with padapter._clarify_lock:
            assert cid in padapter._clarify_selections
            padapter._clarify_choices.pop(cid, None)
            padapter._clarify_questions.pop(cid, None)
            padapter._clarify_card_msg_ids.pop(cid, None)
            padapter._clarify_selections.pop(cid, None)
            padapter._clarify_timestamps.pop(cid, None)

    @pytest.mark.asyncio
    async def test_cleanup_after_successful_update(self):
        from hermes_lark_streaming.patching import adapter as padapter

        cid = "cid_r3_05b"
        with padapter._clarify_lock:
            padapter._clarify_card_msg_ids[cid] = "card_msg_10"
            padapter._clarify_questions[cid] = "Q?"
            padapter._clarify_selections[cid] = "my choice"
            padapter._clarify_timestamps[cid] = 1_000_000.0

        ctrl = _make_ctrl()
        ctrl._client_ok = lambda: True
        ctrl._client = MagicMock()
        ctrl._client.update_card = AsyncMock(return_value=None)

        with patch("hermes_lark_streaming.controller.get_controller", return_value=ctrl):
            await padapter._schedule_confirm_card(cid=cid)

        # SUCCESS → everything cleaned
        with padapter._clarify_lock:
            assert cid not in padapter._clarify_selections
            assert cid not in padapter._clarify_card_msg_ids


# ══════════════════════════════════════════════════════════════════════
# R3-06 — clarify authorization is fail-closed
# ══════════════════════════════════════════════════════════════════════

class TestClarifyAuthzFailClosed:
    def _make_data(self):
        event = SimpleNamespace(
            operator=SimpleNamespace(open_id="ou_x"),
            action=SimpleNamespace(option="0", input_value="", form_value={}),
        )
        return SimpleNamespace(event=event)

    def test_missing_authz_method_fails_closed(self):
        """Adapters without _is_interactive_operator_authorized (old hermes)
        must reject the click instead of letting any group member impersonate
        the author."""
        from hermes_lark_streaming.patching.adapter import _handle_clarify_card_action

        adapter = SimpleNamespace()  # NO authz method
        data = self._make_data()
        with patch("hermes_lark_streaming.patching.adapter._clarify_questions", {"c1": "Q"}), \
             patch("hermes_lark_streaming.patching.adapter._clarify_choices", {"c1": ["A", "B"]}), \
             patch("hermes_lark_streaming.patching.adapter._clarify_selections", {}):
            result = _handle_clarify_card_action(
                adapter, data, "select", {"clarify_id": "c1"},
            )
        assert result is None or getattr(result, "card", None) is None

    def test_unauthorized_operator_rejected(self):
        from hermes_lark_streaming.patching.adapter import _handle_clarify_card_action

        adapter = SimpleNamespace(_is_interactive_operator_authorized=lambda oid: False)
        data = self._make_data()
        with patch("hermes_lark_streaming.patching.adapter._clarify_questions", {"c1": "Q"}), \
             patch("hermes_lark_streaming.patching.adapter._clarify_choices", {"c1": ["A", "B"]}), \
             patch("hermes_lark_streaming.patching.adapter._clarify_selections", {}):
            result = _handle_clarify_card_action(
                adapter, data, "select", {"clarify_id": "c1"},
            )
        assert result is None or getattr(result, "card", None) is None


# ══════════════════════════════════════════════════════════════════════
# R3-09 — select value parsing degrades to text lookup
# ══════════════════════════════════════════════════════════════════════

class TestSelectValueParsing:
    def _run(self, option_value, choices):
        import sys as _sys
        import types as _types
        from hermes_lark_streaming.patching.adapter import _handle_clarify_card_action

        adapter = SimpleNamespace(
            _is_interactive_operator_authorized=lambda oid: True,
            _loop=None,
        )
        event = SimpleNamespace(
            operator=SimpleNamespace(open_id="ou_ok"),
            action=SimpleNamespace(option=option_value, input_value="", form_value={}),
        )
        data = SimpleNamespace(event=event)
        resolved = {}

        def fake_resolve(clarify_id, text):
            resolved["text"] = text

        tools_mod = _types.ModuleType("tools")
        cg_mod = _types.ModuleType("tools.clarify_gateway")
        cg_mod.resolve_gateway_clarify = fake_resolve
        tools_mod.clarify_gateway = cg_mod

        with patch("hermes_lark_streaming.patching.adapter._clarify_questions", {"c9": "Q"}), \
             patch("hermes_lark_streaming.patching.adapter._clarify_choices", {"c9": choices}), \
             patch("hermes_lark_streaming.patching.adapter._clarify_selections", {}), \
             patch.dict(_sys.modules, {"tools": tools_mod, "tools.clarify_gateway": cg_mod}):
            result = _handle_clarify_card_action(
                adapter, data, "select", {"clarify_id": "c9"},
            )
        return resolved

    def test_index_string_still_works(self):
        resolved = self._run("1", ["A", "B"])
        assert resolved["text"] == "B"

    def test_choice_text_fallback(self):
        """A future value-format change (choice text instead of index) now
        degrades to a text lookup instead of swallowing the click."""
        resolved = self._run("First option", ["First option", "Second option"])
        assert resolved["text"] == "First option"

    def test_garbage_value_rejected(self):
        resolved = self._run("nope", ["A", "B"])
        assert resolved == {}


# ══════════════════════════════════════════════════════════════════════
# Submitted-card response — no more TypeError (choices kwarg)
# ══════════════════════════════════════════════════════════════════════

class TestSubmittedCardResponseContract:
    def test_build_clarify_submitted_card_signature(self):
        """The builder accepts exactly (question, selected, clarify_id) — the
        old call site passed a nonexistent `choices` kwarg and raised
        TypeError on every clarify click."""
        import inspect
        from hermes_lark_streaming.cardkit.special import build_clarify_submitted_card

        params = inspect.signature(build_clarify_submitted_card).parameters
        assert set(params) == {"question", "selected", "clarify_id"}


# ══════════════════════════════════════════════════════════════════════
# R2-01 — incremental escape cache
# ══════════════════════════════════════════════════════════════════════

class TestEscapeCache:
    def test_cache_matches_full_recompute(self):
        """The cached incremental escape must equal a full recompute for a
        wide variety of delta shapes."""
        from hermes_lark_streaming.cardkit.md import escape_markdown_asterisks

        state = UnifiedLinearState()
        deltas = [
            "hello ",           # plain
            "world *bold*",     # bold pair in one delta
            " more",            # plain
            "*",                # lone asterisk start
            "text",             # pairs with previous lone asterisk!
            " `code`",          # inline code
            "```py\nx=1\n```",  # fenced code
            " tail",            # plain after code
            "**unterminated",   # unterminated bold
            " done",            # plain
        ]
        for d in deltas:
            state.on_answer_delta(d)
            cached = state.escaped_answer_view()
            full = escape_markdown_asterisks(state.answer_text)
            assert cached == full, f"mismatch after delta {d!r}"

    def test_reset_escape_cache(self):
        state = UnifiedLinearState()
        state.on_answer_delta("abc")
        state.escaped_answer_view()
        # direct replacement (on_completed MISMATCH branch)
        state.answer_text = "completely different *text*"
        state.reset_escape_cache()
        assert state._escaped_cache is None
        assert state.escaped_answer_view() == "completely different *text*"

    def test_empty_text(self):
        state = UnifiedLinearState()
        assert state.escaped_answer_view() == ""


# ══════════════════════════════════════════════════════════════════════
# R2-02 — storage caps
# ══════════════════════════════════════════════════════════════════════

class TestStorageCaps:
    def test_reasoning_rounds_capped(self):
        state = UnifiedLinearState()
        for i in range(60):
            state.on_reasoning_delta(f"round {i}")
            state.on_answer_delta("x")  # finalize round
        assert len(state.reasoning_rounds) == UnifiedLinearState._MAX_REASONING_ROUNDS_STORED
        # latest rounds kept
        assert state.reasoning_rounds[-1].text == "round 59"

    def test_panel_events_reindexed_after_drop(self):
        state = UnifiedLinearState()
        for i in range(55):
            state.on_reasoning_delta(f"r{i}")
            state.on_tool_event(is_new_tool=True)
            state.on_answer_delta("x")
        # no reasoning event may reference an out-of-range round index
        n_rounds = len(state.reasoning_rounds)
        for kind, idx in state._panel_events:
            if kind == "reasoning":
                assert 0 <= idx < n_rounds, f"stale reasoning idx {idx} (rounds={n_rounds})"

    def test_panel_events_capped(self):
        state = UnifiedLinearState()
        for i in range(UnifiedLinearState._MAX_PANEL_EVENTS_STORED + 30):
            state.on_tool_event(is_new_tool=True)
        assert len(state._panel_events) <= UnifiedLinearState._MAX_PANEL_EVENTS_STORED

    def test_bg_review_messages_capped(self):
        state = UnifiedLinearState()
        for i in range(UnifiedLinearState._MAX_BG_REVIEW_MESSAGES_STORED + 10):
            state.on_background_review(f"msg {i}")
        assert len(state.bg_review_messages) <= UnifiedLinearState._MAX_BG_REVIEW_MESSAGES_STORED
        assert state.bg_review_messages[-1] == "msg 29"


# ══════════════════════════════════════════════════════════════════════
# R2-03 — record_end fallback respects the cap
# ══════════════════════════════════════════════════════════════════════

class TestToolStepCap:
    def test_record_end_fallback_respects_cap(self):
        from hermes_lark_streaming.state.tooluse import ToolUseTracker

        tracker = ToolUseTracker(max_steps=5)
        # fill the cap with started steps
        for i in range(5):
            tracker.record_start(f"tool_{i}")
        # ends for tools whose starts were dropped by the cap
        for i in range(5, 10):
            tracker.record_end(f"tool_{i}", output="ok")
        assert len(tracker._session.steps) == 5  # cap held


# ══════════════════════════════════════════════════════════════════════
# R1-06 — GC-safe patch dedupe (sentinel attr)
# ══════════════════════════════════════════════════════════════════════

class TestSentinelPatchDedupe:
    def test_sentinel_attr_set_on_patch(self):
        from hermes_lark_streaming.patching import (
            _apply_feishu_adapter_patches,
            _patched_feishu_classes,
        )

        async def _send(self, *a, **kw): ...
        async def _edit(self, *a, **kw): ...
        async def _clarify(self, *a, **kw): ...
        async def _card_action(self, data): ...

        FeishuAdapter = type("FeishuAdapter", (), {
            "send": _send,
            "edit_message": _edit,
            "send_clarify": _clarify,
            "_handle_card_action_event": _card_action,
        })

        assert _apply_feishu_adapter_patches(FeishuAdapter) is True
        assert getattr(FeishuAdapter, "_hls_patched", False) is True
        assert id(FeishuAdapter) in _patched_feishu_classes
        # cleanup mirror
        _patched_feishu_classes.discard(id(FeishuAdapter))


# ══════════════════════════════════════════════════════════════════════
# R1-03 — Phase 2 schema error closes streaming + answer text fallback
# ══════════════════════════════════════════════════════════════════════

class TestSchemaErrorRecovery:
    @pytest.mark.asyncio
    async def test_schema_error_closes_streaming(self):
        """Phase 2 SCHEMA ERROR must close streaming so the card leaves its
        loading animation (was: stuck animating until seal)."""
        from hermes_lark_streaming.controller.linear_mixin import UnifiedControllerMixin
        from hermes_lark_streaming.feishu import FeishuAPIError

        ctrl = _make_ctrl()
        session = _make_session("m_se")
        session.linear = True
        session.unified_state = UnifiedLinearState()
        session.state = STREAMING
        session.card_id = "card_se"
        ctrl._sess_put("m_se", session)
        # NOTE: "answer" must NOT be in _creation_stages so Phase 2 runs and
        # hits the schema error on add_elements.

        state = session.unified_state
        state.on_reasoning_delta("thinking...")
        state.on_answer_delta("the answer")

        client = MagicMock()
        close_mock = AsyncMock(return_value=None)
        client.cardkit_close_streaming = close_mock
        ctrl._client = client
        ctrl._initialized = True

        # Phase 2 add_elements raises schema error
        async def _batch_update_fail(card_id, actions, sequence=0):
            raise FeishuAPIError("unknown property", code=300315)
        client.cardkit_batch_update = _batch_update_fail
        client.cardkit_stream_element = AsyncMock()

        # Run one unified flush via the mixin method
        with patch.object(UnifiedControllerMixin, "_preservative_seal", new=AsyncMock(return_value=True)), \
             patch.object(StreamCardController, "_send_text_fallback", new=AsyncMock()) as fb:
            await ctrl._do_unified_flush(session)
            assert session._streaming_closed is True
            close_mock.assert_awaited_once()

            # and completion delivers the answer via text fallback
            await ctrl._do_linear_complete(session)
            fb.assert_awaited_once()
            kwargs = fb.await_args.kwargs
            assert kwargs.get("fallback_text") == "the answer"


# ══════════════════════════════════════════════════════════════════════
# Collapse hint — bilingual + count summing (R4)
# ══════════════════════════════════════════════════════════════════════

class TestCollapseHint:
    def test_is_collapse_hint_child_both_formats(self):
        from hermes_lark_streaming.cardkit.elements import _is_collapse_hint_child

        assert _is_collapse_hint_child({"content": "⚡ 还有 3 项已折叠"})
        assert _is_collapse_hint_child({"content": "⚡ 还有 2 轮早期推理、5 步早期操作已折叠"})
        assert _is_collapse_hint_child({"content": "⚡ 7 items collapsed"})
        # reasoning text must NOT match
        assert not _is_collapse_hint_child({"content": "the bridge collapsed under load and we should think about ⚡ that"})
        assert not _is_collapse_hint_child({"content": "⚡ " + "x" * 200})
        assert not _is_collapse_hint_child("not a dict")

    def test_digit_sum_counts_both_formats(self):
        """Summing digit groups yields the collapsed total for both spellings."""
        import re
        for hint, expected in [
            ("⚡ 还有 3 项已折叠", 3),
            ("⚡ 还有 2 轮早期推理、5 步早期操作已折叠", 7),
            ("⚡ 7 items collapsed", 7),
        ]:
            assert sum(int(n) for n in re.findall(r"\d+", hint)) == expected

    def test_panel_children_hint_is_bilingual(self):
        from hermes_lark_streaming.cardkit.elements import build_panel_children
        from hermes_lark_streaming.state.linear import ReasoningRound

        rounds = [
            ReasoningRound(index=i, text=f"r{i}") for i in range(30)
        ]
        for r in rounds:
            r.elapsed_ms = 1.0
        children = build_panel_children(
            reasoning_rounds=rounds,
            current_reasoning_text="",
            tool_steps=[],
            show_reasoning=True,
            max_reasoning_rounds=20,
            max_tool_steps=20,
        )
        hint = children[0]
        assert hint.get("i18n_content") is not None
        assert "zh_cn" in hint["i18n_content"] and "en_us" in hint["i18n_content"]


# ══════════════════════════════════════════════════════════════════════
# R2-09 — answer flush interval is configurable
# ══════════════════════════════════════════════════════════════════════

class TestAnswerFlushIntervalConfig:
    def test_default_150ms(self):
        ctrl = _make_ctrl()
        assert ctrl._cfg.answer_flush_interval_ms == 150.0
        assert ctrl._cfg.answer_flush_interval_sec == 0.15

    def test_override(self):
        ctrl = _make_ctrl()
        ctrl._cfg._raw["hermes_lark_streaming"]["answer_flush_interval_ms"] = 300
        assert ctrl._cfg.answer_flush_interval_ms == 300.0

    def test_clamped(self):
        ctrl = _make_ctrl()
        ctrl._cfg._raw["hermes_lark_streaming"]["answer_flush_interval_ms"] = 10
        assert ctrl._cfg.answer_flush_interval_ms == 70.0


# ══════════════════════════════════════════════════════════════════════
# /stop detection — broadened keywords (R3-07)
# ══════════════════════════════════════════════════════════════════════

class TestStopResponseDetection:
    @pytest.mark.asyncio
    async def test_hermes_locale_stop_messages_detected(self):
        """Both hermes locale spellings (zh + en) match the detector."""
        from hermes_lark_streaming.patching.adapter import _dispatch_feishu_outbound

        ctrl = _make_ctrl()
        # an active streaming card in this chat — /stop must abort it
        stop_session = _make_session("m_stop", chat_id="chat")
        stop_session.state = STREAMING
        stop_session.card_msg_id = "card_stop"
        ctrl._sess_values_snapshot = lambda: [stop_session]
        passthrough = AsyncMock(return_value=SimpleNamespace(success=True))

        with patch("hermes_lark_streaming.controller.get_controller", return_value=ctrl):
            for msg in (
                "⚡ 已停止。你可以继续此会话。",
                "⚡ Stopped. You can continue this session.",
            ):
                await _dispatch_feishu_outbound(
                    SimpleNamespace(), "chat", msg, passthrough, metadata=None,
                )
        # both were suppressed (no passthrough hit)
        passthrough.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_stop_message_flows_to_gateway_card(self):
        from hermes_lark_streaming.patching.adapter import _dispatch_feishu_outbound

        ctrl = _make_ctrl()
        ctrl._do_gateway_deliver = AsyncMock(return_value=("cm", None))
        passthrough = AsyncMock()

        with patch("hermes_lark_streaming.controller.get_controller", return_value=ctrl):
            await _dispatch_feishu_outbound(
                SimpleNamespace(), "chat", "Session compressed", passthrough, metadata=None,
            )
        ctrl._do_gateway_deliver.assert_awaited_once()
        passthrough.assert_not_awaited()
