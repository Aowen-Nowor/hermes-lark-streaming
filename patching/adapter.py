"""FeishuAdapter interception layer — send, edit, reactions, and clarify cards."""

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
    _patched_feishu_classes,
    _send_result_ok,
)

# ── FeishuAdapter interception layer (Phase 1: gateway message cards) ─

def _classify_gateway_message(content: str) -> str:
    """Classify a gateway-internal message by its content for card category."""
    if not isinstance(content, str):
        return "system"
    # Auth / pairing messages
    if any(kw in content for kw in ("pairing code", "pairing requests", "配对码", "I don't recognize you")):
        return "auth"
    # v1.8.0 (P3-4): Gateway lifecycle notices — informational, NOT errors.
    # hermes _notify_active_sessions_of_shutdown (gateway/run.py) hardcodes
    # "⚠️ Gateway restarting — …" / "⚠️ Gateway shutting down — …"; the
    # drain path sends "⏳ Gateway restarting — queued for the next turn…"
    # and zh locales use "♻ 正在重启网关…" / "⏳ 正在等待 N 个活跃代理结束
    # 后重启…". The ⚠️ prefix made every one of them land in the "error"
    # bucket below — production 2026-08 audit: 13/13 planned restarts were
    # misclassified as errors in the card registry / logs.
    if any(kw in content for kw in (
        "Gateway restarting", "Gateway shutting down", "Gateway stopping",
        "queued for the next turn", "not accepting another turn",
        "Draining", "正在重启网关", "活跃代理结束后重启",
    )):
        return "lifecycle"
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

def _is_cron_delivery_metadata(metadata: Any) -> bool:
    """Detect hermes cron delivery — cron/scheduler.py stamps ``job_id`` into
    the delivery metadata (route_metadata = {"job_id": ...}), which survives
    through DeliveryRouter._deliver_to_platform → transport.send → adapter."""
    return isinstance(metadata, dict) and bool(metadata.get("job_id"))


async def _dispatch_feishu_outbound(
    adapter: Any,
    chat_id: str,
    content,
    passthrough: Callable[[], Any],
    *,
    metadata: Any = None,
):
    """Shared interception for ALL feishu-bound plain-text sends (v1.7.0).

    Used by the native FeishuAdapter.send wrapper AND the RELAY wrappers
    (RelayAdapter.send / send_for_platform) so relay-fronted deployments get
    identical behavior: turn-final suppression, /stop detection, cron cards,
    gateway message cards. ``passthrough`` invokes the original send with the
    original arguments (built by the caller as a closure).
    """
    # ── EphemeralReply passthrough (v1.3.1 fix) ──
    # NOT duplicate agent replies. They must NEVER be suppressed by the
    try:
        from gateway.platforms.base import EphemeralReply
        if isinstance(content, EphemeralReply):
            return await passthrough()
    except (ImportError, AttributeError):
        pass  # EphemeralReply not available in this Hermes version

    if not isinstance(content, str):
        return await passthrough()

    # ── Guard: skip empty content ──
    if not content.strip():
        return await passthrough()

    _text_content = content

    if getattr(adapter, "_hls_cron_sending", 0) or getattr(adapter, "_hls_bg_sending", 0):
        return await passthrough()

    # ── Cron delivery lane (v1.7.0 RELAY fix, P1) ──
    # In relay-fronted deployments _wrap_cron_deliver finds no native
    # FeishuAdapter in ``adapters`` (the connector owns the credentials), so
    # cron pushes arrive HERE via RelayAdapter.send_for_platform carrying
    # hermes' delivery metadata (job_id). Route them to the cron card exactly
    # like the native-mode instance replacement does. Must run BEFORE the
    # agent-ctx check: a cron push during an active agent turn must never be
    # mistaken for that turn's text reply.
    if _is_cron_delivery_metadata(metadata):
        try:
            from ..controller import get_controller
            _ctrl = get_controller()
            if _ctrl and _ctrl.enabled and str(content).strip():
                _logger.info(
                    "hermes-lark-streaming v%s: relay cron lane — redirecting "
                    "cron delivery to card, chat=%s job=%s content_len=%d",
                    __version__,
                    (chat_id or "?")[:12],
                    str(metadata.get("job_id"))[:12],
                    len(content),
                )
                await _ctrl._do_cron_deliver(chat_id, str(content).strip())
                return _send_result_ok()
        except Exception:
            _logger.warning(
                "relay cron card delivery failed, falling back to plain text "
                "chat=%s",
                (chat_id or "?")[:12],
                exc_info=True,
            )
        # cron card failed → fall through to plain-text passthrough below

    # ── Agent path: suppress duplicate text reply ──
    ctx = _msg_ctx.get(None)
    if ctx is not None:
        eid = ctx.get("event_message_id", "")
        if eid:
            # We're inside an agent message pipeline.
            # If card was already sent, suppress the gateway's text reply.
            if ctx.get("card_sent"):
                return _send_result_ok()
            else:
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
                            return _send_result_ok()
                except Exception:
                    _logger.debug("HLS: suppressed exception", exc_info=True)
                # Agent still running, card not yet sent — don't interfere
                return await passthrough()

    # v1.3.2 fix (B3-04): the previous detection used a bare substring
    # v1.7.0 (R3-07): keyword set + length aligned with the hermes locale
    # contract (locales/{zh,en}.yaml stop.*: "⚡ 已停止。你可以继续此会话。" /
    # "⚡ Stopped. You can continue this session.") and broadened
    # defensively so a locale tweak degrades to a duplicate gateway card
    # instead of silently breaking /stop card aborts.
    _stripped = content.strip()
    _is_stop_response = (
        len(_stripped) < 80  # /stop responses stay short (headroom for locales)
        and _stripped.startswith("⚡")
        and any(
            kw in _stripped
            for kw in ("已停止", "已中断", "stopped", "Stopped", "Interrupted")
        )
    )
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
                        return _send_result_ok()
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
                return await passthrough()

            cleaned = _text_content
            if not cleaned.strip():
                cleaned = content
            if not cleaned.strip():
                return await passthrough()

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
                return _send_result_ok(card_msg_id)
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
    return await passthrough()


def _wrap_feishu_adapter_send(orig_send: Callable) -> Callable:
    """Intercept ``FeishuAdapter.send()`` — convert text to gateway cards.

    v1.7.0: interception body moved to _dispatch_feishu_outbound (shared
    with the RELAY wrappers). This wrapper binds the original method.
    """
    async def _intercepted_send(self_feishu, chat_id, content, reply_to=None, metadata=None, **kwargs):
        # On-demand repatch: if this adapter instance's class isn't patched yet
        # (deferred loading edge case), patch it now. O(1) lookup.
        # v1.7.0 (R1-06): sentinel attr is the PRIMARY check — id() values can
        # be reused after GC (hot module reload), which previously made the
        # dedupe set skip a fresh class. The id set stays as a mirror for
        # compatibility with existing tests.
        _cls = type(self_feishu)
        if not getattr(_cls, "_hls_patched", False) and id(_cls) not in _patched_feishu_classes:
            from . import _apply_feishu_adapter_patches
            _apply_feishu_adapter_patches(_cls, is_repatch=True)

        return await _dispatch_feishu_outbound(
            self_feishu,
            chat_id,
            content,
            lambda: orig_send(self_feishu, chat_id, content, reply_to=reply_to, metadata=metadata, **kwargs),
            metadata=metadata,
        )
    return _intercepted_send


# ── RELAY mode (v1.7.0): relay-fronted Feishu deployments ──────────
#
# hermes v0.20.5 can front Feishu through a RelayAdapter: the connector
# process owns the Feishu credentials and the gateway holds none, so
# ``adapters`` has NO Feishu entry and every Feishu-bound send flows through
# the single RelayAdapter instance:
#   * interactive / follow-up finals → RelayAdapter.send() (platform learned
#     from inbound traffic via _platform_by_chat, or forced via metadata
#     key "_relay_logical_platform")
#   * cron / scheduled / persisted-home lane → DeliveryTransport.send() →
#     RelayAdapter.send_for_platform(logical_platform, ...) with the platform
#     as an explicit argument (gateway/delivery.py:83)
# The plugin's FeishuAdapter patches never fire in that deployment — cron
# pushes degraded to plain text and gateway-internal messages lost their
# cards. These wrappers route Feishu-bound relay sends through the SAME
# _dispatch_feishu_outbound logic (P1 RELAY fix).

def _resolve_relay_logical_platform(adapter: Any, chat_id: Any, metadata: Any) -> str:
    """Best-effort logical platform for one relay send ("" when unknown)."""
    if isinstance(metadata, dict):
        explicit = metadata.get("_relay_logical_platform")
        if explicit:
            return str(getattr(explicit, "value", explicit)).lower()
    platform_map = getattr(adapter, "_platform_by_chat", None)
    if isinstance(platform_map, dict):
        return str(platform_map.get(str(chat_id), "") or "").lower()
    return ""


def _is_relay_interim_send(metadata: Any) -> bool:
    """Consumer-declared interim send (commentary / tail flush) — NOT the
    turn-final. Same posture as native mode's "agent still running"
    passthrough: never convert interim frames into cards."""
    return isinstance(metadata, dict) and bool(metadata.get("_interim_send"))


def _wrap_relay_adapter_send(orig_send: Callable) -> Callable:
    """Intercept ``RelayAdapter.send()`` for relay-fronted Feishu chats."""
    async def _intercepted_relay_send(self_relay, chat_id, content, reply_to=None, metadata=None, **kwargs):
        try:
            _platform = _resolve_relay_logical_platform(self_relay, chat_id, metadata)
        except Exception:
            _platform = ""
        if _platform not in ("feishu", "lark"):
            # Relay fronts N platforms — anything not Feishu/Lark passes through.
            return await orig_send(self_relay, chat_id, content, reply_to=reply_to, metadata=metadata, **kwargs)
        if _is_relay_interim_send(metadata):
            return await orig_send(self_relay, chat_id, content, reply_to=reply_to, metadata=metadata, **kwargs)
        _logger.debug(
            "relay_send: feishu-bound relay send intercepted chat=%s content_len=%d",
            (chat_id or "?")[:12], len(content) if isinstance(content, str) else -1,
        )
        return await _dispatch_feishu_outbound(
            self_relay,
            chat_id,
            content,
            lambda: orig_send(self_relay, chat_id, content, reply_to=reply_to, metadata=metadata, **kwargs),
            metadata=metadata,
        )
    return _intercepted_relay_send


def _wrap_relay_adapter_send_for_platform(orig_sfp: Callable) -> Callable:
    """Intercept ``RelayAdapter.send_for_platform()`` — cron/scheduled lane."""
    async def _intercepted_relay_sfp(self_relay, logical_platform, chat_id, content, reply_to=None, metadata=None, **kwargs):
        _platform_value = getattr(logical_platform, "value", logical_platform)
        if str(_platform_value or "").lower() not in ("feishu", "lark"):
            return await orig_sfp(self_relay, logical_platform, chat_id, content, reply_to=reply_to, metadata=metadata, **kwargs)
        if _is_relay_interim_send(metadata):
            return await orig_sfp(self_relay, logical_platform, chat_id, content, reply_to=reply_to, metadata=metadata, **kwargs)
        return await _dispatch_feishu_outbound(
            self_relay,
            chat_id,
            content,
            lambda: orig_sfp(self_relay, logical_platform, chat_id, content, reply_to=reply_to, metadata=metadata, **kwargs),
            metadata=metadata,
        )
    return _intercepted_relay_sfp

def _register_gateway_card(card_msg_id: str, *, chat_id: str, card_id: str | None, category: str) -> None:
    """Register a gateway card so edit_message can update it later."""
    if not card_msg_id:
        return
    with _gateway_cards_lock:
        _gateway_cards[card_msg_id] = {
            "chat_id": chat_id,
            "card_id": card_id,
            "category": category,
            "registered_at": time.time(),
        }
        # v1.3.1: prune oldest entries when over capacity
        _GATEWAY_CARDS_MAX = 500
        if len(_gateway_cards) > _GATEWAY_CARDS_MAX:
            # Sort by registered_at, remove oldest 20% to amortize prune cost
            excess = len(_gateway_cards) - _GATEWAY_CARDS_MAX + (_GATEWAY_CARDS_MAX // 5)
            sorted_keys = sorted(_gateway_cards, key=lambda k: _gateway_cards[k].get("registered_at", 0))
            for k in sorted_keys[:excess]:
                _gateway_cards.pop(k, None)
            _logger.debug("HLS: _gateway_cards pruned %d entries (was %d)", excess, len(_gateway_cards) + excess)

def _unregister_gateway_card(card_msg_id: str) -> None:
    """Remove a gateway card from the registry."""
    with _gateway_cards_lock:
        _gateway_cards.pop(card_msg_id, None)

def _wrap_feishu_adapter_edit(orig_edit: Callable) -> Callable:
    """Intercept ``FeishuAdapter.edit_message()`` — update gateway card content."""
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
                pass

        # ── Fallback: original edit_message ──
        _fallback_kwargs = {k: v for k, v in kwargs.items() if k != "metadata"}
        try:
            return await orig_edit(self_feishu, chat_id, message_id, content, **_fallback_kwargs)
        except TypeError:
            # If the original still rejects kwargs, try with no extra kwargs
            return await orig_edit(self_feishu, chat_id, message_id, content)

    return _intercepted_edit

# ── Reaction → card status indicator (Phase 3) ─────────────────────

# Map Feishu reaction emoji codes to human-readable status labels.
# Two spelling families exist across hermes revisions:
#   * unicode emoji (older revisions)
#   * Feishu-internal codes "Typing" / "CrossMark" (hermes v0.20.5,
#     plugins/platforms/feishu/adapter.py _FEISHU_REACTION_IN_PROGRESS /
#     _FEISHU_REACTION_FAILURE) — v1.7.0 added: the unicode keys never
#     matched those, so the card-status feature could not fire at all.
_REACTION_STATUS_MAP: dict[str, str] = {
    "👀": "Reading",
    "👍": "Done",
    "🤔": "Thinking",
    "⏳": "Processing",
    "✅": "Completed",
    "🔄": "Refreshing",
    "📝": "Composing",
    "Typing": "Processing",
    "CrossMark": "Error",
}

def _wrap_feishu_adapter_add_reaction(orig_add_reaction: Callable) -> Callable:
    """Intercept ``FeishuAdapter.add_reaction()`` — card status indicator."""
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
                    pass

        # ── Fallback: original add_reaction ──
        return await orig_add_reaction(self_feishu, message_id, emoji, **kwargs)

    return _intercepted_add_reaction

def _wrap_feishu_adapter_delete_reaction(orig_delete_reaction: Callable) -> Callable:
    """Intercept ``FeishuAdapter.delete_reaction()`` — clear card status."""
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
                    pass

        # ── Fallback: original delete_reaction ──
        return await orig_delete_reaction(self_feishu, message_id, emoji, **kwargs)

    return _intercepted_delete_reaction

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
    """Intercept ``FeishuAdapter.send_clarify()`` — render interactive card."""

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

            # v1.3.0 fix: Flush + cancel pending timers BEFORE sending clarify card.
            # Fix: find the active streaming session for this chat_id, cancel its
            try:
                for _mid, _sess in ctrl._sess_items_snapshot():
                    if _sess.chat_id == chat_id and not _sess.is_terminal_phase:
                        if _sess.unified_state and _sess.unified_state.has_dirty:
                            _logger.info(
                                "clarify card: flushing pending answer before clarify "
                                "msg=%s dirty=%s",
                                (_mid or "?")[:12],
                                bool(_sess.unified_state.answer_dirty),
                            )
                            # Force immediate flush and wait for completion.
                            # This cancels the pending timer and writes dirty data now.
                            await _sess.flush.flush_now(
                                lambda s=_sess: ctrl._do_unified_flush(s)
                            )
                        else:
                            # No dirty data — just cancel the pending timer so the
                            # streaming card stops updating while the clarify is shown.
                            _sess.flush._cancel_timer()
                        break
            except Exception:
                _logger.debug("clarify card: pre-flush failed (non-fatal)", exc_info=True)

            from ..cardkit import build_clarify_card, normalize_clarify_choices

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

def _safe_action_value_repr(action_value: Any) -> str:
    """Safely repr an action_value dict for logging (truncated, no secrets)."""
    try:
        import json
        s = json.dumps(action_value, ensure_ascii=False, default=str)
        return s[:200]
    except Exception:
        return repr(action_value)[:200]

def _wrap_handle_card_action_event(original_method: Callable) -> Callable:
    """Wrap ``FeishuAdapter._handle_card_action_event`` — the REAL interception point."""

    async def _wrapped(self, data):
        event = getattr(data, "event", None)
        action = getattr(event, "action", None)
        action_value = getattr(action, "value", {}) or {}

        clarify_action = (
            action_value.get("hermes_clarify_action")
            if isinstance(action_value, dict) else None
        )

        if clarify_action:
            # v1.8.0 (P3-2): 文案刷新——v1.5.0 已删除 _on_card_action_trigger
            # 包装器，自 v1.4.2 起 _handle_card_action_event 就是 clarify 的
            # 唯一拦截主链路（生产 E2E 6/6 全走此路径）。旧日志里的
            # "SDK stale bound method path — wrapper bypassed" 描述的是
            # v1.5.0 之前的废弃架构，误导读者以为降级路径被触发。
            _cid = action_value.get("clarify_id", "") if isinstance(action_value, dict) else ""
            _logger.info(
                "HLS: clarify card action %r received — handling clarify "
                "resolution (main interception path), clarify_id=%s",
                clarify_action,
                (_cid or "?")[:12],
            )
            try:
                _handle_clarify_card_action(self, data, clarify_action, action_value)
            except Exception:
                _logger.warning(
                    "HLS: clarify card action handling in _handle_card_action_event "
                    "failed — clarify_id=%s",
                    (_cid or "?")[:12],
                    exc_info=True,
                )
            return  # suppress /card synthetic command generation

        # Non-clarify card actions: hermes' default handler (0.21 源码
        # plugins/platforms/feishu/adapter.py:3191 _handle_card_action_event)
        # synthesizes a "/card <tag> <value>" COMMAND message from every card
        # click, which the gateway then rejects ("Unknown command /card")——
        # 垃圾命令会污染会话。抑制并打 WARNING 留痕。
        try:
            action_tag = str(getattr(action, "tag", "") or "button")
        except Exception:
            action_tag = "button"
        _logger.warning(
            "HLS: card action %r reached _handle_card_action_event — suppressing "
            "/card synthetic command (gateway would reject it). action_value=%s",
            action_tag,
            _safe_action_value_repr(action_value),
        )
        return  # suppress

    return _wrapped

async def _schedule_confirm_card(*, cid: str) -> None:
    """Server-side card update: soft-lock → hard-lock (confirmed state)."""
    # v1.3.2 fix (B3-05): removed redundant local `import asyncio` —
    # asyncio is already imported at module level.

    # Small delay to ensure the CallBackCard (submitted state) is processed first
    await asyncio.sleep(1.0)

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
        # v1.7.0 (R3-05): cleanup ONLY after a successful confirm update. The
        # submitted card (with its retry button) is still showing when
        # update_card fails — keeping _clarify_selections alive means the
        # retry button keeps working instead of silently dying; TTL pruning
        # (_CLARIFY_TTL_SEC) reaps anything left over.
        _cleanup()
    except Exception:
        _logger.warning(
            "clarify card: server-side confirm update failed for clarify_id=%s "
            "(stored selection KEPT so the retry button still works)",
            (cid or "?")[:12],
            exc_info=True,
        )

def _handle_clarify_card_action(
    adapter_instance,
    data: Any,
    clarify_action: str,
    action_value: dict,
) -> Any:
    """Handle a clarify card action callback — three-state flow."""
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
        # v1.7.0: build_clarify_submitted_card has NO `choices` parameter —
        # the old call passed one and raised TypeError on EVERY clarify
        # select/input/retry action (caught + logged upstream, but the
        # submitted-state response was never built and the log noise made
        # every clarify click look like a failure).
        card_data = build_clarify_submitted_card(
            question=q, selected=selected_text, clarify_id=cid,
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
    # v1.7.0 (R3-06): fail-closed. Previously an adapter without the
    # _is_interactive_operator_authorized method (old hermes) fell straight
    # through, letting ANY group member click clarify cards as the original
    # author. hermes v0.20.5 ships the method
    # (plugins/platforms/feishu/adapter.py:2771) so the closed path only
    # affects genuinely old installs.
    event = getattr(data, "event", None)
    operator = getattr(event, "operator", None)
    open_id = str(getattr(operator, "open_id", "") or "")
    _authz = getattr(adapter_instance, "_is_interactive_operator_authorized", None)
    if not callable(_authz):
        _logger.warning(
            "clarify card: adapter lacks _is_interactive_operator_authorized "
            "(old hermes?) — failing CLOSED for clarify_id=%s operator=%s",
            (clarify_id or "?")[:12],
            open_id or "<unknown>",
        )
        return _empty_response()
    if not _authz(open_id):
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
        # v1.7.0 (R3-09): the option VALUE contract is owned by
        # cardkit/special.py ("value": str(i) — plain index strings). Parse
        # defensively: accept an index string first, then an exact
        # choice-text match, so a future value-format change degrades to a
        # text lookup instead of silently swallowing the user's click.
        choice_text = ""
        try:
            _idx = int(selected_option)
            if 0 <= _idx < len(choices_list):
                choice_text = choices_list[_idx]
        except (ValueError, TypeError):
            choice_text = ""
        if not choice_text and selected_option and selected_option in choices_list:
            choice_text = selected_option
        if not choice_text:
            _logger.warning(
                "clarify card: invalid option value '%s' for clarify_id=%s (choices=%s)",
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
