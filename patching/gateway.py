"""GatewayRunner method wrappers and cron delivery interception."""

from __future__ import annotations

import asyncio
import functools
import time
from typing import Any, Callable

from .. import __version__
from ..state.phase import TERMINAL_PHASES, CardPhase
from . import (
    _msg_ctx,
    _started_msg_ids,
    _started_msg_ids_lock,
    _thread_local_ctx,
    _logger,
    _send_result_ok,
)

# ── v1.8.3 (P1, issue #18-2): platform isolation helper ─────────

def _is_feishu_platform(source: Any) -> bool:
    """True when the message source is a Feishu/Lark platform.

    v1.8.3 (P1): single source of truth for platform gating in every
    GatewayRunner wrapper. The previous pattern
    ``getattr(getattr(source, "platform", None), "value", "")`` silently
    fails when ``source.platform`` is a plain string (returns "") — hermes'
    own code normalizes both shapes (gateway/platforms/base.py
    ``_platform_name``: ``getattr(platform, "value", platform)``), proving
    both Enum and raw string occur in the wild. A failed check let QQ/
    Telegram/etc. messages reach Feishu-only card sessions (API error
    99992354 on non-om_ message ids), and the string-platform shape would
    have disabled Feishu streaming entirely.
    """
    platform = getattr(source, "platform", None)
    value = getattr(platform, "value", platform)
    return str(value or "").lower() in ("feishu", "lark")

def _session_usage_from(ctx: dict | None, result: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """v1.8.3 (P1, issue #12): footer usage stats — agent session counters first.

    The live AIAgent session counters (``agent.session_*``) are the primary
    source: hermes maintains them for every provider in
    ``agent/turn_usage.record_response_usage()``. The turn result dict is the
    secondary source: current hermes copies the same counters into the result
    (``agent/turn_finalizer.finalize_turn``), so both agree there; older
    releases (v0.19.x, issue #12) left these keys out of the result entirely.
    ``context_length`` (the context max) exists ONLY on
    ``agent.context_compressor`` — no hermes release puts it into the result
    dict, which is why the footer "context" field never rendered.

    All readings are zero-skipping: a missing/zero value from one source
    never overwrites a real reading from the other.
    """
    res = result if isinstance(result, dict) else {}
    agent = (ctx or {}).get("_agent_ref")
    compressor = getattr(agent, "context_compressor", None) if agent else None

    def _first(*values: Any) -> int:
        for v in values:
            if v:
                return int(v)
        return 0

    def _agent(attr: str) -> Any:
        return getattr(agent, attr, 0) if agent else 0

    tokens = {
        "input_tokens": _first(_agent("session_input_tokens"), res.get("input_tokens", 0)),
        "output_tokens": _first(_agent("session_output_tokens"), res.get("output_tokens", 0)),
        "cache_read_tokens": _first(_agent("session_cache_read_tokens"), res.get("cache_read_tokens", 0)),
        "cache_write_tokens": _first(_agent("session_cache_write_tokens"), res.get("cache_write_tokens", 0)),
    }
    context = {
        # Last real prompt size — the result key (current hermes) and the
        # compressor attribute hold the same value; compressor covers old hermes.
        "used_tokens": _first(
            getattr(compressor, "last_prompt_tokens", 0), res.get("last_prompt_tokens", 0)
        ),
        # Model context window — compressor-only (see docstring).
        "max_tokens": _first(
            getattr(compressor, "context_length", 0), res.get("context_length", 0)
        ),
    }
    return tokens, context

# ── GatewayRunner method wrappers ──────────────────────────────────

def _wrap_handle_message(orig: Callable) -> Callable:
    """Inject NORMALIZE hook at the top of GatewayRunner._handle_message."""

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

        try:
            _text = (getattr(event, "text", "") or "").strip()
            if _text.lower().startswith("/aowen"):
                _source = getattr(event, "source", None)
                if _is_feishu_platform(_source) and hasattr(self, "_running_agents"):
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
                            return ""
        except Exception:
            _logger.debug("HLS: /aowen interrupt hint check failed", exc_info=True)

        return await orig(self, event, *args, **kwargs)

    return wrapper

def _wrap_handle_message_with_agent(orig: Callable) -> Callable:
    """Inject START hook at entry and ABORT/INTERRUPT detection on return."""

    @functools.wraps(orig)
    async def wrapper(self, event, source, *args, **kwargs):
        # v1.8.3 (P1, issue #18-2): non-Feishu traffic is handled natively by
        # hermes — no card sessions, no hooks, no _msg_ctx. Entry-point gate,
        # Enum/str tolerant (see _is_feishu_platform).
        if not _is_feishu_platform(source):
            return await orig(self, event, source, *args, **kwargs)

        mid = event.message_id
        anchor_id = self._reply_anchor_for_event(event)
        chat_id = source.chat_id if hasattr(source, "chat_id") else ""
        # v1.8.1 (P1): 飞书话题隔离键。on_feishu_normalize（_handle_message
        # 顶部）已先行修正引用消息的虚假 thread_id，此处读到的是真实值。
        thread_id = getattr(source, "thread_id", None)
        # v1.8.1 (P1): START 钩子只对飞书渠道生效。此前无平台过滤：QQ/
        # Telegram 等其它渠道的消息也会创建流式卡会话，卡片创建时对
        # 非 om_ 消息 id 调 reply 必然失败，会话停在 CREATION_FAILED（终
        # 态、无泄漏，但每次都刷错误日志且纯飞书部署外的多渠道部署不可
        # 用）。源头堵：非飞书消息不建会话；下游回调包装器因查不到会话
        # 自然放行纯文本回复，行为不变。
        _is_feishu = True  # 已过入口门禁，见 _is_feishu_platform

        # Track this message as started (for interrupt detection)
        with _started_msg_ids_lock:
            _started_msg_ids.add(mid)

        # ── START hook ──
        if _is_feishu:
            try:
                from .hooks import on_message_started

                on_message_started(
                    message_id=mid,
                    chat_id=chat_id,
                    anchor_id=anchor_id,
                    thread_id=thread_id,
                )
            except Exception:
                _logger.warning("HLS: suppressed exception", exc_info=True)
        msg_context = {
            "message_id": mid,
            "chat_id": chat_id,
            "anchor_id": anchor_id,
            "event_message_id": "",  # filled by _wrap_run_agent
            "card_sent": False,
            "_msg_start_time": time.monotonic(),  # 自计时：替代无法获取的 _response_time 局部变量
        }
        _msg_ctx.set(msg_context)

        # v1.3.4 fix (P1): 确保 orig() 抛异常时 _msg_ctx / _started_msg_ids
        # 导致 _msg_ctx 保留 stale event_message_id，下一条消息的
        # FeishuAdapter.send() 被静默抑制（"卡片不出现" bug）。
        def _hls_cleanup_ctx() -> None:
            with _started_msg_ids_lock:
                _started_msg_ids.discard(mid)
            _msg_ctx.set(None)
            _thread_local_ctx.data = None

        try:
            result = await orig(self, event, source, *args, **kwargs)
        except BaseException as exc:
            # v1.8.3 (P1, issue #18-4): seal the card session on uncaught
            # exceptions BEFORE cleanup. Previously this path only cleaned the
            # ctx and re-raised — the session stayed non-terminal forever,
            # _prune_stale_sessions skips active sessions to avoid killing
            # live ones, so the session (and its card stuck on "正在加载上下
            # 文...") leaked until process restart. Sealing via
            # on_message_completed shows the error panel and puts the session
            # into a terminal phase for TTL recycling. CancelledError counts
            # as user-driven abort (/stop, shutdown); everything else as error.
            try:
                from ..controller import get_controller
                from .hooks import on_message_completed

                _ctrl = get_controller()
                _sess = _ctrl._sess_get(mid) if (_ctrl and _ctrl.enabled) else None
                if _sess is not None and not _sess.is_terminal_phase and _sess.state != CardPhase.COMPLETING:
                    _err_elapsed = time.monotonic() - msg_context.get("_msg_start_time", time.monotonic())
                    _is_cancel = isinstance(exc, asyncio.CancelledError)
                    _err_msg = "Task cancelled" if _is_cancel else (
                        f"agent error: {type(exc).__name__}: {str(exc)[:160]}".rstrip()
                    )
                    _sealed = on_message_completed(
                        message_id=mid,
                        answer="",
                        duration=_err_elapsed,
                        error_message=_err_msg,
                        aborted=_is_cancel,
                    )
                    if _sealed:
                        msg_context["card_sent"] = True
                        _logger.info(
                            "HLS: sealed card on uncaught %s msg=%s",
                            type(exc).__name__, (mid or "?")[:12],
                        )
            except Exception:
                _logger.debug("HLS: exception-path seal failed", exc_info=True)
            finally:
                _hls_cleanup_ctx()
            raise

        # point to the new message's context. We must use the original
        ctx = msg_context

        # AST injection, so we return None to simulate "stale agent result",
        if result is not None:
            if ctx and ctx.get("card_sent"):
                _logger.info(
                    "card already sent for msg=%s, suppressing gateway reply",
                    mid[:12],
                )
                _hls_cleanup_ctx()
                return None
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
                            _hls_cleanup_ctx()
                            return None
            except Exception:
                _logger.warning("HLS: suppressed exception", exc_info=True)
        # None (the "Discarding stale agent result" path or the
        if result is None:
            if ctx and ctx.get("card_sent"):
                # Bug fix: Hermes returns None when already_sent=True (our
                # interrupt. Without the session-active check, stale message
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
                            thread_id=thread_id,
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
        # v1.3.4 fix (P1): cleanup on normal exit path (early returns and
        # exceptions handled by _hls_cleanup_ctx above).
        _hls_cleanup_ctx()

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
        # v1.8.3 (P1, issue #18-2): non-Feishu traffic passes through natively.
        # Previously the interrupt-recursive path below fired on_message_interrupted
        # + on_message_started UNCONDITIONALLY — a QQ/Telegram (or string-platform)
        # message interrupting an agent run created a Feishu card session keyed on
        # a non-om_ message id, and every card API call on it failed with 99992354.
        if not _is_feishu_platform(source):
            return await orig(
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

        # message's ID. We must create a fresh context for the recursive call
        _saved_parent_ctx = None  # Will hold parent context for restoration
        _original_msg_context_ref = None  # Reference to the original msg_context dict
        ctx = _msg_ctx.get()
        if ctx is not None and event_message_id:
            if _interrupt_depth > 0 and ctx.get("event_message_id") != event_message_id:
                # BUG FIX (v0.15.4): We must keep a reference to the original
                _original_msg_context_ref = ctx.get("_original_msg_context_ref") or ctx
                _saved_parent_ctx = dict(ctx)  # Save a copy for restoration after orig()
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

                # anchor_id fix: use event_message_id as the new card's
                # v1.8.1 (P1): interrupt 链路同样传递话题隔离键，新卡归入
                # 新消息所属话题。
                _src_thread_id = getattr(source, "thread_id", None)
                try:
                    from .hooks import on_message_interrupted
                    on_message_interrupted(
                        message_id=_saved_parent_ctx.get("message_id", ""),
                        new_message_id=event_message_id,
                        chat_id=ctx["chat_id"],
                        anchor_id=event_message_id,
                        thread_id=_src_thread_id,
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
                        thread_id=_src_thread_id,
                    )
                except Exception:
                    _logger.warning("HLS: suppressed exception", exc_info=True)
            else:
                ctx["event_message_id"] = event_message_id
            # Copy to thread-local for thread-pool workers
            _thread_local_ctx.data = dict(ctx)

        # v1.3.4 fix (P1): 确保 orig() 抛异常时 _saved_parent_ctx 被恢复。
        # 错误的 message_id（"wrong card gets completion" bug）。
        try:
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
        except BaseException:
            if _saved_parent_ctx is not None:
                _msg_ctx.set(_saved_parent_ctx)
                _thread_local_ctx.data = dict(_saved_parent_ctx)
            raise

        # We must fire B's COMPLETE hook first (with B's result), then
        # Previous bug: only A's ABORTED COMPLETE was fired, leaving
        # - B's card quotes A's text (stale session content)
        ctx = _msg_ctx.get()
        # v1.8.1 (P1): hermes _run_agent 捕获 agent 异常后返回错误**字符串**
        # （run.py except 分支 "Sorry, I encountered an unexpected error..."）
        # 而非 result dict。回合已结束但 message.completed 从未触发——若不
        # 在这里封卡，流式卡永转圈、会话非终态永不回收（泄漏）。用错误
        # 字符串作为 error_message 封卡，语义与 dict 路径的 error 字段
        # 一致。封卡成功置 card_sent，外层 _handle_message_with_agent 的
        # 既有抑制机制自然接管（错误文本已上卡，不再重复发纯文本）。
        if isinstance(result, str):
            _ctx_for_error = ctx or _saved_parent_ctx
            if _ctx_for_error is not None:
                try:
                    from .hooks import on_message_completed

                    _err_elapsed = time.monotonic() - _ctx_for_error.get(
                        "_msg_start_time", time.monotonic()
                    )
                    _sealed = on_message_completed(
                        message_id=_ctx_for_error.get("message_id", ""),
                        answer="",
                        duration=_err_elapsed,
                        error_message=result,
                        aborted=False,
                    )
                    if _sealed:
                        _ctx_for_error["card_sent"] = True
                        _logger.info(
                            "run_agent: sealed card with error string result "
                            "msg=%s len=%d",
                            (_ctx_for_error.get("message_id") or "?")[:12],
                            len(result),
                        )
                except Exception:
                    _logger.warning(
                        "run_agent: error-string seal failed", exc_info=True
                    )
            # 递归中断场景：返回前恢复父上下文（与下方完整路径一致，
            # 否则后续同线程消息错拿 child 上下文）。
            if _saved_parent_ctx is not None:
                _msg_ctx.set(_saved_parent_ctx)
                _thread_local_ctx.data = dict(_saved_parent_ctx)
            return result
        if _saved_parent_ctx is not None:
            # Step 1: Fire B's (child) COMPLETE hook normally
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
                    reasoning_tokens = getattr(_agent_ref_child, "session_reasoning_tokens", 0) if _agent_ref_child else 0
                    estimated_cost_usd = getattr(_agent_ref_child, "session_estimated_cost_usd", 0) if _agent_ref_child else 0
                    cost_status = getattr(_agent_ref_child, "session_cost_status", "unknown") if _agent_ref_child else "unknown"

                    # v1.8.3 (P1, issue #12): unified usage extraction
                    _tokens_child, _context_child = _session_usage_from(ctx, result)

                    card_sent_child = on_message_completed(
                        message_id=ctx["message_id"],
                        answer=result.get("final_response", ""),
                        duration=_elapsed_child,
                        model=result.get("model", ""),
                        tokens=_tokens_child,
                        context=_context_child,
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

            try:
                # Step 2: Fire A's (parent) ABORTED COMPLETE
                from .hooks import on_message_completed
                on_message_completed(
                    message_id=_saved_parent_ctx["message_id"],
                    answer="",
                    duration=time.monotonic() - _saved_parent_ctx.get("_msg_start_time", time.monotonic()),
                    aborted=True,
                    error_message="Interrupted by new message",
                )
                _saved_parent_ctx["card_sent"] = True
                # BUG FIX (v0.15.4): Also set card_sent on the original
                if _original_msg_context_ref is not None:
                    _original_msg_context_ref["card_sent"] = True
                # Also mark already_sent so Hermes's gateway doesn't send text reply
                if isinstance(result, dict):
                    result["already_sent"] = True
            except Exception:
                _logger.debug("run_agent: parent ABORTED completion failed", exc_info=True)
        elif ctx is not None:
            try:
                from .hooks import on_message_completed

                _elapsed = time.monotonic() - ctx.get("_msg_start_time", time.monotonic())

                is_interrupted = result.get("interrupted", False) or result.get("partial", False)

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

                _agent_ref = ctx.get("_agent_ref")
                reasoning_tokens = getattr(_agent_ref, "session_reasoning_tokens", 0) if _agent_ref else 0
                estimated_cost_usd = getattr(_agent_ref, "session_estimated_cost_usd", 0) if _agent_ref else 0
                cost_status = getattr(_agent_ref, "session_cost_status", "unknown") if _agent_ref else "unknown"

                # v1.8.3 (P1, issue #12): unified usage extraction
                _tokens, _context = _session_usage_from(ctx, result)

                card_sent = on_message_completed(
                    message_id=ctx["message_id"],
                    answer=result.get("final_response", ""),
                    duration=_elapsed,
                    model=result.get("model", ""),
                    tokens=_tokens,
                    context=_context,
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
        # _msg_ctx now points to the child message's context. We must
        if _saved_parent_ctx is not None:
            _msg_ctx.set(_saved_parent_ctx)
            _thread_local_ctx.data = dict(_saved_parent_ctx)

        return result

    return wrapper

def _wrap_run_conversation(orig: Callable) -> Callable:
    """Wrap all 6 streaming callbacks right before run_conversation executes."""
    # Lazy import to avoid circular dependency at module load time
    from .callbacks import _maybe_wrap_callbacks  # noqa: F811

    # v1.3.4 fix (P1): inspect.signature 可能对 C 扩展函数/wrapped callable
    import inspect
    try:
        _has_persist_ts = "persist_user_timestamp" in inspect.signature(orig).parameters
    except (ValueError, TypeError):
        _has_persist_ts = False

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

    wrapper._hls_wrapped = True
    return wrapper

# ── Background task wrapper ───────────────────────────────────────

def _wrap_run_background_task(orig: Callable) -> Callable:
    """Inject START/COMPLETE hooks for ``/background`` tasks so they get streaming cards."""

    @functools.wraps(orig)
    async def wrapper(self, prompt, source, task_id, event_message_id=None, **kwargs):
        # Only intercept Feishu platform (v1.8.3: Enum/str tolerant helper)
        if not _is_feishu_platform(source):
            return await orig(
                self, prompt, source, task_id,
                event_message_id=event_message_id, **kwargs,
            )

        chat_id = getattr(source, "chat_id", "")
        # v1.8.1 (P2): hermes 的 /bg 命令处理器把用户消息的回复锚
        # （om_ 消息 id）作为 event_message_id 传入（slash_commands.py
        # _handle_background_command → _run_background_task）。此前包装器
        # 签名未显式接收，被 **kwargs 吞掉后 START hook 只拿 task_id
        # （bg_HHMMSS_hex 合成 id）建卡——卡片 reply 对非 om_ id 必然
        # 失败，/bg 从引入以来从未真正出过流式卡。现在把它作为卡片的
        # 回复锚传下去；旧版 hermes 不传该参数时退回旧行为（无锚）。
        _bg_anchor = event_message_id or None
        _bg_thread_id = getattr(source, "thread_id", None)

        # Set up message context so _maybe_wrap_callbacks works
        _msg_ctx.set({
            "message_id": task_id,
            "chat_id": chat_id,
            "anchor_id": _bg_anchor,
            "event_message_id": task_id,  # Use task_id so callbacks find a valid eid
            "card_sent": False,
            "_msg_start_time": time.monotonic(),
            "_agent_ref": None,  # Will be filled by _maybe_wrap_callbacks
        })
        _thread_local_ctx.data = dict(_msg_ctx.get())

        # ── Fire START hook ──
        try:
            from .hooks import on_message_started
            on_message_started(
                message_id=task_id,
                chat_id=chat_id,
                anchor_id=_bg_anchor,
                thread_id=_bg_thread_id,
            )
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
                    try:
                        from gateway.platforms.base import SendResult
                        return SendResult(success=True)
                    except (ImportError, AttributeError):
                        return None
                return await original_send(chat_id, content, **send_kwargs)

            adapter.send = _intercepting_send
            # run concurrently on the same adapter, the first to finish must
            adapter._hls_bg_sending = getattr(adapter, '_hls_bg_sending', 0) + 1

        # v1.3.4 fix (P1): orig() + COMPLETE hook 都在 try 块内，finally
        try:
            result = await orig(
                self, prompt, source, task_id,
                event_message_id=event_message_id, **kwargs,
            )

            # ── Fire COMPLETE hook ──
            ctx = _msg_ctx.get()
            if ctx is not None:
                try:
                    from .hooks import on_message_completed

                    _elapsed = time.monotonic() - ctx.get("_msg_start_time", time.monotonic())

                    # Extract cache tokens from agent reference (set by _maybe_wrap_callbacks)
                    _agent_ref = ctx.get("_agent_ref")
                    reasoning_tokens = getattr(_agent_ref, "session_reasoning_tokens", 0) if _agent_ref else 0
                    estimated_cost_usd = getattr(_agent_ref, "session_estimated_cost_usd", 0) if _agent_ref else 0
                    cost_status = getattr(_agent_ref, "session_cost_status", "unknown") if _agent_ref else "unknown"

                    # v1.8.3 (P1, issue #12): unified usage extraction
                    _tokens, _context = _session_usage_from(ctx, result)

                    card_sent = on_message_completed(
                        message_id=task_id,
                        answer=(result or {}).get("final_response", ""),
                        duration=_elapsed,
                        model=(result or {}).get("model", ""),
                        tokens=_tokens,
                        context=_context,
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

            return result
        finally:
            if original_send and adapter:
                # v1.7.0 (R1-08): restore only what WE installed. With two
                # concurrent background tasks on one adapter, task A's old
                # `adapter.send = original_send` clobbered task B's still-live
                # wrapper mid-flight (B's interception silently died). Only
                # restore when our wrapper is still the active attribute.
                if getattr(adapter, "send", None) is _intercepting_send:
                    adapter.send = original_send
                # v1.3.2 fix (B3-06): default to 0 (not 1) for consistency —
                # a counter should never default to 1 when decrementing.
                adapter._hls_bg_sending = getattr(adapter, '_hls_bg_sending', 0) - 1
            # v1.3.4 fix (P1): clear context in finally — runs on ALL paths
            _msg_ctx.set(None)
            _thread_local_ctx.data = None

    return wrapper

# ── Cron delivery wrapper ──────────────────────────────────────────

def _wrap_cron_deliver(orig: Callable) -> Callable:
    """Intercept cron ``_deliver_result`` and redirect Feishu deliveries to CardKit cards."""

    @functools.wraps(orig)
    def wrapper(job, content, adapters=None, loop=None, **kwargs):
        # Only intercept when there are adapters with a Feishu/Lark platform
        if not adapters:
            return orig(job, content, adapters=adapters, loop=loop, **kwargs)

        feishu_adapter = None

        try:
            from gateway.config import Platform

            for p in list(adapters.keys()):
                pn = p.value.lower() if hasattr(p, "value") else str(p).lower()
                if pn in ("feishu", "lark"):
                    feishu_adapter = adapters[p]
                    break
        except (ImportError, AttributeError):
            pass

        if feishu_adapter is None:
            # v1.7.0 (P1 RELAY fix): relay-fronted deployment — there is no
            # native FeishuAdapter in ``adapters`` (the connector process owns
            # the credentials). Cron cards are handled by the class-level
            # RelayAdapter.send_for_platform wrapper, which detects hermes'
            # cron delivery metadata (job_id) and redirects to the cron card
            # (see patching/adapter.py _dispatch_feishu_outbound). Just log
            # and pass through to hermes' normal delivery.
            try:
                from gateway.config import Platform as _Platform

                _relay = adapters.get(_Platform.RELAY) if adapters else None
                if (
                    _relay is not None
                    and callable(getattr(_relay, "fronts_platform", None))
                    and _relay.fronts_platform("feishu")
                ):
                    _logger.info(
                        "hermes-lark-streaming v%s: cron delivery on RELAY lane "
                        "(no native feishu adapter; card redirect happens at "
                        "RelayAdapter.send_for_platform, job=%s)",
                        __version__,
                        job.get("id", "?")[:12],
                    )
            except Exception:
                _logger.debug("HLS: relay lane detection failed", exc_info=True)
            return orig(job, content, adapters=adapters, loop=loop, **kwargs)

        _logger.debug(
            # v1.8.0 (P2-2): 旧文案 "cron delivery intercepted"（拦截）误导操作
            # 者以为消息被阻断；且每次推送都打 INFO。改为中性描述 + debug。
            "hermes-lark-streaming v%s: cron delivery redirected to streaming "
            "card (job=%s)",
            __version__,
            job.get("id", "?")[:12],
        )

        # ── Temporarily replace Feishu adapter.send with card-sending version ──
        original_send = feishu_adapter.send

        async def _card_sending_send(chat_id, content, **send_kwargs):
            """Redirect Feishu adapter.send to CardKit card delivery."""
            try:
                from ..controller import get_controller
                ctrl = get_controller()
                _logger.debug(
                    "cron _card_sending_send: ctrl.enabled=%s chat=%s content_len=%d",
                    ctrl.enabled,
                    chat_id[:12] if chat_id else "?",
                    len(content) if content else 0,
                )
                if ctrl.enabled and content:
                    cleaned = content
                    if not cleaned.strip():
                        cleaned = content

                    await ctrl._do_cron_deliver(chat_id, cleaned.strip())

                    _logger.info(
                        "hermes-lark-streaming v%s: cron card delivered: chat=%s",
                        __version__,
                        chat_id[:12],
                    )
                    # Return a success result so the original _deliver_result
                    # thinks the send succeeded
                    return _send_result_ok()
            except Exception:
                # v1.7.0: was a bare `pass` — a failed cron card silently fell
                # back to plain text with zero log evidence.
                _logger.warning(
                    "cron _card_sending_send: card delivery failed, falling "
                    "back to plain text chat=%s",
                    (chat_id or "?")[:12],
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
            # v1.7.0 (R1-08): same conditional-restore as the bg-task wrapper —
            # concurrent cron deliveries on one adapter must not clobber each
            # other's interception.
            if getattr(feishu_adapter, "send", None) is _card_sending_send:
                feishu_adapter.send = original_send
            # v1.3.2 fix (B3-06): default to 0 (not 1) for consistency.
            feishu_adapter._hls_cron_sending = getattr(feishu_adapter, '_hls_cron_sending', 0) - 1

    wrapper._hls_wrapped = True
    return wrapper
