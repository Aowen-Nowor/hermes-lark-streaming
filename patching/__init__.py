"""Runtime monkey patching — replaces AST source injection at import time.

Strategy
────────
Instead of modifying ``gateway/run.py`` on disk (AST patching), we apply
runtime patches by wrapping methods on ``GatewayRunner`` and ``AIAgent``
when the plugin loads.

    GatewayRunner._handle_message           → NORMALIZE (before original)
    GatewayRunner._handle_message_with_agent → START (before) + ABORT/INTERRUPT (after)
    GatewayRunner._run_agent                 → event_message_id injection + COMPLETE (after)
    AIAgent.run_conversation                 → wraps all 6 callbacks (ANSWER, THINKING,
                                                TOOL, REASONING, BACKGROUND_REVIEW)
    cron.scheduler._deliver_result           → redirect cron Feishu deliveries to CardKit
    FeishuAdapter.send                       → intercept ALL text → convert to cards
    FeishuAdapter.edit_message               → update gateway card content (Phase 2)
    FeishuAdapter.add_reaction / _add_reaction  → card status indicator (Phase 3)
    FeishuAdapter.delete_reaction / _remove_reaction → card status clear (Phase 3)
    FeishuAdapter.send_clarify               → interactive clarify card (dropdown + input)
    FeishuAdapter._on_card_action_trigger    → clarify card callback handler

Message context (``message_id``, ``event_message_id``, ``chat_id``, …) is
propagated through a ``contextvars.ContextVar`` — safe within a single async
task execution context.
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Callable

from .. import __version__

# ── Hermes compatibility adapter (Task 3.2 + 3.3) ──────────────────
# All Hermes internal module access is funneled through HermesCompat.
# When Hermes upgrades, only patching/hermes_adapter.py needs to be updated.
# The try/except mirrors the root __init__.py pattern: relative import
# works when loaded by Hermes's plugin loader; absolute import works
# when pytest imports this file directly (conftest pre-registers the
# package in sys.modules).
try:
    from .hermes_adapter import HermesCompat
except ImportError:  # pragma: no cover — fallback for pytest-only path
    from hermes_lark_streaming.patching.hermes_adapter import HermesCompat  # type: ignore[no-redef]


__all__ = [
    # Shared state
    '_thread_local_ctx',
    '_logger',
    '_msg_ctx',
    '_started_msg_ids',
    '_started_msg_ids_lock',
    '_gateway_cards',
    '_gateway_cards_lock',
    '_gw_runner_patched',
    '_patch_status',
    # Functions
    '_get_config',
    '_get_event_message_id',
    '_get_thread_local_ctx',
    '_apply_gateway_runner_patches',
    'apply_patches',
    '_schedule_direct_patch',
    '_apply_direct_agent_patch',
    # From gateway
    '_wrap_handle_message',
    '_wrap_handle_message_with_agent',
    '_wrap_run_agent',
    '_wrap_run_background_task',
    '_wrap_cron_deliver',
    '_wrap_run_conversation',
    # From callbacks
    '_maybe_wrap_callbacks',
    # From adapter
    '_classify_gateway_message',
    '_wrap_feishu_adapter_send',
    '_register_gateway_card',
    '_unregister_gateway_card',
    '_wrap_feishu_adapter_edit',
    '_wrap_feishu_adapter_add_reaction',
    '_wrap_feishu_adapter_delete_reaction',
    '_wrap_feishu_adapter_send_clarify',
    '_wrap_feishu_card_action_trigger',
    '_handle_clarify_card_action',
    '_REACTION_STATUS_MAP',
    '_clarify_choices',
    '_clarify_questions',
    '_clarify_card_msg_ids',
    '_clarify_selections',
    '_clarify_answers',
    '_clarify_card_info',
    # From hooks
    'on_feishu_normalize',
    'on_message_started',
    'on_message_completed',
    'on_tool_updated',
    'on_answer_delta',
    'on_thinking_delta',
    'on_reasoning_delta',
    'on_background_review_message',
    'on_message_aborted',
    'on_message_interrupted',
    'on_cron_deliver',
    '_safe_hook',
]


# Thread-local storage for context propagation into worker threads
_thread_local_ctx = threading.local()
_thread_local_ctx.data = None

_logger = logging.getLogger("hermes_lark_streaming")

# Reused across calls so we don't create a new Config() per message.
# v1.3.0 P1-03: _config global cache removed — Config is a singleton since
# v1.2.0 (Config() always returns the same instance), so the outer cache was
# redundant. _get_config() now just returns Config() directly.
def _get_config():
    from ..config import Config
    return Config()


# ── Context propagation ────────────────────────────────────────────
# Set in _wrap_run_agent (from event_message_id param), read by callback
# wrappers in _maybe_wrap_callbacks.

_msg_ctx: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "hermes_lark_streaming_msg_ctx", default=None
)

# Track message starts for interrupt detection.
# When _handle_message_with_agent is called for a new message while
# an old call is still in-flight, the old call's None return indicates
# the old session was interrupted (not just aborted).
_started_msg_ids: set[str] = set()
_started_msg_ids_lock = threading.Lock()

# ── Gateway card registry (Phase 2: edit_message support) ────────────
# Maps card_msg_id → {"chat_id": str, "card_id": str|None, "category": str}
# Used by _wrap_feishu_adapter_edit to update cards created by
# _wrap_feishu_adapter_send instead of trying to edit plain text.
_gateway_cards: dict[str, dict[str, Any]] = {}
_gateway_cards_lock = threading.Lock()

# ── GatewayRunner delayed-patch guard ────────────────────────────────
# Set to True once _apply_gateway_runner_patches() succeeds (either
# immediately or from the delayed-poll thread).  Prevents double-patching.
_gw_runner_patched: bool = False

# ── Patch status report (v1.1.0) ────────────────────────────────────
# Populated by apply_patches() after all patching is done.  Read by the
# doctor CLI command (__main__.py doctor) to report which patches were
# successfully applied and which failed/skipped.
_patch_status: dict[str, Any] = {}

# When both the module-level patch and the direct AIAgent patch are active,
# The guard prevents the second call from injecting the prefix again.


def _get_event_message_id() -> str | None:
    ctx = _msg_ctx.get()
    if ctx is None:
        ctx = _get_thread_local_ctx()
    if ctx is None:
        return None
    return ctx.get("event_message_id")


def _get_thread_local_ctx() -> dict | None:
    return getattr(_thread_local_ctx, "data", None)


# ── Import wrapper functions from sub-modules ──────────────────────
# These imports must come AFTER shared state is defined to avoid circular
# import issues (sub-modules import shared state from this module).
# The sub-modules are:
#   callbacks — _maybe_wrap_callbacks and inner wrappers
#   adapter   — FeishuAdapter wrappers, clarify cards

from .gateway import (  # noqa: E402
    _wrap_handle_message,
    _wrap_handle_message_with_agent,
    _wrap_run_agent,
    _wrap_run_background_task,
    _wrap_cron_deliver,
    _wrap_run_conversation,
)
from .callbacks import (  # noqa: E402
    _maybe_wrap_callbacks,
)
from .adapter import (  # noqa: E402
    _classify_gateway_message,
    _wrap_feishu_adapter_send,
    _register_gateway_card,
    _unregister_gateway_card,
    _wrap_feishu_adapter_edit,
    _wrap_feishu_adapter_add_reaction,
    _wrap_feishu_adapter_delete_reaction,
    _wrap_feishu_adapter_send_clarify,
    _wrap_feishu_card_action_trigger,
    _handle_clarify_card_action,
    _REACTION_STATUS_MAP,
    _clarify_choices,
    _clarify_questions,
    _clarify_card_msg_ids,
    _clarify_selections,
    _clarify_answers,
    _clarify_card_info,
)
from .hooks import (  # noqa: E402
    on_feishu_normalize,
    on_message_started,
    on_message_completed,
    on_tool_updated,
    on_answer_delta,
    on_thinking_delta,
    on_reasoning_delta,
    on_background_review_message,
    on_message_aborted,
    on_message_interrupted,
    on_cron_deliver,
    _safe_hook,
)


# ── Public entry point ─────────────────────────────────────────────


def _apply_gateway_runner_patches() -> bool:
    """Apply the three critical GatewayRunner method patches.

    Patches:
      - ``_handle_message``           → NORMALIZE hook
      - ``_handle_message_with_agent`` → START + ABORT/INTERRUPT hooks
      - ``_run_agent``                → event_message_id injection + COMPLETE hook
      - ``_run_background_task``       → START/COMPLETE for background tasks (optional)

    Returns ``True`` if the patches were applied successfully,
    ``False`` if gateway.run could not be imported or was incompatible.

    Thread-safe: guarded by ``_gw_runner_patched`` flag so the delayed
    thread won't double-patch if the immediate path already succeeded.
    """
    global _gw_runner_patched

    if _gw_runner_patched:
        return True  # Already patched (e.g. immediate path succeeded)

    # Use HermesCompat instead of a direct ``from gateway.run import GatewayRunner``.
    # HermesCompat handles the import once, recording availability; the
    # delayed-poll thread re-checks by constructing a fresh instance.
    GatewayRunner = HermesCompat().gateway_runner_class
    if GatewayRunner is None:
        return False  # Not available yet

    try:
        # Patch each method individually so one missing method
        # doesn't prevent the others from being patched.
        _patched_methods = []
        if hasattr(GatewayRunner, '_handle_message'):
            GatewayRunner._handle_message = _wrap_handle_message(GatewayRunner._handle_message)
            _patched_methods.append('_handle_message')
        else:
            _logger.warning("hermes-lark-streaming: GatewayRunner._handle_message not found, skipping patch")

        if hasattr(GatewayRunner, '_handle_message_with_agent'):
            GatewayRunner._handle_message_with_agent = _wrap_handle_message_with_agent(
                GatewayRunner._handle_message_with_agent
            )
            _patched_methods.append('_handle_message_with_agent')
        else:
            _logger.warning("hermes-lark-streaming: GatewayRunner._handle_message_with_agent not found, skipping patch")

        if hasattr(GatewayRunner, '_run_agent'):
            GatewayRunner._run_agent = _wrap_run_agent(GatewayRunner._run_agent)
            _patched_methods.append('_run_agent')
        else:
            _logger.warning("hermes-lark-streaming: GatewayRunner._run_agent not found, skipping patch")

        # ── Background task patch ──
        # Wraps _run_background_task to inject START/COMPLETE hooks
        # so /background tasks also get streaming cards.
        try:
            GatewayRunner._run_background_task = _wrap_run_background_task(
                GatewayRunner._run_background_task
            )
            _patched_methods.append('_run_background_task')
        except AttributeError:
            _logger.debug("hermes-lark-streaming: _run_background_task not found, background cards disabled")

        if not _patched_methods:
            _logger.error(
                "hermes-lark-streaming: GatewayRunner patch FAILED — "
                "no methods found. Streaming cards will NOT work."
            )
            return False

        _gw_runner_patched = True
        _logger.info(
            "hermes-lark-streaming: GatewayRunner patched methods: %s",
            ', '.join(_patched_methods),
        )
        return True
    except (ImportError, AttributeError) as e:
        _logger.error(
            "hermes-lark-streaming: GatewayRunner patch FAILED — "
            "gateway.run found but incompatible. "
            "Streaming cards will NOT work. Error: %s", e,
        )
        return False


def apply_patches() -> None:
    """Apply all runtime monkey patches to ``GatewayRunner`` and ``AIAgent``.

    Call exactly once during plugin loading (from ``plugin.register()``).
    Idempotent — protected by a module-level flag.

    **Architecture-adaptive patching**: Hermes has been restructured
    multiple times internally.  This function probes which modules are
    available and applies the optimal patch strategy for that layout,
    rather than assuming a specific internal structure.

    Two equivalent patch paths for ``run_conversation``:

    1. **Module-level** (``agent.conversation_loop.run_conversation``) —
       patches the "water main" so ALL callers are intercepted.  Only
       available on Hermes v0.10+.
    2. **Direct AIAgent** (``AIAgent.run_conversation``) — patches the
       "faucet".  Works on ALL Hermes versions and is functionally
       equivalent to the module-level patch.

    Both paths call ``_maybe_wrap_callbacks(self)`` and handle
    ensures no double-injection when both are active.
    """
    if getattr(apply_patches, "_applied", False):
        return
    apply_patches._applied = True  # type: ignore[attr-defined]

    _logger.info("hermes-lark-streaming v%s: apply_patches() starting", __version__)

    # ── HermesCompat: single source of truth for Hermes internals ──
    # All Hermes internal module access (GatewayRunner, AIAgent,
    # FeishuAdapter, cron.scheduler, agent.conversation_loop) is funneled
    # through this one instance.  See patching/hermes_adapter.py for the full list.
    compat = HermesCompat()
    # ``layout`` is kept for the doctor CLI's ``hermes_layout`` print and
    # for parity with the legacy ``_detect_hermes_layout()`` contract.
    layout = compat.get_layout_report()

    # ── Patch GatewayRunner ──
    # This is the core patch — without it, streaming cards cannot work.
    gw_patched = False
    gw_delayed = False
    if compat.has_gateway_runner:
        # gateway.run already loaded — patch immediately
        if _apply_gateway_runner_patches():
            gw_patched = True
            _logger.info("hermes-lark-streaming: GatewayRunner patched ✓")
    else:
        # gateway.run not yet loaded — start delayed-patch poll thread
        _logger.info(
            "hermes-lark-streaming: gateway.run not loaded yet — "
            "starting delayed patch poll (2s interval, 60s timeout)",
        )
        gw_delayed = True

        def _delayed_gw_patch():
            """Poll for gateway.run and apply GatewayRunner patches once available."""
            deadline = time.monotonic() + 60.0  # 60-second timeout
            while time.monotonic() < deadline:
                time.sleep(2.0)  # Poll every 2 seconds
                if _apply_gateway_runner_patches():
                    _logger.info(
                        "hermes-lark-streaming: GatewayRunner patched (delayed) ✓"
                    )
                    return
                _logger.debug(
                    "hermes-lark-streaming: delayed patch — gateway.run still not available, "
                    "retrying (%.0fs remaining)",
                    deadline - time.monotonic(),
                )
            # Timeout — gateway.run never became available
            _logger.error(
                "hermes-lark-streaming: gateway.run NOT FOUND after 60s — "
                "this Hermes version may be too old or installed incorrectly. "
                "Streaming cards will NOT work. "
                "Please check: 1) Hermes is running via gateway mode, "
                "2) Hermes version >= v0.5.0, "
                "3) Re-run: hermes setup && hermes gateway start",
            )

        _delayed_thread = threading.Thread(target=_delayed_gw_patch, daemon=True)
        _delayed_thread.start()

    # ── Patch run_conversation (strategy depends on Hermes layout) ──
    # Both strategies are functionally equivalent — they both call
    # The module-level patch is preferred only because it intercepts
    # ALL callers, not just AIAgent.

    _module_patch_applied = False
    if compat.has_conversation_loop:
        # Hermes v0.10+: patch the module-level function (preferred).
        # HermesCompat has already resolved the module via its 3-strategy
        # fallback (sys.modules → anchor-based → standard import) which
        # bypasses any namespace collision.
        _cl_mod = compat.conversation_loop_module
        _cl_run_conversation = compat.conversation_loop_func
        try:
            _cl_mod.run_conversation = _wrap_run_conversation(_cl_run_conversation)
            _module_patch_applied = True
            _logger.info("hermes-lark-streaming: agent.conversation_loop module patched ✓")
        except (AttributeError, TypeError) as e:
            _logger.warning(
                "hermes-lark-streaming: agent.conversation_loop found but "
                "patch failed (%s). Falling back to direct AIAgent patch.", e,
            )

    if not _module_patch_applied:
        # Hermes <v0.10 OR module patch failed: use direct AIAgent patch
        _logger.info(
            "hermes-lark-streaming: using direct AIAgent patch "
            "(Hermes %s conversation_loop module)",
            "has no" if not compat.has_conversation_loop else "has incompatible",
        )

    # Always apply the direct AIAgent patch as well — it serves as:
    # 1. The PRIMARY patch when conversation_loop doesn't exist (older Hermes)
    # 2. A belt-and-suspenders backup when conversation_loop IS patched
    _apply_direct_agent_patch()

    # ── Cron scheduler ──
    # Patch the module-level _deliver_result function instead of the
    # Scheduler class method.  In Hermes, _deliver_result is a standalone
    # function in cron.scheduler, not Scheduler._deliver_result.
    # HermesCompat already probed both ``cron.scheduler`` and
    # ``gateway.cron.scheduler`` and stored whichever resolved in
    # ``compat.cron_scheduler_module``.
    cron_patched = False
    if compat.has_cron_scheduler:
        try:
            _cron_mod = compat.cron_scheduler_module
            _cron_mod._deliver_result = _wrap_cron_deliver(_cron_mod._deliver_result)
            cron_patched = True
            _logger.info(
                "hermes-lark-streaming: cron scheduler patched ✓ (module=%s)",
                getattr(_cron_mod, "__name__", "?"),
            )
        except (AttributeError, TypeError) as e:
            _logger.debug("hermes-lark-streaming: cron.scheduler patch failed (%s)", e)

    # ── FeishuAdapter interception (Phase 1: gateway message cards) ──
    # Patch FeishuAdapter.send() and edit_message() to intercept ALL
    # text messages and convert non-agent messages to CardKit cards.
    # This covers: slash commands, auth messages, errors, notifications,
    # session lifecycle, busy-ack, gateway lifecycle, etc.
    feishu_patched = False
    FeishuAdapter = compat.feishu_adapter_class
    if FeishuAdapter is not None:
        try:
            FeishuAdapter.send = _wrap_feishu_adapter_send(FeishuAdapter.send)
            try:
                FeishuAdapter.edit_message = _wrap_feishu_adapter_edit(FeishuAdapter.edit_message)
            except AttributeError:
                _logger.debug("hermes-lark-streaming: FeishuAdapter.edit_message not found, edit interception skipped")
            # Phase 3: Reaction → card status indicator
            # Hermes ≥某个版本 将 add_reaction/delete_reaction 改为
            # _add_reaction/_remove_reaction（private），需兼容两种命名
            try:
                FeishuAdapter.add_reaction = _wrap_feishu_adapter_add_reaction(FeishuAdapter.add_reaction)
            except AttributeError:
                try:
                    FeishuAdapter._add_reaction = _wrap_feishu_adapter_add_reaction(FeishuAdapter._add_reaction)
                except AttributeError:
                    _logger.debug("hermes-lark-streaming: FeishuAdapter.add_reaction/_add_reaction not found, reaction interception skipped")
            try:
                FeishuAdapter.delete_reaction = _wrap_feishu_adapter_delete_reaction(FeishuAdapter.delete_reaction)
            except AttributeError:
                try:
                    FeishuAdapter._remove_reaction = _wrap_feishu_adapter_delete_reaction(FeishuAdapter._remove_reaction)
                except AttributeError:
                    _logger.debug("hermes-lark-streaming: FeishuAdapter.delete_reaction/_remove_reaction not found, reaction interception skipped")
            # NOTE(v0.15.4): send_image_file / send_image interceptors DELETED (2026-06-09).
            # The v0.15.3 interception was fundamentally broken — it injected file:// URLs
            # into session.text.on_partial() which were then stripped by
            # _strip_invalid_image_keys(), and suppressed the original standalone
            # send, causing images to disappear entirely.
            # Images are now sent as standalone messages (pre-v0.15.3 behavior).
            # The three zombie functions (_try_add_image_to_session,
            # _wrap_feishu_adapter_send_image_file, _wrap_feishu_adapter_send_image)
            # have been fully removed from patching/ sub-package.

            # ── Clarify interactive card patches ──
            # Patch send_clarify to render interactive CardKit cards instead of
            # text-based numbered lists.  Patch _on_card_action_trigger to handle
            # clarify card callbacks (dropdown select, text input).
            clarify_patched = False
            try:
                FeishuAdapter.send_clarify = _wrap_feishu_adapter_send_clarify(FeishuAdapter.send_clarify)
                clarify_patched = True
                _logger.info("hermes-lark-streaming: FeishuAdapter.send_clarify patched ✓ (clarify interactive card)")
            except AttributeError:
                _logger.debug("hermes-lark-streaming: FeishuAdapter.send_clarify not found, clarify card skipped")
            try:
                FeishuAdapter._on_card_action_trigger = _wrap_feishu_card_action_trigger(FeishuAdapter._on_card_action_trigger)
                _logger.info("hermes-lark-streaming: FeishuAdapter._on_card_action_trigger patched ✓ (clarify card callback)")
            except AttributeError:
                _logger.debug("hermes-lark-streaming: FeishuAdapter._on_card_action_trigger not found, clarify callback skipped")

            feishu_patched = True
            _logger.info("hermes-lark-streaming: FeishuAdapter.send/edit/reaction/image/clarify patched ✓ (gateway message cards enabled)")
        except AttributeError as e:
            _logger.info("hermes-lark-streaming: FeishuAdapter patch skipped (%s)", e)
    else:
        _logger.info("hermes-lark-streaming: FeishuAdapter not available via HermesCompat, patch skipped")

    # ── Summary ──
    # v1.1.0: Record patch status in a structured dict for doctor command
    global _patch_status
    _patch_status = {
        "version": __version__,
        "gateway_runner": "✓" if gw_patched else ("pending" if gw_delayed else "✗"),
        "conversation_loop": "✓" if _module_patch_applied else "n/a (direct AIAgent)",
        "aiagent_direct": "applied",
        "cron_scheduler": "✓" if cron_patched else "n/a",
        "background_task": "✓" if gw_patched else ("pending" if gw_delayed else "n/a"),
        "feishu_adapter": "✓" if feishu_patched else "✗",
        "hermes_layout": layout,
    }
    _logger.info(
        "HLS: patch summary v%s — GatewayRunner=%s conversation_loop=%s "
        "AIAgent=applied cron=%s background=%s FeishuAdapter=%s layout=%s",
        __version__,
        _patch_status["gateway_runner"],
        _patch_status["conversation_loop"],
        _patch_status["cron_scheduler"],
        _patch_status["background_task"],
        _patch_status["feishu_adapter"],
        layout,
    )

    # Deferred direct patch: retry AIAgent.run_conversation after Hermes
    # finishes loading all modules (belt-and-suspenders for lazy imports)
    _schedule_direct_patch()


def _schedule_direct_patch() -> None:
    """Schedule _apply_direct_agent_patch to run after Hermes finishes loading."""
    import threading

    def _delayed_patch():
        import time
        time.sleep(2)  # Wait for Hermes to finish loading
        _apply_direct_agent_patch()

    t = threading.Thread(target=_delayed_patch, daemon=True)
    t.start()
    _logger.info("hermes-lark-streaming: scheduled direct agent patch (2s delay)")


def _apply_direct_agent_patch() -> None:
    """Directly patch AIAgent.run_conversation as belt-and-suspenders.

    The module-level agent.conversation_loop.run_conversation patch should
    suffice, but in some Hermes runtimes the module attribute replacement
    doesn't propagate to the AIAgent method's lazy import.  This function
    patches the instance method directly.
    """
    # Use HermesCompat to resolve AIAgent — keeps all Hermes internal
    # imports in one file (Task 3.2/3.3). HermesCompat returns None
    # silently when run_agent isn't loaded yet, matching the legacy
    # ``except ImportError`` deferred-patch behavior.
    AIAgent = HermesCompat().aiagent_class
    if AIAgent is None:
        _logger.info("hermes-lark-streaming: AIAgent.run_conversation direct patch deferred (run_agent not yet loaded)")
        return

    try:
        _orig_method = AIAgent.run_conversation

        # Guard: skip if already patched
        if getattr(_orig_method, "_hls_direct_patched", False):
            _logger.info("hermes-lark-streaming: AIAgent.run_conversation already directly patched, skip")
            return

        # v1.3.0 perf: compute signature check ONCE at wrap time (the signature
        # never changes at runtime). Was ~10-50μs wasted per message.
        # v1.3.4 fix (P1): inspect.signature 可能对 C 扩展/wrapped callable 抛异常
        import inspect
        try:
            _has_persist_ts = "persist_user_timestamp" in inspect.signature(_orig_method).parameters
        except (ValueError, TypeError):
            _has_persist_ts = False

        def _patched_run_conversation(
            self,
            user_message,
            system_message=None,
            conversation_history=None,
            task_id=None,
            stream_callback=None,
            persist_user_message=None,
            persist_user_timestamp=None,
            **kwargs,
        ):
            # v1.3.0: inject_time removed — Hermes v0.17.0+ has built-in
            # gateway.message_timestamps.enabled for this purpose.

            _maybe_wrap_callbacks(self)
            try:
                # 用关键字参数传递，兼容有/无 persist_user_timestamp 的 Hermes 版本
                # 如果原方法不支持 persist_user_timestamp，它会被 **kwargs 吞掉
                call_kwargs = {
                    "system_message": system_message,
                    "conversation_history": conversation_history,
                    "task_id": task_id,
                    "stream_callback": stream_callback,
                    "persist_user_message": persist_user_message,
                }
                # v1.3.0 perf: cache inspect.signature result at wrap time
                # (the signature never changes at runtime — was ~10-50μs/message wasted)
                if _has_persist_ts:
                    call_kwargs["persist_user_timestamp"] = persist_user_timestamp
                call_kwargs.update(kwargs)
                return _orig_method(self, user_message, **call_kwargs)
            finally:
                pass  # v1.3.0: inject_time guard removed

        _patched_run_conversation._hls_direct_patched = True
        AIAgent.run_conversation = _patched_run_conversation
        _logger.info("hermes-lark-streaming: AIAgent.run_conversation patched directly")
    except AttributeError as e:
        _logger.info("hermes-lark-streaming: AIAgent.run_conversation direct patch deferred (run_agent not yet loaded: %s)", e)
