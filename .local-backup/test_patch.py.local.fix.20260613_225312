"""patch.py 测试 — 运行时 Hook 函数单元测试."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hermes_lark_streaming.patching.hooks import (
    _safe_hook,
    on_answer_delta,
    on_background_review_message,
    on_cron_deliver,
    on_feishu_normalize,
    on_message_aborted,
    on_message_completed,
    on_message_interrupted,
    on_message_started,
    on_reasoning_delta,
    on_thinking_delta,
    on_tool_updated,
)


# ── Helpers ──


def _make_ctrl(*, enabled: bool = True) -> MagicMock:
    """Create a mock controller with .enabled set."""
    ctrl = MagicMock()
    ctrl.enabled = enabled
    return ctrl


# ── _safe_hook decorator ──


class TestSafeHook:
    """_safe_hook decorator: enabled check, exception handling, log_level."""

    def test_returns_default_return_when_disabled(self) -> None:
        """When ctrl.enabled is False, return default_return immediately."""
        ctrl = _make_ctrl(enabled=False)

        @_safe_hook(default_return=42)
        def my_hook(*, ctrl: Any, message_id: str, **kwargs: Any) -> Any:
            raise AssertionError("should not be called")

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            result = my_hook(message_id="m1")

        assert result == 42

    def test_returns_default_return_on_exception(self) -> None:
        """When the wrapped function raises, return default_return."""
        ctrl = _make_ctrl(enabled=True)

        @_safe_hook(default_return="fallback")
        def my_hook(*, ctrl: Any, message_id: str, **kwargs: Any) -> Any:
            raise RuntimeError("boom")

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            result = my_hook(message_id="m1")

        assert result == "fallback"

    def test_calls_wrapped_function_when_enabled(self) -> None:
        """When ctrl.enabled is True, the wrapped function is called."""
        ctrl = _make_ctrl(enabled=True)
        called_with: dict[str, Any] = {}

        @_safe_hook(default_return=None)
        def my_hook(*, ctrl: Any, message_id: str, **kwargs: Any) -> Any:
            called_with["ctrl"] = ctrl
            called_with["message_id"] = message_id
            called_with["kwargs"] = kwargs
            return "ok"

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            result = my_hook(message_id="m1", extra="val")

        assert result == "ok"
        assert called_with["ctrl"] is ctrl
        assert called_with["message_id"] == "m1"
        assert called_with["kwargs"] == {"extra": "val"}

    def test_default_return_is_none_when_not_specified(self) -> None:
        """default_return defaults to None."""
        ctrl = _make_ctrl(enabled=False)

        @_safe_hook()
        def my_hook(*, ctrl: Any, message_id: str, **kwargs: Any) -> Any:
            return "never"

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            result = my_hook(message_id="m1")

        assert result is None

    def test_uses_correct_log_level_warning(self) -> None:
        """Default log_level='warning' logs via _logger.warning."""
        ctrl = _make_ctrl(enabled=True)

        @_safe_hook(default_return=None, log_level="warning")
        def my_hook(*, ctrl: Any, message_id: str, **kwargs: Any) -> Any:
            raise RuntimeError("test error")

        with (
            patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl),
            patch("hermes_lark_streaming.patching.hooks._logger") as mock_logger,
        ):
            my_hook(message_id="m1")

        mock_logger.warning.assert_called_once()
        mock_logger.debug.assert_not_called()

    def test_uses_correct_log_level_debug(self) -> None:
        """log_level='debug' logs via _logger.debug."""
        ctrl = _make_ctrl(enabled=True)

        @_safe_hook(default_return=False, log_level="debug")
        def my_hook(*, ctrl: Any, message_id: str, **kwargs: Any) -> Any:
            raise RuntimeError("test error")

        with (
            patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl),
            patch("hermes_lark_streaming.patching.hooks._logger") as mock_logger,
        ):
            my_hook(message_id="m1")

        mock_logger.debug.assert_called_once()
        mock_logger.warning.assert_not_called()

    def test_wrapper_requires_message_id_keyword(self) -> None:
        """The wrapper enforces keyword-only message_id."""
        ctrl = _make_ctrl(enabled=True)

        @_safe_hook()
        def my_hook(*, ctrl: Any, message_id: str, **kwargs: Any) -> Any:
            return None

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            with pytest.raises(TypeError):
                my_hook("m1")  # type: ignore[arg-type]

    def test_exception_logging_includes_exc_info(self) -> None:
        """Exception is logged with exc_info=True."""
        ctrl = _make_ctrl(enabled=True)

        @_safe_hook(default_return=None, log_level="warning")
        def my_hook(*, ctrl: Any, message_id: str, **kwargs: Any) -> Any:
            raise ValueError("bad")

        with (
            patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl),
            patch("hermes_lark_streaming.patching.hooks._logger") as mock_logger,
        ):
            my_hook(message_id="m1")

        call_args = mock_logger.warning.call_args
        assert call_args[1].get("exc_info") is True


# ── on_message_started ──


class TestOnMessageStarted:
    """on_message_started delegates to ctrl.on_message_started."""

    def test_delegates_with_correct_params(self) -> None:
        ctrl = _make_ctrl(enabled=True)

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            on_message_started(message_id="m1", chat_id="c1", anchor_id="a1")

        ctrl.on_message_started.assert_called_once_with(
            message_id="m1", chat_id="c1", anchor_id="a1"
        )

    def test_delegates_without_anchor_id(self) -> None:
        ctrl = _make_ctrl(enabled=True)

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            on_message_started(message_id="m1", chat_id="c1")

        ctrl.on_message_started.assert_called_once_with(
            message_id="m1", chat_id="c1", anchor_id=None
        )

    def test_returns_none_when_disabled(self) -> None:
        ctrl = _make_ctrl(enabled=False)

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            result = on_message_started(message_id="m1", chat_id="c1")

        assert result is None
        ctrl.on_message_started.assert_not_called()


# ── on_message_completed ──


class TestOnMessageCompleted:
    """on_message_completed delegates to ctrl.on_completed and returns bool()."""

    def test_delegates_with_all_params(self) -> None:
        ctrl = _make_ctrl(enabled=True)
        ctrl.on_completed.return_value = True

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            result = on_message_completed(
                message_id="m1",
                answer="hello",
                duration=1.5,
                model="gpt-4",
                tokens={"input_tokens": 10, "output_tokens": 20},
                context={"used_tokens": 5, "max_tokens": 100},
                api_calls=3,
                history_offset=2,
                compression_exhausted=True,
                aborted=True,
                error_message="timeout",
                reasoning_tokens=100,
                estimated_cost_usd=0.05,
                cost_status="estimated",
            )

        assert result is True
        ctrl.on_completed.assert_called_once_with(
            message_id="m1",
            answer="hello",
            duration=1.5,
            model="gpt-4",
            tokens={"input_tokens": 10, "output_tokens": 20},
            context={"used_tokens": 5, "max_tokens": 100},
            api_calls=3,
            history_offset=2,
            compression_exhausted=True,
            aborted=True,
            error_message="timeout",
            reasoning_tokens=100,
            estimated_cost_usd=0.05,
            cost_status="estimated",
        )

    def test_returns_bool_of_result(self) -> None:
        ctrl = _make_ctrl(enabled=True)
        ctrl.on_completed.return_value = "truthy"

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            result = on_message_completed(message_id="m1")

        assert result is True  # bool("truthy") == True

    def test_returns_bool_false_on_falsy_result(self) -> None:
        ctrl = _make_ctrl(enabled=True)
        ctrl.on_completed.return_value = 0

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            result = on_message_completed(message_id="m1")

        assert result is False  # bool(0) == False

    def test_returns_false_when_disabled(self) -> None:
        ctrl = _make_ctrl(enabled=False)

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            result = on_message_completed(message_id="m1")

        assert result is False
        ctrl.on_completed.assert_not_called()

    def test_new_params_default_values(self) -> None:
        """compression_exhausted, aborted, error_message have correct defaults."""
        ctrl = _make_ctrl(enabled=True)
        ctrl.on_completed.return_value = True

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            on_message_completed(message_id="m1")

        call_kwargs = ctrl.on_completed.call_args[1]
        assert call_kwargs["compression_exhausted"] is False
        assert call_kwargs["aborted"] is False
        assert call_kwargs["error_message"] == ""


# ── on_tool_updated ──


class TestOnToolUpdated:
    """on_tool_updated delegates to ctrl.on_tool_update."""

    def test_delegates_with_correct_params(self) -> None:
        ctrl = _make_ctrl(enabled=True)

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            result = on_tool_updated(
                message_id="m1", tool_name="read", status="started", detail="file.py"
            )

        ctrl.on_tool_update.assert_called_once_with(
            message_id="m1", tool_name="read", status="started", detail="file.py"
        )
        assert result is True

    def test_returns_false_when_disabled(self) -> None:
        ctrl = _make_ctrl(enabled=False)

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            result = on_tool_updated(message_id="m1", tool_name="read", status="started")

        assert result is False
        ctrl.on_tool_update.assert_not_called()


# ── on_answer_delta ──


class TestOnAnswerDelta:
    """on_answer_delta delegates to ctrl.on_answer."""

    def test_delegates_with_correct_params(self) -> None:
        ctrl = _make_ctrl(enabled=True)

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            result = on_answer_delta(message_id="m1", text="hello world")

        ctrl.on_answer.assert_called_once_with(message_id="m1", text="hello world")
        assert result is True

    def test_returns_false_when_disabled(self) -> None:
        ctrl = _make_ctrl(enabled=False)

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            result = on_answer_delta(message_id="m1", text="hello")

        assert result is False
        ctrl.on_answer.assert_not_called()


# ── on_thinking_delta ──


class TestOnThinkingDelta:
    """on_thinking_delta delegates to ctrl.on_thinking."""

    def test_delegates_with_correct_params(self) -> None:
        ctrl = _make_ctrl(enabled=True)

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            result = on_thinking_delta(message_id="m1", text="thinking...")

        ctrl.on_thinking.assert_called_once_with(message_id="m1", text="thinking...")
        assert result is True

    def test_returns_false_when_disabled(self) -> None:
        ctrl = _make_ctrl(enabled=False)

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            result = on_thinking_delta(message_id="m1", text="thinking...")

        assert result is False
        ctrl.on_thinking.assert_not_called()


# ── on_reasoning_delta ──


class TestOnReasoningDelta:
    """on_reasoning_delta delegates to ctrl.on_reasoning."""

    def test_delegates_with_correct_params(self) -> None:
        ctrl = _make_ctrl(enabled=True)

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            result = on_reasoning_delta(message_id="m1", text="reasoning step")

        ctrl.on_reasoning.assert_called_once_with(message_id="m1", text="reasoning step")
        assert result is True

    def test_returns_false_when_disabled(self) -> None:
        ctrl = _make_ctrl(enabled=False)

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            result = on_reasoning_delta(message_id="m1", text="reasoning")

        assert result is False
        ctrl.on_reasoning.assert_not_called()


# ── on_background_review_message ──


class TestOnBackgroundReviewMessage:
    """on_background_review_message delegates to ctrl.defer_background_review."""

    def test_delegates_and_returns_deferred_bool(self) -> None:
        ctrl = _make_ctrl(enabled=True)
        ctrl.defer_background_review.return_value = True
        sender = MagicMock()

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            result = on_background_review_message(
                message_id="m1", text="review text", sender=sender
            )

        ctrl.defer_background_review.assert_called_once_with(
            message_id="m1", text="review text", sender=sender
        )
        assert result is True

    def test_returns_false_when_not_deferred(self) -> None:
        ctrl = _make_ctrl(enabled=True)
        ctrl.defer_background_review.return_value = False
        sender = MagicMock()

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            result = on_background_review_message(
                message_id="m1", text="review", sender=sender
            )

        assert result is False

    def test_returns_false_when_disabled(self) -> None:
        ctrl = _make_ctrl(enabled=False)

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            result = on_background_review_message(
                message_id="m1", text="review", sender=MagicMock()
            )

        assert result is False
        ctrl.defer_background_review.assert_not_called()


# ── on_message_aborted ──


class TestOnMessageAborted:
    """on_message_aborted delegates to ctrl.on_aborted."""

    def test_delegates_with_correct_params(self) -> None:
        ctrl = _make_ctrl(enabled=True)

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            on_message_aborted(message_id="m1")

        ctrl.on_aborted.assert_called_once_with(message_id="m1")

    def test_returns_none_when_disabled(self) -> None:
        ctrl = _make_ctrl(enabled=False)

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            result = on_message_aborted(message_id="m1")

        assert result is None
        ctrl.on_aborted.assert_not_called()


# ── on_message_interrupted ──


class TestOnMessageInterrupted:
    """on_message_interrupted delegates to ctrl.on_interrupted with old/new mapping."""

    def test_delegates_with_correct_param_mapping(self) -> None:
        """message_id -> old_message_id, new_message_id stays as is."""
        ctrl = _make_ctrl(enabled=True)

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            on_message_interrupted(
                message_id="old_msg",
                new_message_id="new_msg",
                chat_id="c1",
                anchor_id="a1",
            )

        ctrl.on_interrupted.assert_called_once_with(
            old_message_id="old_msg",
            new_message_id="new_msg",
            chat_id="c1",
            anchor_id="a1",
        )

    def test_delegates_without_anchor_id(self) -> None:
        ctrl = _make_ctrl(enabled=True)

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            on_message_interrupted(
                message_id="old_msg",
                new_message_id="new_msg",
                chat_id="c1",
            )

        ctrl.on_interrupted.assert_called_once_with(
            old_message_id="old_msg",
            new_message_id="new_msg",
            chat_id="c1",
            anchor_id=None,
        )

    def test_returns_none_when_disabled(self) -> None:
        ctrl = _make_ctrl(enabled=False)

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            result = on_message_interrupted(
                message_id="old_msg",
                new_message_id="new_msg",
                chat_id="c1",
            )

        assert result is None
        ctrl.on_interrupted.assert_not_called()


# ── on_cron_deliver (async) ──


class TestOnCronDeliver:
    """on_cron_deliver: async, loop check, enabled check, exception handling."""

    @pytest.mark.asyncio
    async def test_returns_false_when_loop_is_none(self) -> None:
        """When loop is None, return False immediately."""
        result = await on_cron_deliver(chat_id="c1", content="hello", loop=None)
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_controller_disabled(self) -> None:
        ctrl = _make_ctrl(enabled=False)

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            result = await on_cron_deliver(
                chat_id="c1", content="hello", loop=MagicMock()
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_returns_true_on_successful_call(self) -> None:
        ctrl = _make_ctrl(enabled=True)
        ctrl.on_cron_deliver_async = AsyncMock(return_value=True)

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            result = await on_cron_deliver(
                chat_id="c1", content="hello", loop=MagicMock()
            )

        assert result is True
        ctrl.on_cron_deliver_async.assert_called_once_with(
            chat_id="c1", content="hello", loop=ctrl.on_cron_deliver_async.call_args[1]["loop"]
        )

    @pytest.mark.asyncio
    async def test_returns_bool_of_result(self) -> None:
        ctrl = _make_ctrl(enabled=True)
        ctrl.on_cron_deliver_async = AsyncMock(return_value="truthy_value")

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            result = await on_cron_deliver(
                chat_id="c1", content="hello", loop=MagicMock()
            )

        assert result is True  # bool("truthy_value") is True

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self) -> None:
        ctrl = _make_ctrl(enabled=True)
        ctrl.on_cron_deliver_async = AsyncMock(side_effect=RuntimeError("fail"))

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            result = await on_cron_deliver(
                chat_id="c1", content="hello", loop=MagicMock()
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_is_async_function(self) -> None:
        """on_cron_deliver should be an async function (uses await)."""
        import inspect

        assert inspect.iscoroutinefunction(on_cron_deliver)

    @pytest.mark.asyncio
    async def test_passes_loop_to_on_cron_deliver_async(self) -> None:
        ctrl = _make_ctrl(enabled=True)
        ctrl.on_cron_deliver_async = AsyncMock(return_value=True)
        loop = MagicMock()

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            await on_cron_deliver(chat_id="c1", content="hello", loop=loop)

        ctrl.on_cron_deliver_async.assert_called_once_with(
            chat_id="c1", content="hello", loop=loop
        )


# ── on_feishu_normalize ──


class TestOnFeishuNormalize:
    """on_feishu_normalize: manual ctrl.enabled check, platform filter, thread_id clearing."""

    def _make_source(self, platform_value: str = "feishu", thread_id: str | None = "bad_thread") -> SimpleNamespace:
        """Create a mock source with platform.value and thread_id."""
        platform = SimpleNamespace(value=platform_value)
        source = SimpleNamespace(platform=platform, thread_id=thread_id)
        return source

    def _make_event(
        self,
        reply_to: str | None = None,
        raw_message: Any = None,
    ) -> SimpleNamespace:
        """Create a mock event with reply_to_message_id and raw_message."""
        return SimpleNamespace(
            reply_to_message_id=reply_to,
            raw_message=raw_message,
        )

    def test_clears_thread_id_when_reply_to_and_source_thread_id_no_real_thread_id(self) -> None:
        """Core fix: clear source.thread_id when reply_to + source_thread_id but no real_thread_id."""
        ctrl = _make_ctrl(enabled=True)
        source = self._make_source(thread_id="fake_thread")
        event = self._make_event(
            reply_to="reply_msg_123",
            raw_message={"event": {"message": {}}},  # no thread_id
        )

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            on_feishu_normalize(
                message_id="m1",
                source=source,
                event=event,
                reply_anchor_id="a1",
            )

        assert source.thread_id is None
        assert event.source is source

    def test_does_not_clear_thread_id_when_real_thread_id_exists(self) -> None:
        """When raw_message has a real thread_id, source.thread_id should remain."""
        ctrl = _make_ctrl(enabled=True)
        source = self._make_source(thread_id="thread_from_reply")
        event = self._make_event(
            reply_to="reply_msg_123",
            raw_message={"event": {"message": {"thread_id": "real_thread"}}},
        )

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            on_feishu_normalize(
                message_id="m1",
                source=source,
                event=event,
            )

        assert source.thread_id == "thread_from_reply"

    def test_does_not_clear_thread_id_when_no_reply_to(self) -> None:
        """When no reply_to, don't clear thread_id even if source_thread_id exists."""
        ctrl = _make_ctrl(enabled=True)
        source = self._make_source(thread_id="some_thread")
        event = self._make_event(
            reply_to=None,
            raw_message={},
        )

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            on_feishu_normalize(
                message_id="m1",
                source=source,
                event=event,
            )

        assert source.thread_id == "some_thread"

    def test_does_not_clear_thread_id_when_no_source_thread_id(self) -> None:
        """When source has no thread_id, no clearing needed."""
        ctrl = _make_ctrl(enabled=True)
        source = self._make_source(thread_id=None)
        event = self._make_event(
            reply_to="reply_msg",
            raw_message={},
        )

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            on_feishu_normalize(
                message_id="m1",
                source=source,
                event=event,
            )

        assert source.thread_id is None

    def test_skips_non_feishu_platforms(self) -> None:
        """Non-feishu platforms should be skipped entirely."""
        ctrl = _make_ctrl(enabled=True)
        source = self._make_source(platform_value="slack", thread_id="bad_thread")
        event = self._make_event(
            reply_to="reply_msg",
            raw_message={},
        )

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            on_feishu_normalize(
                message_id="m1",
                source=source,
                event=event,
            )

        # thread_id should not have been cleared
        assert source.thread_id == "bad_thread"

    def test_returns_early_when_disabled(self) -> None:
        """When ctrl.enabled is False, do nothing."""
        ctrl = _make_ctrl(enabled=False)
        source = self._make_source(thread_id="bad_thread")
        event = self._make_event(
            reply_to="reply_msg",
            raw_message={},
        )

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            on_feishu_normalize(
                message_id="m1",
                source=source,
                event=event,
            )

        assert source.thread_id == "bad_thread"

    def test_raw_message_as_dict_with_event_key(self) -> None:
        """raw_message is dict with event.message containing no thread_id."""
        ctrl = _make_ctrl(enabled=True)
        source = self._make_source(thread_id="bad_thread")
        event = self._make_event(
            reply_to="reply_msg",
            raw_message={"event": {"message": {"content": "hi"}}},
        )

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            on_feishu_normalize(message_id="m1", source=source, event=event)

        assert source.thread_id is None

    def test_raw_message_as_object(self) -> None:
        """raw_message is an object with .event.message."""
        ctrl = _make_ctrl(enabled=True)
        source = self._make_source(thread_id="bad_thread")

        msg_obj = SimpleNamespace(thread_id=None)
        event_obj = SimpleNamespace(message=msg_obj)
        raw = SimpleNamespace(event=event_obj)
        event = self._make_event(reply_to="reply_msg", raw_message=raw)

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            on_feishu_normalize(message_id="m1", source=source, event=event)

        assert source.thread_id is None

    def test_raw_message_dict_without_event_key(self) -> None:
        """raw_message is dict without 'event' key — falls back to raw.get('message')."""
        ctrl = _make_ctrl(enabled=True)
        source = self._make_source(thread_id="bad_thread")
        event = self._make_event(
            reply_to="reply_msg",
            raw_message={"message": {}},
        )

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            on_feishu_normalize(message_id="m1", source=source, event=event)

        assert source.thread_id is None

    def test_raw_message_as_object_without_event(self) -> None:
        """raw_event is not a dict, falls back to getattr(raw_event, 'message', None)."""
        ctrl = _make_ctrl(enabled=True)
        source = self._make_source(thread_id="bad_thread")

        msg_obj = SimpleNamespace(thread_id=None)
        event_obj = SimpleNamespace(message=msg_obj)
        raw = SimpleNamespace(event=event_obj)
        event = self._make_event(reply_to="reply_msg", raw_message=raw)

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            on_feishu_normalize(message_id="m1", source=source, event=event)

        assert source.thread_id is None

    def test_raw_message_is_none(self) -> None:
        """When raw_message is None, real_thread_id is None — should still work."""
        ctrl = _make_ctrl(enabled=True)
        source = self._make_source(thread_id="bad_thread")
        event = self._make_event(reply_to="reply_msg", raw_message=None)

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            on_feishu_normalize(message_id="m1", source=source, event=event)

        assert source.thread_id is None

    def test_raw_message_as_object_with_thread_id_attr(self) -> None:
        """raw_message is an object with .thread_id attribute."""
        ctrl = _make_ctrl(enabled=True)
        source = self._make_source(thread_id="bad_thread")

        msg_obj = SimpleNamespace(thread_id="real_thread")
        event_obj = SimpleNamespace(message=msg_obj)
        raw = SimpleNamespace(event=event_obj)
        event = self._make_event(reply_to="reply_msg", raw_message=raw)

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            on_feishu_normalize(message_id="m1", source=source, event=event)

        # real_thread_id is "real_thread", so source.thread_id should NOT be cleared
        assert source.thread_id == "bad_thread"

    def test_raw_event_is_not_dict_uses_getattr(self) -> None:
        """When raw_event is not a dict but has .message attribute."""
        ctrl = _make_ctrl(enabled=True)
        source = self._make_source(thread_id="bad_thread")

        msg_obj = SimpleNamespace(thread_id=None)
        raw_event_obj = SimpleNamespace(message=msg_obj)
        raw = SimpleNamespace(event=raw_event_obj)
        event = self._make_event(reply_to="reply_msg", raw_message=raw)

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            on_feishu_normalize(message_id="m1", source=source, event=event)

        assert source.thread_id is None

    def test_no_raw_event_falls_back_to_raw_message_key(self) -> None:
        """When raw_event is None, try raw.get('message')."""
        ctrl = _make_ctrl(enabled=True)
        source = self._make_source(thread_id="bad_thread")
        event = self._make_event(
            reply_to="reply_msg",
            raw_message={"message": {"thread_id": "real_thread"}},
        )

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            on_feishu_normalize(message_id="m1", source=source, event=event)

        # real_thread_id exists, so should NOT clear
        assert source.thread_id == "bad_thread"

    def test_platform_without_value_attr(self) -> None:
        """Platform is a string without .value attribute."""
        ctrl = _make_ctrl(enabled=True)
        # platform is just a string "feishu" — but source.platform.value would fail
        # The code does: getattr(getattr(source, "platform", None), "value", "")
        # So platform="feishu" -> getattr("feishu", "value", "") -> ""
        source = SimpleNamespace(platform="feishu", thread_id="bad_thread")
        event = self._make_event(reply_to="reply_msg", raw_message={})

        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            on_feishu_normalize(message_id="m1", source=source, event=event)

        # platform_value will be "" (string has no .value), so it won't match "feishu"
        assert source.thread_id == "bad_thread"
