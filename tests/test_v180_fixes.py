"""v1.8.0 regression tests — production-log audit fixes (P1×2 / P2×3 / P3×5).

Covers the fixes shipped in v1.8.0:
  * P2-3: Phase 2 batch 300315 state re-sync (07-02 incident chain —
    non-idempotent retry collision lost the answer and forced a
    full-rebuild loop); Phase 3 element-id-precise self-healing;
    extract_not_found_element_id parser.
  * P2-1: WS channel-health observability (record_inbound → /aowen
    monitor "渠道健康" section; reset preserves last_inbound_at).
  * P2-2: cron interception log hygiene (debug + neutral wording).
  * P3-3: pruning stale terminal sessions downgraded to debug.
  * P3-4: gateway lifecycle notices (⚠️ Gateway restarting/shutting down)
    classified "lifecycle" instead of "error".
  * P1-1: lark-oapi >= 1.6.4 floor + startup check severity logic
    (hermes >= 0.19 + old SDK → ERROR, otherwise WARNING).
"""

from __future__ import annotations

import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from hermes_lark_streaming.controller import StreamCardController
from hermes_lark_streaming.controller.mixin import STREAMING
from hermes_lark_streaming.cardkit import (
    ANSWER_ELEMENT_ID,
    UNIFIED_PANEL_ELEMENT_ID,
    _LOADING_HINT_ELEMENT_ID,
    _LOADING_ELEMENT_ID,
)
from hermes_lark_streaming.feishu import FeishuAPIError

# Reuse the helpers/loop-cleanup registry from test_controller (the conftest
# autouse fixture closes loops registered there after each test).
from tests.test_controller import _make_session, _setup_ctrl


# ── P2-3: extract_not_found_element_id ────────────────────────────


class TestExtractNotFoundElementId:
    """v1.8.0 (P2-3): 300315 messages name the missing elementID — parse it."""

    def test_parses_named_element_id(self) -> None:
        from hermes_lark_streaming.feishu import extract_not_found_element_id
        e = FeishuAPIError(
            "cardkit_batch_update: code=300315, msg=ErrMsg: not find elementID : context_loading_hint;",
            code=300315,
        )
        assert extract_not_found_element_id(e) == "context_loading_hint"

    def test_parses_named_panel_id(self) -> None:
        from hermes_lark_streaming.feishu import extract_not_found_element_id
        e = FeishuAPIError(
            "cardkit_batch_update: code=300315, msg=ErrMsg: not find elementID : agent_process_panel;",
            code=300315,
        )
        assert extract_not_found_element_id(e) == "agent_process_panel"

    def test_300313_without_name_returns_none(self) -> None:
        from hermes_lark_streaming.feishu import extract_not_found_element_id
        e = FeishuAPIError("cardkit_stream_element: code=300313, msg=element not exist", code=300313)
        assert extract_not_found_element_id(e) is None

    def test_non_element_error_returns_none(self) -> None:
        from hermes_lark_streaming.feishu import extract_not_found_element_id
        e = FeishuAPIError("cardkit_batch_update: code=99991400, msg=rate limited", code=99991400)
        assert extract_not_found_element_id(e) is None


# ── P2-3: Phase 2 state re-sync (07-02 incident chain) ─────────────


class TestPhase2ElementNotFoundV180:
    """v1.8.0 (P2-3): Phase 2 batch 300315 → re-sync creation state + keep dirty.

    Root cause (07-02 production): batch_update is not idempotent. A prior
    attempt applied server-side (answer inserted, hint deleted) but its
    response was lost; the retry collides with 300315 "not find elementID:
    context_loading_hint". The old handler cleared the dirty flags WITHOUT
    marking the elements as created — every later flush rebuilt the FULL
    add_elements batch, hit 300315 again (loop), and the answer never
    reached the card (text fallback at completion).
    """

    @pytest.mark.asyncio
    async def test_phase2_resyncs_state_and_streams_same_flush(self) -> None:
        """300315 on Phase 2 batch → mark answer/hint state, keep dirty,
        and stream the pending content in the SAME flush (fall-through)."""
        ctrl = _setup_ctrl(linear=True)
        session = _make_session("msg_p2a", linear=True)
        session.state = STREAMING
        session.card_id = "card_p2a"
        # First-flush state: Phase 2 never succeeded from the plugin's view.
        session._creation_stages = set()
        session.existing_elements = {_LOADING_HINT_ELEMENT_ID, _LOADING_ELEMENT_ID}
        session.unified_state.on_answer_delta("hello answer")
        ctrl._sessions["msg_p2a"] = session

        err = FeishuAPIError(
            "cardkit_batch_update: code=300315, msg=ErrMsg: not find elementID : context_loading_hint;",
            code=300315,
        )
        ctrl._client.cardkit_batch_update = AsyncMock(side_effect=err)
        ctrl._client.cardkit_stream_element = AsyncMock()

        await ctrl._do_unified_flush(session)

        # Re-synced to what the server actually has.
        assert "answer" in session._creation_stages, (
            "Phase 2 300315 must mark the answer element as created "
            "(prior batch applied server-side)"
        )
        assert ANSWER_ELEMENT_ID in session.existing_elements
        assert "hint_removed" in session._creation_stages
        assert _LOADING_HINT_ELEMENT_ID not in session.existing_elements
        # Fall-through: the pending content was streamed in THIS flush —
        # the answer no longer depends on a full batch rebuild.
        ctrl._client.cardkit_stream_element.assert_called_once()
        assert session.unified_state.answer_dirty is False, (
            "answer_dirty must be cleared by the successful stream_element "
            "call, not by the old clear-and-drop path"
        )

    @pytest.mark.asyncio
    async def test_phase2_resync_then_second_flush_is_incremental(self) -> None:
        """After the re-sync, the next flush must NOT rebuild the full
        add_elements batch (the 07-02 full-rebuild loop is gone)."""
        ctrl = _setup_ctrl(linear=True)
        session = _make_session("msg_p2b", linear=True)
        session.state = STREAMING
        session.card_id = "card_p2b"
        session._creation_stages = set()
        session.existing_elements = {_LOADING_HINT_ELEMENT_ID, _LOADING_ELEMENT_ID}
        session.unified_state.on_answer_delta("hello answer")
        ctrl._sessions["msg_p2b"] = session

        err = FeishuAPIError(
            "cardkit_batch_update: code=300315, msg=ErrMsg: not find elementID : context_loading_hint;",
            code=300315,
        )
        ctrl._client.cardkit_batch_update = AsyncMock(side_effect=err)
        ctrl._client.cardkit_stream_element = AsyncMock()

        # First flush — 300315 collision, state re-synced, content streamed.
        await ctrl._do_unified_flush(session)
        assert "answer" in session._creation_stages

        # New content arrives → second flush must be answer-stream only.
        session.unified_state.on_answer_delta(" and more")
        captured_actions: list[list[dict]] = []

        async def capture(card_id: str, actions: list[dict], **kw: object) -> None:
            captured_actions.append(actions)

        ctrl._client.cardkit_batch_update = AsyncMock(side_effect=capture)
        ctrl._client.cardkit_stream_element = AsyncMock()
        await ctrl._do_unified_flush(session)

        for actions_batch in captured_actions:
            for action in actions_batch:
                assert action.get("action") != "add_elements", (
                    "Phase 2 re-sync must switch later flushes to the "
                    "incremental stream path — no full add_elements rebuild"
                )
        ctrl._client.cardkit_stream_element.assert_called_once()

    @pytest.mark.asyncio
    async def test_phase2_resync_includes_panel_when_batch_had_it(self) -> None:
        """When the colliding batch carried the panel, the re-sync marks the
        panel as created too (Phase 3 update path takes over)."""
        ctrl = _setup_ctrl(linear=True)
        session = _make_session("msg_p2c", linear=True)
        session.state = STREAMING
        session.card_id = "card_p2c"
        session._creation_stages = set()
        session.existing_elements = {_LOADING_HINT_ELEMENT_ID, _LOADING_ELEMENT_ID}
        # Reasoning first → panel_visible in the batch.
        session.unified_state.on_reasoning_delta("thinking hard")
        time.sleep(0.01)
        session.unified_state.on_answer_delta("answer text")
        ctrl._sessions["msg_p2c"] = session

        err = FeishuAPIError(
            "cardkit_batch_update: code=300315, msg=ErrMsg: not find elementID : context_loading_hint;",
            code=300315,
        )
        ctrl._client.cardkit_batch_update = AsyncMock(side_effect=err)
        ctrl._client.cardkit_stream_element = AsyncMock()

        await ctrl._do_unified_flush(session)

        assert "panel" in session._creation_stages
        assert UNIFIED_PANEL_ELEMENT_ID in session.existing_elements
        assert "answer" in session._creation_stages


# ── P2-3: Phase 3 element-id-precise self-healing ──────────────────


class TestPhase3ElementIdSelfHealingV180:
    """v1.8.0 (P2-3): Phase 3 parses the NAMED element — a missing panel
    drops its tracking so the next flush re-adds it (also guards the
    Phase 2 re-sync's rare panel mis-mark race)."""

    @pytest.mark.asyncio
    async def test_phase3_panel_not_found_drops_panel_tracking(self) -> None:
        ctrl = _setup_ctrl(linear=True)
        session = _make_session("msg_p3a", linear=True)
        session.state = STREAMING
        session.card_id = "card_p3a"
        session._creation_stages = {"answer", "panel", "hint_removed"}
        session.existing_elements = {
            ANSWER_ELEMENT_ID,
            UNIFIED_PANEL_ELEMENT_ID,
            _LOADING_ELEMENT_ID,
        }
        session.unified_state.on_reasoning_delta("think")
        ctrl._sessions["msg_p3a"] = session

        err = FeishuAPIError(
            "cardkit_batch_update: code=300315, msg=ErrMsg: not find elementID : agent_process_panel;",
            code=300315,
        )
        ctrl._client.cardkit_batch_update = AsyncMock(side_effect=err)
        ctrl._client.cardkit_stream_element = AsyncMock()

        await ctrl._do_unified_flush(session)

        assert "panel" not in session._creation_stages, (
            "Phase 3 must drop panel tracking when the panel element itself "
            "is named as missing — next flush re-adds it"
        )
        assert UNIFIED_PANEL_ELEMENT_ID not in session.existing_elements
        # Panel dirty preserved → the add path retries with content.
        assert session.unified_state.panel_dirty or session.unified_state.tool_steps_dirty

    @pytest.mark.asyncio
    async def test_phase3_unnamed_element_keeps_v141_behavior(self) -> None:
        """300313 (no named id) keeps the v1.4.1 behavior: sync hint only."""
        ctrl = _setup_ctrl(linear=True)
        session = _make_session("msg_p3b", linear=True)
        session.state = STREAMING
        session.card_id = "card_p3b"
        session._creation_stages = {"answer", "panel"}
        session.existing_elements = {
            ANSWER_ELEMENT_ID,
            UNIFIED_PANEL_ELEMENT_ID,
            _LOADING_HINT_ELEMENT_ID,
            _LOADING_ELEMENT_ID,
        }
        session.unified_state.on_reasoning_delta("think")
        ctrl._sessions["msg_p3b"] = session

        err = FeishuAPIError(
            "cardkit_batch_update: code=300313, msg=element not exist",
            code=300313,
        )
        ctrl._client.cardkit_batch_update = AsyncMock(side_effect=err)
        ctrl._client.cardkit_stream_element = AsyncMock()

        await ctrl._do_unified_flush(session)

        # v1.4.1 behavior unchanged: hint synced, panel tracking kept.
        assert "hint_removed" in session._creation_stages
        assert "panel" in session._creation_stages
        assert UNIFIED_PANEL_ELEMENT_ID in session.existing_elements


# ── P2-1: WS channel-health observability ──────────────────────────


class TestRecordInboundV180:
    """v1.8.0 (P2-1): record_inbound / get_metrics / monitor card / reset."""

    def _aowen(self):
        # Import the package-qualified module — the same instance the
        # plugin code (patching/hooks.py) writes to.
        from hermes_lark_streaming import aowen
        return aowen

    def test_record_inbound_updates_counter_and_timestamp(self) -> None:
        aowen = self._aowen()
        old = aowen.get_metrics()
        before = old["inbound_messages"]
        t0 = time.time()
        aowen.record_inbound()
        m = aowen.get_metrics()
        assert m["inbound_messages"] == before + 1
        assert m["last_inbound_at"] is not None and m["last_inbound_at"] >= t0

    def test_get_metrics_derives_last_inbound_age_human(self) -> None:
        aowen = self._aowen()
        aowen.record_inbound()
        m = aowen.get_metrics()
        assert m["last_inbound_age_human"].endswith("前")

    def test_monitor_card_contains_channel_health_section(self) -> None:
        aowen = self._aowen()
        aowen.record_inbound()
        card = aowen.build_monitor_card()
        body_md = str(card)
        assert "渠道健康" in body_md
        assert "最近入站" in body_md
        assert "入站消息" in body_md

    def test_monitor_reset_preserves_last_inbound_at(self) -> None:
        aowen = self._aowen()
        aowen.record_inbound()
        ts = aowen.get_metrics()["last_inbound_at"]
        aowen._do_reset()
        m = aowen.get_metrics()
        assert m["inbound_messages"] == 0
        assert m["last_inbound_at"] == ts, (
            "last_inbound_at is a channel-liveness fact, not a counter — "
            "reset must preserve it"
        )

    def test_on_feishu_normalize_records_inbound_for_feishu(self) -> None:
        from hermes_lark_streaming.patching.hooks import on_feishu_normalize

        ctrl = StreamCardController()
        ctrl._cfg._raw = {
            "hermes_lark_streaming": {"enabled": True, "linear": True},
            "feishu": {"app_id": "app", "app_secret": "secret"},
        }
        aowen = self._aowen()
        before = aowen.get_metrics()["inbound_messages"]

        source = SimpleNamespace(
            platform=SimpleNamespace(value="feishu"), thread_id=None
        )
        event = SimpleNamespace(reply_to_message_id=None, raw_message={"event": {"message": {}}})
        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            on_feishu_normalize(message_id="m_in_1", source=source, event=event)

        assert aowen.get_metrics()["inbound_messages"] == before + 1

    def test_on_feishu_normalize_skips_non_feishu_platform(self) -> None:
        from hermes_lark_streaming.patching.hooks import on_feishu_normalize

        ctrl = StreamCardController()
        ctrl._cfg._raw = {
            "hermes_lark_streaming": {"enabled": True, "linear": True},
            "feishu": {"app_id": "app", "app_secret": "secret"},
        }
        aowen = self._aowen()
        before = aowen.get_metrics()["inbound_messages"]

        source = SimpleNamespace(
            platform=SimpleNamespace(value="telegram"), thread_id=None
        )
        event = SimpleNamespace(reply_to_message_id=None, raw_message={})
        with patch("hermes_lark_streaming.patching.hooks.get_controller", return_value=ctrl):
            on_feishu_normalize(message_id="m_in_2", source=source, event=event)

        assert aowen.get_metrics()["inbound_messages"] == before


# ── P2-2: cron log hygiene ─────────────────────────────────────────


class TestCronLogHygieneV180:
    """v1.8.0 (P2-2): the misleading "intercepted" chorus is debug-level."""

    @pytest.mark.asyncio
    async def test_do_cron_deliver_no_info_chorus(self, caplog) -> None:
        ctrl = _setup_ctrl(linear=True)
        ctrl._client.send_card_to_chat = AsyncMock(return_value="om_cron_1")
        with caplog.at_level(logging.INFO, logger="hermes_lark_streaming"):
            await ctrl._do_cron_deliver("chat_cron", "scheduled content")
        for record in caplog.records:
            assert "cron _do_cron_deliver" not in record.getMessage(), (
                "cron chorus log must be debug-level, not INFO"
            )


# ── P3-3: pruning log level ────────────────────────────────────────


class TestPruningLogLevelV180:
    """v1.8.0 (P3-3): stale terminal-session pruning is debug, not warning."""

    def test_prune_stale_terminal_session_logs_at_debug(self, caplog) -> None:
        ctrl = _setup_ctrl(linear=True)
        session = _make_session("msg_prune_ttl", linear=True)
        session.created_at = time.time() - 999999  # way past TTL
        from hermes_lark_streaming.controller.mixin import COMPLETED
        session.state = COMPLETED
        ctrl._sessions["msg_prune_ttl"] = session

        with caplog.at_level(logging.WARNING, logger="hermes_lark_streaming"):
            ctrl._prune_stale_sessions()

        for record in caplog.records:
            assert "pruning stale terminal session" not in record.getMessage(), (
                "routine terminal-session pruning must not log at WARNING "
                "(180 occurrences / 2 months in production, zero real incidents)"
            )
        assert "msg_prune_ttl" not in ctrl._sessions


# ── P3-4: lifecycle classification ─────────────────────────────────


class TestLifecycleClassificationV180:
    """v1.8.0 (P3-4): shutdown/restart notices are "lifecycle", not "error".

    hermes _notify_active_sessions_of_shutdown hardcodes
    "⚠️ Gateway restarting — …" / "⚠️ Gateway shutting down — …"; the ⚠️
    prefix used to land them in the error bucket (13/13 production stops
    misclassified).
    """

    CASES = [
        "⚠️ Gateway restarting — Your current task will be interrupted. Send any message after restart and I'll try to resume where you left off.",
        "⚠️ Gateway shutting down — Your current task will be interrupted.",
        "⏳ Gateway restarting — queued for the next turn after it comes back.",
        "⏳ Gateway is restarting and is not accepting another turn right now.",
        "⏳ Draining 2 active agent(s) before restart...",
        "⏳ 正在等待 2 个活跃代理结束后重启...",
        "♻ 正在重启网关。如果 60 秒内没有收到通知，请在控制台运行 `hermes gateway restart` 重启。",
    ]

    @pytest.mark.parametrize("content", CASES)
    def test_lifecycle_notices(self, content: str) -> None:
        from hermes_lark_streaming.patching.adapter import _classify_gateway_message
        assert _classify_gateway_message(content) == "lifecycle"

    def test_error_with_warning_emoji_still_error(self) -> None:
        """Regression guard: real errors keep the error classification."""
        from hermes_lark_streaming.patching.adapter import _classify_gateway_message
        assert _classify_gateway_message("⚠️ Provider authentication failed") == "error"

    def test_plain_still_system(self) -> None:
        from hermes_lark_streaming.patching.adapter import _classify_gateway_message
        assert _classify_gateway_message("Just a regular message") == "system"


# ── P1-1: lark-oapi floor startup check ────────────────────────────


class TestLarkOapiFloorV180:
    """v1.8.0 (P1-1): startup check severity logic.

    hermes >= 0.19 passes extra_ua_tags (lark-oapi >= 1.6.4 only) — that
    combination breaks the feishu WS connect (07-21 production outage),
    so it logs ERROR; everything else below the floor logs WARNING.
    """

    def _run(self, versions: dict[str, str], caplog) -> None:
        from hermes_lark_streaming.plugin import _check_lark_oapi_floor
        with patch("importlib.metadata.version", side_effect=lambda name: versions[name]), \
                caplog.at_level(logging.DEBUG, logger="hermes_lark_streaming"):
            _check_lark_oapi_floor()

    def test_below_floor_with_new_hermes_logs_error(self, caplog) -> None:
        self._run({"lark-oapi": "1.5.1", "hermes-agent": "0.21.0"}, caplog)
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert errors, "old SDK + hermes >= 0.19 must log at ERROR (WS will break)"
        assert "1.6.4" in errors[0].getMessage()

    def test_below_floor_with_old_hermes_logs_warning_not_error(self, caplog) -> None:
        self._run({"lark-oapi": "1.5.1", "hermes-agent": "0.18.2"}, caplog)
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        warns = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert not errors, "old hermes does not pass extra_ua_tags — not an ERROR"
        assert warns, "still below our declared floor — WARNING expected"

    def test_below_floor_with_unknown_hermes_logs_warning(self, caplog) -> None:
        from hermes_lark_streaming.plugin import _check_lark_oapi_floor

        def _version(name: str) -> str:
            if name == "lark-oapi":
                return "1.5.1"
            raise Exception("no metadata")

        with patch("importlib.metadata.version", side_effect=_version), \
                caplog.at_level(logging.DEBUG, logger="hermes_lark_streaming"):
            _check_lark_oapi_floor()
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        warns = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert not errors
        assert warns

    def test_at_floor_is_silent(self, caplog) -> None:
        self._run({"lark-oapi": "1.6.4", "hermes-agent": "0.21.0"}, caplog)
        assert not [
            r for r in caplog.records
            if r.levelno in (logging.WARNING, logging.ERROR)
        ]

    def test_above_floor_is_silent(self, caplog) -> None:
        self._run({"lark-oapi": "1.7.3", "hermes-agent": "0.21.0"}, caplog)
        assert not [
            r for r in caplog.records
            if r.levelno in (logging.WARNING, logging.ERROR)
        ]
