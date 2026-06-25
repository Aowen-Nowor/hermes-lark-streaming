"""Callback wrapping for AIAgent streaming callbacks.

Split from monkey_patch.py — contains:
  - _maybe_wrap_callbacks() and all inner wrapper functions
    (_answer_wrapper, _thinking_wrapper, _tool_wrapper,
     _reasoning_wrapper, _background_review_wrapper)

Dedup architecture (v1.1.0 — converged from 5 layers to 2):

  Layer 1 — ``_hls_wrapper`` mark:
      Each wrapper function is tagged with ``_hls_wrapper=True`` so the
      guard in :func:`_maybe_wrap_callbacks` can detect already-wrapped
      callbacks and skip re-wrapping.  This prevents double-wrap chains
      that would call ``on_*_delta`` twice per token.

  Layer 2 — ``already_streamed`` passthrough + ``_stream_consumed_len``:
      Hermes delivers the same text through TWO callbacks:
        1. ``stream_delta_callback``  — incremental deltas during streaming
        2. ``interim_assistant_callback`` — full accumulated text after
           the model response completes, with ``already_streamed=True/False``
      ``_stream_consumed_len[eid]`` tracks the total length consumed by
      ``stream_delta_callback`` for this eid.  When
      ``interim_assistant_callback`` arrives, if ``already_streamed=True``
      OR the text length ``<= _stream_consumed_len``, the text was fully
      streamed and we skip ``on_thinking_delta`` (passing through to the
      original callback for segment break / state management).

Removed layers (v1.1.0):
  - ``_native_reasoning_active`` flag in ``UnifiedLinearState``: replaced
    by ``len(state._current_reasoning) > 0`` check in ``_linear_on_thinking``.
  - ``_force_rewrap`` flag in ``_msg_ctx``: wrappers now re-resolve ``eid``
    from ``_msg_ctx`` at call time, so a stale ``eid`` captured at wrap
    time is no longer a concern.  The ``_hls_wrapper`` guard handles the
    common case (same agent, same task) — for the interrupt-reuse case
    (same agent, new task with new ``eid`` in ``_msg_ctx``), the wrappers
    transparently use the new ``eid`` without needing a re-wrap.
  - ``late_reasoning_wrapper`` convoluted special case: simplified to
    a single check — if main callbacks are already wrapped but
    ``reasoning_callback`` is not, wrap it inline.
"""

from __future__ import annotations

from typing import Any

from . import (
    _msg_ctx,
    _thread_local_ctx,
    _logger,
    _get_event_message_id,
)


def _resolve_eid(fallback_eid: str | None) -> str | None:
    """Re-resolve the current event_message_id from _msg_ctx at call time.

    Wrappers capture ``eid`` at wrap time, but for the interrupt-reuse
    scenario (same agent object reused for a new message), the wrapped
    callbacks must use the new ``eid`` from the per-task ``_msg_ctx``.

    Falls back to the captured ``fallback_eid`` when no ctx is set
    (defensive — should not happen in practice).
    """
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

    _logger.debug(
        "HLS: _maybe_wrap_callbacks enter eid=%s has_stream_delta=%s "
        "has_interim=%s has_tool=%s has_reasoning=%s has_bg=%s",
        eid[:12] if eid else "?",
        bool(getattr(agent, "stream_delta_callback", None)),
        bool(getattr(agent, "interim_assistant_callback", None)),
        bool(getattr(agent, "tool_progress_callback", None)),
        bool(getattr(agent, "reasoning_callback", None)),
        bool(getattr(agent, "background_review_callback", None)),
    )

    # ── Guard: skip if ANY main callback is already wrapped ──
    # The _hls_wrapper mark is set on every wrapper function we install.
    # If stream_delta_callback OR interim_assistant_callback already has
    # the mark, the agent was already wrapped (either in this task or in
    # a previous task that reused the agent object).  We skip the main
    # wrapping loop but still handle the late-arriving reasoning_callback
    # case below.
    _current_stream = getattr(agent, "stream_delta_callback", None)
    _current_interim = getattr(agent, "interim_assistant_callback", None)
    _any_wrapped = (
        (_current_stream and getattr(_current_stream, "_hls_wrapper", False))
        or (_current_interim and getattr(_current_interim, "_hls_wrapper", False))
    )
    if _any_wrapped:
        _logger.debug(
            "HLS: guard SKIP — callbacks already wrapped "
            "(stream=%s interim=%s) eid=%s",
            bool(_current_stream and getattr(_current_stream, "_hls_wrapper", False)),
            bool(_current_interim and getattr(_current_interim, "_hls_wrapper", False)),
            eid[:12] if eid else "?",
        )
        # ── Late-arriving reasoning_callback fix ──
        # Hermes sometimes sets reasoning_callback AFTER the first
        # _maybe_wrap_callbacks call.  Without wrapping it, on_reasoning
        # is never called, so reasoning text is only delivered via
        # _linear_on_thinking (interim_assistant_callback path) — which
        # works, but bypasses the native-reasoning fast path.
        #
        # If main callbacks are already wrapped but reasoning_callback
        # is not, wrap it inline and return.
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
                # Skip calling _orig_late if it is already an HLS wrapper
                # (agent reuse scenario) — would call on_reasoning_delta
                # again with a stale eid, duplicating reasoning text.
                if not getattr(_orig_late, "_hls_wrapper", False):
                    return _orig_late(text, *args, **kwargs)

            agent.reasoning_callback = _late_reasoning_wrapper
            setattr(agent.reasoning_callback, "_hls_wrapper", True)
            _logger.debug(
                "HLS: late-wrapped reasoning_callback eid=%s",
                eid[:12] if eid else "?",
            )
        return

    # ── ANSWER: wrap stream_delta_callback ──
    # Track total consumed text LENGTH for dedup with interim_assistant_callback.
    # We use length instead of exact text match because:
    #   - stream_delta_callback delivers incremental deltas: "The ", "user ", ...
    #   - interim_assistant_callback delivers accumulated text: "The user keeps asking"
    #   - Exact match on last chunk fails; length-based check is robust.
    #
    # v1.3.2 fix (P3-02): _stream_consumed_len is cleaned up when the thinking
    # wrapper detects the message is complete (text length <= consumed_len and
    # already_streamed=True), preventing unbounded growth in long-running agents
    # that handle many messages (interrupt-reuse scenario).
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
                    _logger.debug(
                        "HLS: answer_wrapper consumed text len=%d eid=%s",
                        len(text), _eid[:12],
                    )
                    # Record total consumed length for dedup with interim_assistant_callback
                    _stream_consumed_len[_eid] = _stream_consumed_len.get(_eid, 0) + len(text)
                    return
            except Exception:
                _logger.debug("HLS: answer_wrapper exception", exc_info=True)
            return _orig_stream(text, *args, **kwargs)

        agent.stream_delta_callback = _answer_wrapper
        _logger.debug("HLS: _maybe_wrap_callbacks stream_delta_callback wrapped")
    else:
        # ── Create synthetic stream_delta_callback when Hermes streaming is disabled ──
        # When Hermes streaming is disabled (streaming.enabled: false), the gateway
        # sets agent.stream_delta_callback = None. Without this callback, the agent
        # silently drops incremental answer tokens — they never reach the plugin,
        # and CardKit streaming shows no answer content until on_completed dumps
        # the full text at once.
        #
        # Fix: Create our own stream_delta_callback that routes answer tokens to
        # on_answer_delta. The agent will call this for every answer token, enabling
        # CardKit streaming even without gateway streaming.
        #
        # Call patterns from Hermes agent:
        #   stream_delta_callback(delta.content)  — incremental text token
        #   stream_delta_callback(None)           — stream boundary (tool start/end)
        #   stream_delta_callback(final_text)     — guardrail halt response
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
                    _logger.debug(
                        "HLS: answer_wrapper_synthetic consumed text len=%d eid=%s",
                        len(text), _eid[:12],
                    )
                    _stream_consumed_len[_eid] = _stream_consumed_len.get(_eid, 0) + len(text)
                    return
            except Exception:
                _logger.debug("HLS: answer_wrapper_synthetic exception", exc_info=True)
            # No original callback to call — Hermes didn't provide one

        agent.stream_delta_callback = _answer_wrapper_synthetic
        setattr(agent.stream_delta_callback, "_hls_wrapper", True)
        _logger.debug(
            "HLS: created synthetic stream_delta_callback "
            "(Hermes streaming disabled) eid=%s",
            eid[:12] if eid else "?",
        )

    # ── THINKING: wrap interim_assistant_callback ──
    # Routes interim content (status messages, thinking text) to the card.
    # When the card consumes the text, skip the original callback to prevent
    # duplicate messages (card + plain text) on Feishu.
    #
    # Dedup strategy (layer 2):
    #   1. If already_streamed=True (Hermes tells us the text was already
    #      delivered via stream_delta_callback), skip on_thinking_delta
    #      entirely and pass through to _orig_interim for segment break.
    #   2. If the text length <= total consumed by stream_delta_callback,
    #      the text was fully streamed — skip on_thinking_delta.
    #   3. Otherwise, process through on_thinking_delta for the card.
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
                    _logger.debug(
                        "HLS: thinking_wrapper SKIP(already_streamed) eid=%s len=%d",
                        _eid[:12], len(text) if text else 0,
                    )
                    # v1.3.2 fix (P3-02): clean up consumed-length tracking for
                    # this message — the streaming is done, no need to keep the
                    # entry. Prevents unbounded growth in interrupt-reuse scenarios.
                    _cleanup_consumed_len(_eid)
                    return _orig_interim(text, *args, **kwargs)

                # ── Length-based dedup ──
                consumed_len = _stream_consumed_len.get(_eid, 0)
                if text and consumed_len > 0 and len(text) <= consumed_len:
                    _logger.debug(
                        "HLS: thinking_wrapper SKIP(length_dedup) eid=%s "
                        "consumed=%d interim=%d",
                        _eid[:12], consumed_len, len(text),
                    )
                    # v1.3.2 fix (P3-02): same cleanup — text fully consumed,
                    # message streaming is done.
                    _cleanup_consumed_len(_eid)
                    return _orig_interim(text, *args, **kwargs)

                if text:
                    from .hooks import on_thinking_delta
                    _logger.debug(
                        "HLS: thinking_wrapper CALLING on_thinking_delta eid=%s "
                        "len=%d text_head=%r consumed_len=%d",
                        _eid[:12], len(text), text[:60], consumed_len,
                    )
                    consumed = on_thinking_delta(message_id=_eid, text=text)
                    if consumed:
                        _logger.debug(
                            "HLS: thinking_wrapper consumed text len=%d eid=%s",
                            len(text), _eid[:12],
                        )
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
    _logger.debug(
        "HLS: reasoning_wrapper SETUP eid=%s _orig_reasoning=%s "
        "orig_has_hls=%s",
        eid[:12] if eid else "?",
        bool(_orig_reasoning),
        getattr(_orig_reasoning, "_hls_wrapper", False) if _orig_reasoning else "N/A",
    )

    def _reasoning_wrapper(text, *args, **kwargs):
        _eid = _resolve_eid(eid)
        if not _eid:
            # No ctx — call original if present
            if _orig_reasoning and not getattr(_orig_reasoning, "_hls_wrapper", False):
                return _orig_reasoning(text, *args, **kwargs)
            return
        _logger.debug(
            "HLS: reasoning_wrapper CALLED eid=%s text=%r orig_has_hls=%s",
            _eid[:12],
            text[:50] if text else "",
            getattr(_orig_reasoning, "_hls_wrapper", False) if _orig_reasoning else "N/A",
        )
        try:
            from .hooks import on_reasoning_delta

            if text:
                on_reasoning_delta(message_id=_eid, text=text)
        except Exception:
            _logger.debug("HLS: reasoning_wrapper exception", exc_info=True)
        # Skip calling _orig_reasoning if it is already an HLS wrapper
        # (agent reuse scenario) — would call on_reasoning_delta again,
        # duplicating reasoning text in the collapsible panel.
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
