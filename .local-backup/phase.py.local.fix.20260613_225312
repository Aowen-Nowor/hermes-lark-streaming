"""Card lifecycle phase — explicit state machine with enforced transitions.

Design rationale (aligned with openclaw-lark StreamingCardController):
- Explicit PHASE_TRANSITIONS map defines legal transitions
- Terminal phases have no outgoing transitions (absorbing states)
- TerminalReason tracks WHY a session ended
- create_epoch prevents stale creation callbacks from corrupting state
- should_proceed() unifies terminal + unavailable + phase checks
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger("hermes_lark_streaming")


# ── Phase constants ───────────────────────────────────────────────────
# All phases are plain strings for backward compatibility.
# Existing code using ``session.state == "idle"`` continues to work.
# New code should use ``session.transition(to, source)`` for validated
# transitions.

class CardPhase:
    """Card lifecycle phases — string constants for backward compatibility.

    Phases represent the lifecycle stage of a card session, from creation
    through streaming to terminal state.  Each phase has a defined set of
    legal successor phases; attempts to transition to an illegal successor
    are logged and rejected.

    Backward compatibility: all phase constants are plain strings, so
    existing code using ``session.state == "idle"`` continues to work.
    New code should use ``session.transition(to, source)`` for validated
    transitions.
    """

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


class CardVisualState:
    """Visual appearance of a card — separate from lifecycle phase.

    While CardPhase tracks the *lifecycle* (what operations are legal),
    CardVisualState tracks the *appearance* (what the card looks like).
    Multiple phases can map to the same visual state.
    """

    THINKING = "thinking"      # Yellow/neutral header, "Thinking..." text
    STREAMING = "streaming"    # No header, streaming text, tool panel
    COMPLETE = "complete"      # Green header, collapsible reasoning, footer
    ERROR = "error"            # Red header, error notice
    ABORTED = "aborted"        # Orange header, "Stopped" notice


# ── Legal phase transitions ──────────────────────────────────────────
# Maps each phase to the set of phases it may transition to.
# Terminal phases (COMPLETED, CREATION_FAILED, ABORTED, TERMINATED) have
# no outgoing transitions — they are absorbing states.

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

# ── Phase → Visual State mapping ─────────────────────────────────────

PHASE_TO_VISUAL: dict[str, str] = {
    CardPhase.IDLE: CardVisualState.THINKING,
    CardPhase.CREATING: CardVisualState.THINKING,
    CardPhase.STREAMING: CardVisualState.STREAMING,
    CardPhase.COMPLETING: CardVisualState.STREAMING,  # Still streaming during drain
    CardPhase.COMPLETED: CardVisualState.COMPLETE,
    CardPhase.CREATION_FAILED: CardVisualState.ERROR,
    CardPhase.ABORTED: CardVisualState.ABORTED,
    CardPhase.TERMINATED: CardVisualState.ERROR,
}

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


def get_visual_state(phase: str) -> str:
    """Get the visual state for a given phase."""
    return PHASE_TO_VISUAL.get(phase, CardVisualState.THINKING)
