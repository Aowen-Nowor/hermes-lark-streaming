"""Callback wrapping for AIAgent streaming callbacks."""

from __future__ import annotations

from typing import Any

from . import (
    _msg_ctx,
    _thread_local_ctx,
    _logger,
    _get_event_message_id,
)

def _resolve_eid(fallback_eid: str | None) -> str | None:
    """Re-resolve the current event_message_id from _msg_ctx at call time."""
    _eid = _get_event_message_id()
    return _eid if _eid else fallback_eid

def _maybe_wrap_callbacks(agent) -> None:
    """Replace streaming callbacks on *agent* with wrappers that also fire
    Feishu CardKit updates.  Skips silently when outside a Feishu message
    context (i.e. no event_message_id in context)."""
    _logger.debug("HLS: _maybe_wrap_callbacks invoked, has_stream=%s, eid_lookup=%s", bool(getattr(agent, "stream_delta_callback", None)), bool(_get_event_message_id()))

    eid = _get_event_message_id()
    if not eid:
        _logger.debug("HLS: skip — no event_message_id in ctx")
        return  # Not in a hermes-lark-streaming context — skip

    _current_stream = getattr(agent, "stream_delta_callback", None)
    _current_interim = getattr(agent, "interim_assistant_callback", None)
    _any_wrapped = (
        (_current_stream and getattr(_current_stream, "_hls_wrapper", False))
        or (_current_interim and getattr(_current_interim, "_hls_wrapper", False))
    )
    if _any_wrapped:
        # ── Late-arriving reasoning_callback fix ──
        _late_reasoning = getattr(agent, "reasoning_callback", None)
        if _late_reasoning and not getattr(_late_reasoning, "_hls_wrapper", False):
            _orig_late = _late_reasoning

            def _late_reasoning_wrapper(text, *args, **kwargs):
                _eid = _resolve_eid(eid)
                try:
                    from .hooks import on_reasoning_delta
                    if text and _eid:
                        on_reasoning_delta(message_id=_eid, text=text)
                except Exception:
                    _logger.debug("HLS: suppressed exception", exc_info=True)
                # again with a stale eid, duplicating reasoning text.
                if not getattr(_orig_late, "_hls_wrapper", False):
                    return _orig_late(text, *args, **kwargs)

            agent.reasoning_callback = _late_reasoning_wrapper
            setattr(agent.reasoning_callback, "_hls_wrapper", True)
        return

    # v1.3.2 fix (P3-02): _stream_consumed_len is cleaned up when the thinking
    _stream_consumed_len: dict[str, int] = {}

    def _cleanup_consumed_len(_eid: str) -> None:
        """Remove consumed-length tracking for a completed message."""
        _stream_consumed_len.pop(_eid, None)

    if getattr(agent, "stream_delta_callback", None):
        _orig_stream = agent.stream_delta_callback

        def _answer_wrapper(text, *args, **kwargs):
            _eid = _resolve_eid(eid)
            if not _eid:
                return _orig_stream(text, *args, **kwargs)
            try:
                from .hooks import on_answer_delta

                if text and on_answer_delta(message_id=_eid, text=text):
                    # Record total consumed length for dedup with interim_assistant_callback
                    _stream_consumed_len[_eid] = _stream_consumed_len.get(_eid, 0) + len(text)
                    return
            except Exception:
                _logger.debug("HLS: answer_wrapper exception", exc_info=True)
            return _orig_stream(text, *args, **kwargs)

        agent.stream_delta_callback = _answer_wrapper
        _logger.debug("HLS: _maybe_wrap_callbacks stream_delta_callback wrapped")
    else:
        # Fix: Create our own stream_delta_callback that routes answer tokens to
        def _answer_wrapper_synthetic(text, *args, **kwargs):
            # Handle None — stream boundary signal from conversation_loop
            # (tool boundary flush / end-of-stream). Just ignore it.
            if text is None:
                return
            _eid = _resolve_eid(eid)
            if not _eid:
                return
            try:
                from .hooks import on_answer_delta

                if text and on_answer_delta(message_id=_eid, text=text):
                    _stream_consumed_len[_eid] = _stream_consumed_len.get(_eid, 0) + len(text)
                    return
            except Exception:
                _logger.debug("HLS: answer_wrapper_synthetic exception", exc_info=True)
            # No original callback to call — Hermes didn't provide one

        agent.stream_delta_callback = _answer_wrapper_synthetic
        setattr(agent.stream_delta_callback, "_hls_wrapper", True)

    if getattr(agent, "interim_assistant_callback", None):
        _orig_interim = agent.interim_assistant_callback

        def _thinking_wrapper(text, *args, **kwargs):
            _eid = _resolve_eid(eid)
            if not _eid:
                return _orig_interim(text, *args, **kwargs)
            try:
                # ── already_streamed passthrough (Hermes hint) ──
                already_streamed = kwargs.get("already_streamed", False)
                if already_streamed:
                    # v1.3.2 fix (P3-02): clean up consumed-length tracking for
                    _cleanup_consumed_len(_eid)
                    return _orig_interim(text, *args, **kwargs)

                # ── Length-based dedup ──
                consumed_len = _stream_consumed_len.get(_eid, 0)
                if text and consumed_len > 0 and len(text) <= consumed_len:
                    # v1.3.2 fix (P3-02): same cleanup — text fully consumed,
                    # message streaming is done.
                    _cleanup_consumed_len(_eid)
                    return _orig_interim(text, *args, **kwargs)

                if text:
                    from .hooks import on_thinking_delta
                    consumed = on_thinking_delta(message_id=_eid, text=text)
                    if consumed:
                        return
            except Exception:
                _logger.debug("HLS: thinking_wrapper exception", exc_info=True)
            return _orig_interim(text, *args, **kwargs)

        agent.interim_assistant_callback = _thinking_wrapper
        setattr(agent.interim_assistant_callback, "_hls_wrapper", True)
        _logger.debug("HLS: _maybe_wrap_callbacks interim_assistant_callback wrapped")
    else:
        _logger.debug("HLS: _maybe_wrap_callbacks NO interim_assistant_callback on agent")

    # ── TOOL: wrap tool_progress_callback ──
    if getattr(agent, "tool_progress_callback", None):
        _orig_tool = agent.tool_progress_callback

        def _tool_wrapper(event_type, tool_name=None, preview=None, *args, **kwargs):
            _eid = _resolve_eid(eid)
            if not _eid:
                return _orig_tool(event_type, tool_name, preview, *args, **kwargs)
            try:
                from .hooks import on_tool_updated

                if event_type in ("tool.started", "tool.completed"):
                    if on_tool_updated(
                        message_id=_eid,
                        tool_name=tool_name or "",
                        status="started" if event_type == "tool.started" else "completed",
                        detail=preview or "",
                    ):
                        return
            except Exception:
                _logger.debug("HLS: tool_wrapper exception", exc_info=True)
            return _orig_tool(event_type, tool_name, preview, *args, **kwargs)

        agent.tool_progress_callback = _tool_wrapper

    # Mark wrapper functions so guard can detect them next time
    if getattr(agent, "stream_delta_callback", None):
        setattr(agent.stream_delta_callback, "_hls_wrapper", True)
    # interim_assistant_callback is already marked above (in its wrapper block)
    if getattr(agent, "tool_progress_callback", None):
        setattr(agent.tool_progress_callback, "_hls_wrapper", True)

    # ── REASONING: wrap reasoning_callback ──
    _orig_reasoning = getattr(agent, "reasoning_callback", None)

    def _reasoning_wrapper(text, *args, **kwargs):
        _eid = _resolve_eid(eid)
        if not _eid:
            # No ctx — call original if present
            if _orig_reasoning and not getattr(_orig_reasoning, "_hls_wrapper", False):
                return _orig_reasoning(text, *args, **kwargs)
            return
        try:
            from .hooks import on_reasoning_delta

            if text:
                on_reasoning_delta(message_id=_eid, text=text)
        except Exception:
            _logger.debug("HLS: reasoning_wrapper exception", exc_info=True)
        if _orig_reasoning and not getattr(_orig_reasoning, "_hls_wrapper", False):
            return _orig_reasoning(text, *args, **kwargs)

    agent.reasoning_callback = _reasoning_wrapper
    setattr(agent.reasoning_callback, "_hls_wrapper", True)

    # ── BACKGROUND_REVIEW: wrap background_review_callback ──
    if getattr(agent, "background_review_callback", None):
        _orig_bg = agent.background_review_callback

        def _bg_wrapper(message, *args, **kwargs):
            _eid = _resolve_eid(eid)
            if not _eid:
                return _orig_bg(message, *args, **kwargs)
            try:
                from .hooks import on_background_review_message

                deferred = on_background_review_message(
                    message_id=_eid,
                    text=message,
                    sender=_orig_bg,
                )
                if deferred:
                    return
            except Exception:
                _logger.debug("HLS: bg_wrapper exception", exc_info=True)
            return _orig_bg(message, *args, **kwargs)

        agent.background_review_callback = _bg_wrapper

    # Mark background_review_callback wrapper (already marked above for others)
    if getattr(agent, "background_review_callback", None):
        setattr(agent.background_review_callback, "_hls_wrapper", True)

    # ── Store agent reference for cache token extraction ──
    ctx = _msg_ctx.get()
    if ctx is not None:
        ctx["_agent_ref"] = agent
        _thread_local_ctx.data = dict(ctx)
