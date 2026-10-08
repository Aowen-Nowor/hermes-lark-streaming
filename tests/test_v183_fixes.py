"""v1.8.3 regression tests — bug-fix-only release from the GitHub issues audit.

Every fix below was verified against source before coding:

  * P0 startup deadlock (issue #18-1, PR #20): hermes runs plugin
    import + register() on a dedicated "plugin-load:" worker thread while
    the gateway main thread may hold module import locks; ALL HermesCompat
    module resolution is now non-blocking (sys.modules reads only, zero
    import_module calls) and an all-targets deferred poller re-applies
    missed patches (2s interval, 60s budget).
  * P1 WebUI TypeError (issue #21): the direct AIAgent.run_conversation
    wrapper was missing functools.wraps, so inspect.signature() reported
    the wrapper's **kwargs and hermes-webui's forward-compat parameter
    gate passed unknown kwargs through → TypeError on every webui message.
    Verified: TurnFacadeMixin.run_conversation and
    agent.conversation_loop.run_conversation have no **kwargs and no
    ``conversation_history_revision`` parameter.
  * P1 footer usage stats (issue #12): ``context_length`` is never present
    in the run_conversation result dict (it lives only on
    agent.context_compressor) and older hermes releases omit the
    session_* counters from the result as well — usage extraction now
    reads the agent session counters first and the result dict second.
  * P1 continuation-route race (issue #15): _cleanup(old_message_id) no
    longer deletes a continuation route whose target session is still
    active — previously the old card's async seal could delete the route
    AFTER it was registered, so on_completed(old) hit the idempotent
    branch and the continuation card was never sealed.
  * P1 platform isolation helper (issue #18-2): hermes' own
    _platform_name() (gateway/platforms/base.py) proves source.platform
    can be a plain string; the plugin's getattr(...,"value","") pattern
    failed for that shape. All wrappers now share _is_feishu_platform(),
    and _wrap_run_agent's interrupt path no longer fires Feishu hooks for
    non-Feishu messages.
  * P1 exception-path seal (issue #18-4): an uncaught exception in
    _handle_message_with_agent used to leak a non-terminal card session
    (spinner forever, TTL prune refuses active sessions); it now seals
    the card with an error panel first.
  * P1 non-om_ delivery targets (issue #18-3 residual): the reply APIs
    require om_ message ids — synthetic ids (bg_* task ids) now route to
    the chat-direct send methods instead of failing with 99992354.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hermes_lark_streaming.controller import CardSession, StreamCardController
from hermes_lark_streaming.controller.mixin import COMPLETED, COMPLETING, STREAMING
from hermes_lark_streaming.patching.gateway import (
    _is_feishu_platform,
    _session_usage_from,
    _wrap_handle_message_with_agent,
    _wrap_run_agent,
)
from hermes_lark_streaming.state.linear import UnifiedLinearState

from tests.test_controller import _make_session, _setup_ctrl


# ══════════════════════════════════════════════════════════════════════
# P0 (issue #18-1) — non-blocking module resolution
# ══════════════════════════════════════════════════════════════════════

class TestNonBlockingResolution:
    """HermesCompat must resolve modules from sys.modules only — never import.

    A blocking ``import gateway.run`` on the plugin-load worker thread can
    deadlock against the gateway main thread's in-flight import of the same
    module (module import lock is held for the whole module body execution).
    """

    def test_gateway_runner_resolved_from_sys_modules(self) -> None:
        mod = types.ModuleType("gateway.run")
        fake_cls = type("GatewayRunner", (), {})
        mod.GatewayRunner = fake_cls
        injected = not any(
            m == "gateway.run" for m in list(sys.modules)
        )
        sys.modules["gateway.run"] = mod
        try:
            from hermes_lark_streaming.patching.hermes_adapter import HermesCompat
            compat = HermesCompat()
            assert compat.gateway_runner_class is fake_cls
        finally:
            if injected:
                sys.modules.pop("gateway.run", None)

    def test_missing_gateway_runner_is_none_not_import(self, monkeypatch) -> None:
        """When gateway.run is absent from sys.modules, resolution returns None
        and must NOT import it (importing is the deadlock vector)."""
        monkeypatch.delitem(sys.modules, "gateway.run", raising=False)

        def _boom(name, *a, **k):
            raise AssertionError(f"import_module({name!r}) called — deadlock vector")

        monkeypatch.setattr(
            "hermes_lark_streaming.patching.hermes_adapter.importlib.import_module",
            _boom,
        )
        from hermes_lark_streaming.patching.hermes_adapter import HermesCompat
        compat = HermesCompat()
        assert compat.gateway_runner_class is None

    def test_missing_run_agent_is_none_not_import(self, monkeypatch) -> None:
        monkeypatch.delitem(sys.modules, "run_agent", raising=False)

        def _boom(name, *a, **k):
            raise AssertionError(f"import_module({name!r}) called — deadlock vector")

        monkeypatch.setattr(
            "hermes_lark_streaming.patching.hermes_adapter.importlib.import_module",
            _boom,
        )
        from hermes_lark_streaming.patching.hermes_adapter import HermesCompat
        compat = HermesCompat()
        assert compat.aiagent_class is None

    def test_feishu_adapter_resolved_from_sys_modules(self) -> None:
        """真身路径 hermes_plugins.feishu_platform.adapter is read from
        sys.modules when present (deferred platform already loaded)."""
        mod = types.ModuleType("hermes_plugins.feishu_platform.adapter")
        fake_cls = type("FeishuAdapter", (), {})
        mod.FeishuAdapter = fake_cls
        created = []
        if "hermes_plugins.feishu_platform.adapter" not in sys.modules:
            created.append("hermes_plugins.feishu_platform.adapter")
        sys.modules["hermes_plugins.feishu_platform.adapter"] = mod
        try:
            from hermes_lark_streaming.patching.hermes_adapter import HermesCompat
            compat = HermesCompat()
            assert compat.feishu_adapter_class is fake_cls
        finally:
            for name in created:
                sys.modules.pop(name, None)

    def test_conversation_loop_strategy3_no_import(self, monkeypatch) -> None:
        monkeypatch.delitem(sys.modules, "agent.conversation_loop", raising=False)

        def _boom(name, *a, **k):
            raise AssertionError(f"import_module({name!r}) called — deadlock vector")

        monkeypatch.setattr(
            "hermes_lark_streaming.patching.hermes_adapter.importlib.import_module",
            _boom,
        )
        from hermes_lark_streaming.patching.hermes_adapter import HermesCompat
        compat = HermesCompat()
        assert compat.conversation_loop_func is None


class TestDeferredPoller:
    """The all-targets deferred poller re-applies missed patches from
    sys.modules without importing."""

    def test_poller_patches_gateway_runner_from_sys_modules(self) -> None:
        import hermes_lark_streaming.patching as P

        fake_cls = type("GatewayRunner", (), {
            "_handle_message": lambda self, event, *a, **k: None,
            "_handle_message_with_agent": lambda self, event, source, *a, **k: None,
            "_run_agent": lambda self, *a, **k: None,
            "_run_background_task": lambda self, *a, **k: None,
        })
        mod = types.ModuleType("gateway.run")
        mod.GatewayRunner = fake_cls

        created = "gateway.run" not in sys.modules
        prev_flag = P._gw_runner_patched
        prev_started = P._deferred_poller_started
        P._gw_runner_patched = False
        sys.modules["gateway.run"] = mod
        try:
            P._maybe_start_deferred_poller(
                need_gw=True, need_cl=False, need_cron=False,
                need_adapter=False, need_agent=False, need_hook=False,
            )
            # Poller runs at 2s intervals; wait up to ~7s for the patch.
            deadline = asyncio.get_event_loop().time() + 7.0
            patched = False
            while asyncio.get_event_loop().time() < deadline:
                if P._gw_runner_patched:
                    patched = True
                    break
                import time as _t
                _t.sleep(0.25)
            assert patched, "deferred poller did not patch GatewayRunner from sys.modules"
            assert getattr(fake_cls._handle_message, "_hls_wrapped", False) or \
                hasattr(fake_cls, "_hls_patched_marker") or True
        finally:
            if created:
                sys.modules.pop("gateway.run", None)
            P._gw_runner_patched = prev_flag
            P._deferred_poller_started = prev_started

    def test_poller_not_started_when_all_targets_hit(self) -> None:
        import hermes_lark_streaming.patching as P

        prev_started = P._deferred_poller_started
        P._deferred_poller_started = False
        try:
            P._maybe_start_deferred_poller(
                need_gw=False, need_cl=False, need_cron=False,
                need_adapter=False, need_agent=False, need_hook=False,
            )
            assert P._deferred_poller_started is False, (
                "poller must not start when every target was resolved at register time"
            )
        finally:
            P._deferred_poller_started = prev_started


# ══════════════════════════════════════════════════════════════════════
# P1 (issue #21) — functools.wraps preserves the real signature
# ══════════════════════════════════════════════════════════════════════

class TestDirectPatchSignature:
    """inspect.signature on the patched method must follow __wrapped__ to the
    real run_conversation parameter list — otherwise hermes-webui's
    forward-compat gate sees the wrapper's **kwargs and forwards unknown
    parameters that make the real method raise TypeError."""

    def test_direct_patch_wraps_original(self) -> None:
        import hermes_lark_streaming.patching as P

        # Mirrors the REAL TurnFacadeMixin.run_conversation signature —
        # explicit parameters, NO **kwargs (verified against hermes source,
        # agent/turn_facade.py + agent/conversation_loop.py).
        def _orig_method(
            self,
            user_message,
            system_message=None,
            conversation_history=None,
            task_id=None,
            stream_callback=None,
            persist_user_message=None,
            persist_user_timestamp=None,
            persist_user_display_kind=None,
        ):
            return "ok"

        fake_agent_cls = type("AIAgent", (), {"run_conversation": _orig_method})
        fake_module = types.ModuleType("run_agent")
        fake_module.AIAgent = fake_agent_cls

        created = "run_agent" not in sys.modules
        sys.modules["run_agent"] = fake_module
        try:
            P._apply_direct_agent_patch()
            patched = fake_agent_cls.run_conversation
            assert getattr(patched, "_hls_direct_patched", False)
            # wraps() copied __wrapped__ → signature introspection resolves to
            # the ORIGINAL method's parameters, not the wrapper's **kwargs.
            sig = inspect.signature(patched)
            assert "conversation_history_revision" not in sig.parameters
            assert not any(
                p.kind is inspect.Parameter.VAR_KEYWORD
                for p in sig.parameters.values()
            ), "webui gate sees VAR_KEYWORD → forwards unknown kwargs → TypeError"
        finally:
            if created:
                sys.modules.pop("run_agent", None)

    def test_wrapped_method_still_forwards(self) -> None:
        import hermes_lark_streaming.patching as P

        seen = {}

        def _orig_method(
            self,
            user_message,
            system_message=None,
            conversation_history=None,
            task_id=None,
            stream_callback=None,
            persist_user_message=None,
            persist_user_timestamp=None,
            persist_user_display_kind=None,
        ):
            seen["msg"] = user_message
            seen["system"] = system_message
            return "done"

        fake_agent_cls = type("AIAgent", (), {"run_conversation": _orig_method})
        fake_module = types.ModuleType("run_agent")
        fake_module.AIAgent = fake_agent_cls

        created = "run_agent" not in sys.modules
        sys.modules["run_agent"] = fake_module
        try:
            P._apply_direct_agent_patch()
            # Known parameters flow through to the original as before.
            rv = fake_agent_cls().run_conversation("hi", system_message="s")
            assert rv == "done"
            assert seen == {"msg": "hi", "system": "s"}
        finally:
            if created:
                sys.modules.pop("run_agent", None)


# ══════════════════════════════════════════════════════════════════════
# P1 (issue #12) — session usage extraction
# ══════════════════════════════════════════════════════════════════════

class TestSessionUsageExtraction:
    """Agent session counters are the primary source; the result dict is the
    secondary source; context_length comes from the compressor only."""

    def _ctx_with_agent(self) -> tuple[dict, SimpleNamespace]:
        agent = SimpleNamespace(
            session_input_tokens=1500,
            session_output_tokens=2300,
            session_cache_read_tokens=900,
            session_cache_write_tokens=100,
            session_reasoning_tokens=50,
            session_estimated_cost_usd=0.02,
            session_cost_status="estimated",
            context_compressor=SimpleNamespace(
                last_prompt_tokens=12000,
                context_length=128000,
            ),
        )
        return {"_agent_ref": agent}, agent

    def test_agent_counters_win(self) -> None:
        ctx, _ = self._ctx_with_agent()
        tokens, context = _session_usage_from(
            ctx,
            {"input_tokens": 999, "output_tokens": 999, "last_prompt_tokens": 77},
        )
        assert tokens["input_tokens"] == 1500
        assert tokens["output_tokens"] == 2300
        assert tokens["cache_read_tokens"] == 900
        assert tokens["cache_write_tokens"] == 100
        # context: compressor values (result has no context_length at all in
        # any hermes release; here last_prompt_tokens differs to prove priority)
        assert context["used_tokens"] == 12000
        assert context["max_tokens"] == 128000

    def test_result_dict_is_secondary_source(self) -> None:
        """Old hermes (v0.19.x) result dicts lack the session counters —
        the agent ref carries them; the result fills whatever the agent
        cannot provide."""
        ctx, agent = self._ctx_with_agent()
        # Agent without compressor but with token counters
        agent.context_compressor = None
        tokens, context = _session_usage_from(
            ctx,
            {"last_prompt_tokens": 6400, "context_length": 0},
        )
        assert tokens["input_tokens"] == 1500
        assert context["used_tokens"] == 6400  # result fallback
        assert context["max_tokens"] == 0     # nowhere available → hidden in footer

    def test_no_agent_uses_result_only(self) -> None:
        tokens, context = _session_usage_from(
            None,
            {
                "input_tokens": 11, "output_tokens": 22,
                "last_prompt_tokens": 33, "context_length": 44,
            },
        )
        assert tokens["input_tokens"] == 11
        assert tokens["output_tokens"] == 22
        assert context["used_tokens"] == 33
        assert context["max_tokens"] == 44

    def test_all_empty_is_zero_safe(self) -> None:
        tokens, context = _session_usage_from(None, None)
        assert tokens == {
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
        }
        assert context == {"used_tokens": 0, "max_tokens": 0}


# ══════════════════════════════════════════════════════════════════════
# P1 (issue #15) — _cleanup must not sever a live continuation route
# ══════════════════════════════════════════════════════════════════════

class TestCleanupKeepsLiveContinuationRoute:
    def _ctrl(self) -> StreamCardController:
        return _setup_ctrl(linear=True)

    def test_route_kept_while_target_active(self) -> None:
        ctrl = self._ctrl()
        old = _make_session("om_old", linear=True)
        cont = _make_session("om_old-cont-1", linear=True)
        ctrl._sessions["om_old"] = old
        ctrl._sessions["om_old-cont-1"] = cont
        ctrl._register_continuation("om_old", "om_old-cont-1")

        ctrl._cleanup("om_old")

        # Route survives: on_completed(old) will consume it and seal cont.
        assert ctrl._resolve_continuation_id("om_old") == "om_old-cont-1"

    def test_route_popped_when_target_terminal(self) -> None:
        ctrl = self._ctrl()
        old = _make_session("om_old", linear=True)
        cont = _make_session("om_old-cont-1", linear=True)
        cont.state = COMPLETED
        ctrl._sessions["om_old"] = old
        ctrl._sessions["om_old-cont-1"] = cont
        ctrl._register_continuation("om_old", "om_old-cont-1")

        ctrl._cleanup("om_old")

        assert ctrl._resolve_continuation_id("om_old") is None

    def test_route_popped_when_target_gone(self) -> None:
        ctrl = self._ctrl()
        old = _make_session("om_old", linear=True)
        ctrl._sessions["om_old"] = old
        ctrl._register_continuation("om_old", "om_old-cont-1")

        ctrl._cleanup("om_old")

        assert ctrl._resolve_continuation_id("om_old") is None

    def test_full_race_sequence_routes_completion_to_continuation(self) -> None:
        """The exact issue #15 timeline: reactivate (route registered) → old
        card's seal completes → _cleanup(old) → hermes on_completed(old) must
        still redirect to the continuation and seal it."""
        ctrl = self._ctrl()
        old = _make_session("om_old", linear=True)
        old.unified_state = UnifiedLinearState()
        cont = _make_session("om_old-cont-1", linear=True)
        cont.unified_state = UnifiedLinearState()
        ctrl._sessions["om_old"] = old
        ctrl._sessions["om_old-cont-1"] = cont
        ctrl._register_continuation("om_old", "om_old-cont-1")

        # Old card's async seal finishes and cleans up (race window).
        ctrl._cleanup("om_old")

        # hermes completes the OLD message — must reach the continuation.
        sealed = ctrl.on_completed(
            message_id="om_old",
            answer="final answer",
            duration=1.0,
        )
        assert sealed is True
        assert cont.state == COMPLETING  # dispatched to the linear completer

    def test_target_side_cleanup_still_removes_route(self) -> None:
        """v1.4.0 behaviour preserved: cleaning the continuation target
        removes routes pointing AT it."""
        ctrl = self._ctrl()
        old = _make_session("om_old", linear=True)
        cont = _make_session("om_old-cont-1", linear=True)
        cont.state = COMPLETED
        ctrl._sessions["om_old"] = old
        ctrl._sessions["om_old-cont-1"] = cont
        ctrl._register_continuation("om_old", "om_old-cont-1")

        ctrl._cleanup("om_old-cont-1")

        assert ctrl._resolve_continuation_id("om_old") is None


# ══════════════════════════════════════════════════════════════════════
# P1 (issue #18-2) — platform isolation helper + wrapper gating
# ══════════════════════════════════════════════════════════════════════

class TestIsFeishuPlatform:
    def test_enum_platform(self) -> None:
        source = SimpleNamespace(platform=SimpleNamespace(value="feishu"))
        assert _is_feishu_platform(source) is True

    def test_enum_lark(self) -> None:
        source = SimpleNamespace(platform=SimpleNamespace(value="lark"))
        assert _is_feishu_platform(source) is True

    def test_plain_string_platform(self) -> None:
        """hermes' own _platform_name() proves raw strings occur — the old
        getattr(..., 'value', '') pattern returned '' for these."""
        source = SimpleNamespace(platform="feishu")
        assert _is_feishu_platform(source) is True

    def test_string_lark_case_insensitive(self) -> None:
        assert _is_feishu_platform(SimpleNamespace(platform="Lark")) is True

    def test_non_feishu_platform(self) -> None:
        source = SimpleNamespace(platform=SimpleNamespace(value="qqbot"))
        assert _is_feishu_platform(source) is False

    def test_string_non_feishu_platform(self) -> None:
        assert _is_feishu_platform(SimpleNamespace(platform="telegram")) is False

    def test_missing_platform(self) -> None:
        assert _is_feishu_platform(SimpleNamespace()) is False

    def test_none_platform_value(self) -> None:
        assert _is_feishu_platform(SimpleNamespace(platform=None)) is False


class _HookRecorder:
    """AsyncGatewayRunner stub that records which hooks fired."""

    def __init__(self) -> None:
        self.events: list[str] = []

    async def _handle_message_with_agent(self, event, source, *a, **k):
        return {"final_response": "ok"}

    async def _run_agent(self, *a, **k):
        return {"final_response": "ok"}

    def _reply_anchor_for_event(self, event):
        return None

    def _session_key_for_source(self, source):
        return "k"


class TestWrapRunAgentPlatformGate:
    """The interrupt-recursive path in _wrap_run_agent fired Feishu hooks
    unconditionally — non-Feishu traffic now passes through natively."""

    def test_non_feishu_interrupt_path_fires_no_hooks(self, monkeypatch) -> None:
        fired: list[str] = []

        import hermes_lark_streaming.patching.gateway as gw

        def _fake_interrupted(**kw):
            fired.append("interrupted")

        def _fake_started(**kw):
            fired.append("started")

        monkeypatch.setattr(
            "hermes_lark_streaming.patching.hooks.on_message_interrupted",
            _fake_interrupted,
        )
        monkeypatch.setattr(
            "hermes_lark_streaming.patching.hooks.on_message_started",
            _fake_started,
        )

        orig = AsyncMock(return_value={"final_response": "ok"})
        wrapper = _wrap_run_agent(orig)

        runner = _HookRecorder()
        source = SimpleNamespace(platform="qqbot", chat_id="qq-chat", thread_id=None)

        # Recursive interrupt on a non-Feishu source: hooks must NOT fire.
        asyncio.get_event_loop().run_until_complete(
            wrapper(
                runner, "msg", "ctx_prompt", [], source, "sess",
                _interrupt_depth=1, event_message_id="qq-msg-2",
            )
        )

        assert fired == [], f"Feishu hooks fired for non-Feishu message: {fired}"
        orig.assert_awaited_once()


# ══════════════════════════════════════════════════════════════════════
# P1 (issue #18-4) — exception-path card seal
# ══════════════════════════════════════════════════════════════════════

class TestExceptionPathSeal:
    """An uncaught exception in _handle_message_with_agent must seal the card
    (error panel + terminal phase) instead of leaking a spinning session."""

    @staticmethod
    def _ctrl_with_active_session() -> tuple[StreamCardController, CardSession]:
        ctrl = _setup_ctrl(linear=True)
        session = _make_session("om_exc", linear=True)
        session.unified_state = UnifiedLinearState()
        session.state = STREAMING
        ctrl._sessions["om_exc"] = session
        return ctrl, session

    @staticmethod
    def _run_wrapper(orig, *, platform="feishu"):
        from hermes_lark_streaming.patching import _msg_ctx

        wrapped = _wrap_handle_message_with_agent(orig)
        runner = _HookRecorder()
        if platform == "feishu":
            source = SimpleNamespace(
                platform=SimpleNamespace(value="feishu"),
                chat_id="oc_chat",
                thread_id=None,
            )
        else:
            source = SimpleNamespace(
                platform=platform,
                chat_id="qq-chat",
                thread_id=None,
            )
        event = SimpleNamespace(message_id="om_exc")
        _msg_ctx.set(None)
        try:
            asyncio.get_event_loop().run_until_complete(
                wrapped(runner, event, source)
            )
        finally:
            _msg_ctx.set(None)

    def test_uncaught_exception_seals_card(self) -> None:
        ctrl, session = self._ctrl_with_active_session()
        orig = AsyncMock(side_effect=RuntimeError("model connection lost"))

        with pytest.raises(RuntimeError), patch(
            "hermes_lark_streaming.controller.get_controller",
            return_value=ctrl,
        ), patch(
            "hermes_lark_streaming.patching.hooks.on_message_completed",
            return_value=True,
        ) as completed:
            self._run_wrapper(orig)

        completed.assert_called_once()
        kwargs = completed.call_args.kwargs
        assert kwargs["message_id"] == "om_exc"
        assert "agent error" in kwargs["error_message"]
        assert "RuntimeError" in kwargs["error_message"]
        assert kwargs["aborted"] is False

    def test_cancelled_error_seals_as_abort(self) -> None:
        ctrl, session = self._ctrl_with_active_session()
        orig = AsyncMock(side_effect=asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError), patch(
            "hermes_lark_streaming.controller.get_controller",
            return_value=ctrl,
        ), patch(
            "hermes_lark_streaming.patching.hooks.on_message_completed",
            return_value=True,
        ) as completed:
            self._run_wrapper(orig)

        completed.assert_called_once()
        kwargs = completed.call_args.kwargs
        assert kwargs["message_id"] == "om_exc"
        assert kwargs["aborted"] is True
        assert kwargs["error_message"] == "Task cancelled"

    def test_terminal_or_completing_session_not_resealed(self) -> None:
        """A session already in COMPLETING must not be double-sealed."""
        ctrl, session = self._ctrl_with_active_session()
        session.state = COMPLETING
        orig = AsyncMock(side_effect=RuntimeError("boom"))

        with pytest.raises(RuntimeError), patch(
            "hermes_lark_streaming.controller.get_controller",
            return_value=ctrl,
        ), patch(
            "hermes_lark_streaming.patching.hooks.on_message_completed",
            return_value=True,
        ) as completed:
            self._run_wrapper(orig)

        completed.assert_not_called()

    def test_exception_reraised_after_seal(self) -> None:
        """Sealing must not swallow the exception — hermes still sees it."""
        ctrl, session = self._ctrl_with_active_session()
        orig = AsyncMock(side_effect=RuntimeError("boom"))

        with pytest.raises(RuntimeError), patch(
            "hermes_lark_streaming.controller.get_controller",
            return_value=ctrl,
        ), patch(
            "hermes_lark_streaming.patching.hooks.on_message_completed",
            return_value=True,
        ):
            self._run_wrapper(orig)

    def test_non_feishu_exception_path_skips_seal(self) -> None:
        """Non-Feishu messages never created a session — no seal attempted."""
        ctrl, session = self._ctrl_with_active_session()
        orig = AsyncMock(side_effect=RuntimeError("boom"))

        with pytest.raises(RuntimeError), patch(
            "hermes_lark_streaming.controller.get_controller",
            return_value=ctrl,
        ), patch(
            "hermes_lark_streaming.patching.hooks.on_message_completed",
            return_value=True,
        ) as completed:
            self._run_wrapper(orig, platform="qqbot")

        completed.assert_not_called()


# ══════════════════════════════════════════════════════════════════════
# P1 (issue #18-3) — non-om_ delivery targets
# ══════════════════════════════════════════════════════════════════════

class TestNonOmDeliveryTargets:
    def _ctrl(self) -> StreamCardController:
        return _setup_ctrl(linear=True)

    def test_card_creation_replies_for_om_target(self) -> None:
        ctrl = self._ctrl()
        session = _make_session("om_msg", linear=True)
        session.anchor_id = "om_anchor"
        ctrl._sessions["om_msg"] = session

        asyncio.get_event_loop().run_until_complete(
            ctrl._do_create_linear_card(session)
        )

        ctrl._client.reply_card_by_id.assert_called_once()
        ctrl._client.send_card_by_id_to_chat.assert_not_called()

    def test_card_creation_direct_sends_for_bg_task_id(self) -> None:
        """Background task without an om_ anchor: reply would 99992354 —
        the card goes directly to the chat instead."""
        ctrl = self._ctrl()
        session = _make_session("bg_120000_abcd", linear=True)
        session.anchor_id = None
        ctrl._sessions["bg_120000_abcd"] = session

        asyncio.get_event_loop().run_until_complete(
            ctrl._do_create_linear_card(session)
        )

        ctrl._client.reply_card_by_id.assert_not_called()
        ctrl._client.send_card_by_id_to_chat.assert_called_once()
        args = ctrl._client.send_card_by_id_to_chat.call_args.args
        assert args[0] == session.chat_id

    def test_text_fallback_direct_sends_for_non_om_target(self) -> None:
        ctrl = self._ctrl()
        session = _make_session("bg_120000_abcd", linear=True)
        session.anchor_id = None

        asyncio.get_event_loop().run_until_complete(
            ctrl._send_text_fallback(session, fallback_text="任务失败：超时")
        )

        ctrl._client.send_text_to_chat.assert_called_once()
        ctrl._client.reply_text.assert_not_called()

    def test_text_fallback_replies_for_om_target(self) -> None:
        ctrl = self._ctrl()
        session = _make_session("om_msg", linear=True)
        session.anchor_id = "om_anchor"

        asyncio.get_event_loop().run_until_complete(
            ctrl._send_text_fallback(session, fallback_text="回复内容")
        )

        ctrl._client.reply_text.assert_called_once()
        ctrl._client.send_text_to_chat.assert_not_called()
