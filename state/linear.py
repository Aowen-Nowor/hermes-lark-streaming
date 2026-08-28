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
        # v1.7.0 (R2-01): incremental escape cache for answer_text
        "_escaped_cache",
        "_escaped_src_len",
    )

    # v1.7.0 (R2-02): storage caps. Display already trims to max=20 at render
    # time; storage keeps headroom for the seal summary (uses the latest
    # round). Unbounded lists previously grew forever on marathon sessions
    # (sub-agent chains, long chained reasoning, review floods).
    _MAX_REASONING_ROUNDS_STORED = 50
    _MAX_PANEL_EVENTS_STORED = 100
    _MAX_BG_REVIEW_MESSAGES_STORED = 20

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

        # v1.7.0 (R2-01): escaped-answer cache (see escaped_answer_view)
        self._escaped_cache: str | None = None
        self._escaped_src_len: int = 0

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

    def reset_escape_cache(self) -> None:
        """v1.7.0 (R2-01): invalidate the escaped-answer cache — call after
        DIRECTLY replacing answer_text (e.g. on_completed's MISMATCH branch)."""
        self._escaped_cache = None
        self._escaped_src_len = 0

    def escaped_answer_view(self) -> str:
        """v1.7.0 (R2-01): escaped answer_text with an incremental cache.

        The stream_element API is "set full content" semantics — every flush
        re-sent the ENTIRE answer through escape_markdown_asterisks (5 regex
        passes over ever-growing text). The cache appends only the new delta
        when it is provably safe:

        * the cached prefix is unchanged (append-only stream), and
        * the delta contains no '*' and no '`' — escape_markdown_asterisks is
          the identity on such text, and none of its regexes can pair across
          the boundary (every relevant pattern needs one of those two chars on
          BOTH sides), and
        * the cached tail is not mid-placeholder (no '\x00').

        Anything else falls back to a full recompute, so worst case equals the
        old behavior — the fast path can never produce a different escape.
        """
        src = self.answer_text
        if not src:
            return src
        cache = self._escaped_cache
        cached_len = self._escaped_src_len
        if cache is not None and cached_len == len(src):
            # No new content since the last escape — cached value is current.
            return cache
        if (
            cache is not None
            and 0 < cached_len < len(src)
            and "\x00" not in cache
            and not cache.endswith(("*", "`", "\\"))
        ):
            # v1.7.0: the endswith guard closes the cross-boundary lookahead
            # hazard — a trailing UNESCAPED '*' in the cached prefix was left
            # unescaped precisely because nothing followed it; a plain delta
            # then supplies the follower and the full recompute escapes it
            # (found by test_v170_fixes.TestEscapeCache).
            new_part = src[cached_len:]
            if "*" not in new_part and "`" not in new_part:
                cache = cache + new_part
                self._escaped_cache = cache
                self._escaped_src_len = len(src)
                return cache
        from ..cardkit.md import escape_markdown_asterisks
        cache = escape_markdown_asterisks(src)
        self._escaped_cache = cache
        self._escaped_src_len = len(src)
        return cache

    def on_tool_event(self, is_new_tool: bool = True) -> None:
        """Tool call event. Finalizes any in-progress reasoning first."""
        self._finalize_current_reasoning()
        if is_new_tool:
            self._panel_events.append(("tool", self._tool_count))
            self._tool_count += 1
        self.tool_steps_dirty = True
        self.panel_dirty = True
        self.panel_visible = True
        self._enforce_storage_caps()

    def on_background_review(self, message: str) -> None:
        """Background review message (e.g. quality check, memory update)."""
        self.bg_review_messages.append(message)
        # v1.7.0 (R2-02): review floods are capped too (kept newest).
        if len(self.bg_review_messages) > self._MAX_BG_REVIEW_MESSAGES_STORED:
            self.bg_review_messages = self.bg_review_messages[
                -self._MAX_BG_REVIEW_MESSAGES_STORED:
            ]

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
        self._enforce_storage_caps()

    def _enforce_storage_caps(self) -> None:
        """v1.7.0 (R2-02): bound in-memory growth (see _MAX_* constants).
        Dropping the oldest reasoning rounds re-indexes the reasoning entries
        of _panel_events so the timeline stays consistent with storage."""
        dropped_rounds = 0
        while len(self.reasoning_rounds) > self._MAX_REASONING_ROUNDS_STORED:
            self.reasoning_rounds.pop(0)
            dropped_rounds += 1
        if dropped_rounds:
            kept: list[tuple[str, int]] = []
            for kind, idx in self._panel_events:
                if kind == "reasoning":
                    if idx >= dropped_rounds:
                        kept.append((kind, idx - dropped_rounds))
                    # else: event pointed at a dropped round — discard
                else:
                    kept.append((kind, idx))
            self._panel_events = kept
        if len(self._panel_events) > self._MAX_PANEL_EVENTS_STORED:
            self._panel_events = self._panel_events[-self._MAX_PANEL_EVENTS_STORED:]
        if len(self.bg_review_messages) > self._MAX_BG_REVIEW_MESSAGES_STORED:
            self.bg_review_messages = self.bg_review_messages[-self._MAX_BG_REVIEW_MESSAGES_STORED:]

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
