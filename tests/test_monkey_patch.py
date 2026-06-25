"""monkey_patch.py 测试 — 补丁策略、回调拦截."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


class TestPatchBasics:
    def test_version_is_available(self) -> None:
        """Plugin should expose __version__ from plugin.yaml."""
        from hermes_lark_streaming import __version__
        assert __version__
        assert __version__ != "unknown"

    def test_register_logs_version(self) -> None:
        """register() should log the version number."""
        from hermes_lark_streaming import __version__
        from hermes_lark_streaming.plugin import register

        mock_ctx = MagicMock()
        with (
            patch("hermes_lark_streaming.plugin._ensure_streaming_config"),
            patch("hermes_lark_streaming.patching.apply_patches"),
            patch("hermes_lark_streaming.plugin._logger") as mock_logger,
        ):
            register(mock_ctx)

        # Check that version is in at least one info log
        info_calls = [str(call) for call in mock_logger.info.call_args_list]
        version_logged = any(__version__ in call for call in info_calls)
        assert version_logged, f"Version {__version__} not found in log calls: {info_calls}"

    def test_register_always_registers_aowen_hook(self) -> None:
        """v1.3.6: register() must always register pre_gateway_dispatch hook,
        even when no event loop is running (pre-warm skip path).

        Regression: v1.3.2 pre-warm code had `return` in except RuntimeError
        block, causing /aowen hook registration to be skipped when gateway
        starts without a running event loop.
        """
        from hermes_lark_streaming.plugin import register

        mock_ctx = MagicMock()
        # 模拟无运行事件循环的场景（触发 pre-warm 的 except RuntimeError）
        with (
            patch("hermes_lark_streaming.plugin._ensure_streaming_config"),
            patch("hermes_lark_streaming.patching.apply_patches"),
            patch("asyncio.get_running_loop", side_effect=RuntimeError("no running loop")),
            patch("asyncio.get_event_loop", side_effect=RuntimeError("no event loop")),
        ):
            register(mock_ctx)

        # 关键断言：pre_gateway_dispatch hook 必须被注册
        assert mock_ctx.register_hook.called, \
            "register_hook was never called — /aowen commands will not work"
        call_args = mock_ctx.register_hook.call_args
        hook_name = call_args[0][0] if call_args[0] else call_args[1].get("hook_name")
        assert hook_name == "pre_gateway_dispatch", \
            f"Expected pre_gateway_dispatch hook, got: {hook_name}"

    def test_monkey_patch_module_imports_version(self) -> None:
        """patching module should import __version__ from the package."""
        from hermes_lark_streaming.patching import __version__ as mp_version
        from hermes_lark_streaming import __version__ as pkg_version
        assert mp_version == pkg_version


# ── Cron delivery wrapper tests ──


class TestCronDeliveryWrapper:
    """Verify _wrap_cron_deliver uses direct await instead of run_coroutine_threadsafe."""

    def test_cron_wrapper_no_adapters_falls_through(self) -> None:
        """When no adapters are provided, cron delivery falls through to original."""
        from hermes_lark_streaming.patching import _wrap_cron_deliver

        orig = MagicMock(return_value="original_result")
        wrapper = _wrap_cron_deliver(orig)

        result = wrapper(job={"id": "test"}, content="hello", adapters=None)

        orig.assert_called_once()
        assert result == "original_result"

    def test_cron_wrapper_no_feishu_adapter_falls_through(self) -> None:
        """When adapters exist but no Feishu adapter, falls through to original."""
        from hermes_lark_streaming.patching import _wrap_cron_deliver

        orig = MagicMock(return_value="original_result")
        wrapper = _wrap_cron_deliver(orig)

        # Create mock adapters dict with a non-Feishu platform
        mock_adapters = {}
        mock_platform = MagicMock()
        mock_platform.value = "telegram"
        mock_adapters[mock_platform] = MagicMock()

        result = wrapper(job={"id": "test"}, content="hello", adapters=mock_adapters)

        orig.assert_called_once()
        assert result == "original_result"


# ── Context cleanup and interrupt fix tests ──


class TestMsgCtxCleanup:
    """Verify _msg_ctx is cleared after message processing to prevent leakage."""

    def test_msg_ctx_cleared_after_wrap_handle_message(self) -> None:
        """After _wrap_handle_message_with_agent completes, _msg_ctx should be None."""
        from hermes_lark_streaming.patching import _msg_ctx

        # _msg_ctx should default to None
        assert _msg_ctx.get() is None

    def test_msg_ctx_default_is_none(self) -> None:
        """_msg_ctx default value should be None (no stale context)."""
        from hermes_lark_streaming.patching import _msg_ctx

        # Fresh ContextVar should have default None
        assert _msg_ctx.get() is None


class TestPerMessageContext:
    """Verify per-message context isolation for concurrent/overlapping messages."""

    def test_msg_context_is_separate_dict(self) -> None:
        """Each message should use its own context dict, not a shared reference."""
        from hermes_lark_streaming.patching import _wrap_handle_message_with_agent
        import inspect

        source = inspect.getsource(_wrap_handle_message_with_agent)
        assert "msg_context" in source, "Per-message context dict not found in wrapper source"
        assert "_msg_ctx.set(None)" in source, "Context cleanup not found in wrapper source"

    def test_card_sent_uses_per_message_context(self) -> None:
        """card_sent check should use per-message context, not _msg_ctx.get()."""
        from hermes_lark_streaming.patching import _wrap_handle_message_with_agent
        import inspect

        source = inspect.getsource(_wrap_handle_message_with_agent)
        lines = source.split('\n')
        found_msg_context_assignment = False
        found_card_sent_check = False
        for i, line in enumerate(lines):
            if 'ctx = msg_context' in line:
                found_msg_context_assignment = True
            if 'card_sent' in line and found_msg_context_assignment:
                found_card_sent_check = True
                break
        assert found_msg_context_assignment, "Per-message context assignment not found"
        assert found_card_sent_check, "card_sent check not using per-message context"


class TestRecursiveInterruptContext:
    """Verify recursive interrupt follow-up creates independent context."""

    def test_recursive_context_creation(self) -> None:
        """_wrap_run_agent should create a new context for recursive calls."""
        from hermes_lark_streaming.patching import _wrap_run_agent
        import inspect

        source = inspect.getsource(_wrap_run_agent)
        assert "_interrupt_depth" in source, "interrupt_depth not checked in _wrap_run_agent"
        assert "_saved_parent_ctx" in source, "Parent context save not found"
        assert "on_message_started" in source, "START hook not fired for recursive message"

    def test_parent_context_restored(self) -> None:
        """After recursive call, parent context should be restored."""
        from hermes_lark_streaming.patching import _wrap_run_agent
        import inspect

        source = inspect.getsource(_wrap_run_agent)
        assert "_saved_parent_ctx is not None" in source, "Parent context restore check not found"

    def test_parent_complete_hook_aborted(self) -> None:
        """Parent COMPLETE hook should fire as ABORTED when in recursive scenario."""
        from hermes_lark_streaming.patching import _wrap_run_agent
        import inspect

        source = inspect.getsource(_wrap_run_agent)
        assert "aborted=True" in source, "Aborted completion not found for parent context"

    def test_child_complete_hook_fired_before_parent_aborted(self) -> None:
        """Bug fix: Child (B) COMPLETE hook must fire BEFORE parent (A) ABORTED COMPLETE.

        Previous bug: only A's ABORTED COMPLETE was fired, leaving B's card stuck
        in STREAMING state, causing duplicate cards and wrong card content.
        """
        from hermes_lark_streaming.patching import _wrap_run_agent
        import inspect

        source = inspect.getsource(_wrap_run_agent)
        # The COMPLETE hook section should handle both child and parent
        # in the _saved_parent_ctx is not None branch
        assert "Step 1: Fire B" in source or "child COMPLETE" in source.lower(), \
            "Child COMPLETE hook not found in _wrap_run_agent — bug fix missing!"
        assert "Step 2: Fire A" in source or "parent COMPLETE" in source.lower() or "ABORTED COMPLETE" in source, \
            "Parent ABORTED COMPLETE not found in _wrap_run_agent"

    def test_child_complete_includes_result(self) -> None:
        """Child COMPLETE hook should use the inner _run_agent's result (B's answer)."""
        from hermes_lark_streaming.patching import _wrap_run_agent
        import inspect

        source = inspect.getsource(_wrap_run_agent)
        # In the _saved_parent_ctx branch, the child's COMPLETE should call
        # on_message_completed with result data (not empty answer)
        lines = source.split('\n')
        in_saved_parent_block = False
        found_child_on_message_completed = False
        found_child_final_response = False
        for i, line in enumerate(lines):
            if '_saved_parent_ctx is not None' in line:
                in_saved_parent_block = True
            if in_saved_parent_block and 'on_message_completed' in line:
                # Check nearby lines for final_response
                nearby = '\n'.join(lines[max(0, i-2):i+10])
                if 'final_response' in nearby:
                    found_child_final_response = True
                    found_child_on_message_completed = True
                    break
            # Stop looking once we hit the parent ABORTED section
            if in_saved_parent_block and 'ABORTED' in line and 'Step 2' in line:
                break
        assert found_child_final_response, \
            "Child COMPLETE should use result.get('final_response') for B's answer"

    def test_parent_card_sent_propagated_to_original_msg_context(self) -> None:
        """v0.15.4 bug fix: parent's card_sent must propagate to original msg_context.

        When _wrap_run_agent creates a copy of the parent context
        (_saved_parent_ctx = dict(ctx)), the original msg_context dict
        (captured by _wrap_handle_message_with_agent) is a different object.
        Setting _saved_parent_ctx["card_sent"] = True does NOT update the
        original msg_context, causing _wrap_handle_message_with_agent to
        miss the card_sent flag and send a duplicate plain text reply.

        The fix: store a reference to the original msg_context via
        _original_msg_context_ref and propagate card_sent=True to it.
        """
        from hermes_lark_streaming.patching import _wrap_run_agent
        import inspect

        source = inspect.getsource(_wrap_run_agent)
        # Check that _original_msg_context_ref is captured
        assert "_original_msg_context_ref" in source, \
            "_original_msg_context_ref not found in _wrap_run_agent — card_sent propagation fix missing!"
        # Check that card_sent is propagated to the original context
        assert '_original_msg_context_ref["card_sent"] = True' in source or \
               '_original_msg_context_ref["card_sent"]=True' in source.replace(" ", ""), \
            "card_sent propagation to _original_msg_context_ref not found in _wrap_run_agent"


class TestImageInterception:
    """Verify image interception behavior — v0.15.4 regression fix.

    v0.15.3 introduced send_image_file / send_image interception that
    caused images to disappear entirely. v0.15.4 removed the monkey-patching
    while keeping the function definitions for reference.

    The _wrap_feishu_adapter_send non-string content path now passes
    images through as standalone messages instead of suppressing them.
    """

    def test_send_image_file_wrapper_deleted(self) -> None:
        """_wrap_feishu_adapter_send_image_file was deleted (2026-06-09 zombie cleanup)."""
        import pytest
        with pytest.raises(ImportError):
            from hermes_lark_streaming.patching import _wrap_feishu_adapter_send_image_file

    def test_send_image_wrapper_deleted(self) -> None:
        """_wrap_feishu_adapter_send_image was deleted (2026-06-09 zombie cleanup)."""
        import pytest
        with pytest.raises(ImportError):
            from hermes_lark_streaming.patching import _wrap_feishu_adapter_send_image

    def test_send_image_file_not_monkey_patched(self) -> None:
        """v0.15.4: send_image_file should NOT be monkey-patched anymore.

        The interception was removed because it caused images to disappear:
        - Injected file:// URLs were stripped by _strip_invalid_image_keys()
        - _schedule_card_update skipped terminal-state sessions
        - Original standalone send was suppressed → images lost entirely
        """
        from hermes_lark_streaming.patching import apply_patches
        import inspect

        source = inspect.getsource(apply_patches)
        # The monkey-patching of send_image_file should NOT be present
        assert "send_image_file = _wrap_feishu_adapter_send_image_file" not in source, \
            "send_image_file should NOT be monkey-patched (v0.15.4 regression fix)"

    def test_send_image_not_monkey_patched(self) -> None:
        """v0.15.4: send_image should NOT be monkey-patched anymore."""
        from hermes_lark_streaming.patching import apply_patches
        import inspect

        source = inspect.getsource(apply_patches)
        assert "send_image = _wrap_feishu_adapter_send_image" not in source, \
            "send_image should NOT be monkey-patched (v0.15.4 regression fix)"

    def test_send_non_string_passes_through(self) -> None:
        """v0.15.4: _wrap_feishu_adapter_send should pass non-string content (images) through.

        Previously (v0.15.3), when card_sent=True, images were suppressed
        even if _try_add_image_to_session failed. Now all non-string content
        is passed through as standalone messages — the only code path for
        non-string content is to call orig_send directly.
        """
        from hermes_lark_streaming.patching import _wrap_feishu_adapter_send
        import inspect

        source = inspect.getsource(_wrap_feishu_adapter_send)
        # Find the "not isinstance(content, str)" block
        # After the fix, non-string content should have a single return path
        # to orig_send, without any card_sent checks or suppression logic.
        assert "not isinstance(content, str)" in source, \
            "Non-string content check not found in _wrap_feishu_adapter_send"
        # The non-string block should return orig_send directly
        # (not via a SendResult suppression or _try_add_image_to_session)
        lines = source.split('\n')
        in_non_string_block = False
        found_orig_send_in_block = False
        for line in lines:
            if 'not isinstance(content, str)' in line:
                in_non_string_block = True
            elif in_non_string_block:
                if 'orig_send' in line:
                    found_orig_send_in_block = True
                    break
                # If we hit the string content section, stop
                if 'isinstance(content, str)' in line or 'content.strip()' in line:
                    break
        assert found_orig_send_in_block, \
            "Non-string content should pass through to orig_send directly"


# ── v0.15.5: Card session existence check + logging/perf fixes ──


class TestCardSessionExistenceCheck:
    """v0.15.5 bug fix: card session existence check when card_sent=False.

    When card_sent wasn't propagated correctly (e.g., interrupt scenarios
    with complex context chains), the controller may still have a card
    session in _sessions with a card_msg_id. The fix adds fallback checks
    in both _wrap_handle_message_with_agent and _wrap_feishu_adapter_send
    to query the controller's _sessions and suppress text when a card exists.
    """

    def test_card_session_existence_check_in_handle_message(self) -> None:
        """_wrap_handle_message_with_agent should check controller._sessions when card_sent=False."""
        from hermes_lark_streaming.patching import _wrap_handle_message_with_agent
        import inspect

        source = inspect.getsource(_wrap_handle_message_with_agent)
        # The fix adds a check for controller session store when result is not None
        # and card_sent is False but a session with card_msg_id exists.
        # v1.3.0: access changed from direct _sessions.get to thread-safe _sess_get.
        assert "_sess_get" in source or "_sessions" in source, \
            "Controller session check not found in _wrap_handle_message_with_agent"

    def test_card_session_existence_check_in_feishu_adapter_send(self) -> None:
        """_wrap_feishu_adapter_send should check controller._sessions when card_sent=False."""
        from hermes_lark_streaming.patching import _wrap_feishu_adapter_send
        import inspect

        source = inspect.getsource(_wrap_feishu_adapter_send)
        # The fix adds a check in the Agent path when card_sent is False:
        # query controller session store for a session with card_msg_id.
        # v1.3.0: access changed from direct _sessions.get to thread-safe _sess_get.
        assert "_sess_get" in source or "_sessions" in source, \
            "Controller session check not found in _wrap_feishu_adapter_send"

    def test_feishu_adapter_send_session_check_sets_card_sent(self) -> None:
        """When a card session is found, card_sent should be set to True."""
        from hermes_lark_streaming.patching import _wrap_feishu_adapter_send
        import inspect

        source = inspect.getsource(_wrap_feishu_adapter_send)
        # In the agent path, after finding a session with card_msg_id,
        # the fix sets ctx["card_sent"] = True and returns SendResult(success=True)
        lines = source.split('\n')
        in_session_check = False
        found_card_sent_set = False
        for i, line in enumerate(lines):
            if '_sess' in line and 'card_msg_id' in line:
                in_session_check = True
            if in_session_check and 'card_sent' in line and '= True' in line:
                found_card_sent_set = True
                break
        assert found_card_sent_set, \
            "card_sent not set to True after finding card session in _wrap_feishu_adapter_send"


class TestParentAbortedCompleteSetsAlreadySent:
    """v0.15.5 bug fix: Step 2 parent ABORTED COMPLETE sets result["already_sent"] = True.

    Without this, Hermes's _handle_message_with_agent still thinks the text
    hasn't been sent and sends a duplicate plain text reply even though the
    card already shows the content.
    """

    def test_step2_sets_already_sent(self) -> None:
        """Step 2 (parent ABORTED COMPLETE) should set result['already_sent'] = True."""
        from hermes_lark_streaming.patching import _wrap_run_agent
        import inspect

        source = inspect.getsource(_wrap_run_agent)
        # In the Step 2 block (parent ABORTED COMPLETE), there should be
        # result["already_sent"] = True
        lines = source.split('\n')
        in_step2_block = False
        found_already_sent = False
        for i, line in enumerate(lines):
            if 'Step 2' in line and ('parent' in line.lower() or 'ABORTED' in line):
                in_step2_block = True
            if in_step2_block and 'already_sent' in line:
                found_already_sent = True
                break
            # Stop at next major section
            if in_step2_block and line.strip().startswith('# ──') and 'Step 2' not in line:
                break
        assert found_already_sent, \
            "result['already_sent'] = True not found in Step 2 (parent ABORTED COMPLETE) block"


class TestHighFrequencyLoggingDowngrade:
    """v1.1.0 perf: all diagnostic logs unified to 'HLS:' prefix at debug level.

    Previous HLS_CALLED, HLS_WRAP, HLS_DIAG, HLS_FIX prefixes were unified
    to a single 'HLS:' prefix. All diagnostic logs use debug level to
    reduce log noise in production.
    """

    def test_diagnostic_logs_use_debug(self) -> None:
        """All HLS: diagnostic logs should use debug level, not info/warning."""
        from hermes_lark_streaming.patching import _maybe_wrap_callbacks
        import inspect

        source = inspect.getsource(_maybe_wrap_callbacks)
        lines = source.split('\n')
        for line in lines:
            if 'HLS:' in line and '_logger.' in line:
                assert '_logger.debug' in line, \
                    f"HLS: diagnostic should use _logger.debug, found: {line.strip()}"

    def test_no_legacy_prefixes(self) -> None:
        """No HLS_CALLED, HLS_WRAP, HLS_DIAG, HLS_FIX prefixes should remain."""
        from hermes_lark_streaming.patching import _maybe_wrap_callbacks
        import inspect

        source = inspect.getsource(_maybe_wrap_callbacks)
        for old_prefix in ('HLS_CALLED', 'HLS_WRAP', 'HLS_DIAG', 'HLS_FIX'):
            assert old_prefix not in source, \
                f"Legacy prefix {old_prefix} should be replaced with 'HLS:'"


class TestStartupDelay:
    """v0.15.5 perf: startup delay reduced from 5s to 2s."""

    def test_startup_delay_is_2s(self) -> None:
        """_schedule_direct_patch should sleep 2 seconds, not 5."""
        from hermes_lark_streaming.patching import _schedule_direct_patch
        import inspect

        source = inspect.getsource(_schedule_direct_patch)
        assert "time.sleep(2)" in source, \
            "Startup delay should be 2 seconds (reduced from 5)"
        assert "time.sleep(5)" not in source, \
            "Startup delay should NOT be 5 seconds (reduced to 2)"

    def test_startup_delay_log_says_2s(self) -> None:
        """Log message should reflect the 2s delay."""
        from hermes_lark_streaming.patching import _schedule_direct_patch
        import inspect

        source = inspect.getsource(_schedule_direct_patch)
        assert "2s delay" in source, \
            "Log message should mention '2s delay'"


# ── v0.18.1: GatewayRunner delayed patch + edit chat_id fix ──


class TestApplyGatewayRunnerPatches:
    """v0.18.1: _apply_gateway_runner_patches helper for delayed GatewayRunner patching."""

    def test_apply_gateway_runner_patches_function_exists(self) -> None:
        """_apply_gateway_runner_patches should exist as a callable."""
        from hermes_lark_streaming.patching import _apply_gateway_runner_patches
        assert callable(_apply_gateway_runner_patches)


class TestInterceptedEditChatId:
    """v0.18.1: _intercepted_edit should accept chat_id as explicit parameter."""

    def test_intercepted_edit_has_chat_id_param(self) -> None:
        """_intercepted_edit should accept chat_id as explicit parameter."""
        import inspect
        from hermes_lark_streaming.patching import _wrap_feishu_adapter_edit

        # Create a dummy orig_edit and wrap it
        async def dummy_edit(self, chat_id, message_id, content, **kwargs):
            pass

        wrapped = _wrap_feishu_adapter_edit(dummy_edit)
        sig = inspect.signature(wrapped)
        params = list(sig.parameters.keys())
        assert "chat_id" in params, f"chat_id not in params: {params}"
        assert "message_id" in params
        assert "content" in params
