"""Unified linear state — single-panel reasoning+tool tracking for linear mode."""

from __future__ import annotations

import time

class ReasoningRound:
    """One round of AI reasoning / thinking."""

    __slots__ = ("index", "text", "elapsed_ms", "start_time", "finalized")

    def __init__(self, index: int, text: str = "", start_time: float = 0.0) -> None:
        self.index = index
        self.text = text
        self.elapsed_ms: float = 0.0
        self.start_time = start_time
        self.finalized: bool = False

class UnifiedLinearState:
    """Unified panel linear state — all reasoning+tool in 1 panel, 1 answer element."""

    __slots__ = (
        "reasoning_rounds",
        "_current_reasoning",
        "_reasoning_start",
        "tool_steps_dirty",
        "answer_text",
        "panel_dirty",
        "answer_dirty",
        "panel_visible",
        "bg_review_messages",
        "_panel_events",
        "_tool_count",
    )

    def __init__(self) -> None:
        # Reasoning tracking
        self.reasoning_rounds: list[ReasoningRound] = []
        self._current_reasoning: str = ""
        self._reasoning_start: float = 0.0

        # Tool tracking — dirty flag only; actual steps come from ToolUseTracker
        self.tool_steps_dirty: bool = False

        # Answer tracking
        self.answer_text: str = ""

        # Dirty flags
        self.panel_dirty: bool = False
        self.answer_dirty: bool = False

        # Panel visibility — set to True once the first reasoning or tool
        # event arrives so the renderer knows to create the element.
        self.panel_visible: bool = False

        # Background review
        self.bg_review_messages: list[str] = []

        self._panel_events: list[tuple[str, int]] = []
        self._tool_count: int = 0

    def on_reasoning_delta(self, text: str) -> None:
        """Reasoning text increment. Starts a new round if not already in one."""
        import logging as _logging
        _diag_logger = _logging.getLogger("hermes_lark_streaming")
        _diag_logger.debug(
            "HLS: on_reasoning_delta text=%r current_len=%d rounds=%d",
            text[:40] if text else "",
            len(self._current_reasoning),
            len(self.reasoning_rounds),
        )
        # v1.3.0 bug fix: the previous implementation compared only the first
        # The correct check is the FULL prefix: if ``text`` starts with the
        if (
            self._current_reasoning
            and len(text) >= len(self._current_reasoning)
            and text[:len(self._current_reasoning)] == self._current_reasoning
        ):
            _diag_logger.debug(
                "HLS: on_reasoning_delta skips post-stream duplicate "
                "text_len=%d current_len=%d",
                len(text), len(self._current_reasoning),
            )
            return
        if not self._current_reasoning:
            # First token of a new reasoning round
            self._reasoning_start = time.time()
        self._current_reasoning += text
        self.panel_dirty = True
        self.panel_visible = True

    def on_answer_delta(self, text: str) -> None:
        """Answer text increment. Finalizes any in-progress reasoning first."""
        self._finalize_current_reasoning()
        self.answer_text += text
        self.answer_dirty = True

    def on_tool_event(self, is_new_tool: bool = True) -> None:
        """Tool call event. Finalizes any in-progress reasoning first."""
        self._finalize_current_reasoning()
        if is_new_tool:
            self._panel_events.append(("tool", self._tool_count))
            self._tool_count += 1
        self.tool_steps_dirty = True
        self.panel_dirty = True
        self.panel_visible = True

    def on_background_review(self, message: str) -> None:
        """Background review message (e.g. quality check, memory update)."""
        self.bg_review_messages.append(message)

    def _finalize_current_reasoning(self) -> None:
        """Finalize the current reasoning round, moving it to :attr:`reasoning_rounds`."""
        if not self._current_reasoning:
            return
        elapsed = (time.time() - self._reasoning_start) * 1000 if self._reasoning_start else 0.0
        round_ = ReasoningRound(
            index=len(self.reasoning_rounds) + 1,
            text=self._current_reasoning,
            start_time=self._reasoning_start,
        )
        round_.elapsed_ms = elapsed
        round_.finalized = True
        self.reasoning_rounds.append(round_)
        self._panel_events.append(("reasoning", len(self.reasoning_rounds) - 1))
        self._current_reasoning = ""
        self._reasoning_start = 0.0

    def finalize(self) -> None:
        """Finalize any in-progress reasoning (called at message completion)."""
        self._finalize_current_reasoning()

    @property
    def current_reasoning_text(self) -> str:
        """Get the in-progress reasoning text (for streaming display)."""
        return self._current_reasoning

    @property
    def has_current_reasoning(self) -> bool:
        """Whether there is an in-progress reasoning round."""
        return bool(self._current_reasoning)

    @property
    def total_reasoning_count(self) -> int:
        """Total reasoning rounds (finalized + in-progress)."""
        count = len(self.reasoning_rounds)
        if self._current_reasoning:
            count += 1
        return count

    @property
    def total_reasoning_elapsed_ms(self) -> float:
        """Total reasoning elapsed time across all rounds (milliseconds)."""
        total = sum(r.elapsed_ms for r in self.reasoning_rounds)
        if self._reasoning_start:
            total += (time.time() - self._reasoning_start) * 1000
        return total

    @property
    def panel_events(self) -> list[tuple[str, int]]:
        """Chronological timeline of panel events."""
        return self._panel_events

    @property
    def has_dirty(self) -> bool:
        """Whether any dirty data needs flushing to the card."""
        return (
            self.panel_dirty
            or self.answer_dirty
            or bool(self.bg_review_messages)
        )
