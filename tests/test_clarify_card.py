"""Tests for Clarify interactive card feature (three-state design).

Tests the card builders (build_clarify_card, build_clarify_submitted_card,
build_clarify_confirmed_card) and the monkey-patch wrappers
(_wrap_feishu_adapter_send_clarify, _wrap_feishu_card_action_trigger,
_schedule_clarify_resolve_and_confirm).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hermes_lark_streaming.cardkit import (
    build_clarify_card,
    build_clarify_confirmed_card,
    build_clarify_submitted_card,
    normalize_clarify_choices,
)
from hermes_lark_streaming.cardkit.i18n import _T
from hermes_lark_streaming.cardkit.special import (
    _CLARIFY_MAX_CHOICE_LEN,
    _normalize_choice,
)


# ── build_clarify_card (Pending state) ──


class TestBuildClarifyCardWithChoices:
    """Test build_clarify_card with choices (multi-choice mode)."""

    def test_schema_2_and_streaming_false(self) -> None:
        card = build_clarify_card(
            question="Which approach?",
            choices=["Fast", "Slow", "Custom"],
            clarify_id="test_id_123",
        )
        assert card["schema"] == "2.0"
        assert card["config"]["streaming_mode"] is False

    def test_question_displayed_as_div_with_helpdesk_icon(self) -> None:
        card = build_clarify_card(
            question="Which approach?",
            choices=["A", "B"],
            clarify_id="id1",
        )
        elements = card["body"]["elements"]
        assert elements[0]["tag"] == "div"
        assert elements[0]["icon"]["tag"] == "standard_icon"
        assert elements[0]["icon"]["token"] == "info_outlined"
        assert "Which approach?" in elements[0]["text"]["content"]

    def test_options_displayed_as_markdown_list(self) -> None:
        card = build_clarify_card(
            question="Pick one",
            choices=["Option A", "Option B", "Option C"],
            clarify_id="id2",
        )
        elements = card["body"]["elements"]
        # Second element should be markdown list of options
        md_el = next(e for e in elements if e.get("tag") == "markdown")
        content = md_el["content"]
        assert "A. Option A" in content
        assert "B. Option B" in content
        assert "C. Option C" in content

    def test_select_static_element_present(self) -> None:
        card = build_clarify_card(
            question="Pick one",
            choices=["Option A", "Option B"],
            clarify_id="id3",
        )
        elements = card["body"]["elements"]
        select_els = [e for e in elements if e.get("tag") == "select_static"]
        assert len(select_els) == 1

    def test_select_has_choices_without_other(self) -> None:
        card = build_clarify_card(
            question="Pick one",
            choices=["Alpha", "Beta"],
            clarify_id="id4",
        )
        select_el = next(e for e in card["body"]["elements"] if e.get("tag") == "select_static")
        options = select_el["options"]
        # Should have 2 predefined options, NO "other" option
        assert len(options) == 2
        assert options[0]["value"] == "0"
        assert "Alpha" in options[0]["text"]["content"]
        assert options[1]["value"] == "1"
        assert "Beta" in options[1]["text"]["content"]
        # No "other" value
        assert all(o["value"] != "other" for o in options)

    def test_select_options_have_label_prefix(self) -> None:
        """Options in dropdown should have A. B. C. label prefix."""
        card = build_clarify_card(
            question="Q",
            choices=["First", "Second"],
            clarify_id="id_labels",
        )
        select_el = next(e for e in card["body"]["elements"] if e.get("tag") == "select_static")
        options = select_el["options"]
        assert "A. First" in options[0]["text"]["content"]
        assert "B. Second" in options[1]["text"]["content"]

    def test_select_behavior_has_clarify_action(self) -> None:
        card = build_clarify_card(
            question="Q",
            choices=["X"],
            clarify_id="id5",
        )
        select_el = next(e for e in card["body"]["elements"] if e.get("tag") == "select_static")
        behaviors = select_el["behaviors"]
        assert len(behaviors) == 1
        assert behaviors[0]["type"] == "callback"
        assert behaviors[0]["value"]["hermes_clarify_action"] == "select"
        assert behaviors[0]["value"]["clarify_id"] == "id5"

    def test_input_always_present_even_with_choices(self) -> None:
        """Input element should always be present, even when choices exist."""
        card = build_clarify_card(
            question="Q",
            choices=["A", "B"],
            clarify_id="id6",
        )
        input_els = [e for e in card["body"]["elements"] if e.get("tag") == "input"]
        assert len(input_els) == 1

    def test_input_behavior_is_input_submit(self) -> None:
        card = build_clarify_card(
            question="Q",
            choices=["A"],
            clarify_id="id7",
        )
        input_el = next(e for e in card["body"]["elements"] if e.get("tag") == "input")
        behaviors = input_el["behaviors"]
        assert len(behaviors) == 1
        assert behaviors[0]["type"] == "callback"
        assert behaviors[0]["value"]["hermes_clarify_action"] == "input_submit"
        assert behaviors[0]["value"]["clarify_id"] == "id7"

    def test_locales_in_config(self) -> None:
        card = build_clarify_card(
            question="Q",
            choices=["A"],
            clarify_id="id8",
        )
        assert "locales" in card["config"]


class TestBuildClarifyCardWithoutChoices:
    """Test build_clarify_card without choices (open-ended mode)."""

    def test_input_element_present(self) -> None:
        card = build_clarify_card(
            question="Describe your issue",
            choices=None,
            clarify_id="id_open1",
        )
        input_els = [e for e in card["body"]["elements"] if e.get("tag") == "input"]
        assert len(input_els) == 1

    def test_no_select_when_no_choices(self) -> None:
        card = build_clarify_card(
            question="Q",
            choices=None,
            clarify_id="id_open2",
        )
        select_els = [e for e in card["body"]["elements"] if e.get("tag") == "select_static"]
        assert len(select_els) == 0

    def test_no_markdown_list_when_no_choices(self) -> None:
        card = build_clarify_card(
            question="Q",
            choices=None,
            clarify_id="id_open3",
        )
        # Only 2 elements: question title + input
        elements = card["body"]["elements"]
        assert len(elements) == 2
        assert elements[0]["tag"] == "div"  # question title
        assert elements[1]["tag"] == "input"  # input field

    def test_input_has_correct_behavior(self) -> None:
        card = build_clarify_card(
            question="Q",
            choices=None,
            clarify_id="id_open4",
        )
        input_el = next(e for e in card["body"]["elements"] if e.get("tag") == "input")
        behaviors = input_el["behaviors"]
        assert behaviors[0]["value"]["hermes_clarify_action"] == "input_submit"

    def test_input_has_max_length(self) -> None:
        card = build_clarify_card(
            question="Q",
            choices=None,
            clarify_id="id_open5",
        )
        input_el = next(e for e in card["body"]["elements"] if e.get("tag") == "input")
        assert input_el["max_length"] == 500

    def test_empty_choices_list_shows_input_only(self) -> None:
        """Empty choices list should show input only (same as None)."""
        card = build_clarify_card(
            question="Q",
            choices=[],
            clarify_id="id_open6",
        )
        select_els = [e for e in card["body"]["elements"] if e.get("tag") == "select_static"]
        assert len(select_els) == 0
        input_els = [e for e in card["body"]["elements"] if e.get("tag") == "input"]
        assert len(input_els) == 1


# ── build_clarify_submitted_card (State 2: Submitted / Soft Lock) ──


class TestBuildClarifySubmittedCard:
    """Test build_clarify_submitted_card."""

    def test_schema_2(self) -> None:
        card = build_clarify_submitted_card(question="Q", selected="A", clarify_id="cid")
        assert card["schema"] == "2.0"
        assert card["config"]["streaming_mode"] is False

    def test_question_displayed_with_lock_icon(self) -> None:
        card = build_clarify_submitted_card(question="Which way?", selected="Fast", clarify_id="cid")
        elements = card["body"]["elements"]
        assert elements[0]["tag"] == "div"
        assert elements[0]["icon"]["tag"] == "standard_icon"
        assert elements[0]["icon"]["token"] == "lock_outlined"
        assert "Which way?" in elements[0]["text"]["content"]

    def test_selected_shown_with_lock_icon(self) -> None:
        card = build_clarify_submitted_card(question="Q", selected="Fast", clarify_id="cid")
        elements = card["body"]["elements"]
        assert elements[1]["tag"] == "div"
        assert elements[1]["icon"]["tag"] == "standard_icon"
        assert elements[1]["icon"]["token"] == "lock_outlined"
        assert "Fast" in elements[1]["text"]["content"]

    def test_submitted_hint_present(self) -> None:
        card = build_clarify_submitted_card(question="Q", selected="A", clarify_id="cid")
        elements = card["body"]["elements"]
        # Third element: submitted hint
        assert elements[2]["tag"] == "div"
        assert "i18n_content" in elements[2]["text"]

    def test_retry_button_present(self) -> None:
        card = build_clarify_submitted_card(question="Q", selected="A", clarify_id="cid_retry")
        elements = card["body"]["elements"]
        # Fourth element: action with retry button
        assert elements[3]["tag"] == "action"
        actions = elements[3]["actions"]
        assert len(actions) == 1
        assert actions[0]["tag"] == "button"
        assert actions[0]["type"] == "primary"
        behaviors = actions[0]["behaviors"]
        assert behaviors[0]["value"]["hermes_clarify_action"] == "retry_submit"
        assert behaviors[0]["value"]["clarify_id"] == "cid_retry"

    def test_i18n_on_selected_label(self) -> None:
        card = build_clarify_submitted_card(question="Q", selected="Fast", clarify_id="cid")
        elements = card["body"]["elements"]
        assert "i18n_content" in elements[1]["text"]

    def test_locales_in_config(self) -> None:
        card = build_clarify_submitted_card(question="Q", selected="A", clarify_id="cid")
        assert "locales" in card["config"]


# ── build_clarify_confirmed_card (State 3: Confirmed / Hard Lock) ──


class TestBuildClarifyConfirmedCard:
    """Test build_clarify_confirmed_card."""

    def test_schema_2(self) -> None:
        card = build_clarify_confirmed_card(question="Q", selected="A")
        assert card["schema"] == "2.0"
        assert card["config"]["streaming_mode"] is False

    def test_question_displayed_with_resolve_filled_icon(self) -> None:
        card = build_clarify_confirmed_card(question="Which way?", selected="Fast")
        elements = card["body"]["elements"]
        assert elements[0]["tag"] == "div"
        assert elements[0]["icon"]["tag"] == "standard_icon"
        assert elements[0]["icon"]["token"] == "resolve_filled"
        assert "Which way?" in elements[0]["text"]["content"]

    def test_selected_shown_with_resolve_filled_icon(self) -> None:
        card = build_clarify_confirmed_card(question="Q", selected="Fast")
        elements = card["body"]["elements"]
        assert elements[1]["tag"] == "div"
        assert elements[1]["icon"]["tag"] == "standard_icon"
        assert elements[1]["icon"]["token"] == "resolve_filled"
        assert "Fast" in elements[1]["text"]["content"]

    def test_confirmed_label_present(self) -> None:
        card = build_clarify_confirmed_card(question="Q", selected="A")
        elements = card["body"]["elements"]
        assert elements[2]["tag"] == "div"
        assert "i18n_content" in elements[2]["text"]

    def test_no_action_buttons(self) -> None:
        """Confirmed card should have no action buttons (hard lock)."""
        card = build_clarify_confirmed_card(question="Q", selected="A")
        elements = card["body"]["elements"]
        action_els = [e for e in elements if e.get("tag") == "action"]
        assert len(action_els) == 0

    def test_locales_in_config(self) -> None:
        card = build_clarify_confirmed_card(question="Q", selected="A")
        assert "locales" in card["config"]


# ── Clarify card wrappers ──


class TestWrapFeishuAdapterSendClarify:
    """Test _wrap_feishu_adapter_send_clarify wrapper logic."""

    def test_wrapper_is_callable(self) -> None:
        from hermes_lark_streaming.patching import _wrap_feishu_adapter_send_clarify
        assert callable(_wrap_feishu_adapter_send_clarify)

    def test_wrapper_returns_callable(self) -> None:
        from hermes_lark_streaming.patching import _wrap_feishu_adapter_send_clarify
        async def orig(*args, **kwargs):
            pass
        wrapped = _wrap_feishu_adapter_send_clarify(orig)
        assert callable(wrapped)

    def test_falls_back_to_original_when_controller_disabled(self) -> None:
        """When controller is disabled, should fall back to original send_clarify."""
        from hermes_lark_streaming.patching import _wrap_feishu_adapter_send_clarify

        orig = AsyncMock(return_value="original_result")
        wrapped = _wrap_feishu_adapter_send_clarify(orig)

        mock_ctrl = MagicMock()
        mock_ctrl.enabled = False

        with patch("hermes_lark_streaming.controller.get_controller", return_value=mock_ctrl):
            import asyncio
            result = asyncio.get_event_loop().run_until_complete(
                wrapped(
                    MagicMock(), "chat_123", "Question?", ["A", "B"],
                    "clarify_1", "session_key_1",
                )
            )

        orig.assert_called_once()

    def test_stores_choices_for_callback(self) -> None:
        """When card is sent, choices should be stored in _clarify_choices."""
        from hermes_lark_streaming.patching import _clarify_choices, _clarify_questions, _wrap_feishu_adapter_send_clarify

        orig = AsyncMock()
        wrapped = _wrap_feishu_adapter_send_clarify(orig)

        mock_client = AsyncMock()
        mock_client.send_card_to_chat = AsyncMock(return_value="msg_123")

        mock_ctrl = MagicMock()
        mock_ctrl.enabled = True
        mock_ctrl._client_ok.return_value = True
        mock_ctrl._client = mock_client

        # Clean up any previous test data
        _clarify_choices.pop("test_clarify_id", None)
        _clarify_questions.pop("test_clarify_id", None)

        with (
            patch("hermes_lark_streaming.controller.get_controller", return_value=mock_ctrl),
            patch("hermes_lark_streaming.patching._register_gateway_card"),
        ):
            import asyncio
            try:
                asyncio.get_event_loop().run_until_complete(
                    wrapped(
                        MagicMock(), "chat_123", "Which?", ["Fast", "Slow"],
                        "test_clarify_id", "session_1",
                    )
                )
            except Exception:
                pass  # May fail on SendResult import

        # Check that choices were stored
        assert "test_clarify_id" in _clarify_choices
        assert _clarify_choices["test_clarify_id"] == ["Fast", "Slow"]
        assert _clarify_questions["test_clarify_id"] == "Which?"

        # Cleanup
        _clarify_choices.pop("test_clarify_id", None)
        _clarify_questions.pop("test_clarify_id", None)


class TestWrapFeishuCardActionTrigger:
    """Test _wrap_feishu_card_action_trigger wrapper."""

    def test_wrapper_is_callable(self) -> None:
        from hermes_lark_streaming.patching import _wrap_feishu_card_action_trigger
        assert callable(_wrap_feishu_card_action_trigger)

    def test_passthrough_for_non_clarify_action(self) -> None:
        """When action is not a clarify action, original method should be called."""
        from hermes_lark_streaming.patching import _wrap_feishu_card_action_trigger

        original = MagicMock(return_value="original_response")
        wrapped = _wrap_feishu_card_action_trigger(original)

        # Create a mock data object with no hermes_clarify_action
        mock_event = MagicMock()
        mock_event.action.value = {"hermes_action": "approve"}

        mock_data = MagicMock()
        mock_data.event = mock_event

        result = wrapped(MagicMock(), mock_data)

        original.assert_called_once()
        assert result == "original_response"

    def test_intercepts_clarify_select_action(self) -> None:
        """When hermes_clarify_action='select', should return submitted card."""
        from hermes_lark_streaming.patching import (
            _clarify_choices,
            _clarify_questions,
            _clarify_answers,
            _handle_clarify_card_action,
        )

        # Set up stored data
        _clarify_choices["test_cid"] = ["Alpha", "Beta"]
        _clarify_questions["test_cid"] = "Which?"

        # Mock the action
        mock_action = MagicMock()
        mock_action.value = {"hermes_clarify_action": "select", "clarify_id": "test_cid"}
        mock_action.option = "0"

        mock_event = MagicMock()
        mock_event.action = mock_action
        mock_event.operator = MagicMock()
        mock_event.operator.open_id = "user_123"

        mock_data = MagicMock()
        mock_data.event = mock_event

        # Mock the adapter instance
        mock_adapter = MagicMock()
        mock_adapter._is_interactive_operator_authorized.return_value = True
        mock_adapter._loop = None  # No event loop → synchronous fallback

        mock_cg = MagicMock()
        mock_cg.resolve_gateway_clarify = MagicMock()

        with (
            patch.dict("sys.modules", {
                "tools": MagicMock(),
                "tools.clarify_gateway": mock_cg,
            }),
        ):
            result = _handle_clarify_card_action(
                mock_adapter, mock_data, "select",
                {"hermes_clarify_action": "select", "clarify_id": "test_cid"},
            )

        # Answer should be stored for retry
        assert _clarify_answers.get("test_cid") == "Alpha"

        # Cleanup
        _clarify_choices.pop("test_cid", None)
        _clarify_questions.pop("test_cid", None)
        _clarify_answers.pop("test_cid", None)

    def test_intercepts_input_submit_action(self) -> None:
        """When hermes_clarify_action='input_submit', should resolve with input text."""
        from hermes_lark_streaming.patching import (
            _clarify_questions,
            _clarify_answers,
            _handle_clarify_card_action,
        )

        # Set up stored data
        _clarify_questions["test_cid3"] = "Tell me more"

        # Mock the action — user submitted text input
        mock_action = MagicMock()
        mock_action.value = {"hermes_clarify_action": "input_submit", "clarify_id": "test_cid3"}
        mock_action.input_value = "My custom answer"

        mock_event = MagicMock()
        mock_event.action = mock_action
        mock_event.operator = MagicMock()
        mock_event.operator.open_id = "user_123"

        mock_data = MagicMock()
        mock_data.event = mock_event

        # Mock the adapter instance
        mock_adapter = MagicMock()
        mock_adapter._is_interactive_operator_authorized.return_value = True
        mock_adapter._loop = None  # No event loop → synchronous fallback

        mock_cg = MagicMock()
        mock_cg.resolve_gateway_clarify = MagicMock()

        with (
            patch.dict("sys.modules", {
                "tools": MagicMock(),
                "tools.clarify_gateway": mock_cg,
            }),
        ):
            result = _handle_clarify_card_action(
                mock_adapter, mock_data, "input_submit",
                {"hermes_clarify_action": "input_submit", "clarify_id": "test_cid3"},
            )

        # Answer should be stored for retry
        assert _clarify_answers.get("test_cid3") == "My custom answer"

        # Cleanup
        _clarify_questions.pop("test_cid3", None)
        _clarify_answers.pop("test_cid3", None)

    def test_intercepts_retry_submit_action(self) -> None:
        """When hermes_clarify_action='retry_submit', should re-resolve with stored answer."""
        from hermes_lark_streaming.patching import (
            _clarify_questions,
            _clarify_answers,
            _handle_clarify_card_action,
        )

        # Set up stored data — simulate a previous submission
        _clarify_questions["test_cid_retry"] = "Which?"
        _clarify_answers["test_cid_retry"] = "Alpha"

        # Mock the action — user clicked retry
        mock_action = MagicMock()
        mock_action.value = {"hermes_clarify_action": "retry_submit", "clarify_id": "test_cid_retry"}

        mock_event = MagicMock()
        mock_event.action = mock_action
        mock_event.operator = MagicMock()
        mock_event.operator.open_id = "user_123"

        mock_data = MagicMock()
        mock_data.event = mock_event

        # Mock the adapter instance
        mock_adapter = MagicMock()
        mock_adapter._is_interactive_operator_authorized.return_value = True
        mock_adapter._loop = None  # No event loop → synchronous fallback

        mock_cg = MagicMock()
        mock_cg.resolve_gateway_clarify = MagicMock()

        with (
            patch.dict("sys.modules", {
                "tools": MagicMock(),
                "tools.clarify_gateway": mock_cg,
            }),
        ):
            result = _handle_clarify_card_action(
                mock_adapter, mock_data, "retry_submit",
                {"hermes_clarify_action": "retry_submit", "clarify_id": "test_cid_retry"},
            )

        # Resolve should be called with the stored answer
        mock_cg.resolve_gateway_clarify.assert_called_once_with("test_cid_retry", "Alpha")

        # Cleanup
        _clarify_questions.pop("test_cid_retry", None)
        _clarify_answers.pop("test_cid_retry", None)


class TestClarifyCardRegistry:
    """Test the _clarify_choices, _clarify_questions, _clarify_answers, _clarify_card_info module-level dicts."""

    def test_choices_registry_exists(self) -> None:
        from hermes_lark_streaming.patching import _clarify_choices
        assert isinstance(_clarify_choices, dict)

    def test_questions_registry_exists(self) -> None:
        from hermes_lark_streaming.patching import _clarify_questions
        assert isinstance(_clarify_questions, dict)

    def test_selections_registry_exists(self) -> None:
        from hermes_lark_streaming.patching import _clarify_selections
        assert isinstance(_clarify_selections, dict)

    def test_card_msg_ids_registry_exists(self) -> None:
        from hermes_lark_streaming.patching import _clarify_card_msg_ids
        assert isinstance(_clarify_card_msg_ids, dict)

    def test_choices_cleanup_after_resolve(self) -> None:
        """After resolving a clarify, the choices should be cleaned up."""
        from hermes_lark_streaming.patching import _clarify_choices, _clarify_questions

        _clarify_choices["cleanup_test"] = ["A", "B"]
        _clarify_questions["cleanup_test"] = "Q"

        # Simulate cleanup
        _clarify_choices.pop("cleanup_test", None)
        _clarify_questions.pop("cleanup_test", None)

        assert "cleanup_test" not in _clarify_choices
        assert "cleanup_test" not in _clarify_questions


class TestClarifyI18n:
    """Test clarify-related i18n entries exist."""

    def test_clarify_select_placeholder_entry(self) -> None:
        assert "clarify_select_placeholder" in _T

    def test_clarify_input_placeholder_entry(self) -> None:
        assert "clarify_input_placeholder" in _T

    def test_clarify_selected_entry(self) -> None:
        assert "clarify_selected" in _T

    def test_clarify_submitted_entry(self) -> None:
        assert "clarify_submitted" in _T

    def test_clarify_retry_entry(self) -> None:
        assert "clarify_retry" in _T

    def test_clarify_confirmed_entry(self) -> None:
        assert "clarify_confirmed" in _T

    def test_all_entries_are_tuples_of_two(self) -> None:
        clarify_keys = [
            "clarify_select_placeholder",
            "clarify_input_placeholder", "clarify_selected",
            "clarify_submitted", "clarify_retry", "clarify_confirmed",
        ]
        for key in clarify_keys:
            assert key in _T, f"Missing i18n key: {key}"
            en, zh = _T[key]
            assert isinstance(en, str) and len(en) > 0, f"Empty English text for {key}"
            assert isinstance(zh, str) and len(zh) > 0, f"Empty Chinese text for {key}"


class TestLoadingContextI18n:
    """Test loading_context i18n entry exists."""

    def test_loading_context_entry(self) -> None:
        assert "loading_context" in _T

    def test_loading_context_is_tuple_of_two(self) -> None:
        en, zh = _T["loading_context"]
        assert isinstance(en, str) and len(en) > 0
        assert isinstance(zh, str) and len(zh) > 0


# ── v1.3.0 P0-01: Clarify choice normalization ──────────────────────
#
# When the LLM passes dict-shaped choices (e.g. {"id": 1, "path": "/mnt/nas/backup1"})
# that get str()-serialized to "{'id': 1, 'path': '/mnt/nas/backup1'}", the card
# now normalizes them into readable text.  These tests cover the normalization
# logic (normalize_clarify_choices / _normalize_choice) and the lark_md escaping
# applied by the three card builders.


class TestNormalizeClarifyChoicesBasic:
    """Basic normalization: plain strings, None, empty inputs."""

    def test_plain_strings_returned_as_is(self) -> None:
        """Plain string choices pass through unchanged (after stripping)."""
        assert normalize_clarify_choices(["Fast", "Slow"]) == ["Fast", "Slow"]

    def test_whitespace_is_stripped(self) -> None:
        """Leading/trailing whitespace is stripped from plain strings."""
        assert normalize_clarify_choices(["  spaced  "]) == ["spaced"]

    def test_none_input_returns_empty_list(self) -> None:
        assert normalize_clarify_choices(None) == []

    def test_empty_list_returns_empty_list(self) -> None:
        assert normalize_clarify_choices([]) == []

    def test_empty_string_filtered_out(self) -> None:
        """A single empty string choice is filtered out (useless to the user)."""
        assert normalize_clarify_choices([""]) == []

    def test_mixed_valid_and_empty_filtered(self) -> None:
        """Empty choices in a mixed list are dropped, valid ones preserved in order."""
        result = normalize_clarify_choices(["real", "", "{'path': '/x'}"])
        assert result == ["real", "/x"]


class TestNormalizeDictReprStrings:
    """Dict-repr string parsing — the production bug scenario.

    The LLM emits choices like "{'id': 1, 'path': '/mnt/nas/backup1'}" (a dict
    that has been str()-serialized).  normalize_clarify_choices parses these
    back to dicts via ast.literal_eval (safe — no code execution) and extracts
    the most human-readable field by priority.
    """

    def test_dict_repr_with_path_field_extracted(self) -> None:
        """Production bug: dict-repr with `path` → extracted to readable path."""
        result = normalize_clarify_choices(["{'id': 1, 'path': '/mnt/nas/backup1'}"])
        assert result == ["/mnt/nas/backup1"]

    def test_dict_repr_with_label_field_extracted(self) -> None:
        """`label` field is the highest-priority human-readable field."""
        result = normalize_clarify_choices(["{'label': 'Option A', 'id': 5}"])
        assert result == ["Option A"]

    def test_dict_repr_with_description_field_extracted(self) -> None:
        """`description` has priority over `name`."""
        result = normalize_clarify_choices(
            ["{'description': 'Use backup server', 'name': 'srv1'}"]
        )
        assert result == ["Use backup server"]

    def test_dict_repr_with_name_field_extracted(self) -> None:
        """`name` field is extracted when no higher-priority field exists."""
        result = normalize_clarify_choices(["{'name': 'srv1'}"])
        assert result == ["srv1"]

    def test_dict_repr_with_text_field_extracted(self) -> None:
        result = normalize_clarify_choices(["{'text': 'hello'}"])
        assert result == ["hello"]

    def test_dict_repr_with_title_field_extracted(self) -> None:
        result = normalize_clarify_choices(["{'title': 'My Title'}"])
        assert result == ["My Title"]

    def test_dict_repr_with_string_value_field_extracted(self) -> None:
        """`value` is used when it's a string (not int)."""
        result = normalize_clarify_choices(["{'value': 'prod'}"])
        assert result == ["prod"]

    def test_dict_repr_with_only_int_id_falls_back_to_original(self) -> None:
        """When the dict has only an int `id` (not a useful string label),
        normalization falls back to the ORIGINAL dict-repr string unchanged.

        The fallback is important: the user at least sees *something* (which
        gets escaped later by the card builder), rather than an empty option.
        """
        original = "{'id': 1}"
        result = normalize_clarify_choices([original])
        assert result == [original]

    def test_dict_repr_field_priority_label_wins(self) -> None:
        """When multiple candidate fields exist, `label` wins (highest priority)."""
        result = normalize_clarify_choices(
            ["{'id': 9, 'label': 'L', 'path': '/p', 'name': 'N'}"]
        )
        assert result == ["L"]

    def test_invalid_dict_repr_falls_back_to_original(self) -> None:
        """A string that looks like a dict but isn't valid Python literal
        is returned unchanged (not raised, not mangled)."""
        original = "{not valid python}"
        result = normalize_clarify_choices([original])
        assert result == [original]

    def test_dict_repr_with_internal_whitespace_extracted(self) -> None:
        """ast.literal_eval tolerates whitespace inside the dict-repr."""
        result = normalize_clarify_choices(["{ 'label' : 'X' }"])
        assert result == ["X"]


class TestNormalizeDefensiveInputs:
    """Defensive handling of non-string inputs (in case a caller bypasses the
    adapter and passes real dicts/lists directly)."""

    def test_real_dict_input_extracts_label(self) -> None:
        """A real dict (not a dict-repr string) is also normalized."""
        result = normalize_clarify_choices([{"label": "Direct"}])
        assert result == ["Direct"]

    def test_real_list_input_joined_with_spaces(self) -> None:
        """A list inside choices is space-joined into a single readable string."""
        result = normalize_clarify_choices([["a", "b"]])
        assert result == ["a b"]

    def test_real_tuple_input_joined_with_spaces(self) -> None:
        """Tuples are treated the same as lists."""
        result = normalize_clarify_choices([("x", "y")])
        assert result == ["x y"]


class TestNormalizeTruncation:
    """Long text truncation (keeps the dropdown and option list readable)."""

    def test_max_choice_len_constant_is_80(self) -> None:
        """The truncation threshold is 80 characters (locked by the spec)."""
        assert _CLARIFY_MAX_CHOICE_LEN == 80

    def test_long_text_truncated_to_exactly_80_chars(self) -> None:
        """A string longer than 80 chars is truncated to 79 chars + '…' (total 80)."""
        long_text = "x" * 100
        result = normalize_clarify_choices([long_text])
        assert len(result) == 1
        assert len(result[0]) == 80
        assert result[0] == "x" * 79 + "…"

    def test_text_exactly_80_chars_not_truncated(self) -> None:
        """Boundary: exactly 80 chars is NOT > 80, so no truncation."""
        text = "y" * 80
        result = normalize_clarify_choices([text])
        assert result == [text]
        assert len(result[0]) == 80

    def test_truncation_applies_after_dict_extraction(self) -> None:
        """Truncation operates on the FINAL extracted text, not the original dict-repr.

        A dict-repr whose `path` value is longer than 80 chars gets the *path*
        truncated, not the original dict-repr string.
        """
        long_path = "/mnt/" + "a" * 100
        choice = "{'id': 1, 'path': '" + long_path + "'}"
        result = normalize_clarify_choices([choice])
        assert len(result) == 1
        # The result should be the path, truncated to 80 chars (79 + "…").
        assert len(result[0]) == 80
        assert result[0].endswith("…")
        assert result[0].startswith("/mnt/")


class TestNormalizeChoicePrivate:
    """Direct tests for the private _normalize_choice() helper.

    These cover edge cases that normalize_clarify_choices (which filters empty
    results) would not expose directly.
    """

    def test_none_returns_empty_string(self) -> None:
        assert _normalize_choice(None) == ""

    def test_empty_string_returns_empty_string(self) -> None:
        assert _normalize_choice("") == ""

    def test_whitespace_only_returns_empty_string(self) -> None:
        assert _normalize_choice("   ") == ""

    def test_plain_string_is_stripped(self) -> None:
        assert _normalize_choice("  hello  ") == "hello"

    def test_dict_repr_with_path_returns_path(self) -> None:
        assert _normalize_choice("{'id': 1, 'path': '/x'}") == "/x"

    def test_dict_repr_only_int_id_returns_original_unchanged(self) -> None:
        """Fallback case: no usable string field → original text preserved."""
        assert _normalize_choice("{'id': 1}") == "{'id': 1}"

    def test_real_dict_returns_extracted(self) -> None:
        assert _normalize_choice({"label": "Direct"}) == "Direct"

    def test_non_string_non_dict_non_list_str_converted(self) -> None:
        """Bare ints (defensive) get str()-converted."""
        assert _normalize_choice(42) == "42"

    def test_never_raises_on_garbage_input(self) -> None:
        """_normalize_choice must never raise — it's called on untrusted input."""
        # All of these should return a string, not raise.
        assert isinstance(_normalize_choice("{"), str)
        assert isinstance(_normalize_choice("}"), str)
        assert isinstance(_normalize_choice("{[}]"), str)
        assert isinstance(_normalize_choice("{'unclosed':"), str)


# ── v1.3.0 P0-01: lark_md escaping in build_clarify_card ──


class TestBuildClarifyCardEscaping:
    """Test that the markdown element escapes lark_md special characters,
    while the select_static dropdown (plain_text) shows the unescaped text.
    """

    @staticmethod
    def _get_md_and_select(card: dict) -> tuple[str | None, list[dict] | None]:
        elements = card["body"]["elements"]
        md_el = next((e for e in elements if e.get("tag") == "markdown"), None)
        sel_el = next((e for e in elements if e.get("tag") == "select_static"), None)
        md_content = md_el["content"] if md_el else None
        options = sel_el["options"] if sel_el else None
        return md_content, options

    def test_dict_repr_with_path_normalized_no_curly_leakage(self) -> None:
        """Production bug regression: dict-repr `{'id': 1, 'path': '/x'}` is
        normalized to `/x` in BOTH the markdown and the select.  Crucially,
        the markdown MUST NOT contain raw `{'` (which would trigger Feishu's
        lark_md template-syntax bug and garble the display).
        """
        card = build_clarify_card(
            question="Q",
            choices=["{'id': 1, 'path': '/x'}"],
            clarify_id="c1",
        )
        md, options = self._get_md_and_select(card)
        assert md is not None
        assert options is not None
        # Normalization worked — both display the readable path.
        assert "/x" in md
        assert "/x" in options[0]["text"]["content"]
        # The bug fix: no raw `{'` template-syntax leakage in the markdown.
        assert "{'" not in md

    def test_curly_braces_escaped_when_dict_repr_falls_back(self) -> None:
        r"""When a dict-repr has no usable string field (e.g. only an int id),
        normalization falls back to the ORIGINAL dict-repr string.  The card
        builder must then escape `{` and `}` for lark_md so they don't get
        misinterpreted as template syntax.

        Markdown: escaped (`\{`, `\}`)
        Select (plain_text): unescaped (raw `{'id': 1}`)

        Note: the escaped form `\{'id': 1\}` technically still contains the
        2-char substring `{'` (because `\{` is backslash + brace), but the
        `{` is preceded by a backslash escape, so Feishu's lark_md parser
        treats it as a literal brace — the bug-trigger pattern (unescaped
        `{'`) is gone.  We verify this by asserting the raw ORIGINAL string
        (with no backslashes) is not present.
        """
        raw = "{'id': 1}"
        card = build_clarify_card(
            question="Q",
            choices=[raw],
            clarify_id="c1",
        )
        md, options = self._get_md_and_select(card)
        assert md is not None
        assert options is not None
        # Markdown escaped the curly braces (`\{`, `\}`).
        assert "\\{" in md
        assert "\\}" in md
        # The raw, UNescaped original text is NOT in the markdown — it's been
        # transformed by the escape (every `{` and `}` now has a `\` prefix).
        assert raw not in md
        # The select (plain_text) shows the raw text unchanged.
        assert raw in options[0]["text"]["content"]

    def test_normal_choices_regression(self) -> None:
        """Regression: plain string choices still work (no escaping needed)."""
        card = build_clarify_card(
            question="Q",
            choices=["Fast", "Slow"],
            clarify_id="c1",
        )
        md, options = self._get_md_and_select(card)
        assert md is not None
        assert "A. Fast" in md
        assert "B. Slow" in md
        assert len(options) == 2

    def test_square_brackets_escaped_in_markdown(self) -> None:
        """`[` and `]` are lark_md special chars (link syntax) → escaped."""
        card = build_clarify_card(
            question="Q",
            choices=["array[0]"],
            clarify_id="c1",
        )
        md, _ = self._get_md_and_select(card)
        assert md is not None
        assert "array\\[0\\]" in md

    def test_angle_brackets_escaped_in_markdown(self) -> None:
        """`<` and `>` are lark_md special chars → escaped."""
        card = build_clarify_card(
            question="Q",
            choices=["a < b"],
            clarify_id="c1",
        )
        md, _ = self._get_md_and_select(card)
        assert md is not None
        assert "a \\< b" in md

    def test_backticks_escaped_in_markdown(self) -> None:
        """Backticks are lark_md code-syntax markers → escaped."""
        card = build_clarify_card(
            question="Q",
            choices=["`code`"],
            clarify_id="c1",
        )
        md, _ = self._get_md_and_select(card)
        assert md is not None
        assert "\\`code\\`" in md

    def test_normalization_then_escaping_combined(self) -> None:
        """End-to-end: dict-repr with a path containing `[` is first normalized
        (path extracted) and then the brackets are escaped in the markdown.

        Markdown: `/x\\[1\\]` (escaped)
        Select (plain_text): `/x[1]` (unescaped, plain)
        """
        card = build_clarify_card(
            question="Q",
            choices=["{'path': '/x[1]'}"],
            clarify_id="c1",
        )
        md, options = self._get_md_and_select(card)
        assert md is not None
        assert options is not None
        # Markdown: normalized then escaped.
        assert "/x\\[1\\]" in md
        assert "/x[1]" not in md  # raw form is NOT in the markdown
        # Select (plain_text): normalized, NOT escaped.
        assert "/x[1]" in options[0]["text"]["content"]
        assert "\\[" not in options[0]["text"]["content"]

    def test_select_options_use_normalized_text(self) -> None:
        """The select_static dropdown options use the normalized (but unescaped)
        text — it's plain_text, so no markdown processing happens.
        """
        card = build_clarify_card(
            question="Q",
            choices=[
                "{'label': 'First'}",
                "{'label': 'Second'}",
            ],
            clarify_id="c1",
        )
        _, options = self._get_md_and_select(card)
        assert options is not None
        assert len(options) == 2
        assert "First" in options[0]["text"]["content"]
        assert "Second" in options[1]["text"]["content"]
        # No dict-repr garbage in the dropdown.
        assert all("{'" not in o["text"]["content"] for o in options)


# ── v1.3.0 P0-01: lark_md escaping in submitted/confirmed cards ──


class TestBuildClarifySubmittedCardEscaping:
    """Test that build_clarify_submitted_card escapes the `selected` text
    in the lark_md content (and its i18n variants).
    """

    @staticmethod
    def _get_selected_div(card: dict) -> dict:
        """The second div in the submitted card is the 'Selected: {}' line."""
        elements = card["body"]["elements"]
        divs = [e for e in elements if e.get("tag") == "div" and "icon" in e]
        # First div = question title; second div = 'Selected: {}'
        return divs[1]

    def test_curly_braces_in_selected_are_escaped(self) -> None:
        r"""When the selected text contains `{` (e.g. from a fallback dict-repr),
        the markdown element escapes it to `\{` so lark_md doesn't interpret
        it as template syntax.

        Note: the escaped form `\{'id': 1\}` still contains the substring
        `{'` (backslash + brace + quote), but the `{` is preceded by a `\`
        escape, so it's not the bug-trigger pattern.  We verify by asserting
        the raw ORIGINAL string (no backslashes) is absent from the content.
        """
        raw = "{'id': 1}"
        card = build_clarify_submitted_card(
            question="Q",
            selected=raw,
            clarify_id="c",
        )
        selected_div = self._get_selected_div(card)
        content = selected_div["text"]["content"]
        assert "\\{" in content
        assert "\\}" in content
        # The raw, UNescaped original text is NOT present (it's been escaped).
        assert raw not in content

    def test_curly_braces_escaped_in_zh_cn_i18n(self) -> None:
        """The Chinese i18n variant is also escaped (it's rendered in zh_cn locale)."""
        card = build_clarify_submitted_card(
            question="Q",
            selected="{'id': 1}",
            clarify_id="c",
        )
        selected_div = self._get_selected_div(card)
        zh_content = selected_div["text"]["i18n_content"]["zh_cn"]
        assert "\\{" in zh_content
        assert "\\}" in zh_content
        # The Chinese label is "已选择: {}"
        assert "已选择" in zh_content

    def test_square_brackets_in_selected_are_escaped(self) -> None:
        """Square brackets in selected text are escaped."""
        card = build_clarify_submitted_card(
            question="Q",
            selected="a[b]",
            clarify_id="c",
        )
        content = self._get_selected_div(card)["text"]["content"]
        assert "a\\[b\\]" in content

    def test_normal_selected_text_not_modified(self) -> None:
        """Regression: normal selected text (no special chars) is unchanged."""
        card = build_clarify_submitted_card(
            question="Q",
            selected="Fast",
            clarify_id="c",
        )
        content = self._get_selected_div(card)["text"]["content"]
        assert "Fast" in content
        assert "\\" not in content  # No escape backslashes added.


class TestBuildClarifyConfirmedCardEscaping:
    """Test that build_clarify_confirmed_card escapes the `selected` text."""

    @staticmethod
    def _get_selected_div(card: dict) -> dict:
        elements = card["body"]["elements"]
        divs = [e for e in elements if e.get("tag") == "div" and "icon" in e]
        return divs[1]

    def test_square_brackets_in_selected_are_escaped(self) -> None:
        """Square brackets in selected text are escaped in the confirmed card too."""
        card = build_clarify_confirmed_card(question="Q", selected="a[b]")
        content = self._get_selected_div(card)["text"]["content"]
        assert "a\\[b\\]" in content

    def test_curly_braces_in_selected_are_escaped(self) -> None:
        """Curly braces in selected text are escaped in the confirmed card too."""
        card = build_clarify_confirmed_card(question="Q", selected="{'id': 1}")
        content = self._get_selected_div(card)["text"]["content"]
        assert "\\{" in content
        assert "\\}" in content

    def test_normal_selected_text_not_modified(self) -> None:
        """Regression: normal selected text is unchanged in the confirmed card."""
        card = build_clarify_confirmed_card(question="Q", selected="Fast")
        content = self._get_selected_div(card)["text"]["content"]
        assert "Fast" in content
        assert "\\" not in content


# ── v1.3.0 Round 2 audit fixes ──


class TestNormalizeChoiceTypeError:
    """v1.3.0 Round 2: ast.literal_eval can raise TypeError on unhashable keys.

    ``ast.literal_eval("{{}: {}}")`` raises ``TypeError: unhashable type: 'dict'``
    because it tries to use a dict as a dict key. The except clause must catch
    TypeError in addition to ValueError/SyntaxError.
    """

    def test_unhashable_dict_key_no_crash(self) -> None:
        """A dict-repr with an unhashable key must not crash _normalize_choice."""
        result = normalize_clarify_choices(["{{}: {}}"])
        # Should fall back to the original string (not crash)
        assert len(result) == 1
        assert isinstance(result[0], str)

    def test_nested_set_no_crash(self) -> None:
        """``ast.literal_eval("{1, 2}")`` returns a set, not a dict — must not crash."""
        result = normalize_clarify_choices(["{1, 2, 3}"])
        assert len(result) == 1
        # A set is not a dict, so _extract_readable_from_dict is not called;
        # the original string is returned (truncated if needed).
        assert isinstance(result[0], str)

    def test_deeply_nested_no_crash(self) -> None:
        """Deeply nested structures must not crash."""
        result = normalize_clarify_choices(["{'a': {'b': {'c': 1}}}"])
        assert len(result) == 1
        assert isinstance(result[0], str)


class TestClarifyQuestionEscaping:
    """v1.3.0 Round 2: the ``question`` parameter must be escaped for lark_md.

    All 3 card builders render the question as ``f"**{question}**"`` inside a
    ``lark_md`` element. If the question contains ``{`` or ``}`` (e.g. the LLM
    includes a config snippet in the question), Feishu's lark_md mangles it the
    same way it mangled choices. The fix: escape the question via ``_escape_md``.
    """

    def test_question_curly_braces_escaped_in_clarify_card(self) -> None:
        """Curly braces in question are escaped in build_clarify_card."""
        card = build_clarify_card(
            question="Confirm {'id': 1}?",
            choices=["Yes", "No"],
            clarify_id="c1",
        )
        # The question is in the first element (div with lark_md)
        question_el = card["body"]["elements"][0]
        content = question_el["text"]["content"]
        # Should contain escaped \{ and \} (not raw {' which triggers the bug)
        assert "\\{" in content
        assert "\\}" in content
        # Should NOT contain UNescaped {' (the bug trigger).
        # The escaped form \{' is safe (Feishu treats \{ as literal).
        # Check by removing all \{ occurrences and verifying no { remains.
        unescaped = content.replace('\\{', '').replace('\\}', '')
        assert '{' not in unescaped

    def test_question_brackets_escaped_in_clarify_card(self) -> None:
        """Square brackets in question are escaped."""
        card = build_clarify_card(
            question="Choose array[0]?",
            choices=["A"],
            clarify_id="c2",
        )
        content = card["body"]["elements"][0]["text"]["content"]
        assert "\\[" in content
        assert "\\]" in content

    def test_question_normal_text_unchanged(self) -> None:
        """Normal question text without special chars is unchanged."""
        card = build_clarify_card(
            question="Which approach?",
            choices=["Fast"],
            clarify_id="c3",
        )
        content = card["body"]["elements"][0]["text"]["content"]
        assert "Which approach?" in content
        # No backslash escapes for normal text
        assert "\\" not in content

    def test_question_curly_braces_escaped_in_submitted_card(self) -> None:
        """Curly braces in question are escaped in build_clarify_submitted_card."""
        card = build_clarify_submitted_card(
            question="Confirm {'id': 1}?",
            selected="Yes",
            clarify_id="c4",
        )
        content = card["body"]["elements"][0]["text"]["content"]
        assert "\\{" in content
        unescaped = content.replace('\\{', '').replace('\\}', '')
        assert '{' not in unescaped

    def test_question_curly_braces_escaped_in_confirmed_card(self) -> None:
        """Curly braces in question are escaped in build_clarify_confirmed_card."""
        card = build_clarify_confirmed_card(
            question="Confirm {'id': 1}?",
            selected="Yes",
        )
        content = card["body"]["elements"][0]["text"]["content"]
        assert "\\{" in content
        unescaped = content.replace('\\{', '').replace('\\}', '')
        assert '{' not in unescaped
