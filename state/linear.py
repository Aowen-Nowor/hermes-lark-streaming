"""Unified linear state — single-panel reasoning+tool tracking for linear mode.

Replaces the segment-based LinearState. All reasoning rounds and tool calls
are tracked within a single unified collapsible panel (1 card element).

Design rationale
----------------
The old ``LinearState`` managed multiple ``Segment`` objects (reasoning, tool,
answer segments). Each segment created a separate collapsible panel on the
Feishu card, leading to element-count explosion near the 200-element card
limit. ``UnifiedLinearState`` collapses all reasoning rounds and tool calls
into **one** unified collapsible panel (1 element) plus **one** streaming
element for the answer — at most 2 top-level elements regardless of
conversation length.
"""

from __future__ import annotations

import time


# ---------------------------------------------------------------------------
# ReasoningRound
# ---------------------------------------------------------------------------

class ReasoningRound:
    """One round of AI reasoning / thinking.

    A round is created when the first ``reasoning_delta`` token arrives after
    a non-reasoning event (answer, tool, or start of message). It is *not*
    finalised until either a different event type arrives or the whole message
    completes.

    Attributes
    ----------
    index : int
        1-based round number (for display purposes).
    text : str
        Accumulated reasoning text for this round.
    elapsed_ms : float
        Wall-clock duration of this round in milliseconds. Zero while the
        round is still in progress; populated on finalisation.
    start_time : float
        ``time.time()`` when the round started (monotonic-ish).
    finalized : bool
        ``True`` once the round has been closed.
    """

    __slots__ = ("index", "text", "elapsed_ms", "start_time", "finalized")

    def __init__(self, index: int, text: str = "", start_time: float = 0.0) -> None:
        self.index = index
        self.text = text
        self.elapsed_ms: float = 0.0
        self.start_time = start_time
        self.finalized: bool = False

    def __repr__(self) -> str:  # pragma: no cover
        status = "finalized" if self.finalized else "active"
        preview = self.text[:40].replace("\n", "\\n")
        return (
            f"ReasoningRound(index={self.index}, {status}, "
            f"elapsed_ms={self.elapsed_ms:.0f}, text={preview!r}…)"
        )


# ---------------------------------------------------------------------------
# UnifiedLinearState
# ---------------------------------------------------------------------------

class UnifiedLinearState:
    """Unified panel linear state — all reasoning+tool in 1 panel, 1 answer element.

    Key invariant
    -------------
    The unified panel is a single ``collapsible_panel`` element on the card
    (element_id = ``UNIFIED_PANEL_ELEMENT_ID``). Its internal children
    (reasoning rounds, tool steps) are sub-elements rendered as markdown
    inside the panel body — they do **not** count toward the Feishu
    200-element card limit.

    Dirty flags
    -----------
    panel_dirty
        Reasoning or tool content changed → needs ``partial_update_element``.
    answer_dirty
        Answer text changed → needs ``stream_element``.
    tool_steps_dirty
        Tool call event arrived (actual step data lives in
        :class:`ToolUseTracker`; this flag merely signals that the panel
        content must be regenerated).

    Background review
    ------------------
    Background review messages (e.g. "checking response quality", "updating
    memory") are accumulated in :attr:`bg_review_messages` and rendered as a
    separate panel once the first message arrives.
    """

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

        # Chronological timeline: [("reasoning", idx), ("tool", idx), ...]
        # Records the order in which reasoning rounds and tool calls
        # occur, so the unified panel can render them in chronological
        # order rather than grouping all reasoning before all tools.
        self._panel_events: list[tuple[str, int]] = []
        self._tool_count: int = 0

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

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
        # Post-stream dedup: if reasoning was already delivered incrementally
        # (i.e. _current_reasoning is non-empty) and the incoming text looks
        # like the full accumulated text, skip it.  This replaces the old
        # _native_reasoning_active flag with a simple "has any reasoning
        # been tracked yet?" check (len(self._current_reasoning) > 0).
        #
        # v1.3.0 bug fix: the previous implementation compared only the first
        # 30 characters (``min(30, ...)``).  A legitimate incremental chunk
        # that happened to share its first 30 chars with the accumulated
        # reasoning was silently dropped, truncating the displayed reasoning.
        # The correct check is the FULL prefix: if ``text`` starts with the
        # entire ``_current_reasoning``, it is a re-delivery of the complete
        # reasoning (possibly with trailing content), so skip it.
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
        """Tool call event. Finalizes any in-progress reasoning first.

        Parameters
        ----------
        is_new_tool : bool
            True when a new tool starts (``record_start``), False when an
            existing tool is updated (``record_end``). Only new tools are
            added to the chronological timeline; tool status updates
            (success/error) just mark the panel dirty for re-rendering.
        """
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

    # ------------------------------------------------------------------
    # Finalization
    # ------------------------------------------------------------------

    def _finalize_current_reasoning(self) -> None:
        """Finalize the current reasoning round, moving it to :attr:`reasoning_rounds`.

        This is a no-op if no reasoning round is in progress.
        """
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

    # ------------------------------------------------------------------
    # Read-only properties
    # ------------------------------------------------------------------

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
        """Chronological timeline of panel events.

        Returns a list of ``(kind, index)`` tuples recording the order
        in which reasoning rounds and tool calls occurred::

            [("reasoning", 0), ("tool", 0), ("reasoning", 1), ("tool", 1), ...]

        The renderer uses this to interleave reasoning and tool elements
        in chronological order rather than grouping all reasoning before
        all tools.
        """
        return self._panel_events

    @property
    def has_dirty(self) -> bool:
        """Whether any dirty data needs flushing to the card."""
        return (
            self.panel_dirty
            or self.answer_dirty
            or bool(self.bg_review_messages)
        )

    def __repr__(self) -> str:  # pragma: no cover
        parts = [
            f"rounds={len(self.reasoning_rounds)}",
            f"answer_len={len(self.answer_text)}",
            f"panel_dirty={self.panel_dirty}",
            f"answer_dirty={self.answer_dirty}",
        ]
        if self._current_reasoning:
            parts.append("reasoning=active")
        return f"UnifiedLinearState({', '.join(parts)})"
