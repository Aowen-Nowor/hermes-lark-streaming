"""unavailable_guard.py 测试 — 消息不可用保护、API 错误码提取、Guard 终止逻辑."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from hermes_lark_streaming.feishu import MSG_NOT_FOUND
from hermes_lark_streaming.feishu import guard as _guard_module
from hermes_lark_streaming.feishu import (
    UnavailableGuard,
    extract_api_code,
    is_terminal_api_code,
    is_unavailable,
    mark_unavailable,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_cache():
    """Ensure the global unavailable cache is empty before and after each test."""
    _guard_module._unavailable_cache.clear()
    yield
    _guard_module._unavailable_cache.clear()


# ===========================================================================
# mark_unavailable / is_unavailable
# ===========================================================================

class TestMarkUnavailable:
    def test_mark_then_check(self) -> None:
        mark_unavailable("msg_001", 231003)
        assert is_unavailable("msg_001") is True

    def test_mark_with_operation(self) -> None:
        mark_unavailable("msg_002", MSG_NOT_FOUND, operation="reply_card")
        assert is_unavailable("msg_002") is True

    def test_multiple_marks(self) -> None:
        mark_unavailable("msg_a", 231003)
        mark_unavailable("msg_b", MSG_NOT_FOUND)
        assert is_unavailable("msg_a") is True
        assert is_unavailable("msg_b") is True


class TestIsUnavailable:
    def test_none_returns_false(self) -> None:
        assert is_unavailable(None) is False

    def test_empty_string_returns_false(self) -> None:
        assert is_unavailable("") is False

    def test_unknown_message_id_returns_false(self) -> None:
        assert is_unavailable("nonexistent_id") is False

    def test_known_message_id_returns_true(self) -> None:
        mark_unavailable("msg_known", 231003)
        assert is_unavailable("msg_known") is True

    def test_cache_ttl_expiry(self) -> None:
        """Entries older than _UNENHANCED_CACHE_TTL_SEC should be pruned."""
        ttl = _guard_module._UNENHANCED_CACHE_TTL_SEC

        # Manually inject an expired entry
        _guard_module._unavailable_cache["msg_old"] = {
            "code": 231003,
            "operation": "",
            "at": time.time() - ttl - 1,  # 1 second past TTL
        }

        # Before pruning, the raw key exists
        assert "msg_old" in _guard_module._unavailable_cache

        # v1.3.0: pruning is now threshold-gated (only runs when cache > 50).
        # Call _prune_cache() directly to test the prune logic itself.
        with _guard_module._unavailable_cache_lock:
            _guard_module._prune_cache()
        assert "msg_old" not in _guard_module._unavailable_cache

    def test_cache_ttl_not_yet_expired(self) -> None:
        """Entries within TTL should still be present after pruning."""
        mark_unavailable("msg_fresh", 231003)
        assert is_unavailable("msg_fresh") is True

        # Prune again — should still be there
        assert is_unavailable("msg_fresh") is True

    def test_prune_removes_only_expired(self) -> None:
        ttl = _guard_module._UNENHANCED_CACHE_TTL_SEC

        # One expired, one fresh
        _guard_module._unavailable_cache["msg_expired"] = {
            "code": 231003,
            "operation": "",
            "at": time.time() - ttl - 10,
        }
        mark_unavailable("msg_fresh", 230011)

        # v1.3.0: pruning is now threshold-gated. Call _prune_cache() directly
        # to test the prune logic itself.
        with _guard_module._unavailable_cache_lock:
            _guard_module._prune_cache()

        assert "msg_expired" not in _guard_module._unavailable_cache
        assert "msg_fresh" in _guard_module._unavailable_cache


# ===========================================================================
# extract_api_code
# ===========================================================================

class TestExtractApiCode:
    def test_none_returns_none(self) -> None:
        assert extract_api_code(None) is None

    def test_exception_with_int_code(self) -> None:
        err = RuntimeError("something failed")
        err.code = 231003  # type: ignore[attr-defined]
        assert extract_api_code(err) == 231003

    def test_exception_with_non_int_code_falls_through(self) -> None:
        """If .code exists but is not int, should fall through to args parsing."""
        err = RuntimeError("code=12345 happened")
        err.code = "not_an_int"  # type: ignore[attr-defined]
        # Falls through to args parsing, finds code=12345 in string
        assert extract_api_code(err) == 12345

    def test_exception_with_non_int_code_and_no_args_match(self) -> None:
        """Non-int .code and no parseable code in args → returns None."""
        err = RuntimeError("generic error")
        err.code = "not_an_int"  # type: ignore[attr-defined]
        assert extract_api_code(err) is None

    def test_exception_args_with_code_equals(self) -> None:
        err = RuntimeError("request failed: code=12345, msg=not found")
        assert extract_api_code(err) == 12345

    def test_exception_args_with_code_colon(self) -> None:
        err = RuntimeError("error code: 99999 something")
        assert extract_api_code(err) == 99999

    def test_exception_args_with_code_equals_spaces(self) -> None:
        err = RuntimeError("error code=  67890 extra")
        assert extract_api_code(err) == 67890

    def test_exception_no_parseable_code(self) -> None:
        err = RuntimeError("just a plain error")
        assert extract_api_code(err) is None

    def test_exception_empty_args(self) -> None:
        err = RuntimeError()
        assert extract_api_code(err) is None

    def test_exception_non_string_first_arg(self) -> None:
        err = RuntimeError(42)
        assert extract_api_code(err) is None

    def test_int_code_takes_priority_over_args(self) -> None:
        """When .code is int, it should be returned without checking args."""
        err = RuntimeError("code=99999")
        err.code = 231003  # type: ignore[attr-defined]
        assert extract_api_code(err) == 231003


# ===========================================================================
# is_terminal_api_code
# ===========================================================================

class TestIsTerminalApiCode:
    def test_code_231003(self) -> None:
        assert is_terminal_api_code(231003) is True

    def test_msg_not_found(self) -> None:
        assert is_terminal_api_code(MSG_NOT_FOUND) is True

    def test_code_230011(self) -> None:
        assert is_terminal_api_code(230011) is True

    def test_unknown_code(self) -> None:
        assert is_terminal_api_code(999999) is False

    def test_none_returns_false(self) -> None:
        assert is_terminal_api_code(None) is False

    def test_zero_returns_false(self) -> None:
        assert is_terminal_api_code(0) is False


# ===========================================================================
# UnavailableGuard
# ===========================================================================

class TestUnavailableGuardShouldSkip:
    def _make_guard(
        self,
        reply_to_message_id: str | None = "msg_reply",
        card_msg_id: str | None = "msg_card",
    ) -> UnavailableGuard:
        return UnavailableGuard(
            reply_to_message_id=reply_to_message_id,
            get_card_message_id=lambda: card_msg_id,
            on_terminate=MagicMock(),
        )

    def test_returns_true_when_terminated(self) -> None:
        guard = self._make_guard()
        guard._terminated = True
        assert guard.should_skip("test") is True

    def test_returns_false_when_reply_to_message_id_is_none(self) -> None:
        guard = self._make_guard(reply_to_message_id=None)
        assert guard.should_skip("test") is False

    def test_returns_false_when_reply_to_message_id_is_empty(self) -> None:
        guard = self._make_guard(reply_to_message_id="")
        assert guard.should_skip("test") is False

    def test_returns_true_and_terminates_when_message_unavailable(self) -> None:
        """When reply_to_message_id is marked unavailable, should_skip should
        call terminate and return True."""
        guard = self._make_guard()
        mark_unavailable("msg_reply", 231003)
        result = guard.should_skip("test_source")
        assert result is True
        assert guard._terminated is True

    def test_on_terminate_called_when_skip_terminates(self) -> None:
        on_terminate = MagicMock()
        guard = UnavailableGuard(
            reply_to_message_id="msg_reply",
            get_card_message_id=lambda: None,
            on_terminate=on_terminate,
        )
        mark_unavailable("msg_reply", 231003)
        guard.should_skip("test_source")
        on_terminate.assert_called_once()

    def test_returns_false_when_message_still_available(self) -> None:
        guard = self._make_guard()
        assert guard.should_skip("test") is False


class TestUnavailableGuardTerminate:
    def _make_guard(
        self,
        reply_to_message_id: str | None = "msg_reply",
        card_msg_id: str | None = "msg_card",
        on_terminate: MagicMock | None = None,
    ) -> UnavailableGuard:
        if on_terminate is None:
            on_terminate = MagicMock()
        return UnavailableGuard(
            reply_to_message_id=reply_to_message_id,
            get_card_message_id=lambda: card_msg_id,
            on_terminate=on_terminate,
        )

    def test_returns_true_on_terminal_code(self) -> None:
        guard = self._make_guard()
        err = RuntimeError("deleted")
        err.code = 231003  # type: ignore[attr-defined]
        assert guard.terminate("test", err=err) is True

    def test_marks_terminated_on_terminal_code(self) -> None:
        guard = self._make_guard()
        err = RuntimeError("deleted")
        err.code = 231003  # type: ignore[attr-defined]
        guard.terminate("test", err=err)
        assert guard._terminated is True

    def test_calls_on_terminate_on_terminal_code(self) -> None:
        on_terminate = MagicMock()
        guard = self._make_guard(on_terminate=on_terminate)
        err = RuntimeError("deleted")
        err.code = 231003  # type: ignore[attr-defined]
        guard.terminate("test", err=err)
        on_terminate.assert_called_once()

    def test_returns_true_when_already_terminated(self) -> None:
        guard = self._make_guard()
        guard._terminated = True
        assert guard.terminate("test") is True

    def test_on_terminate_not_called_when_already_terminated(self) -> None:
        on_terminate = MagicMock()
        guard = self._make_guard(on_terminate=on_terminate)
        guard._terminated = True
        guard.terminate("test")
        on_terminate.assert_not_called()

    def test_returns_false_on_non_terminal_code(self) -> None:
        guard = self._make_guard()
        err = RuntimeError("rate limited")
        err.code = 230020  # type: ignore[attr-defined]  # non-terminal code
        assert guard.terminate("test", err=err) is False

    def test_returns_false_when_no_error_and_not_in_cache(self) -> None:
        guard = self._make_guard()
        assert guard.terminate("test", err=None) is False

    def test_returns_false_when_error_has_no_parseable_code(self) -> None:
        guard = self._make_guard()
        err = RuntimeError("generic error without code")
        assert guard.terminate("test", err=err) is False

    def test_terminate_with_error_extracts_code_and_terminates(self) -> None:
        """terminate with exception containing terminal code in args string."""
        guard = self._make_guard()
        err = RuntimeError("request failed: code=231003, msg=deleted")
        assert guard.terminate("test", err=err) is True
        assert guard._terminated is True

    def test_marks_reply_to_message_id_as_unavailable(self) -> None:
        guard = self._make_guard(reply_to_message_id="msg_reply", card_msg_id=None)
        err = RuntimeError("deleted")
        err.code = 231003  # type: ignore[attr-defined]
        guard.terminate("test", err=err)
        assert is_unavailable("msg_reply") is True

    def test_marks_card_msg_id_as_unavailable(self) -> None:
        guard = self._make_guard(reply_to_message_id="msg_reply", card_msg_id="msg_card")
        err = RuntimeError("deleted")
        err.code = 231003  # type: ignore[attr-defined]
        guard.terminate("test", err=err)
        assert is_unavailable("msg_card") is True

    def test_marks_both_ids_as_unavailable(self) -> None:
        """When both reply_to_message_id and card_msg_id exist, both are marked."""
        guard = self._make_guard(reply_to_message_id="msg_reply", card_msg_id="msg_card")
        err = RuntimeError("recalled")
        err.code = 230011  # type: ignore[attr-defined]
        guard.terminate("test", err=err)
        assert is_unavailable("msg_reply") is True
        assert is_unavailable("msg_card") is True

    def test_no_reply_id_marks_card_msg_id(self) -> None:
        """When reply_to_message_id is None, card_msg_id should still be marked."""
        guard = self._make_guard(reply_to_message_id=None, card_msg_id="msg_card")
        err = RuntimeError("deleted")
        err.code = 231003  # type: ignore[attr-defined]
        guard.terminate("test", err=err)
        assert is_unavailable("msg_card") is True

    def test_terminal_code_from_cache_when_no_error(self) -> None:
        """When err is None but message is already in unavailable cache with a
        terminal code, terminate should still succeed."""
        guard = self._make_guard(reply_to_message_id="msg_reply", card_msg_id=None)
        mark_unavailable("msg_reply", 231003)
        assert guard.terminate("test", err=None) is True
        assert guard._terminated is True

    def test_terminal_code_from_card_cache_when_no_error(self) -> None:
        """When err is None but card_msg_id is in cache with terminal code."""
        guard = self._make_guard(reply_to_message_id="msg_reply", card_msg_id="msg_card")
        mark_unavailable("msg_card", MSG_NOT_FOUND)
        assert guard.terminate("test", err=None) is True
        assert guard._terminated is True

    def test_non_terminal_code_in_cache_does_not_terminate(self) -> None:
        """If the cached code is non-terminal, terminate should return False."""
        guard = self._make_guard(reply_to_message_id="msg_reply", card_msg_id=None)
        mark_unavailable("msg_reply", 230020)  # non-terminal
        assert guard.terminate("test", err=None) is False

    def test_guard_not_terminated_on_non_terminal(self) -> None:
        guard = self._make_guard()
        err = RuntimeError("rate limited")
        err.code = 230020  # type: ignore[attr-defined]
        guard.terminate("test", err=err)
        assert guard._terminated is False
