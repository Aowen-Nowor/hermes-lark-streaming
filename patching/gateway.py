"""GatewayRunner method wrappers and cron delivery interception.

Split from monkey_patch.py — contains:
  - _wrap_handle_message()
  - _wrap_handle_message_with_agent()
  - _wrap_run_agent()
  - _wrap_run_conversation()
  - _wrap_run_background_task()
  - _wrap_cron_deliver()
"""

from __future__ import annotations

import functools
import time
from typing import Any, Callable

from .. import __version__
from ..state.phase import TERMINAL_PHASES
from . import (
    _msg_ctx,
    _started_msg_ids,
    _started_msg_ids_lock,
    _thread_local_ctx,
    _logger,
)


# ── GatewayRunner method wrappers ──────────────────────────────────


def _wrap_handle_message(orig: Callable) -> Callable:
    """Inject NORMALIZE hook at the top of GatewayRunner._handle_message.

    v1.1.0: Also intercept /aowen commands when an agent is running.
    When ``self._running_agents`` has an entry for this session, Hermes
    would normally take the "agent running" fast path — known slash
    commands return a hint text, but unknown commands (like /aowen) fall
    through to the default interrupt path and get sent to the LLM as
    plain text. We detect /aowen in that fast path and reply with
    build_interrupt_hint_card() instead, borrowing Hermes native
    "Agent is running — wait or /stop first" UX.
    """

    @functools.wraps(orig)
    async def wrapper(self, event, *args, **kwargs):
        # NORMALIZE hook — fires before any message processing
        try:
            from .hooks import on_feishu_normalize

            on_feishu_normalize(
                message_id=event.message_id,
                source=event.source,
                event=event,
                reply_anchor_id=self._reply_anchor_for_event(event),
            )
        except Exception:
            _logger.warning("HLS: suppressed exception", exc_info=True)

        # ── v1.1.0: /aowen interrupt hint ──
        # When an agent is running for this session, /aowen commands
        # would fall through to the LLM (pre_gateway_dispatch hook is
        # not fired on the "agent running" fast path). Intercept here
        # and reply with an orange hint card instead.
        try:
            _text = (getattr(event, "text", "") or "").strip()
            if _text.lower().startswith("/aowen"):
                _source = getattr(event, "source", None)
                _platform = getattr(getattr(_source, "platform", None), "value", "")
                if _platform == "feishu" and hasattr(self, "_running_agents"):
                    _quick_key = None
                    try:
                        _quick_key = self._session_key_for_source(_source)
                    except Exception:
                        _logger.debug("HLS: _session_key_for_source failed", exc_info=True)
                    if _quick_key and _quick_key in self._running_agents:
                        # Agent is running — send interrupt hint card
                        from ..aowen import build_interrupt_hint_card, _send_card_async
                        _chat_id = getattr(_source, "chat_id", "") if _source else ""
                        if _chat_id:
                            _logger.info(
                                "HLS: /aowen during active agent (session=%s), "
                                "sending interrupt hint card",
                                str(_quick_key)[:12],
                            )
                            _send_card_async(_chat_id, build_interrupt_hint_card(), "interrupt_hint")
                            # Return empty string to signal "handled, no further dispatch"
                            # — mirrors Hermes native slash-command hint path
                            # which returns a string reply and stops processing.
                            return ""
        except Exception:
            _logger.debug("HLS: /aowen interrupt hint check failed", exc_info=True)

        return await orig(self, event, *args, **kwargs)

    return wrapper


def _wrap_handle_message_with_agent(orig: Callable) -> Callable:
    """Inject START hook at entry and ABORT/INTERRUPT detection on return."""

    @functools.wraps(orig)
    async def wrapper(self, event, source, *args, **kwargs):
        mid = event.message_id
        anchor_id = self._reply_anchor_for_event(event)
        chat_id = source.chat_id if hasattr(source, "chat_id") else ""

        # Track this message as started (for interrupt detection)
        with _started_msg_ids_lock:
            _started_msg_ids.add(mid)

        # ── START hook ──
        try:
            from .hooks import on_message_started

            on_message_started(
                message_id=mid,
                chat_id=chat_id,
                anchor_id=anchor_id,
            )
        except Exception:
            _logger.warning("HLS: suppressed exception", exc_info=True)
        # Seed message context for downstream hooks
        # Use a dedicated dict per message to prevent context leakage
        # between concurrent/overlapping messages.
        msg_context = {
            "message_id": mid,
            "chat_id": chat_id,
            "anchor_id": anchor_id,
            "event_message_id": "",  # filled by _wrap_run_agent
            "card_sent": False,
            "_msg_start_time": time.monotonic(),  # 自计时：替代无法获取的 _response_time 局部变量
        }
        _msg_ctx.set(msg_context)

        result = await orig(self, event, source, *args, **kwargs)

        # ── Use the per-message context dict instead of _msg_ctx ──
        # When a new message interrupts the old one, _msg_ctx may already
        # point to the new message's context. We must use the original
        # per-message dict captured at entry to correctly detect
        # card_sent and interrupt states.
        ctx = msg_context

        # ── CARD ALREADY SENT → suppress Hermes reply ──
        # Runtime wrapping cannot modify gateway internals like the old
        # AST injection, so we return None to simulate "stale agent result",
        # causing Hermes to skip the text reply.
        if result is not None:
            if ctx and ctx.get("card_sent"):
                _logger.info(
                    "card already sent for msg=%s, suppressing gateway reply",
                    mid[:12],
                )
                with _started_msg_ids_lock:
                    _started_msg_ids.discard(mid)
                return None
            # Also check if a card session exists (even in terminal state).
            # This catches cases where card_sent wasn't propagated correctly
            # (e.g., interrupt scenarios with complex context chains).
            try:
                from ..controller import get_controller
                _ctrl = get_controller()
                if _ctrl and _ctrl.enabled:
                    _eid = ctx.get("event_message_id", "") if ctx else ""
                    if _eid:
                        _sess = _ctrl._sess_get(_eid)
                        if _sess and _sess.card_msg_id:
                            _logger.info(
                                "card session exists for msg=%s (state=%s), suppressing gateway reply",
                                mid[:12], _sess.state,
                            )
                            ctx["card_sent"] = True
                            with _started_msg_ids_lock:
                                _started_msg_ids.discard(mid)
                            return None
            except Exception:
                _logger.warning("HLS: suppressed exception", exc_info=True)
        # ── ABORT / INTERRUPT detection ──
        # When card was already sent, _handle_message_with_agent returns
        # None (the "Discarding stale agent result" path or the
        # already_sent=True path).  Use the per-message context (not
        # _msg_ctx) because the global context may have been overwritten
        # by a newer message.
        if result is None:
            if ctx and ctx.get("card_sent"):
                # Card was sent successfully via on_message_completed.
                # Only fire interrupt if a *genuinely newer* message started
                # after this one AND is still active (has a card session in
                # a non-terminal state).
                #
                # Bug fix: Hermes returns None when already_sent=True (our
                # _wrap_run_agent COMPLETE hook sets this), which is NOT an
                # interrupt. Without the session-active check, stale message
                # IDs left in _started_msg_ids from previous turns cause
                # false interrupt detection, showing "Interrupted by new message"
                # on cards that completed normally.
                with _started_msg_ids_lock:
                    others = _started_msg_ids - {mid}
                _real_interrupt = False
                if others:
                    # Verify the "other" message is genuinely active:
                    # it must have an active (non-terminal) card session.
                    try:
                        from ..controller import get_controller
                        _ctrl = get_controller()
                        if _ctrl and _ctrl.enabled:
                            for _other_mid in others:
                                _other_sess = _ctrl._sess_get(_other_mid)
                                if _other_sess and _other_sess.state not in TERMINAL_PHASES and _other_sess.state != "completing":
                                    _real_interrupt = True
                                    _interrupt_new_mid = _other_mid
                                    break
                        else:
                            # No controller — fall back to old behavior
                            _real_interrupt = True
                            _interrupt_new_mid = next(iter(others))
                    except Exception:
                        _real_interrupt = bool(others)
                        _interrupt_new_mid = next(iter(others)) if others else None
                if _real_interrupt:
                    try:
                        from .hooks import on_message_interrupted

                        on_message_interrupted(
                            message_id=mid,
                            new_message_id=_interrupt_new_mid,
                            chat_id=chat_id,
                            anchor_id=anchor_id,
                        )
                    except Exception:
                        _logger.warning("HLS: suppressed exception", exc_info=True)
                # else: card completed normally, Hermes returned None
                #       to suppress text reply — NOT an abort.
            else:
                # Card was never sent — real abort (error, reset, /stop, etc.)
                try:
                    from .hooks import on_message_aborted

                    on_message_aborted(message_id=mid)
                except Exception:
                    _logger.warning("HLS: suppressed exception", exc_info=True)
        elif ctx and ctx.get("card_sent"):
            # result is not None and card_sent=True — card was completed
            # by _wrap_run_agent's COMPLETE hook. Check if the card session
            # is still in a non-terminal state (e.g. card_sent was set by
            # the adapter interception path, not by actual completion).
            # This catches /stop scenarios where the card is stuck in
            # loading/marquee state.
            try:
                from ..controller import get_controller
                _ctrl = get_controller()
                if _ctrl and _ctrl.enabled:
                    _eid = ctx.get("event_message_id", "")
                    if _eid:
                        _sess = _ctrl._sess_get(_eid)
                        if _sess and _sess.state not in TERMINAL_PHASES and _sess.state != "completing":
                            _logger.info(
                                "card session stuck in non-terminal state for msg=%s "
                                "(state=%s, card_sent=%s), firing abort",
                                mid[:12], _sess.state, ctx.get("card_sent"),
                            )
                            try:
                                from .hooks import on_message_aborted
                                on_message_aborted(message_id=mid)
                            except Exception:
                                _logger.warning("HLS: suppressed exception", exc_info=True)
            except Exception:
                _logger.warning("HLS: suppressed exception", exc_info=True)
        # Cleanup tracking
        with _started_msg_ids_lock:
            _started_msg_ids.discard(mid)

        # ── Clear message context to prevent stale leakage ──
        # After this message is fully processed, the context must be
        # cleared so that subsequent non-agent messages (gateway-internal
        # messages like /status, /help, errors, etc.) sent through
        # FeishuAdapter.send() are NOT incorrectly routed to the "Agent
        # path" where they would be silently suppressed.
        #
        # Bug: Without this cleanup, _msg_ctx retains the old event_message_id
        # and card_sent=True, causing the next FeishuAdapter.send() call
        # to enter the agent suppression path and drop the message.
        _msg_ctx.set(None)
        _thread_local_ctx.data = None

        return result

    return wrapper


def _wrap_run_agent(orig: Callable) -> Callable:
    """Inject COMPLETE hook after agent runs; propagate event_message_id."""

    @functools.wraps(orig)
    async def wrapper(
        self,
        message,
        context_prompt,
        history,
        source,
        session_id,
        session_key=None,
        run_generation=None,
        _interrupt_depth=0,
        event_message_id=None,
        channel_prompt=None,
        **kwargs,
    ):
        # Store event_message_id so callback wrappers can consume it
        # When Hermes recursively calls _run_agent for an interrupt follow-up
        # (_interrupt_depth > 0), the event_message_id changes to the new
        # message's ID. We must create a fresh context for the recursive call
        # instead of mutating the parent message's context dict, because:
        # 1. The parent's COMPLETE hook (after orig() returns) still needs
        #    the original message_id and card_sent state.
        # 2. The recursive call's COMPLETE hook needs the new message_id.
        _saved_parent_ctx = None  # Will hold parent context for restoration
        _original_msg_context_ref = None  # Reference to the original msg_context dict
        ctx = _msg_ctx.get()
        if ctx is not None and event_message_id:
            if _interrupt_depth > 0 and ctx.get("event_message_id") != event_message_id:
                # Recursive interrupt follow-up: save parent context, create new context
                #
                # BUG FIX (v0.15.4): We must keep a reference to the original
                # msg_context dict (from _wrap_handle_message_with_agent) so
                # that when we set card_sent=True on the parent context, the
                # _wrap_handle_message_with_agent wrapper can also see it.
                # Without this, _saved_parent_ctx is a *copy* of the original
                # dict, and the original msg_context.card_sent stays False,
                # causing Hermes to send a duplicate plain text reply.
                _original_msg_context_ref = ctx.get("_original_msg_context_ref") or ctx
                _saved_parent_ctx = dict(ctx)  # Save a copy for restoration after orig()
                _logger.debug(
                    "run_agent: recursive interrupt follow-up, creating new context "
                    "for msg=%s (parent msg=%s, depth=%d)",
                    event_message_id[:12] if event_message_id else "?",
                    (ctx.get("message_id") or "?")[:12],
                    _interrupt_depth,
                )
                ctx = {
                    "message_id": event_message_id,
                    "chat_id": ctx.get("chat_id", ""),
                    "anchor_id": ctx.get("anchor_id"),
                    "event_message_id": event_message_id,
                    "card_sent": False,
                    "_msg_start_time": time.monotonic(),
                    "_agent_ref": None,
                    "_interrupt_depth": _interrupt_depth,
                    "_parent_message_id": ctx.get("message_id"),  # Track parent for cleanup
                    "_original_msg_context_ref": _original_msg_context_ref,  # Propagate ref to original
                }
                _msg_ctx.set(ctx)
                _thread_local_ctx.data = dict(ctx)

                # ── Fire INTERRUPT hook for the parent message immediately ──
                # This ensures the old card is marked as ABORTED before the
                # child starts processing, so the old card shows "Interrupted"
                # state instead of staying in streaming/marquee animation.
                #
                # anchor_id fix: use event_message_id as the new card's
                # anchor (the new message's reply anchor), NOT the parent
                # message's anchor_id.  Hermes passes the pending_event's
                # reply_anchor as event_message_id in the recursive call,
                # so this is the correct anchor for the new card.
                try:
                    from .hooks import on_message_interrupted
                    on_message_interrupted(
                        message_id=_saved_parent_ctx.get("message_id", ""),
                        new_message_id=event_message_id,
                        chat_id=ctx["chat_id"],
                        anchor_id=event_message_id,
                    )
                except Exception:
                    _logger.debug("run_agent: interrupt hook failed", exc_info=True)

                # Fire START hook for the new (interrupted-into) message
                try:
                    from .hooks import on_message_started
                    on_message_started(
                        message_id=event_message_id,
                        chat_id=ctx["chat_id"],
                        anchor_id=event_message_id,
                    )
                except Exception:
                    _logger.warning("HLS: suppressed exception", exc_info=True)
            else:
                ctx["event_message_id"] = event_message_id
            # Copy to thread-local for thread-pool workers
            _thread_local_ctx.data = dict(ctx)

        result = await orig(
            self,
            message,
            context_prompt,
            history,
            source,
            session_id,
            session_key=session_key,
            run_generation=run_generation,
            _interrupt_depth=_interrupt_depth,
            event_message_id=event_message_id,
            channel_prompt=channel_prompt,
            **kwargs,
        )

        # ── COMPLETE hook ──
        # After orig() returns, we need to fire the COMPLETE hooks for
        # the appropriate message(s).
        #
        # When _saved_parent_ctx is not None, we're in a recursive
        # interrupt follow-up: the inner _run_agent(B) has just returned.
        # We must fire B's COMPLETE hook first (with B's result), then
        # fire A's ABORTED COMPLETE (parent was interrupted).
        #
        # Previous bug: only A's ABORTED COMPLETE was fired, leaving
        # B's card stuck in STREAMING state forever, causing:
        # - B's card shows "已停止" (no completion update)
        # - Duplicate gateway card when Hermes sends B's result via
        #   adapter.send() (not intercepted because context was cleared)
        # - B's card quotes A's text (stale session content)
        ctx = _msg_ctx.get()
        if _saved_parent_ctx is not None:
            # ── Step 1: Fire B's (child) COMPLETE hook normally ──
            # B's context is still in _msg_ctx at this point.
            # We use B's result (the inner _run_agent's return value)
            # to complete B's card properly.
            if ctx is not None:
                try:
                    from .hooks import on_message_completed

                    _elapsed_child = time.monotonic() - ctx.get("_msg_start_time", time.monotonic())
                    is_interrupted_child = result.get("interrupted", False) or result.get("partial", False)

                    _finish_reason_child = result.get("finish_reason", "")
                    _error_msg_child = result.get("error") or result.get("interrupt_message", "")
                    if _finish_reason_child and _finish_reason_child != "stop":
                        _logger.warning(
                            "hermes-lark-streaming v%s: child non-stop finish_reason=%s model=%s msg=%s",
                            __version__,
                            _finish_reason_child,
                            result.get("model", "?"),
                            (ctx["message_id"] or "?")[:12],
                        )
                    if _error_msg_child:
                        _logger.warning(
                            "hermes-lark-streaming v%s: child agent error: %s model=%s msg=%s",
                            __version__,
                            _error_msg_child[:200],
                            result.get("model", "?"),
                            (ctx["message_id"] or "?")[:12],
                        )

                    _agent_ref_child = ctx.get("_agent_ref")
                    cache_read_child = getattr(_agent_ref_child, "session_cache_read_tokens", 0) if _agent_ref_child else 0
                    cache_write_child = getattr(_agent_ref_child, "session_cache_write_tokens", 0) if _agent_ref_child else 0
                    reasoning_tokens = getattr(_agent_ref_child, "session_reasoning_tokens", 0) if _agent_ref_child else 0
                    estimated_cost_usd = getattr(_agent_ref_child, "session_estimated_cost_usd", 0) if _agent_ref_child else 0
                    cost_status = getattr(_agent_ref_child, "session_cost_status", "unknown") if _agent_ref_child else "unknown"

                    card_sent_child = on_message_completed(
                        message_id=ctx["message_id"],
                        answer=result.get("final_response", ""),
                        duration=_elapsed_child,
                        model=result.get("model", ""),
                        tokens={
                            "input_tokens": result.get("input_tokens", 0),
                            "output_tokens": result.get("output_tokens", 0),
                            "cache_read_tokens": cache_read_child,
                            "cache_write_tokens": cache_write_child,
                        },
                        context={
                            "used_tokens": result.get("last_prompt_tokens", 0),
                            "max_tokens": result.get("context_length", 0),
                        },
                        api_calls=result.get("api_calls", 0),
                        history_offset=result.get("history_offset", 0),
                        compression_exhausted=result.get("compression_exhausted", False),
                        aborted=is_interrupted_child,
                        error_message=_error_msg_child,
                        reasoning_tokens=reasoning_tokens,
                        estimated_cost_usd=estimated_cost_usd,
                        cost_status=cost_status,
                    )
                    if card_sent_child:
                        result["already_sent"] = True
                        ctx["card_sent"] = True
                        _logger.info(
                            "run_agent: child COMPLETE hook fired for msg=%s card_sent=True",
                            (ctx["message_id"] or "?")[:12],
                        )
                except Exception:
                    _logger.debug("run_agent: child COMPLETE hook failed", exc_info=True)

            # ── Step 2: Fire A's (parent) ABORTED COMPLETE ──
            # The parent message was interrupted by the child (B).
            # Fire its COMPLETE as ABORTED so A's card shows "已停止".
            try:
                from .hooks import on_message_completed
                _logger.debug(
                    "run_agent: parent COMPLETE hook firing as interrupted "
                    "for msg=%s (child msg=%s completed normally)",
                    (_saved_parent_ctx.get("message_id") or "?")[:12],
                    (ctx.get("message_id") or "?")[:12] if ctx else "?",
                )
                on_message_completed(
                    message_id=_saved_parent_ctx["message_id"],
                    answer="",
                    duration=time.monotonic() - _saved_parent_ctx.get("_msg_start_time", time.monotonic()),
                    aborted=True,
                    error_message="Interrupted by new message",
                )
                _saved_parent_ctx["card_sent"] = True
                # BUG FIX (v0.15.4): Also set card_sent on the original
                # msg_context dict so that _wrap_handle_message_with_agent
                # can suppress the duplicate plain text reply.
                if _original_msg_context_ref is not None:
                    _original_msg_context_ref["card_sent"] = True
                    _logger.debug(
                        "run_agent: propagated card_sent=True to original "
                        "msg_context for msg=%s",
                        (_saved_parent_ctx.get("message_id") or "?")[:12],
                    )
                # Also mark already_sent so Hermes's gateway doesn't send text reply
                if isinstance(result, dict):
                    result["already_sent"] = True
            except Exception:
                _logger.debug("run_agent: parent ABORTED completion failed", exc_info=True)
        elif ctx is not None:
            try:
                from .hooks import on_message_completed

                # 自计时：计算从消息开始到 agent 运行完成的耗时
                # 原因：_response_time 是 _handle_message_with_agent 的局部变量，
                # 不在 _run_agent 的返回值 agent_result 中，
                # 所以 result.get("_response_time", 0) 永远返回 0。
                _elapsed = time.monotonic() - ctx.get("_msg_start_time", time.monotonic())

                # ── 检查是否被中断（/stop 或新消息打断） ──
                # Hermes 的 /stop 不会让 _run_agent 返回 None，而是返回
                # interrupted=True / partial=True 的 result。
                # 此时应该显示"已停止"而非"已完成"。
                is_interrupted = result.get("interrupted", False) or result.get("partial", False)

                # ── 诊断日志：记录 finish_reason / error 等关键信息 ──
                # content_filter 等异常 finish_reason 会导致 AI 返回空回复，
                # 记录这些信息便于排查模型 API 侧的内容安全过滤问题。
                _finish_reason = result.get("finish_reason", "")
                _error_msg = result.get("error") or result.get("interrupt_message", "")
                if _finish_reason and _finish_reason != "stop":
                    _logger.warning(
                        "hermes-lark-streaming v%s: non-stop finish_reason=%s model=%s msg=%s",
                        __version__,
                        _finish_reason,
                        result.get("model", "?"),
                        (ctx["message_id"] or "?")[:12],
                    )
                if _error_msg:
                    _logger.warning(
                        "hermes-lark-streaming v%s: agent error: %s model=%s msg=%s",
                        __version__,
                        _error_msg[:200],
                        result.get("model", "?"),
                        (ctx["message_id"] or "?")[:12],
                    )

                # ── Extract cache tokens from agent reference ──
                # _maybe_wrap_callbacks stores _agent_ref in ctx when wrapping
                # callbacks.  We read cache_read_tokens / cache_write_tokens
                # from the agent object for the footer's cache hit rate display.
                _agent_ref = ctx.get("_agent_ref")
                cache_read = getattr(_agent_ref, "session_cache_read_tokens", 0) if _agent_ref else 0
                cache_write = getattr(_agent_ref, "session_cache_write_tokens", 0) if _agent_ref else 0
                reasoning_tokens = getattr(_agent_ref, "session_reasoning_tokens", 0) if _agent_ref else 0
                estimated_cost_usd = getattr(_agent_ref, "session_estimated_cost_usd", 0) if _agent_ref else 0
                cost_status = getattr(_agent_ref, "session_cost_status", "unknown") if _agent_ref else "unknown"

                card_sent = on_message_completed(
                    message_id=ctx["message_id"],
                    answer=result.get("final_response", ""),
                    duration=_elapsed,
                    model=result.get("model", ""),
                    tokens={
                        "input_tokens": result.get("input_tokens", 0),
                        "output_tokens": result.get("output_tokens", 0),
                        "cache_read_tokens": cache_read,
                        "cache_write_tokens": cache_write,
                    },
                    context={
                        "used_tokens": result.get("last_prompt_tokens", 0),
                        "max_tokens": result.get("context_length", 0),
                    },
                    api_calls=result.get("api_calls", 0),
                    history_offset=result.get("history_offset", 0),
                    compression_exhausted=result.get("compression_exhausted", False),
                    aborted=is_interrupted,
                    error_message=_error_msg,
                    reasoning_tokens=reasoning_tokens,
                    estimated_cost_usd=estimated_cost_usd,
                    cost_status=cost_status,
                )
                if card_sent:
                    result["already_sent"] = True
                    ctx["card_sent"] = True
            except Exception:
                _logger.warning("HLS: suppressed exception", exc_info=True)
        # ── Restore parent context after recursive interrupt follow-up ──
        # When we created a new context for the recursive call (above),
        # _msg_ctx now points to the child message's context. We must
        # restore the parent's context so that the parent _wrap_run_agent's
        # COMPLETE hook (which fires after this wrapper returns) can
        # correctly read the parent message's context.
        if _saved_parent_ctx is not None:
            _msg_ctx.set(_saved_parent_ctx)
            _thread_local_ctx.data = dict(_saved_parent_ctx)

        return result

    return wrapper


def _wrap_run_conversation(orig: Callable) -> Callable:
    """Wrap all 6 streaming callbacks right before run_conversation executes.

    v1.3.0: inject_time removed — Hermes v0.17.0+ has built-in
    gateway.message_timestamps.enabled for time injection.
    """
    # Lazy import to avoid circular dependency at module load time
    from .callbacks import _maybe_wrap_callbacks  # noqa: F811

    # v1.3.0 perf: compute signature check ONCE at wrap time (the signature
    # never changes at runtime). Was ~10-50μs wasted per message.
    import inspect
    _has_persist_ts = "persist_user_timestamp" in inspect.signature(orig).parameters

    @functools.wraps(orig)
    def wrapper(
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
            call_kwargs = {
                "system_message": system_message,
                "conversation_history": conversation_history,
                "task_id": task_id,
                "stream_callback": stream_callback,
                "persist_user_message": persist_user_message,
            }
            if _has_persist_ts:
                call_kwargs["persist_user_timestamp"] = persist_user_timestamp
            call_kwargs.update(kwargs)
            return orig(self, user_message, **call_kwargs)
        finally:
            pass  # v1.3.0: inject_time guard removed

    return wrapper


# ── Background task wrapper ───────────────────────────────────────


def _wrap_run_background_task(orig: Callable) -> Callable:
    """Inject START/COMPLETE hooks for ``/background`` tasks so they get streaming cards.

    Background tasks run in a fire-and-forget asyncio task.  There is no
    Feishu ``message_id`` (the user's ``/background`` command message_id is
    already used by the main session).  We use the ``task_id``
    (e.g. ``bg_HHMMSS_xxxxxx``) as the message_id for the card session.

    The card is created as a **new message** (not a reply), since there is no
    original message to reply to in the background context.

    To prevent the original ``_run_background_task`` from also sending a plain
    text "✅ Background task complete" message (which would duplicate our card),
    we temporarily replace the Feishu adapter's ``send`` method with our own
    that suppresses the delivery when our card was already sent.
    """

    @functools.wraps(orig)
    async def wrapper(self, prompt, source, task_id, **kwargs):
        # Only intercept Feishu platform
        platform_name = getattr(getattr(source, "platform", None), "value", "").lower()
        if platform_name not in ("feishu", "lark"):
            return await orig(self, prompt, source, task_id, **kwargs)

        chat_id = getattr(source, "chat_id", "")

        # Set up message context so _maybe_wrap_callbacks works
        _msg_ctx.set({
            "message_id": task_id,
            "chat_id": chat_id,
            "anchor_id": None,  # No reply anchor for background tasks
            "event_message_id": task_id,  # Use task_id so callbacks find a valid eid
            "card_sent": False,
            "_msg_start_time": time.monotonic(),
            "_agent_ref": None,  # Will be filled by _maybe_wrap_callbacks
        })
        _thread_local_ctx.data = dict(_msg_ctx.get())

        # ── Fire START hook ──
        try:
            from .hooks import on_message_started
            on_message_started(message_id=task_id, chat_id=chat_id, anchor_id=None)
        except Exception:
            _logger.debug("background task START hook failed", exc_info=True)

        # ── Wrap adapter.send to suppress duplicate text delivery ──
        adapter = None
        original_send = None

        try:
            if hasattr(self, "adapters") and source.platform:
                adapter = self.adapters.get(source.platform)
        except Exception:
            _logger.warning("HLS: suppressed exception", exc_info=True)
        if adapter:
            original_send = adapter.send

            async def _intercepting_send(chat_id, content, **send_kwargs):
                """Suppress plain text delivery when our card was sent."""
                ctx = _msg_ctx.get()
                if ctx and ctx.get("card_sent"):
                    _logger.debug(
                        "background task: suppressing adapter.send (card already sent), chat=%s",
                        chat_id[:12] if chat_id else "?",
                    )
                    try:
                        from gateway.platforms.base import SendResult
                        return SendResult(success=True)
                    except (ImportError, AttributeError):
                        return None
                return await original_send(chat_id, content, **send_kwargs)

            adapter.send = _intercepting_send
            # Use counter instead of boolean flag — if two background tasks
            # run concurrently on the same adapter, the first to finish must
            # not reset the flag while the second is still running.
            adapter._hls_bg_sending = getattr(adapter, '_hls_bg_sending', 0) + 1

        try:
            result = await orig(self, prompt, source, task_id, **kwargs)
        finally:
            if original_send and adapter:
                adapter.send = original_send
                # v1.3.2 fix (B3-06): default to 0 (not 1) for consistency —
                # a counter should never default to 1 when decrementing.
                adapter._hls_bg_sending = getattr(adapter, '_hls_bg_sending', 0) - 1

        # ── Fire COMPLETE hook ──
        ctx = _msg_ctx.get()
        if ctx is not None:
            try:
                from .hooks import on_message_completed

                _elapsed = time.monotonic() - ctx.get("_msg_start_time", time.monotonic())

                # Extract cache tokens from agent reference (set by _maybe_wrap_callbacks)
                _agent_ref = ctx.get("_agent_ref")
                cache_read = getattr(_agent_ref, "session_cache_read_tokens", 0) if _agent_ref else 0
                cache_write = getattr(_agent_ref, "session_cache_write_tokens", 0) if _agent_ref else 0
                reasoning_tokens = getattr(_agent_ref, "session_reasoning_tokens", 0) if _agent_ref else 0
                estimated_cost_usd = getattr(_agent_ref, "session_estimated_cost_usd", 0) if _agent_ref else 0
                cost_status = getattr(_agent_ref, "session_cost_status", "unknown") if _agent_ref else "unknown"

                card_sent = on_message_completed(
                    message_id=task_id,
                    answer=(result or {}).get("final_response", ""),
                    duration=_elapsed,
                    model=(result or {}).get("model", ""),
                    tokens={
                        "input_tokens": (result or {}).get("input_tokens", 0),
                        "output_tokens": (result or {}).get("output_tokens", 0),
                        "cache_read_tokens": cache_read,
                        "cache_write_tokens": cache_write,
                    },
                    context={
                        "used_tokens": (result or {}).get("last_prompt_tokens", 0),
                        "max_tokens": (result or {}).get("context_length", 0),
                    },
                    api_calls=(result or {}).get("api_calls", 0),
                    history_offset=(result or {}).get("history_offset", 0),
                    compression_exhausted=(result or {}).get("compression_exhausted", False),
                    aborted=False,
                    error_message=(result or {}).get("error") or "",
                    reasoning_tokens=reasoning_tokens,
                    estimated_cost_usd=estimated_cost_usd,
                    cost_status=cost_status,
                )

                if card_sent:
                    ctx["card_sent"] = True
                    # Mark result so upstream knows card was sent
                    if result is not None and isinstance(result, dict):
                        result["_hls_card_sent"] = True
            except Exception:
                _logger.debug("background task COMPLETE hook failed", exc_info=True)

        # Clear context
        _msg_ctx.set(None)
        _thread_local_ctx.data = None

        return result

    return wrapper


# ── Cron delivery wrapper ──────────────────────────────────────────


def _wrap_cron_deliver(orig: Callable) -> Callable:
    """Intercept cron ``_deliver_result`` and redirect Feishu deliveries to CardKit cards.

    The original ``cron.scheduler._deliver_result`` is a **module-level function**
    (not a class method) with signature::

        def _deliver_result(job: dict, content: str, adapters=None, loop=None)

    It iterates over delivery targets from ``_resolve_delivery_targets(job)``,
    and for each Feishu target calls ``runtime_adapter.send(chat_id, text, …)``.

    Our wrapper temporarily replaces the Feishu adapter's ``send`` method with a
    card-sending version.  This way:
    - All the original's logic (thread_id, metadata, error handling) still works.
    - Feishu text messages are replaced with CardKit cards.
    - If card delivery fails, it falls back to the original plain-text send.
    - No duplicate messages (card replaces text, not supplements it).
    - Thread-safe: the original ``send`` is restored in a ``finally`` block.
    """

    @functools.wraps(orig)
    def wrapper(job, content, adapters=None, loop=None, **kwargs):
        # Only intercept when there are adapters with a Feishu/Lark platform
        if not adapters:
            return orig(job, content, adapters=adapters, loop=loop, **kwargs)

        feishu_adapter = None
        feishu_platform_key = None

        try:
            from gateway.config import Platform

            for p in list(adapters.keys()):
                pn = p.value.lower() if hasattr(p, "value") else str(p).lower()
                if pn in ("feishu", "lark"):
                    feishu_adapter = adapters[p]
                    feishu_platform_key = p
                    break
        except (ImportError, AttributeError):
            pass

        if feishu_adapter is None:
            return orig(job, content, adapters=adapters, loop=loop, **kwargs)

        _logger.info(
            "hermes-lark-streaming v%s: cron delivery intercepted, redirecting to card (job=%s)",
            __version__,
            job.get("id", "?")[:12],
        )

        # ── Temporarily replace Feishu adapter.send with card-sending version ──
        original_send = feishu_adapter.send

        async def _card_sending_send(chat_id, content, **send_kwargs):
            """Redirect Feishu adapter.send to CardKit card delivery.

            This async function replaces the Feishu adapter's ``send`` method.
            Hermes calls ``safe_schedule_threadsafe(adapter.send(...), loop)``
            from ``_deliver_result``, which schedules this coroutine on the
            gateway's event loop.  Since we are *already* running on the event
            loop, we can simply ``await`` the card delivery — no
            ``run_coroutine_threadsafe`` / ``asyncio.run`` needed.

            v1.3.1 fix: parameter names aligned to FeishuAdapter.send signature
            (chat_id, content) — Hermes calls with keyword arguments
            ``self.send(chat_id=..., content=...)``, so mismatched names cause
            TypeError.

            Previous versions used ``run_coroutine_threadsafe`` +
            ``future.result(timeout=30)`` when the loop was running, which
            caused a **deadlock**: the loop was blocked waiting for a coroutine
            it could never schedule because it was blocked.  The 30-second
            timeout expired and the delivery fell back to plain text.
            """
            try:
                from ..controller import get_controller
                ctrl = get_controller()
                _logger.info(
                    "cron _card_sending_send: ctrl.enabled=%s chat=%s content_len=%d",
                    ctrl.enabled,
                    chat_id[:12] if chat_id else "?",
                    len(content) if content else 0,
                )
                if ctrl.enabled and content:
                    cleaned = content
                    if not cleaned.strip():
                        cleaned = content

                    # We are running on the event loop (scheduled via
                    # safe_schedule_threadsafe by _deliver_result), so we
                    # can await the card delivery directly.
                    await ctrl._do_cron_deliver(chat_id, cleaned.strip())

                    _logger.info(
                        "hermes-lark-streaming v%s: cron card delivered: chat=%s",
                        __version__,
                        chat_id[:12],
                    )
                    # Return a success result so the original _deliver_result
                    # thinks the send succeeded
                    try:
                        from gateway.platforms.base import SendResult
                        return SendResult(success=True)
                    except (ImportError, AttributeError):
                        return None
            except Exception:
                _logger.debug(
                    "hermes-lark-streaming v%s: cron card delivery failed, falling back to plain text",
                    __version__,
                    exc_info=True,
                )

            # Fallback: send plain text via the original adapter
            return await original_send(chat_id, content, **send_kwargs)

        feishu_adapter.send = _card_sending_send
        # Use counter instead of boolean flag — same rationale as _hls_bg_sending.
        feishu_adapter._hls_cron_sending = getattr(feishu_adapter, '_hls_cron_sending', 0) + 1
        try:
            return orig(job, content, adapters=adapters, loop=loop, **kwargs)
        finally:
            feishu_adapter.send = original_send
            # v1.3.2 fix (B3-06): default to 0 (not 1) for consistency.
            feishu_adapter._hls_cron_sending = getattr(feishu_adapter, '_hls_cron_sending', 0) - 1

    return wrapper

