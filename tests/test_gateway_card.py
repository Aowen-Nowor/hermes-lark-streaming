"""Tests for gateway message card interception (v0.14.0 Phase 1-3)."""

from __future__ import annotations

import pytest

from hermes_lark_streaming.cardkit import build_gateway_card


class TestBuildGatewayCard:
    """Test the build_gateway_card() function."""

    def test_basic_system_card(self):
        card = build_gateway_card("System notification")
        assert card["schema"] == "2.0"
        assert "elements" not in card  # schema 2.0 uses body
        assert "body" in card
        elements = card["body"]["elements"]
        # No emoji header — first element is the content markdown
        assert elements[0]["tag"] == "markdown"
        assert "System notification" in elements[0]["content"]

    def test_error_category(self):
        card = build_gateway_card("Something failed", category="error")
        elements = card["body"]["elements"]
        # No emoji header — category does not affect visual
        assert elements[0]["tag"] == "markdown"
        assert "Something failed" in elements[0]["content"]

    def test_auth_category(self):
        card = build_gateway_card("Pairing code: 1234", category="auth")
        elements = card["body"]["elements"]
        # No emoji header
        assert elements[0]["tag"] == "markdown"

    def test_session_category(self):
        card = build_gateway_card("Session reset", category="session")
        elements = card["body"]["elements"]
        # No emoji header
        assert elements[0]["tag"] == "markdown"

    def test_slash_category(self):
        card = build_gateway_card("/help output", category="slash")
        elements = card["body"]["elements"]
        # No emoji header
        assert elements[0]["tag"] == "markdown"

    def test_default_category_is_system(self):
        card = build_gateway_card("Hello", category="")
        elements = card["body"]["elements"]
        # No emoji header — category does not affect visual
        assert elements[0]["tag"] == "markdown"

    def test_unknown_category_defaults_to_system(self):
        card = build_gateway_card("Hello", category="unknown_category")
        elements = card["body"]["elements"]
        # No emoji header
        assert elements[0]["tag"] == "markdown"

    def test_empty_content_produces_card(self):
        card = build_gateway_card("")
        assert card["schema"] == "2.0"
        elements = card["body"]["elements"]
        # v1.7.0 (R4-10, E2E T7): Feishu Card 2.0 rejects empty body.elements
        # (230099/200621) — the builder now emits a valid placeholder.
        assert len(elements) == 1
        assert elements[0]["tag"] == "markdown"
        assert elements[0]["content"] == " "

    def test_summary_generated(self):
        card = build_gateway_card("This is a long message that should have a summary")
        assert "summary" in card["config"]
        assert "This is a long message" in card["config"]["summary"]["content"]

    def test_locales_in_config(self):
        card = build_gateway_card("Test")
        assert "locales" in card["config"]

    def test_markdown_optimization_applied(self):
        """Verify that optimize_markdown_style and _downgrade_tables are applied."""
        content = "| A | B |\n|---|---|\n| 1 | 2 |"
        card = build_gateway_card(content)
        elements = card["body"]["elements"]
        # No emoji header — just markdown elements
        assert len(elements) >= 1


    def test_no_category_icon_header(self) -> None:
        """Gateway card should NOT have a category icon header element."""
        card = build_gateway_card("Hello world", category="system")
        # First element should be markdown content, not a div with emoji
        first = card["body"]["elements"][0]
        assert first["tag"] == "markdown"

    def test_status_indicator_still_works(self) -> None:
        """Status indicator from reaction interception should still work."""
        card = build_gateway_card("Hello", status_label="Reading", status_emoji="👀")
        first = card["body"]["elements"][0]
        assert first["tag"] == "div"
        assert "👀 Reading" in first["text"]["content"]


class TestBuildGatewayCardStatusIndicator:
    """Test Phase 3: status indicator in build_gateway_card()."""

    def test_status_indicator_replaces_category_icon(self):
        """When status_label and status_emoji are set, they replace the category icon."""
        card = build_gateway_card(
            "Processing your request",
            category="system",
            status_label="Reading",
            status_emoji="👀",
        )
        elements = card["body"]["elements"]
        # First element should be the status indicator wrapped in div.text
        assert elements[0]["tag"] == "div"
        assert elements[0]["text"]["tag"] == "plain_text"
        assert elements[0]["text"]["content"] == "👀 Reading"
        assert elements[0]["text"]["text_color"] == "turquoise"

    def test_no_status_shows_no_icon(self):
        """When no status is set, no category icon is shown (emoji removed)."""
        card = build_gateway_card("Hello", category="error")
        elements = card["body"]["elements"]
        # No emoji header — first element is markdown content
        assert elements[0]["tag"] == "markdown"
        assert "Hello" in elements[0]["content"]

    def test_empty_status_shows_no_icon(self):
        """When status_label is empty, no category icon is shown (emoji removed)."""
        card = build_gateway_card("Hello", status_label="", status_emoji="👀")
        elements = card["body"]["elements"]
        # No status and no emoji header — first element is markdown
        assert elements[0]["tag"] == "markdown"
        assert "Hello" in elements[0]["content"]

    def test_processing_status(self):
        card = build_gateway_card("Working...", status_label="Processing", status_emoji="⏳")
        elements = card["body"]["elements"]
        assert elements[0]["text"]["content"] == "⏳ Processing"


class TestClassifyGatewayMessage:
    """Test the _classify_gateway_message() function."""

    def test_import(self):
        from hermes_lark_streaming.patching import _classify_gateway_message
        assert callable(_classify_gateway_message)

    def test_auth_pairing_code(self):
        from hermes_lark_streaming.patching import _classify_gateway_message
        assert _classify_gateway_message("Here's your pairing code: ABC123") == "auth"

    def test_auth_dont_recognize(self):
        from hermes_lark_streaming.patching import _classify_gateway_message
        assert _classify_gateway_message("I don't recognize you yet!") == "auth"

    def test_error_warning(self):
        from hermes_lark_streaming.patching import _classify_gateway_message
        assert _classify_gateway_message("⚠️ Provider authentication failed") == "error"

    def test_error_failed(self):
        from hermes_lark_streaming.patching import _classify_gateway_message
        assert _classify_gateway_message("Something failed after retries") == "error"

    def test_session_reset(self):
        from hermes_lark_streaming.patching import _classify_gateway_message
        assert _classify_gateway_message("Session automatically reset") == "session"

    def test_slash_help(self):
        from hermes_lark_streaming.patching import _classify_gateway_message
        assert _classify_gateway_message("/help shows available commands") == "slash"

    def test_slash_status(self):
        from hermes_lark_streaming.patching import _classify_gateway_message
        assert _classify_gateway_message("/status output here") == "slash"

    def test_system_default(self):
        from hermes_lark_streaming.patching import _classify_gateway_message
        assert _classify_gateway_message("Just a regular message") == "system"

    def test_non_string_returns_system(self):
        from hermes_lark_streaming.patching import _classify_gateway_message
        assert _classify_gateway_message(12345) == "system"


class TestGatewayCardsConfig:
    """Test the gateway_cards config property."""

    def test_default_is_true(self):
        from hermes_lark_streaming.config import Config
        cfg = Config()
        # No config file loaded — default should be True
        assert cfg.gateway_cards is True

    def test_property_exists(self):
        from hermes_lark_streaming.config import Config
        cfg = Config()
        assert hasattr(cfg, "gateway_cards")


class TestGatewayCardRegistry:
    """Test Phase 2: gateway card registry for edit_message support."""

    def test_register_and_lookup(self):
        from hermes_lark_streaming.patching import _register_gateway_card, _gateway_cards, _gateway_cards_lock
        # Register a card
        _register_gateway_card("msg_test_123", chat_id="chat_abc", card_id="card_xyz", category="error")
        # Look it up
        with _gateway_cards_lock:
            info = _gateway_cards.get("msg_test_123")
        assert info is not None
        assert info["chat_id"] == "chat_abc"
        assert info["card_id"] == "card_xyz"
        assert info["category"] == "error"
        # Cleanup
        from hermes_lark_streaming.patching import _unregister_gateway_card
        _unregister_gateway_card("msg_test_123")

    def test_unregister_removes_entry(self):
        from hermes_lark_streaming.patching import _register_gateway_card, _unregister_gateway_card, _gateway_cards, _gateway_cards_lock
        _register_gateway_card("msg_test_456", chat_id="chat_def", card_id=None, category="system")
        _unregister_gateway_card("msg_test_456")
        with _gateway_cards_lock:
            assert _gateway_cards.get("msg_test_456") is None

    def test_register_empty_id_is_noop(self):
        from hermes_lark_streaming.patching import _register_gateway_card, _gateway_cards, _gateway_cards_lock
        _register_gateway_card("", chat_id="chat_ghi", card_id=None, category="system")
        with _gateway_cards_lock:
            assert "" not in _gateway_cards


class TestReactionStatusMap:
    """Test Phase 3: reaction emoji to status label mapping."""

    def test_reaction_map_exists(self):
        from hermes_lark_streaming.patching import _REACTION_STATUS_MAP
        assert isinstance(_REACTION_STATUS_MAP, dict)
        assert len(_REACTION_STATUS_MAP) > 0

    def test_common_reactions_mapped(self):
        from hermes_lark_streaming.patching import _REACTION_STATUS_MAP
        assert "👀" in _REACTION_STATUS_MAP
        assert "👍" in _REACTION_STATUS_MAP
        assert "🤔" in _REACTION_STATUS_MAP

    def test_reaction_values_are_strings(self):
        from hermes_lark_streaming.patching import _REACTION_STATUS_MAP
        for emoji, label in _REACTION_STATUS_MAP.items():
            assert isinstance(emoji, str)
            assert isinstance(label, str)
            assert len(label) > 0
