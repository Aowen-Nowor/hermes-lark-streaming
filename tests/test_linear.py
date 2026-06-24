"""linear.py 测试 — UnifiedLinearState 轮次管理、边界条件、多轮集成."""

from __future__ import annotations

import time

import pytest

from hermes_lark_streaming.state import linear as linear_module
from hermes_lark_streaming.state.linear import UnifiedLinearState, ReasoningRound


class TestReasoningRoundDefaults:
    def test_all_defaults(self) -> None:
        round_ = ReasoningRound(index=1)
        assert round_.index == 1
        assert round_.text == ""
        assert round_.elapsed_ms == 0.0
        assert round_.start_time == 0.0
        assert round_.finalized is False

    def test_custom_init(self) -> None:
        round_ = ReasoningRound(index=2, text="hello", start_time=100.0)
        assert round_.index == 2
        assert round_.text == "hello"
        assert round_.start_time == 100.0

    def test_slots_no_dynamic_attr(self) -> None:
        round_ = ReasoningRound(index=1)
        with pytest.raises(AttributeError):
            round_.nonexistent = True  # type: ignore[attr-defined]


# ── 推理文本追加（on_reasoning_delta） ──


class TestOnReasoningDelta:
    def test_appends_text(self) -> None:
        state = UnifiedLinearState()
        state.on_reasoning_delta("hello ")
        state.on_reasoning_delta("world")
        assert state.current_reasoning_text == "hello world"
        assert state.has_current_reasoning is True

    def test_panel_dirty_set(self) -> None:
        state = UnifiedLinearState()
        state.on_reasoning_delta("thinking")
        assert state.panel_dirty is True

    def test_panel_visible_set(self) -> None:
        state = UnifiedLinearState()
        state.on_reasoning_delta("thinking")
        assert state.panel_visible is True

    def test_total_reasoning_count_with_current(self) -> None:
        state = UnifiedLinearState()
        state.on_reasoning_delta("hello")
        assert state.total_reasoning_count == 1

    def test_has_current_reasoning_reflects_state(self) -> None:
        """has_current_reasoning replaces the removed _native_reasoning_active flag.

        v1.1.0: ``_native_reasoning_active`` was removed. Native reasoning
        dedup now keys off ``bool(state._current_reasoning)`` via the
        ``has_current_reasoning`` property.
        """
        state = UnifiedLinearState()
        assert state.has_current_reasoning is False
        assert bool(state._current_reasoning) is False
        state.on_reasoning_delta("thinking")
        assert state.has_current_reasoning is True
        assert bool(state._current_reasoning) is True


# ── 推理文本去重（on_reasoning_delta dedup） ──


class TestReasoningDeltaDedup:
    """Post-stream duplicate detection in on_reasoning_delta.

    v1.3.0 bug fix: the old implementation compared only the first 30 chars
    (``min(30, ...)``), which could drop legitimate incremental chunks that
    happened to share a 30-char prefix with the accumulated reasoning.
    """

    def test_full_redelivery_skipped(self) -> None:
        """If the incoming text IS the full accumulated reasoning, skip it."""
        state = UnifiedLinearState()
        state.on_reasoning_delta("hello world")
        before = state._current_reasoning
        # Re-deliver the same full text → should be skipped
        state.on_reasoning_delta("hello world")
        assert state._current_reasoning == before  # unchanged

    def test_full_redelivery_with_extra_skipped(self) -> None:
        """Re-delivery of full reasoning + trailing content is also skipped."""
        state = UnifiedLinearState()
        state.on_reasoning_delta("hello world")
        before = state._current_reasoning
        # Full text + extra → starts with the full accumulated → skipped
        state.on_reasoning_delta("hello world and more")
        assert state._current_reasoning == before  # unchanged

    def test_legitimate_increment_not_dropped(self) -> None:
        """v1.3.0 regression: a new chunk sharing a >30 char prefix but NOT
        being a full re-delivery must NOT be dropped.

        Old bug: ``min(30, ...)`` compared only 30 chars, so a chunk that
        shared 30+ chars with accumulated text was wrongly skipped even
        though it was a genuine new incremental piece.
        """
        state = UnifiedLinearState()
        # Accumulate 40 chars of reasoning
        state.on_reasoning_delta("The quick brown fox jumps over")  # 31 chars
        before_len = len(state._current_reasoning)
        # New chunk that shares the first 30 chars but is NOT a full
        # re-delivery (it's shorter than the accumulated text)
        state.on_reasoning_delta("The quick brown fox jumps!")  # 26 chars, < 31
        # This should be APPENDED (not skipped), because it's shorter than
        # the accumulated reasoning → can't be a full re-delivery
        assert len(state._current_reasoning) == before_len + 26

    def test_shorter_text_not_treated_as_redelivery(self) -> None:
        """Text shorter than accumulated reasoning is never a re-delivery."""
        state = UnifiedLinearState()
        state.on_reasoning_delta("abcdefghij")  # 10 chars
        state.on_reasoning_delta("abc")  # 3 chars, shorter → append
        assert state._current_reasoning == "abcdefghijabc"

    def test_empty_current_no_dedup(self) -> None:
        """No dedup when _current_reasoning is empty (first chunk)."""
        state = UnifiedLinearState()
        state.on_reasoning_delta("first reasoning")
        assert state._current_reasoning == "first reasoning"

    def test_prefix_match_but_longer_appended(self) -> None:
        """If text starts with full accumulated reasoning AND is longer,
        it's treated as a re-delivery (skipped).

        This is the intentional behavior: Hermes sometimes re-delivers the
        complete reasoning text with extra content after streaming. We skip
        to avoid duplicating the reasoning in the panel.
        """
        state = UnifiedLinearState()
        state.on_reasoning_delta("part one")  # 8 chars
        before = state._current_reasoning
        # "part one" + " part two" → starts with full "part one" → skipped
        state.on_reasoning_delta("part one part two")
        assert state._current_reasoning == before


# ── 答案文本追加（on_answer_delta） ──


class TestOnAnswerDelta:
    def test_appends_text(self) -> None:
        state = UnifiedLinearState()
        state.on_answer_delta("hello ")
        state.on_answer_delta("world")
        assert state.answer_text == "hello world"

    def test_answer_dirty_set(self) -> None:
        state = UnifiedLinearState()
        state.on_answer_delta("reply")
        assert state.answer_dirty is True

    def test_finalizes_current_reasoning(self) -> None:
        state = UnifiedLinearState()
        state.on_reasoning_delta("thinking")
        time.sleep(0.01)
        state.on_answer_delta("reply")
        # Current reasoning should be finalized and moved to rounds
        assert state.current_reasoning_text == ""
        assert state.has_current_reasoning is False
        assert len(state.reasoning_rounds) == 1
        assert state.reasoning_rounds[0].text == "thinking"
        assert state.reasoning_rounds[0].finalized is True
        assert state.reasoning_rounds[0].elapsed_ms > 0


# ── 工具事件（on_tool_event） ──


class TestOnToolEvent:
    def test_sets_dirty_flags(self) -> None:
        state = UnifiedLinearState()
        state.on_tool_event()
        assert state.tool_steps_dirty is True
        assert state.panel_dirty is True
        assert state.panel_visible is True

    def test_finalizes_current_reasoning(self) -> None:
        state = UnifiedLinearState()
        state.on_reasoning_delta("thinking")
        time.sleep(0.01)
        state.on_tool_event()
        assert len(state.reasoning_rounds) == 1
        assert state.reasoning_rounds[0].finalized is True


# ── finalize ──


class TestFinalize:
    def test_finalizes_current_reasoning(self) -> None:
        state = UnifiedLinearState()
        state.on_reasoning_delta("think")
        time.sleep(0.001)
        state.finalize()
        assert state.has_current_reasoning is False
        assert len(state.reasoning_rounds) == 1
        assert state.reasoning_rounds[0].finalized is True
        assert state.reasoning_rounds[0].elapsed_ms > 0

    def test_noop_when_no_current_reasoning(self) -> None:
        state = UnifiedLinearState()
        state.finalize()
        assert len(state.reasoning_rounds) == 0

    def test_already_finalized_not_overwritten(self) -> None:
        state = UnifiedLinearState()
        state.on_reasoning_delta("a")
        state.on_answer_delta("b")
        elapsed_1 = state.reasoning_rounds[0].elapsed_ms
        state.finalize()
        assert state.reasoning_rounds[0].elapsed_ms == elapsed_1


# ── has_dirty ──


class TestHasDirty:
    def test_initial_not_dirty(self) -> None:
        state = UnifiedLinearState()
        assert state.has_dirty is False

    def test_panel_dirty(self) -> None:
        state = UnifiedLinearState()
        state.on_reasoning_delta("a")
        assert state.has_dirty is True

    def test_answer_dirty(self) -> None:
        state = UnifiedLinearState()
        state.on_answer_delta("a")
        assert state.has_dirty is True

    def test_cleared_after_manual_reset(self) -> None:
        state = UnifiedLinearState()
        state.on_reasoning_delta("a")
        state.panel_dirty = False
        state.answer_dirty = False
        assert state.has_dirty is False


# ── 多轮集成 ──


class TestMultiRound:
    def test_two_rounds(self) -> None:
        state = UnifiedLinearState()
        state.on_reasoning_delta("think 1")
        state.on_answer_delta("reply 1")
        state.on_tool_event()
        state.on_reasoning_delta("think 2")
        state.on_answer_delta("reply 2")
        assert len(state.reasoning_rounds) == 2
        assert state.reasoning_rounds[0].text == "think 1"
        assert state.reasoning_rounds[1].text == "think 2"
        assert state.answer_text == "reply 1reply 2"

    def test_round_index_numbering(self) -> None:
        state = UnifiedLinearState()
        state.on_reasoning_delta("a")
        state.on_answer_delta("b")
        state.on_reasoning_delta("c")
        state.on_answer_delta("d")
        assert state.reasoning_rounds[0].index == 1
        assert state.reasoning_rounds[1].index == 2

    def test_finalize_complex_scenario(self, monkeypatch: pytest.MonkeyPatch) -> None:
        times = iter(float(i) for i in range(100, 108))
        monkeypatch.setattr(linear_module.time, "time", lambda: next(times))

        state = UnifiedLinearState()
        state.on_reasoning_delta("r1")
        state.on_answer_delta("a1")
        state.on_tool_event()
        state.on_reasoning_delta("r2")
        state.on_answer_delta("a2")
        state.finalize()

        assert len(state.reasoning_rounds) == 2
        assert state.reasoning_rounds[0].text == "r1"
        assert state.reasoning_rounds[1].text == "r2"
        assert state.answer_text == "a1a2"


# ── total_reasoning_elapsed_ms ──


class TestTotalReasoningElapsed:
    def test_zero_when_no_reasoning(self) -> None:
        state = UnifiedLinearState()
        assert state.total_reasoning_elapsed_ms == 0.0

    def test_positive_after_finalized(self) -> None:
        state = UnifiedLinearState()
        state.on_reasoning_delta("think")
        time.sleep(0.01)
        state.on_answer_delta("reply")
        assert state.total_reasoning_elapsed_ms > 0


# ── Background review ──


class TestBackgroundReview:
    def test_adds_message(self) -> None:
        state = UnifiedLinearState()
        state.on_background_review("checking quality")
        assert state.bg_review_messages == ["checking quality"]

    def test_has_dirty_with_bg_review(self) -> None:
        state = UnifiedLinearState()
        state.panel_dirty = False
        state.answer_dirty = False
        state.on_background_review("review")
        assert state.has_dirty is True


# ── Deprecated backward compat aliases ──
# v1.1.0: The deprecated ``Segment`` and ``LinearState`` aliases were
# removed (Task 1.1+1.2). The unified panel architecture replaces the
# segment-based model entirely. Tests for those aliases lived here
# previously and have been deleted along with the symbols they covered.
