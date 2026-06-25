"""hermes-lark-streaming — Feishu/Lark CardKit v2.0 streaming cards for Hermes Agent.

Architecture Overview
────────────────────
This plugin intercepts Hermes's message pipeline and renders real-time
streaming cards with typewriter effect, unified agent process panels,
and proactive TTL extension.

The unified panel architecture (v1.0.2+) replaces the old segment-based
approach with a single collapsible panel element that holds all reasoning
rounds and tool steps.  This reduces card elements from potentially 50+
to just 3-4, eliminating the need for element counting, card splitting,
and progressive degradation.

Module Organization (v1.1.0)
────────────────────────────
Configuration:
  config/                     Sub-package
    __init__.py               Re-exports: Config
    reader.py                 Config reader (Hermes config.yaml, hot reload)

Feishu API:
  feishu/                     Sub-package
    __init__.py               Re-exports: FeishuClient, FeishuAPIError, UnavailableGuard, etc.
    client.py                 FeishuClient (Lark SDK wrapper, transient retry, 300313 retry)
    guard.py                  UnavailableGuard (message-deleted protection)

Flush Throttle:
  flush/                      Sub-package
    __init__.py               Re-exports: FlushController, constants
    controller.py             FlushController (throttle scheduler)

Core Controller:
  controller/                 Sub-package
    __init__.py               Re-exports: StreamCardController, CardSession, states
    core.py                   StreamCardController (singleton, manages sessions)
    mixin.py                  ControllerMixin (cron/gateway deliver, shared utilities)
    linear_mixin.py           UnifiedControllerMixin (unified panel flush/seal — main path)

Card Building:
  cardkit/                    Sub-package
    __init__.py               Re-exports from elements/cards/special
    elements.py               Primitive element builders (unified panel, panels, footers)
    cards.py                  Card assemblers (streaming, complete, unified)
    special.py                Specialized cards (cron, gateway, clarify)
    md.py                     Markdown processing (downgrade, split, optimize)
    i18n.py                   i18n zh/en bilingual text mapping

State & Data:
  state/                      Sub-package
    __init__.py               Re-exports: CardSession, TextState, UnifiedLinearState, etc.
    session.py                CardSession (per-message state, _creation_stages set)
    linear.py                 UnifiedLinearState + ReasoningRound
    phase.py                  CardPhase / TerminalReason state machine
    text.py                   TextState (incremental text tracking)
    tooluse.py                ToolUseTracker (tool call visualization + redaction)

Runtime Patching:
  patching/                   Sub-package
    __init__.py               Entry point + shared state (apply_patches) + re-exports
    hermes_adapter.py         HermesCompat (isolates all Hermes internal module access)
    gateway.py                GatewayRunner wrappers, cron
    callbacks.py              Callback wrapping (answer, thinking, tool, reasoning)
    adapter.py                FeishuAdapter interception (send, edit, reactions, clarify)
    hooks.py                  Hook functions (on_message_started, on_answer_delta, etc.)

Monitoring:
  aowen/                      Sub-package
    __init__.py               /aowen command system (pre_gateway_dispatch hook + metrics + cards)

Plugin Entry:
  plugin/                     Sub-package
    __init__.py               register()/unregister() + config backup + FeishuClient pre-warm

CLI Entry:
  __main__.py                 CLI (status, verify, doctor, cleanup, python)

Logging
───────
Plugin logger name: ``hermes_lark_streaming``
  - Inherits level from Hermes root logger (set by config.yaml ``logging.level``)
  - Logs to ``agent.log`` (catch-all), NOT routed to ``gateway.log``
  - v1.1.0: unified log prefix ``HLS:`` (replaced HLS_DIAG/HLS_WRAP/HLS_CALLED/HLS_FIX)
  - No explicit ``setLevel()`` — level follows Hermes config automatically

Unified Panel Architecture
──────────────────────────
Key invariant: The card contains at most 4 top-level elements:

1. **Unified panel** (``UNIFIED_PANEL_ELEMENT_ID``): A single
   ``collapsible_panel`` that holds all reasoning rounds and tool steps.
   Internal children are rendered in **chronological order** (via
   ``panel_events`` timeline), interleaving reasoning and tools as they
   actually occurred, rather than grouping all reasoning before all tools.
   Sub-elements do NOT count toward the Feishu 200-element card limit.

2. **Answer streaming element** (``ANSWER_ELEMENT_ID``): Receives
   answer text via ``cardkit_stream_element``.

3. **Loading hint** (``_LOADING_HINT_ELEMENT_ID``): Context loading
   placeholder, removed when first content arrives (deletion confirmed
   only after API success).

4. **Loading icon** (``_LOADING_ELEMENT_ID``): Spinner, removed on seal.

This eliminates:
  - Element counting / thresholds
  - Card splitting / rollover
  - Progressive degradation (compact/minimal seal)

Performance improvements:
  - Phase 1 placeholder card has only 2 elements (loading hint + icon)
  - Client pre-warming eliminates first-message latency
  - Default flush interval 200ms (configurable 70~2000ms)
  - 300309 stream-closed fallback ensures content delivery on long conversations
  - Element existence tracking eliminates 300314 seal failures
"""

import logging
from pathlib import Path

_logger = logging.getLogger("hermes_lark_streaming")

_plugin_yaml = Path(__file__).resolve().parent / "plugin.yaml"
if _plugin_yaml.exists():
    for _line in _plugin_yaml.read_text(encoding="utf-8").splitlines():
        if _line.startswith("version:"):
            __version__ = _line.split(":", 1)[1].strip().strip('"').strip("'")
            break
    else:
        __version__ = "unknown"
        _logger.warning("plugin.yaml exists but no 'version:' field found")
else:
    __version__ = "unknown"
    _logger.warning("plugin.yaml not found at %s — installation may be broken", _plugin_yaml)

# Conditional import: relative import works when loaded by Hermes's
# plugin loader (spec_from_file_location with __package__ set);
# absolute import works when pytest imports this file directly
# (conftest.py pre-registers the package in sys.modules).
try:
    from .plugin import register
except ImportError:
    from hermes_lark_streaming.plugin import register  # type: ignore[no-redef]

__all__ = ["register", "__version__"]
