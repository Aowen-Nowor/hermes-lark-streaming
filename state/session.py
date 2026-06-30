"""CardSession — 单条消息的卡片会话状态."""

from __future__ import annotations

import asyncio
import logging
import time
from threading import Lock
from typing import TYPE_CHECKING, Any

# Phase constants — single source of truth in state.phase
from .phase import (
    CardPhase,
    TerminalReason,
    TERMINAL_PHASES,
    _TERMINAL,
    is_legal_transition,
)

# Backward-compatible alias — old code imports IDLE from this module
IDLE = CardPhase.IDLE

from ..flush import PATCH_MS, FlushController
from .linear import UnifiedLinearState
from .text import TextState
from .tooluse import ToolUseTracker
from ..feishu import UnavailableGuard

if TYPE_CHECKING:
    pass

_logger = logging.getLogger("hermes_lark_streaming")


class CardSession:
    """单条消息的卡片会话状态."""

    __slots__ = (
        "_card_ready",
        "_continuation_reactivation_count",
        "_create_epoch_snap",
        "_creation_stages",
        "_first_answer_time",
        "_first_flush_done",
        "_is_continuation",
        "_loop",
        "_phase2_failed",
        "_pending_flush",
        "_streaming_closed",
        "_streaming_closed_logged",
        "_was_aborted",
        "anchor_id",
        "card_created_at",
        "card_id",
        "card_msg_id",
        "card_trace_id",
        "chat_id",
        "create_epoch",
        "created_at",
        "deferred_background_review_closed",
        "deferred_background_review_lock",
        "deferred_background_reviews",
        "error_message",
        "existing_elements",
        "flush",
        "footer",
        "guard",
        "linear",
        "message_id",
        "sequence",
        "state",
        "terminal_reason",
        "terminal_source",
        "text",
        "tool_use",
        "unified_state",
        "use_cardkit",
    )

    def __init__(
        self,
        message_id: str,
        chat_id: str,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.message_id = message_id
        self.anchor_id: str | None = None
        self.chat_id = chat_id
        self.state: str = IDLE
        self.card_msg_id: str | None = None
        self.card_id: str | None = None
        # v1.1.0: card_trace_id — short unique ID for correlating all logs
        # belonging to one card's lifecycle. Format: last 6 chars of msg_id.
        self.card_trace_id: str = (message_id or "??????")[-6:]
        self.use_cardkit: bool = False
        self.text = TextState()
        self.tool_use = ToolUseTracker()
        self.flush = FlushController(throttle_ms=PATCH_MS)
        self.footer: dict[str, Any] = {}
        self.sequence = 1
        self._loop = loop
        self.created_at = time.time()
        self.deferred_background_review_closed = False
        self.deferred_background_reviews: list[tuple[str, Any]] = []
        self.deferred_background_review_lock = Lock()

        # ── State machine enhancements ──
        self.create_epoch: int = 0          # Incremented on terminal phase entry
        self._create_epoch_snap: int = 0    # Snapshotted when creation starts
        self.terminal_reason: str = ""      # Why the session ended
        self.terminal_source: str = ""      # Which code path triggered terminal

        self.guard = UnavailableGuard(
            reply_to_message_id=message_id,
            get_card_message_id=lambda: self.card_msg_id,
            on_terminate=self._on_guard_terminate,
        )

        self.linear = False
        self.unified_state: UnifiedLinearState | None = None
        self.existing_elements: set[str] = set()
        # ── State machine: creation stages ──
        # Replaces the previous _panel_element_created / _answer_element_created /
        # _loading_hint_removed booleans.  Each stage is added to the set when
        # the corresponding card lifecycle event happens:
        #   "panel"        — unified panel element added to card (add_elements)
        #   "answer"       — answer streaming element added to card (add_elements)
        #   "hint_removed" — loading hint element deleted from card
        # The set is checked via ``"panel" in session._creation_stages`` etc.
        # Other orthogonal flags (_streaming_closed, _was_aborted, _pending_flush,
        # _first_flush_done) remain as booleans.
        self._creation_stages: set[str] = set()
        self.card_created_at: float = 0.0
        self._was_aborted: bool = False
        self.error_message: str = ""
        self._first_flush_done: bool = False
        # v1.3.4: Phase 2 永久失败标志（schema_error / element_not_found）
        # 设置后跳过所有 Phase 2/3 flush，完成时走全量重建
        self._phase2_failed: bool = False
        self._first_answer_time: float = 0.0
        self._pending_flush: bool = False
        self._streaming_closed: bool = False
        # v1.2.0 L1: "streaming closed" 日志去重——同一张卡第一次打 INFO，之后降 DEBUG
        self._streaming_closed_logged: bool = False
        self._card_ready: asyncio.Event = asyncio.Event()
        # v1.4.0 fix (问题3 根因1 — delegate_task 后卡片降级纯文本):
        # _is_continuation=True 标记本 session 是为已终态/流式关闭的旧 session
        # 续写而创建的新卡片（同一 chat/anchor 下重激活）。用于日志区分与防止
        # 递归重激活。原 session 在 delegate_task 长任务期间被飞书服务端关闭
        # 流式（_streaming_closed=True），新 token 不再尝试写入旧卡（必失败
        # 300309），而是开新卡续写。
        self._is_continuation: bool = False
        # 同一 session 被重激活的次数计数。每次 _reactivate_session_for_continuation
        # 触发时 +1。用于限制最多重激活 1 次（防止极端情况下新 session 也 300309
        # 时无限递归重激活）。当 _continuation_reactivation_count >= 1 时，
        # on_answer 不再尝试重激活，回退到原 fallback 路径。
        self._continuation_reactivation_count: int = 0

    # ------------------------------------------------------------------
    # State machine — validated transitions
    # ------------------------------------------------------------------

    def transition(self, to: str, source: str = "", reason: str = "") -> bool:
        """Attempt a validated state transition.

        Returns True if the transition was legal and applied, False if
        rejected.  Illegal transitions are logged but do not raise.

        If *to* is a terminal phase and *reason* is provided, the
        terminal_reason / terminal_source fields are set automatically.
        """
        from_phase = self.state
        if from_phase == to:
            return True  # idempotent

        if not is_legal_transition(from_phase, to):
            _logger.warning(
                "phase transition rejected: %s → %s (source=%s msg=%s)",
                from_phase, to, source, (self.message_id or "?")[:12],
            )
            return False

        self.state = to
        _logger.info(
            "phase transition: %s → %s (source=%s msg=%s)",
            from_phase, to, source, (self.message_id or "?")[:12],
        )

        # Track terminal metadata
        if to in TERMINAL_PHASES:
            self.enter_terminal(
                reason=reason or TerminalReason.ERROR,
                source=source,
            )

        # Snapshot epoch when entering CREATING (for stale-create detection)
        if to == CardPhase.CREATING:
            self._create_epoch_snap = self.create_epoch

        return True

    def should_proceed(self, source: str = "") -> bool:
        """Unified guard: returns True if the session can accept updates.

        Combines:
        1. Terminal phase check — session is done
        2. Unavailable guard — message was deleted/recalled

        Side effect: if the guard detects the message is unavailable,
        it will trigger termination (setting TERMINATED state).
        """
        if self.state in TERMINAL_PHASES:
            return False
        if self.guard.should_skip(source):
            return False
        return True

    @property
    def is_terminal_phase(self) -> bool:
        """Whether the session is in a terminal (absorbing) phase."""
        return self.state in TERMINAL_PHASES

    def is_stale_create(self, epoch: int) -> bool:
        """Check if a creation callback is stale (epoch mismatch).

        Usage::

            epoch = session.create_epoch
            # ... await card creation ...
            if session.is_stale_create(epoch):
                return  # Stale callback, ignore
        """
        return epoch != self.create_epoch

    def enter_terminal(self, reason: str, source: str = "") -> None:
        """Enter a terminal phase with reason tracking.

        This is called automatically by ``transition()`` when the target
        is a terminal phase, but can also be called directly when setting
        ``session.state`` without ``transition()`` (legacy code path).
        """
        if self.terminal_reason:
            return  # Already recorded — keep the first reason
        self.terminal_reason = reason
        self.terminal_source = source
        self.create_epoch += 1

    # ------------------------------------------------------------------
    # Internal callbacks
    # ------------------------------------------------------------------

    def _on_guard_terminate(self) -> None:
        """Callback from UnavailableGuard — message was deleted/recalled."""
        if self.state in TERMINAL_PHASES:
            return
        self.state = CardPhase.TERMINATED
        self.enter_terminal(
            reason=TerminalReason.UNAVAILABLE,
            source="unavailable_guard",
        )
        # Signal readiness so awaiters don't deadlock
        self._card_ready.set()
