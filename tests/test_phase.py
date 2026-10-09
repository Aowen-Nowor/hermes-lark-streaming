"""state/phase.py + CardSession 状态机增强测试.

覆盖:
- CardPhase / TerminalReason 常量
- PHASE_TRANSITIONS 合法转换表
- is_legal_transition()
- CardSession.transition() 验证转换
- CardSession.should_proceed() 统一守卫
- CardSession.is_terminal_phase 属性
- CardSession.enter_terminal() / terminal_reason / terminal_source
- CardSession.is_stale_create() epoch 机制
- CardSession._on_guard_terminate() → TERMINATED
- 向后兼容: FAILED == CREATION_FAILED
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from hermes_lark_streaming.state.phase import (
    CardPhase,
    TERMINAL_PHASES,
    _TERMINAL,
    PHASE_TRANSITIONS,
    TerminalReason,
    is_legal_transition,
)
from hermes_lark_streaming.state.session import CardSession


# ── CardPhase 常量 ──────────────────────────────────────────────────


class TestCardPhase:
    """CardPhase string constants and backward compatibility."""

    def test_idle_is_string(self) -> None:
        assert CardPhase.IDLE == "idle"

    def test_creating_is_string(self) -> None:
        assert CardPhase.CREATING == "creating"

    def test_streaming_is_string(self) -> None:
        assert CardPhase.STREAMING == "streaming"

    def test_completing_is_string(self) -> None:
        assert CardPhase.COMPLETING == "completing"

    def test_completed_is_string(self) -> None:
        assert CardPhase.COMPLETED == "completed"

    def test_creation_failed_is_string(self) -> None:
        assert CardPhase.CREATION_FAILED == "creation_failed"

    def test_aborted_is_string(self) -> None:
        assert CardPhase.ABORTED == "aborted"

    def test_terminated_is_string(self) -> None:
        assert CardPhase.TERMINATED == "terminated"

    def test_failed_alias_equals_creation_failed(self) -> None:
        """FAILED is a backward-compatible alias for CREATION_FAILED."""
        assert CardPhase.FAILED == CardPhase.CREATION_FAILED
        assert CardPhase.FAILED == "creation_failed"


class TestTerminalReason:
    """TerminalReason string constants."""

    def test_normal(self) -> None:
        assert TerminalReason.NORMAL == "normal"

    def test_error(self) -> None:
        assert TerminalReason.ERROR == "error"

    def test_abort(self) -> None:
        assert TerminalReason.ABORT == "abort"

    def test_unavailable(self) -> None:
        assert TerminalReason.UNAVAILABLE == "unavailable"

    def test_creation_failed(self) -> None:
        assert TerminalReason.CREATION_FAILED == "creation_failed"


# ── PHASE_TRANSITIONS ────────────────────────────────────────────────


class TestPhaseTransitions:
    """Legal phase transition map."""

    def test_idle_to_creating(self) -> None:
        assert is_legal_transition("idle", "creating")

    def test_idle_to_aborted(self) -> None:
        assert is_legal_transition("idle", "aborted")

    def test_idle_to_terminated(self) -> None:
        assert is_legal_transition("idle", "terminated")

    def test_idle_to_streaming_illegal(self) -> None:
        assert not is_legal_transition("idle", "streaming")

    def test_creating_to_streaming(self) -> None:
        assert is_legal_transition("creating", "streaming")

    def test_creating_to_creation_failed(self) -> None:
        assert is_legal_transition("creating", "creation_failed")

    def test_creating_to_terminated(self) -> None:
        assert is_legal_transition("creating", "terminated")

    def test_streaming_to_completing(self) -> None:
        assert is_legal_transition("streaming", "completing")

    def test_streaming_to_aborted(self) -> None:
        assert is_legal_transition("streaming", "aborted")

    def test_streaming_to_terminated(self) -> None:
        assert is_legal_transition("streaming", "terminated")

    def test_completing_to_completed(self) -> None:
        assert is_legal_transition("completing", "completed")

    def test_completing_to_creation_failed(self) -> None:
        assert is_legal_transition("completing", "creation_failed")

    def test_completing_to_aborted(self) -> None:
        assert is_legal_transition("completing", "aborted")

    def test_completing_to_terminated(self) -> None:
        assert is_legal_transition("completing", "terminated")

    def test_completed_no_outgoing(self) -> None:
        """Terminal phase has no outgoing transitions."""
        assert len(PHASE_TRANSITIONS["completed"]) == 0

    def test_creation_failed_no_outgoing(self) -> None:
        assert len(PHASE_TRANSITIONS["creation_failed"]) == 0

    def test_aborted_no_outgoing(self) -> None:
        assert len(PHASE_TRANSITIONS["aborted"]) == 0

    def test_terminated_no_outgoing(self) -> None:
        assert len(PHASE_TRANSITIONS["terminated"]) == 0

    def test_idempotent_transition(self) -> None:
        """Same → same is legal (idempotent)."""
        assert is_legal_transition("idle", "idle")
        assert is_legal_transition("streaming", "streaming")
        assert is_legal_transition("completed", "completed")

    def test_terminal_phases_frozenset(self) -> None:
        assert TERMINAL_PHASES == frozenset({
            "completed", "creation_failed", "aborted", "terminated",
        })

    def test_terminal_alias(self) -> None:
        """_TERMINAL is a backward-compatible alias for TERMINAL_PHASES."""
        assert _TERMINAL is TERMINAL_PHASES


# ── CardSession.transition() ─────────────────────────────────────────


class TestCardSessionTransition:
    """Validated state transitions via CardSession.transition()."""

    def _make_session(self) -> CardSession:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return CardSession("test_msg", "test_chat", loop)
        finally:
            loop.close()

    def test_legal_transition_returns_true(self) -> None:
        s = self._make_session()
        assert s.transition("creating", "test") is True
        assert s.state == "creating"

    def test_illegal_transition_returns_false(self) -> None:
        s = self._make_session()
        assert s.transition("streaming", "test") is False
        assert s.state == "idle"  # Unchanged

    def test_idempotent_transition_returns_true(self) -> None:
        s = self._make_session()
        assert s.transition("idle", "test") is True
        assert s.state == "idle"

    def test_terminal_transition_sets_reason(self) -> None:
        s = self._make_session()
        s.transition("creating", "test")
        s.transition("creation_failed", "test_source", reason="creation_failed")
        assert s.terminal_reason == "creation_failed"
        assert s.terminal_source == "test_source"

    def test_terminal_transition_increments_epoch(self) -> None:
        s = self._make_session()
        epoch_before = s.create_epoch
        s.transition("creating", "test")
        s.transition("creation_failed", "test", reason="creation_failed")
        assert s.create_epoch == epoch_before + 1

    def test_entering_creating_snapshots_epoch(self) -> None:
        s = self._make_session()
        epoch0 = s.create_epoch
        s.transition("creating", "test")
        assert s._create_epoch_snap == epoch0

    def test_full_lifecycle_transitions(self) -> None:
        s = self._make_session()
        assert s.transition("creating", "start") is True
        assert s.state == "creating"
        assert s.transition("streaming", "card_ready") is True
        assert s.state == "streaming"
        assert s.transition("completing", "on_completed") is True
        assert s.state == "completing"
        assert s.transition("completed", "sealed", reason="normal") is True
        assert s.state == "completed"
        assert s.terminal_reason == "normal"


# ── CardSession.should_proceed() ─────────────────────────────────────


class TestCardSessionShouldProceed:
    """Unified guard combining terminal + unavailable checks."""

    def _make_session(self) -> CardSession:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return CardSession("test_msg", "test_chat", loop)
        finally:
            loop.close()

    def test_idle_returns_true(self) -> None:
        s = self._make_session()
        assert s.should_proceed("test") is True

    def test_streaming_returns_true(self) -> None:
        s = self._make_session()
        s.state = "streaming"
        assert s.should_proceed("test") is True

    def test_completed_returns_false(self) -> None:
        s = self._make_session()
        s.state = "completed"
        assert s.should_proceed("test") is False

    def test_aborted_returns_false(self) -> None:
        s = self._make_session()
        s.state = "aborted"
        assert s.should_proceed("test") is False

    def test_creation_failed_returns_false(self) -> None:
        s = self._make_session()
        s.state = "creation_failed"
        assert s.should_proceed("test") is False

    def test_terminated_returns_false(self) -> None:
        s = self._make_session()
        s.state = "terminated"
        assert s.should_proceed("test") is False


# ── CardSession.is_terminal_phase ─────────────────────────


class TestCardSessionProperties:
    """is_terminal_phase property."""

    def _make_session(self) -> CardSession:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return CardSession("test_msg", "test_chat", loop)
        finally:
            loop.close()

    def test_idle_not_terminal(self) -> None:
        s = self._make_session()
        assert s.is_terminal_phase is False

    def test_completed_is_terminal(self) -> None:
        s = self._make_session()
        s.state = "completed"
        assert s.is_terminal_phase is True

    def test_creation_failed_is_terminal(self) -> None:
        s = self._make_session()
        s.state = "creation_failed"
        assert s.is_terminal_phase is True

    def test_aborted_is_terminal(self) -> None:
        s = self._make_session()
        s.state = "aborted"
        assert s.is_terminal_phase is True

    def test_terminated_is_terminal(self) -> None:
        s = self._make_session()
        s.state = "terminated"
        assert s.is_terminal_phase is True


# ── CardSession.enter_terminal() ─────────────────────────────────────


class TestCardSessionEnterTerminal:
    """enter_terminal sets reason, source, increments epoch."""

    def _make_session(self) -> CardSession:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return CardSession("test_msg", "test_chat", loop)
        finally:
            loop.close()

    def test_enter_terminal_sets_reason(self) -> None:
        s = self._make_session()
        s.enter_terminal(reason="normal", source="test")
        assert s.terminal_reason == "normal"
        assert s.terminal_source == "test"

    def test_enter_terminal_increments_epoch(self) -> None:
        s = self._make_session()
        epoch0 = s.create_epoch
        s.enter_terminal(reason="error", source="test")
        assert s.create_epoch == epoch0 + 1

    def test_enter_terminal_idempotent_keeps_first(self) -> None:
        s = self._make_session()
        s.enter_terminal(reason="normal", source="first")
        s.enter_terminal(reason="error", source="second")
        # First reason wins
        assert s.terminal_reason == "normal"
        assert s.terminal_source == "first"

    def test_enter_terminal_increments_epoch_only_once(self) -> None:
        s = self._make_session()
        epoch0 = s.create_epoch
        s.enter_terminal(reason="normal", source="test")
        epoch1 = s.create_epoch
        s.enter_terminal(reason="error", source="test2")
        assert s.create_epoch == epoch1  # No further increment


# ── CardSession.is_stale_create() ────────────────────────────────────


class TestCardSessionStaleCreate:
    """Epoch-based stale creation detection."""

    def _make_session(self) -> CardSession:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return CardSession("test_msg", "test_chat", loop)
        finally:
            loop.close()

    def test_not_stale_when_epoch_matches(self) -> None:
        s = self._make_session()
        epoch = s.create_epoch
        assert s.is_stale_create(epoch) is False

    def test_stale_after_terminal_entry(self) -> None:
        s = self._make_session()
        epoch = s.create_epoch
        s.enter_terminal(reason="unavailable", source="guard")
        assert s.is_stale_create(epoch) is True

    def test_not_stale_with_new_epoch(self) -> None:
        s = self._make_session()
        s.enter_terminal(reason="unavailable", source="guard")
        new_epoch = s.create_epoch
        assert s.is_stale_create(new_epoch) is False


# ── CardSession._on_guard_terminate() ────────────────────────────────


class TestCardSessionGuardTerminate:
    """UnavailableGuard callback sets TERMINATED state."""

    def _make_session(self) -> CardSession:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return CardSession("test_msg", "test_chat", loop)
        finally:
            loop.close()

    def test_guard_terminate_sets_terminated(self) -> None:
        s = self._make_session()
        s._on_guard_terminate()
        assert s.state == "terminated"

    def test_guard_terminate_sets_reason_unavailable(self) -> None:
        s = self._make_session()
        s._on_guard_terminate()
        assert s.terminal_reason == "unavailable"

    def test_guard_terminate_signals_card_ready(self) -> None:
        s = self._make_session()
        assert not s._card_ready.is_set()
        s._on_guard_terminate()
        assert s._card_ready.is_set()

    def test_guard_terminate_idempotent(self) -> None:
        s = self._make_session()
        s._on_guard_terminate()
        assert s.state == "terminated"
        s._on_guard_terminate()  # Should not crash
        assert s.state == "terminated"
        assert s.terminal_reason == "unavailable"  # First reason preserved


# ── Backward compatibility ───────────────────────────────────────────

