"""Design rationale (aligned with openclaw-lark StreamingCardController):"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger("hermes_lark_streaming")

class CardPhase:
    """Phases represent the lifecycle stage of a card session, from creation"""

    IDLE = "idle"
    CREATING = "creating"
    STREAMING = "streaming"
    COMPLETING = "completing"
    COMPLETED = "completed"
    # CREATION_FAILED replaces the catch-all FAILED for card creation errors.
    # Distinct from TERMINATED so callers can fallthrough to static delivery.
    CREATION_FAILED = "creation_failed"
    ABORTED = "aborted"
    # TERMINATED: message deleted/recalled — stop all updates immediately.
    TERMINATED = "terminated"

    # Backward compatibility: FAILED still exists as an alias for CREATION_FAILED.
    # DEPRECATED: use CREATION_FAILED instead.
    FAILED = "creation_failed"

class TerminalReason:
    """Why a session entered a terminal phase."""

    NORMAL = "normal"              # Streaming completed successfully
    ERROR = "error"                # An error occurred during reply generation
    ABORT = "abort"                # Explicitly cancelled by user
    UNAVAILABLE = "unavailable"    # Source message was deleted/recalled
    CREATION_FAILED = "creation_failed"  # Card creation failed

PHASE_TRANSITIONS: dict[str, frozenset[str]] = {
    CardPhase.IDLE: frozenset({CardPhase.CREATING, CardPhase.ABORTED, CardPhase.TERMINATED}),
    CardPhase.CREATING: frozenset({CardPhase.STREAMING, CardPhase.CREATION_FAILED, CardPhase.TERMINATED}),
    CardPhase.STREAMING: frozenset({CardPhase.COMPLETING, CardPhase.ABORTED, CardPhase.TERMINATED}),
    CardPhase.COMPLETING: frozenset({
        CardPhase.COMPLETED,
        CardPhase.CREATION_FAILED,
        CardPhase.ABORTED,
        CardPhase.TERMINATED,
    }),
    CardPhase.COMPLETED: frozenset(),       # terminal
    CardPhase.CREATION_FAILED: frozenset(),  # terminal
    CardPhase.ABORTED: frozenset(),          # terminal
    CardPhase.TERMINATED: frozenset(),       # terminal
}

TERMINAL_PHASES: frozenset[str] = frozenset({
    CardPhase.COMPLETED,
    CardPhase.CREATION_FAILED,
    CardPhase.ABORTED,
    CardPhase.TERMINATED,
})

# Legacy alias — old code references _TERMINAL
_TERMINAL = TERMINAL_PHASES

# ── Terminal reason → phase mapping ──────────────────────────────────

TERMINAL_REASON_TO_PHASE: dict[str, str] = {
    TerminalReason.NORMAL: CardPhase.COMPLETED,
    TerminalReason.ERROR: CardPhase.COMPLETED,  # Error is a subtype of completed
    TerminalReason.ABORT: CardPhase.ABORTED,
    TerminalReason.UNAVAILABLE: CardPhase.TERMINATED,
    TerminalReason.CREATION_FAILED: CardPhase.CREATION_FAILED,
}

def is_legal_transition(from_phase: str, to_phase: str) -> bool:
    """Check if a phase transition is legal."""
    if from_phase == to_phase:
        return True  # idempotent
    allowed = PHASE_TRANSITIONS.get(from_phase, frozenset())
    return to_phase in allowed
