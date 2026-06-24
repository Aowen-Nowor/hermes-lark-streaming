"""FeishuAdapter interception layer — send, edit, reactions, and clarify cards.

Split from monkey_patch.py — contains:
  - _classify_gateway_message()
  - _wrap_feishu_adapter_send()
  - _register_gateway_card() / _unregister_gateway_card()
  - _wrap_feishu_adapter_edit()
  - _REACTION_STATUS_MAP / _wrap_feishu_adapter_add_reaction() / _wrap_feishu_adapter_delete_reaction()
  - _clarify_* registry / _wrap_feishu_adapter_send_clarify()
  - _wrap_feishu_card_action_trigger() / _handle_clarify_card_action() / _schedule_confirm_card()
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Callable

from .. import __version__
from . import (
    _msg_ctx,
    _gateway_cards,
    _gateway_cards_lock,
    _logger,
    _get_config,
)


# ── FeishuAdapter interception layer (Phase 1: gateway message cards) ─




def _classify_gateway_message(content: str) -> str:
    """Classify a gateway-internal message by its content for card category.

    Returns one of: "error", "auth", "session", "slash", "system"
    """
    if not isinstance(content, str):
        return "system"
    # Auth / pairing messages
    if any(kw in content for kw in ("pairing code", "pairing requests", "配对码", "I don't recognize you")):
        return "auth"
    # Error messages
    if any(kw in content for kw in ("❌", "⚠️", "error", "failed", "Error", "Failed")):
        return "error"
    # Session lifecycle messages
    if any(kw in content for kw in ("Session", "session", "🔄", "♻", "compress", "compres")):
        return "session"
    # Slash command replies (common prefixes)
    if any(kw in content for kw in ("/help", "/status", "/model", "/usage", "/whoami", "/reset", "/new", "/stop", "/resume", "/undo", "/compress", "/goal", "/agents", "/background", "/queue", "/steer", "/yolo", "/footer")):
        return "slash"
    return "system"


def _wrap_feishu_adapter_send(orig_send: Callable) -> Callable:
    """Intercept ``FeishuAdapter.send()`` — convert text to gateway cards.

    This wrapper intercepts ALL text messages sent through the Feishu
    adapter's ``send()`` method and converts them to CardKit cards when:

    1. The message is NOT from the AI agent pipeline (which is already
       handled by callback interception + consumed mechanism).
    2. The controller is enabled and the FeishuClient is initialized.

    When the card delivery fails, it falls back to the original plain
    text ``send()``.

    **Agent path detection**: When a message is being handled by the
    AI agent pipeline, ``_msg_ctx`` has ``card_sent=True`` after the
    streaming card is delivered.  In that case, the gateway's own text
    reply should be suppressed (returned as success with no message
    sent).  When ``card_sent=False`` and there IS an event_message_id,
    the agent is still running — we also skip to avoid interfering
    with the streaming card.

    **Gateway-internal path**: When there is NO ``event_message_id``
    in the context (or the context is None), the message originates
    from the gateway itself (slash commands, auth, errors, etc.)
    and should be converted to a card.
    """
    async def _intercepted_send(self_feishu, chat_id, content, reply_to=None, metadata=None, **kwargs):
        # ── Agent path: handle non-string sends ──
        # Non-string content (e.g. dicts with image_key) is passed through
        # to the original adapter — we only intercept string text messages.
        if not isinstance(content, str):
            return await orig_send(self_feishu, chat_id, content, reply_to=reply_to, metadata=metadata, **kwargs)

        # ── Guard: skip empty content ──
        if not content.strip():
            return await orig_send(self_feishu, chat_id, content, reply_to=reply_to, metadata=metadata, **kwargs)

        _text_content = content

        # ── Guard: check if this is a cron/background fallback send ──
        # When cron's _card_sending_send or background task's
        # _intercepting_send falls back to original_send, it calls
        # the (now-wrapped) send method. In that case we should
        # NOT try to make another card — just send plain text.
        # Detection: cron/bg sets a flag on the adapter instance.
        if getattr(self_feishu, "_hls_cron_sending", 0) or getattr(self_feishu, "_hls_bg_sending", 0):
            return await orig_send(self_feishu, chat_id, content, reply_to=reply_to, metadata=metadata, **kwargs)

        # ── Agent path: suppress duplicate text reply ──
        ctx = _msg_ctx.get(None)
        if ctx is not None:
            eid = ctx.get("event_message_id", "")
            if eid:
                # We're inside an agent message pipeline.
                # If card was already sent, suppress the gateway's text reply.
                if ctx.get("card_sent"):
                    _logger.debug(
                        "feishu_adapter_send: suppressing gateway text reply "
                        "(card already sent), chat=%s content_len=%d",
                        chat_id[:12] if chat_id else "?",
                        len(content),
                    )
                    try:
                        from gateway.platforms.base import SendResult
                        return SendResult(success=True)
                    except (ImportError, AttributeError):
                        return None
                else:
                    # card_sent is False, but check if a card session exists
                    # (even in terminal state like ABORTED). If a card was
                    # created and is visible, suppress the plain text to
                    # avoid duplicates.
                    try:
                        from ..controller import get_controller
                        _ctrl = get_controller()
                        if _ctrl and _ctrl.enabled:
                            _sess = _ctrl._sess_get(eid)
                            if _sess and _sess.card_msg_id:
                                _logger.info(
                                    "feishu_adapter_send: suppressing text reply "
                                    "(card exists for msg=%s, state=%s, card_sent=%s)",
                                    eid[:12], _sess.state, ctx.get("card_sent"),
                                )
                                ctx["card_sent"] = True
                                try:
                                    from gateway.platforms.base import SendResult
                                    return SendResult(success=True)
                                except (ImportError, AttributeError):
                                    return None
                    except Exception:
                        _logger.debug("HLS: suppressed exception", exc_info=True)
                    # Agent still running, card not yet sent — don't interfere
                    return await orig_send(self_feishu, chat_id, content, reply_to=reply_to, metadata=metadata, **kwargs)

        # ── Gateway-internal path: convert to card ──
        # ── /stop response: abort active streaming card ──
        # When the gateway sends a "⚡ 已停止" response from /stop command,
        # check if there's an active streaming card in this chat that needs
        # to be sealed as "stopped". This prevents the card from being stuck
        # in loading/marquee state and also prevents the duplicate "⚡ 已停止"
        # gateway card from appearing alongside the streaming card.
        _is_stop_response = any(kw in content for kw in ("已停止", "stopped", "Stopped"))
        if _is_stop_response:
            try:
                from ..controller import get_controller
                _ctrl = get_controller()
                if _ctrl and _ctrl.enabled:
                    # Find an active streaming session in this chat
                    for _sess in _ctrl._sess_values_snapshot():
                        if (
                            _sess.chat_id == chat_id
                            and _sess.state in ("streaming", "creating", "idle")
                            and _sess.card_msg_id
                        ):
                            _logger.info(
                                "gateway_send: /stop response detected, aborting "
                                "streaming card for msg=%s (state=%s)",
                                (_sess.message_id or "?")[:12],
                                _sess.state,
                            )
                            try:
                                from .hooks import on_message_aborted
                                on_message_aborted(message_id=_sess.message_id)
                            except Exception:
                                _logger.debug("HLS: suppressed exception", exc_info=True)
                            # Suppress the "⚡ 已停止" gateway card —
                            # the streaming card will show the stopped state.
                            try:
                                from gateway.platforms.base import SendResult
                                return SendResult(success=True)
                            except (ImportError, AttributeError):
                                return None
            except Exception:
                _logger.debug("HLS: suppressed exception", exc_info=True)
        _logger.info(
            "gateway_send: entering gateway-internal path, chat=%s content_len=%d",
            chat_id[:12] if chat_id else "?",
            len(content),
        )
        try:
            from ..controller import get_controller
            ctrl = get_controller()
            if ctrl and ctrl.enabled:
                # Check if gateway_cards feature is enabled
                cfg = _get_config()
                if not cfg.gateway_cards:
                    _logger.info("gateway_send: gateway_cards disabled, falling back to plain text")
                    return await orig_send(self_feishu, chat_id, content, reply_to=reply_to, metadata=metadata, **kwargs)

                cleaned = _text_content
                if not cleaned.strip():
                    cleaned = content
                if not cleaned.strip():
                    return await orig_send(self_feishu, chat_id, content, reply_to=reply_to, metadata=metadata, **kwargs)

                category = _classify_gateway_message(cleaned or content)
                card_msg_id, card_id = await ctrl._do_gateway_deliver(
                    chat_id, cleaned.strip() if cleaned.strip() else content,
                    category=category,
                )
                if card_msg_id:
                    # Register the card so edit_message can update it later
                    _register_gateway_card(
                        card_msg_id,
                        chat_id=chat_id,
                        card_id=card_id,
                        category=category,
                    )
                    _logger.info(
                        "hermes-lark-streaming v%s: gateway message card sent: "
                        "chat=%s category=%s content_len=%d card_id=%s",
                        __version__,
                        chat_id[:12] if chat_id else "?",
                        category,
                        len(content),
                        (card_id or "?")[:12],
                    )
                    try:
                        from gateway.platforms.base import SendResult
                        return SendResult(success=True, message_id=card_msg_id)
                    except (ImportError, AttributeError):
                        return None
            else:
                _logger.info(
                    "gateway_send: controller not enabled (ctrl=%s), falling back to plain text",
                    bool(ctrl),
                )
        except Exception:
            _logger.info(
                "hermes-lark-streaming v%s: gateway card delivery failed, "
                "falling back to plain text",
                __version__,
                exc_info=True,
            )

        # ── Fallback: original plain text send ──
        _logger.info(
            "gateway_send: plain text fallback, chat=%s content_len=%d",
            chat_id[:12] if chat_id else "?",
            len(content),
        )
        return await orig_send(self_feishu, chat_id, content, reply_to=reply_to, metadata=metadata, **kwargs)

    return _intercepted_send


def _register_gateway_card(card_msg_id: str, *, chat_id: str, card_id: str | None, category: str) -> None:
    """Register a gateway card so edit_message can update it later."""
    if not card_msg_id:
        return
    with _gateway_cards_lock:
        _gateway_cards[card_msg_id] = {
            "chat_id": chat_id,
            "card_id": card_id,
            "category": category,
        }
    _logger.debug(
        "registered gateway card: msg_id=%s card_id=%s category=%s",
        card_msg_id[:12], (card_id or "?")[:12], category,
    )


def _unregister_gateway_card(card_msg_id: str) -> None:
    """Remove a gateway card from the registry."""
    with _gateway_cards_lock:
        _gateway_cards.pop(card_msg_id, None)


def _wrap_feishu_adapter_edit(orig_edit: Callable) -> Callable:
    """Intercept ``FeishuAdapter.edit_message()`` — update gateway card content.

    When Hermes calls ``edit_message()`` on a message_id that was created
    by our gateway card system (Phase 1), we update the card content instead
    of trying to edit the plain text message (which no longer exists).

    This handles long-running notifications where Hermes initially sends
    a status message and later updates it (e.g. "Thinking..." → "Processing...").

    When the message_id is NOT a gateway card (i.e. it's an original Feishu
    message that was never converted to a card), we pass through to the
    original ``edit_message()``.
    """
    async def _intercepted_edit(self_feishu, chat_id, message_id, content, metadata=None, **kwargs):
        # ── Check if this message_id is a gateway card ──
        with _gateway_cards_lock:
            card_info = _gateway_cards.get(message_id)

        if card_info is not None and isinstance(content, str) and content.strip():
            _logger.info(
                "feishu_adapter_edit: updating gateway card msg_id=%s content_len=%d",
                message_id[:12] if message_id else "?",
                len(content),
            )
            try:
                from ..controller import get_controller
                ctrl = get_controller()
                if ctrl and ctrl.enabled:
                    # Check if gateway_cards feature is enabled
                    cfg = _get_config()
                    if cfg.gateway_cards:
                        cleaned = content
                        if not cleaned.strip():
                            cleaned = content

                        category = _classify_gateway_message(cleaned)
                        updated = await ctrl._do_gateway_card_update(
                            chat_id=card_info.get("chat_id", chat_id),
                            card_msg_id=message_id,
                            card_id=card_info.get("card_id"),
                            content=cleaned.strip(),
                            category=category,
                        )
                        if updated:
                            # Update category in registry
                            with _gateway_cards_lock:
                                if message_id in _gateway_cards:
                                    _gateway_cards[message_id]["category"] = category
                            try:
                                from gateway.platforms.base import SendResult
                                return SendResult(success=True)
                            except (ImportError, AttributeError):
                                return None
            except Exception:
                _logger.debug(
                    "feishu_adapter_edit: card update failed, falling back to original",
                    exc_info=True,
                )

        # ── Fallback: original edit_message ──
        _logger.debug(
            "feishu_adapter_edit: pass-through msg_id=%s content_len=%d",
            message_id[:12] if message_id else "?",
            len(content) if isinstance(content, str) else 0,
        )
        # Strip 'metadata' kwarg — Hermes's StreamConsumer passes it but
        # the original FeishuAdapter.edit_message() may not accept it.
        # This prevents: "edit_message() got an unexpected keyword argument 'metadata'"
        _fallback_kwargs = {k: v for k, v in kwargs.items() if k != "metadata"}
        try:
            return await orig_edit(self_feishu, chat_id, message_id, content, **_fallback_kwargs)
        except TypeError:
            # If the original still rejects kwargs, try with no extra kwargs
            return await orig_edit(self_feishu, chat_id, message_id, content)

    return _intercepted_edit


# ── Reaction → card status indicator (Phase 3) ─────────────────────


# Map Feishu reaction emojis to human-readable status labels
_REACTION_STATUS_MAP: dict[str, str] = {
    "👀": "Reading",
    "👍": "Done",
    "🤔": "Thinking",
    "⏳": "Processing",
    "✅": "Completed",
    "🔄": "Refreshing",
    "📝": "Composing",
}


def _wrap_feishu_adapter_add_reaction(orig_add_reaction: Callable) -> Callable:
    """Intercept ``FeishuAdapter.add_reaction()`` — card status indicator.

    When Hermes adds a reaction to a message that has a gateway card,
    we suppress the reaction emoji and instead show the status as a
    text indicator in the card's header/footer.

    This replaces the "emoji reaction on the user's message" pattern
    (which is invisible in card-only mode) with an in-card status
    indicator.
    """
    async def _intercepted_add_reaction(self_feishu, message_id, emoji, **kwargs):
        # ── Check if this message_id is a gateway card ──
        with _gateway_cards_lock:
            card_info = _gateway_cards.get(message_id)

        if card_info is not None:
            status_label = _REACTION_STATUS_MAP.get(emoji)
            if status_label:
                _logger.info(
                    "feishu_adapter_add_reaction: gateway card status msg_id=%s emoji=%s → %s",
                    message_id[:12] if message_id else "?",
                    emoji,
                    status_label,
                )
                try:
                    from ..controller import get_controller
                    ctrl = get_controller()
                    if ctrl and ctrl.enabled:
                        cfg = _get_config()
                        if cfg.gateway_cards:
                            # Update the card with a status indicator
                            updated = await ctrl._do_gateway_card_status(
                                card_msg_id=message_id,
                                card_id=card_info.get("card_id"),
                                status_label=status_label,
                                emoji=emoji,
                                category=card_info.get("category", "system"),
                            )
                            if updated:
                                # Suppress the actual reaction — card shows status instead
                                try:
                                    from gateway.platforms.base import SendResult
                                    return SendResult(success=True)
                                except (ImportError, AttributeError):
                                    return None
                except Exception:
                    _logger.debug(
                        "feishu_adapter_add_reaction: card status update failed",
                        exc_info=True,
                    )

        # ── Fallback: original add_reaction ──
        return await orig_add_reaction(self_feishu, message_id, emoji, **kwargs)

    return _intercepted_add_reaction


def _wrap_feishu_adapter_delete_reaction(orig_delete_reaction: Callable) -> Callable:
    """Intercept ``FeishuAdapter.delete_reaction()`` — clear card status.

    When Hermes removes a reaction from a gateway card message,
    we clear the status indicator from the card.
    """
    async def _intercepted_delete_reaction(self_feishu, message_id, emoji, **kwargs):
        # ── Check if this message_id is a gateway card ──
        with _gateway_cards_lock:
            card_info = _gateway_cards.get(message_id)

        if card_info is not None:
            status_label = _REACTION_STATUS_MAP.get(emoji)
            if status_label:
                _logger.info(
                    "feishu_adapter_delete_reaction: gateway card clear status msg_id=%s emoji=%s",
                    message_id[:12] if message_id else "?",
                    emoji,
                )
                try:
                    from ..controller import get_controller
                    ctrl = get_controller()
                    if ctrl and ctrl.enabled:
                        cfg = _get_config()
                        if cfg.gateway_cards:
                            # Clear the status indicator from the card
                            updated = await ctrl._do_gateway_card_status(
                                card_msg_id=message_id,
                                card_id=card_info.get("card_id"),
                                status_label="",
                                emoji="",
                                category=card_info.get("category", "system"),
                            )
                            if updated:
                                try:
                                    from gateway.platforms.base import SendResult
                                    return SendResult(success=True)
                                except (ImportError, AttributeError):
                                    return None
                except Exception:
                    _logger.debug(
                        "feishu_adapter_delete_reaction: card status clear failed",
                        exc_info=True,
                    )

        # ── Fallback: original delete_reaction ──
        return await orig_delete_reaction(self_feishu, message_id, emoji, **kwargs)

    return _intercepted_delete_reaction


# ── Clarify interactive card registry ──────────────────────────────────
# Stores the choices list for each clarify_id so the card action callback
# handler can look up the choice text from the option index.
#
# v1.3.0 P1: these 5 dicts are accessed from multiple threads:
#   (a) event-loop thread (send_clarify intercept) — writes
#   (b) Feishu webhook callback thread (_handle_clarify_card_action) — reads/writes
#   (c) scheduled coroutine (_schedule_confirm_card) — reads + pops
#   (d) _prune_expired_clarify — iterates + pops
# A single coarse-grained lock protects all 5 (clarify is rare, contention negligible).
_clarify_lock = threading.Lock()
_clarify_choices: dict[str, list[str]] = {}  # clarify_id → choices list (normalized)
_clarify_questions: dict[str, str] = {}  # clarify_id → question text
_clarify_card_msg_ids: dict[str, str] = {}  # clarify_id → card_msg_id (for server-side confirm update)
_clarify_selections: dict[str, str] = {}  # clarify_id → user's selected/input text (for retry)
_clarify_timestamps: dict[str, float] = {}  # clarify_id → creation time (for TTL cleanup)
_CLARIFY_TTL_SEC = 30 * 60  # 30 分钟后未确认的追问自动清除

# Backward-compatible aliases (old names used in tests)
_clarify_answers = _clarify_selections  # noqa: F841
_clarify_card_info = _clarify_card_msg_ids  # noqa: F841


def _prune_expired_clarify() -> None:
    """清理过期的追问数据（超过 _CLARIFY_TTL_SEC 未确认的条目）."""
    with _clarify_lock:
        if not _clarify_timestamps:
            return
        now = time.time()
        expired = [cid for cid, ts in _clarify_timestamps.items() if now - ts > _CLARIFY_TTL_SEC]
        for cid in expired:
            _clarify_choices.pop(cid, None)
            _clarify_questions.pop(cid, None)
            _clarify_card_msg_ids.pop(cid, None)
            _clarify_selections.pop(cid, None)
            _clarify_timestamps.pop(cid, None)
        if expired:
            _logger.debug("HLS: pruned %d expired clarify entries", len(expired))


def _wrap_feishu_adapter_send_clarify(orig_send_clarify: Callable) -> Callable:
    """Intercept ``FeishuAdapter.send_clarify()`` — render interactive card.

    Instead of the default text-based numbered list, we build a CardKit 2.0
    interactive card with:
      - A ``select_static`` dropdown for choices + "✏️ 自定义输入" option
      - An ``input`` field for open-ended questions (no choices)
      - Callback behaviors that route to our card action handler

    When the card can't be sent (controller disabled, API error), falls
    back to the original text-based send_clarify.
    """

    async def _intercepted_send_clarify(
        self_feishu, chat_id, question, choices, clarify_id, session_key, metadata=None, **kwargs
    ):
        _logger.info(
            "clarify card: send_clarify intercepted chat=%s question=%r choices=%s clarify_id=%s",
            (chat_id or "?")[:12],
            question[:50] if question else "",
            choices,
            (clarify_id or "?")[:12],
        )

        # Prune expired clarify data before creating new entries
        _prune_expired_clarify()

        try:
            from ..controller import get_controller
            ctrl = get_controller()
            if not ctrl or not ctrl.enabled or not ctrl._client_ok():
                _logger.debug("clarify card: controller not available, falling back to text")
                return await orig_send_clarify(
                    self_feishu, chat_id, question, choices, clarify_id, session_key,
                    metadata=metadata, **kwargs
                )

            from ..cardkit import build_clarify_card, normalize_clarify_choices

            # v1.3.0 P0-01: normalize choices BEFORE building the card and
            # storing in the registry.  This ensures:
            #   (a) the card displays readable text (dict-repr → extracted field)
            #   (b) the user's selection (sent to the AI) is the readable text,
            #       not the raw dict-repr garbage.
            normalized = normalize_clarify_choices(choices) if choices else None

            card = build_clarify_card(
                question=question,
                choices=normalized,
                clarify_id=clarify_id,
            )

            # Store normalized choices and question for callback lookup
            with _clarify_lock:
                if normalized:
                    _clarify_choices[clarify_id] = list(normalized)
                _clarify_questions[clarify_id] = question
                _clarify_timestamps[clarify_id] = time.time()

            # Send the card via FeishuClient
            reply_to = None
            if metadata and isinstance(metadata, dict):
                reply_to = metadata.get("reply_to") or metadata.get("message_id")

            if reply_to:
                card_msg_id = await ctrl._client.reply_card(reply_to, card)
            else:
                card_msg_id = await ctrl._client.send_card_to_chat(chat_id, card)

            _logger.info(
                "clarify card: card sent successfully, clarify_id=%s card_msg_id=%s",
                (clarify_id or "?")[:12],
                (card_msg_id or "?")[:12],
            )

            # Store card_msg_id for server-side confirm update
            with _clarify_lock:
                if card_msg_id:
                    _clarify_card_msg_ids[clarify_id] = card_msg_id

            # Register the card in gateway card registry (for edit tracking)
            _register_gateway_card(card_msg_id, chat_id=chat_id, card_id=None, category="clarify")

            # For choices mode: we handle resolution in the card action callback.
            # For open-ended mode (no choices): the input field triggers input_submit.
            # In both cases, we call mark_awaiting_text as a fallback so the
            # gateway text-intercept can also resolve if the user types instead.
            try:
                from tools.clarify_gateway import mark_awaiting_text
                mark_awaiting_text(clarify_id)
                _logger.debug("clarify card: mark_awaiting_text called for clarify_id=%s", (clarify_id or "?")[:12])
            except (ImportError, Exception) as e:
                _logger.debug("clarify card: mark_awaiting_text failed (%s), card callback will handle resolution", e)

            # Return success to suppress the original text-based send_clarify
            try:
                from gateway.platforms.base import SendResult
                return SendResult(success=True, message_id=card_msg_id)
            except (ImportError, AttributeError):
                return None

        except Exception as e:
            _logger.warning(
                "clarify card: failed to send card, falling back to text: %s",
                e,
                exc_info=True,
            )
            return await orig_send_clarify(
                self_feishu, chat_id, question, choices, clarify_id, session_key,
                metadata=metadata, **kwargs
            )

    return _intercepted_send_clarify


def _wrap_feishu_card_action_trigger(original_method: Callable) -> Callable:
    """Wrap ``FeishuAdapter._on_card_action_trigger`` to handle clarify card callbacks.

    When a user interacts with a clarify card (selects a dropdown option,
    submits text input, or clicks a button), this wrapper intercepts the
    callback and:

      - For ``select``: resolves with the selected choice text
      - For ``input_submit``: resolves with the typed text (Enter key)
      - For ``button_submit``: resolves with the typed text (click submit button)
      - For ``retry_submit``: re-sends the previously submitted text

    All actions return a CallBackCard showing the soft-lock "submitted" state
    (with retry button). After hermes successfully processes the resolve,
    the card is updated server-side to the hard-lock "confirmed" state.
    """

    def _wrapped(self, data):
        # ── Check if this is a clarify card action ──
        event = getattr(data, "event", None)
        action = getattr(event, "action", None)
        action_value = getattr(action, "value", {}) or {}

        clarify_action = action_value.get("hermes_clarify_action") if isinstance(action_value, dict) else None

        if clarify_action:
            return _handle_clarify_card_action(self, data, clarify_action, action_value)

        # Not a clarify action — pass through to original
        return original_method(self, data)

    return _wrapped


async def _schedule_confirm_card(*, cid: str) -> None:
    """Server-side card update: soft-lock → hard-lock (confirmed state).

    After hermes successfully receives the user's clarify answer, this
    function updates the card via the IM PATCH API to the confirmed state,
    removing the "重试提交" button and showing "已确认".

    Also cleans up stored clarify data (choices, questions, etc.).

    Args:
        cid: clarify_id to confirm
    """
    import asyncio

    # Small delay to ensure the CallBackCard (submitted state) is processed first
    await asyncio.sleep(1.0)

    # v1.3.0: snapshot all needed data under the lock, then release it
    # before the (potentially slow) network call.  Holding the lock across
    # an await would serialize all clarify confirmations unnecessarily.
    with _clarify_lock:
        card_msg_id = _clarify_card_msg_ids.get(cid, "")
        question = _clarify_questions.get(cid, "")
        selected = _clarify_selections.get(cid, "")

    def _cleanup():
        """Pop all stored entries for this clarify_id (idempotent)."""
        with _clarify_lock:
            _clarify_choices.pop(cid, None)
            _clarify_questions.pop(cid, None)
            _clarify_card_msg_ids.pop(cid, None)
            _clarify_selections.pop(cid, None)
            _clarify_timestamps.pop(cid, None)

    if not card_msg_id:
        _logger.warning(
            "clarify card: cannot confirm, no card_msg_id for clarify_id=%s",
            (cid or "?")[:12],
        )
        _cleanup()
        return

    if not selected:
        _logger.warning(
            "clarify card: cannot confirm, no stored selection for clarify_id=%s",
            (cid or "?")[:12],
        )
        _cleanup()
        return

    try:
        from ..cardkit import build_clarify_confirmed_card
        from ..controller import get_controller

        ctrl = get_controller()
        if not ctrl or not ctrl._client_ok():
            _logger.warning(
                "clarify card: cannot confirm, controller not available for clarify_id=%s",
                (cid or "?")[:12],
            )
            return

        card_data = build_clarify_confirmed_card(
            question=question, selected=selected,
        )
        await ctrl._client.update_card(card_msg_id, card_data)

        _logger.info(
            "clarify card: confirmed (hard lock) for clarify_id=%s card_msg_id=%s",
            (cid or "?")[:12],
            (card_msg_id or "?")[:12],
        )
    except Exception:
        _logger.warning(
            "clarify card: server-side confirm update failed for clarify_id=%s",
            (cid or "?")[:12],
            exc_info=True,
        )
    finally:
        # Always cleanup stored data after confirm attempt
        _cleanup()


def _handle_clarify_card_action(
    adapter_instance,
    data: Any,
    clarify_action: str,
    action_value: dict,
) -> Any:
    """Handle a clarify card action callback — three-state flow.

    This function is called synchronously from the card action trigger.

    Three-state flow:
      1. 待选择态 (build_clarify_card) → card initially sent
      2. 已提交态 (build_clarify_submitted_card) → CallBackCard returned on user action
         - Shows "已提交，等待确认..." + "重试提交" button (soft lock)
      3. 已确认态 (build_clarify_resolved_card) → server-side API update after hermes confirms
         - Shows "已确认", no buttons (hard lock)

    If the user clicks "重试提交", the same selection is re-sent to hermes
    and the card stays in the submitted state.
    """
    # Import P2CardActionTriggerResponse and CallBackCard (may be None if SDK version doesn't support)
    try:
        from lark_oapi.api.cardkit.v1 import P2CardActionTriggerResponse, CallBackCard
    except ImportError:
        P2CardActionTriggerResponse = None
        CallBackCard = None

    def _empty_response():
        if P2CardActionTriggerResponse is None:
            return None
        return P2CardActionTriggerResponse()

    def _submitted_card_response(selected_text: str, choices_list: list[str] | None, q: str, cid: str):
        """Build a CallBackCard showing the soft-lock submitted state."""
        if P2CardActionTriggerResponse is None or CallBackCard is None:
            return _empty_response()
        from ..cardkit import build_clarify_submitted_card
        card_data = build_clarify_submitted_card(
            question=q, selected=selected_text,
            choices=choices_list, clarify_id=cid,
        )
        response = P2CardActionTriggerResponse()
        card = CallBackCard()
        card.type = "raw"
        card.data = card_data
        response.card = card
        return response

    clarify_id = action_value.get("clarify_id", "")
    if not clarify_id:
        _logger.debug("clarify card: callback missing clarify_id, ignoring")
        return _empty_response()

    _logger.info(
        "clarify card: callback received action=%s clarify_id=%s",
        clarify_action,
        (clarify_id or "?")[:12],
    )

    # ── Authorization check ──
    event = getattr(data, "event", None)
    operator = getattr(event, "operator", None)
    open_id = str(getattr(operator, "open_id", "") or "")
    if hasattr(adapter_instance, "_is_interactive_operator_authorized"):
        if not adapter_instance._is_interactive_operator_authorized(open_id):
            _logger.warning(
                "clarify card: unauthorized click by %s for clarify_id=%s",
                open_id or "<unknown>",
                (clarify_id or "?")[:12],
            )
            return _empty_response()

    # v1.3.0: snapshot question + choices atomically (used by all action branches)
    with _clarify_lock:
        question = _clarify_questions.get(clarify_id, "")
        choices = _clarify_choices.get(clarify_id) or None

    # ── Handle retry_submit action (re-send previous selection) ──
    if clarify_action == "retry_submit":
        with _clarify_lock:
            stored_selection = _clarify_selections.get(clarify_id, "")
        if not stored_selection:
            _logger.debug("clarify card: retry but no stored selection for clarify_id=%s", (clarify_id or "?")[:12])
            return _empty_response()

        _logger.info(
            "clarify card: retrying with selection '%s' for clarify_id=%s",
            stored_selection[:50],
            (clarify_id or "?")[:12],
        )

        # Re-resolve the clarify
        loop = getattr(adapter_instance, "_loop", None)
        if loop is not None:
            try:
                from tools.clarify_gateway import resolve_gateway_clarify
                from agent.async_utils import safe_schedule_threadsafe

                async def _do_retry_resolve():
                    resolve_gateway_clarify(clarify_id, stored_selection)
                    # Schedule server-side confirm update after retry
                    await _schedule_confirm_card(cid=clarify_id)

                safe_schedule_threadsafe(
                    _do_retry_resolve(), loop,
                    logger=_logger,
                    log_message="clarify card: failed to schedule retry resolve",
                    log_level=logging.WARNING,
                )
            except (ImportError, Exception) as e:
                _logger.warning("clarify card: retry resolve scheduling failed: %s", e)
                try:
                    from tools.clarify_gateway import resolve_gateway_clarify
                    resolve_gateway_clarify(clarify_id, stored_selection)
                except (ImportError, Exception) as e2:
                    _logger.warning("clarify card: synchronous retry resolve also failed: %s", e2)
        else:
            # No event loop — synchronous fallback
            try:
                from tools.clarify_gateway import resolve_gateway_clarify
                resolve_gateway_clarify(clarify_id, stored_selection)
            except (ImportError, Exception) as e:
                _logger.warning("clarify card: synchronous retry resolve failed: %s", e)

        # Return the same submitted card (soft lock with retry button)
        return _submitted_card_response(stored_selection, choices, question, clarify_id)

    # ── Handle select action (dropdown choice) ──
    if clarify_action == "select":
        selected_option = str(getattr(getattr(event, "action", None), "option", "") or "")

        # Predefined choice selected → resolve
        with _clarify_lock:
            choices_list = list(_clarify_choices.get(clarify_id, []))
        try:
            idx = int(selected_option)
            choice_text = choices_list[idx]
        except (ValueError, IndexError):
            _logger.warning(
                "clarify card: invalid option index '%s' for clarify_id=%s (choices=%s)",
                selected_option,
                (clarify_id or "?")[:12],
                choices_list,
            )
            return _empty_response()

        _logger.info(
            "clarify card: resolving with choice '%s' for clarify_id=%s",
            choice_text,
            (clarify_id or "?")[:12],
        )

        # Store selection for retry
        with _clarify_lock:
            _clarify_selections[clarify_id] = choice_text

        # Resolve the clarify (schedule on event loop since we're in a sync callback)
        loop = getattr(adapter_instance, "_loop", None)
        if loop is not None:
            try:
                from tools.clarify_gateway import resolve_gateway_clarify
                from agent.async_utils import safe_schedule_threadsafe

                async def _do_resolve():
                    resolve_gateway_clarify(clarify_id, choice_text)
                    # Schedule server-side confirm update after resolve
                    await _schedule_confirm_card(cid=clarify_id)

                safe_schedule_threadsafe(
                    _do_resolve(), loop,
                    logger=_logger,
                    log_message="clarify card: failed to schedule resolve_gateway_clarify",
                    log_level=logging.WARNING,
                )
            except (ImportError, Exception) as e:
                _logger.warning("clarify card: resolve_gateway_clarify scheduling failed: %s", e)
                # Try synchronous fallback
                try:
                    from tools.clarify_gateway import resolve_gateway_clarify
                    resolve_gateway_clarify(clarify_id, choice_text)
                except (ImportError, Exception) as e2:
                    _logger.warning("clarify card: synchronous resolve also failed: %s", e2)
        else:
            # No event loop — synchronous fallback
            try:
                from tools.clarify_gateway import resolve_gateway_clarify
                resolve_gateway_clarify(clarify_id, choice_text)
            except (ImportError, Exception) as e:
                _logger.warning("clarify card: synchronous resolve failed: %s", e)

        # Return submitted card (soft lock with retry button) — don't cleanup yet
        return _submitted_card_response(choice_text, choices_list or None, question, clarify_id)

    # ── Handle input_submit action (text input via Enter key) ──
    if clarify_action == "input_submit":
        action_obj = getattr(event, "action", None)
        input_text = str(getattr(action_obj, "input_value", "") or "").strip()

        if not input_text:
            _logger.debug("clarify card: empty input submitted for clarify_id=%s", (clarify_id or "?")[:12])
            return _empty_response()

        _logger.info(
            "clarify card: resolving with input '%s' for clarify_id=%s",
            input_text[:50],
            (clarify_id or "?")[:12],
        )

        # Store selection for retry
        with _clarify_lock:
            _clarify_selections[clarify_id] = input_text

        # Resolve the clarify
        loop = getattr(adapter_instance, "_loop", None)
        if loop is not None:
            try:
                from tools.clarify_gateway import resolve_gateway_clarify
                from agent.async_utils import safe_schedule_threadsafe

                async def _do_resolve_input():
                    resolve_gateway_clarify(clarify_id, input_text)
                    # Schedule server-side confirm update after resolve
                    await _schedule_confirm_card(cid=clarify_id)

                safe_schedule_threadsafe(
                    _do_resolve_input(), loop,
                    logger=_logger,
                    log_message="clarify card: failed to schedule resolve_gateway_clarify",
                    log_level=logging.WARNING,
                )
            except (ImportError, Exception) as e:
                _logger.warning("clarify card: resolve_gateway_clarify scheduling failed: %s", e)
                try:
                    from tools.clarify_gateway import resolve_gateway_clarify
                    resolve_gateway_clarify(clarify_id, input_text)
                except (ImportError, Exception) as e2:
                    _logger.warning("clarify card: synchronous resolve also failed: %s", e2)
        else:
            # No event loop — synchronous fallback
            try:
                from tools.clarify_gateway import resolve_gateway_clarify
                resolve_gateway_clarify(clarify_id, input_text)
            except (ImportError, Exception) as e:
                _logger.warning("clarify card: synchronous resolve failed: %s", e)

        # Return submitted card (soft lock with retry button) — don't cleanup yet
        return _submitted_card_response(input_text, choices, question, clarify_id)

    # ── Handle button_submit action (click submit button) ──
    if clarify_action == "button_submit":
        action_obj = getattr(event, "action", None)
        # Read input from form_value (button callbacks include all form values)
        form_value = getattr(action_obj, "form_value", None) or {}
        input_text = str(form_value.get("clarify_input", "") or "").strip()

        if not input_text:
            _logger.debug("clarify card: empty button submit for clarify_id=%s", (clarify_id or "?")[:12])
            return _empty_response()

        _logger.info(
            "clarify card: resolving with button submit '%s' for clarify_id=%s",
            input_text[:50],
            (clarify_id or "?")[:12],
        )

        # Store selection for retry
        with _clarify_lock:
            _clarify_selections[clarify_id] = input_text

        # Resolve the clarify
        loop = getattr(adapter_instance, "_loop", None)
        if loop is not None:
            try:
                from tools.clarify_gateway import resolve_gateway_clarify
                from agent.async_utils import safe_schedule_threadsafe

                async def _do_resolve_button():
                    resolve_gateway_clarify(clarify_id, input_text)
                    # Schedule server-side confirm update after resolve
                    await _schedule_confirm_card(cid=clarify_id)

                safe_schedule_threadsafe(
                    _do_resolve_button(), loop,
                    logger=_logger,
                    log_message="clarify card: failed to schedule resolve_gateway_clarify",
                    log_level=logging.WARNING,
                )
            except (ImportError, Exception) as e:
                _logger.warning("clarify card: resolve_gateway_clarify scheduling failed: %s", e)
                try:
                    from tools.clarify_gateway import resolve_gateway_clarify
                    resolve_gateway_clarify(clarify_id, input_text)
                except (ImportError, Exception) as e2:
                    _logger.warning("clarify card: synchronous resolve also failed: %s", e2)
        else:
            # No event loop — synchronous fallback
            try:
                from tools.clarify_gateway import resolve_gateway_clarify
                resolve_gateway_clarify(clarify_id, input_text)
            except (ImportError, Exception) as e:
                _logger.warning("clarify card: synchronous resolve failed: %s", e)

        # Return submitted card (soft lock with retry button) — don't cleanup yet
        return _submitted_card_response(input_text, choices, question, clarify_id)

    _logger.debug("clarify card: unknown action '%s', ignoring", clarify_action)
    return _empty_response()
