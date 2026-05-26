<h1 align="center">hermes-lark-streaming</h1>

<p align="center">
  <img src="https://img.shields.io/badge/项目-Vibe%20Coding-ff69b4" alt="Vibe Coding">
  <a href="https://larkcommunity.feishu.cn/wiki/DKkpwgMcJiglIhk88N4cqJEan5f?from=from_copylink"><img src="https://img.shields.io/badge/docs-知识库-3370FF?logo=feishu&logoColor=white" alt="知识库文档"></a>
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/License-MIT-4caf50.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/python-3.11+-3776AB.svg" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/version-0.8.5-ff9800.svg" alt="Version">
</p>

<p align="center">
<a href="https://applink.feishu.cn/client/message/link/open?token=AmoQJk5dwczIahKlW78ADLU%3D"><img src="https://img.shields.io/badge/官方唯一交流群-中国-red" alt="Official Group"></a>
</p>

<p align="center">
English | <a href="README.zh-CN.md">中文版</a>
</p>

Feishu/Lark CardKit v2.0 streaming cards plugin for Hermes Agent — real-time AI response display with typing effect, tool panels, reasoning, and more.

> Forked from [Cheerwhy/hermes-lark-streaming](https://github.com/Cheerwhy/hermes-lark-streaming)

---

## Features

- **Streaming Cards** — Real-time AI response display in interactive cards with typing effect
- **Linear Mode** — Dynamically renders thinking, tool calls, and answer content in a single card, with automatic card splitting for Feishu's 200-element limit
- **Thinking Process** — Display model reasoning/thinking content
- **Tool Calls** — Real-time tool call status and progress with standard icons and result/error blocks
- **CardKit v2.0** — Prioritizes Feishu CardKit streaming API, auto-falls back to IM PATCH
- **Terminal Cards** — Shows complete results including token usage, elapsed time, and context info
- **Message Protection** — Auto-terminates updates when messages are deleted/recalled, avoiding invalid API calls
- **Image Parsing** — Auto-detects markdown image references, downloads and uploads to replace with Feishu img_key
- **Interrupt Handling** — Handles /stop command and message interrupts, displays interrupt status card and auto-starts new session
- **Cron Push** — Scheduled task results pushed as Feishu cards with Markdown rendering preserved
- **i18n** — Built-in Chinese/English bilingual card text (status, tool panel, thinking labels, etc.), auto-switches based on Feishu client language
- **Plugin Lifecycle** — Install/uninstall via `hermes plugins install/uninstall`, no source file modification required
- **Runtime Patches** — Uses monkey patching instead of AST injection, does not modify source files on disk

![Feature Preview](assets/img1.png)

---

## Quick Start

### Prerequisites

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) (running, with Feishu platform configured)
- Python >= 3.11
- Hermes CLI with plugin system support (`hermes plugins` command available)

### Installation

> 插件会自动读取 Hermes 的 `HERMES_HOME` 环境变量定位安装路径（默认 `~/.hermes`），非默认路径下无需额外操作。

```bash
hermes plugins install https://gitee.com/Aowen-Nowor/hermes-lark-streaming
```

Enter `Y` when prompted to enable the plugin, then restart the gateway:

```bash
hermes gateway restart
```

### Uninstallation

```bash
hermes plugins uninstall hermes-lark-streaming
hermes gateway restart
```

### Verify Installation

```bash
# Check plugin status
python -m hermes_lark_streaming status

# Verify environment compatibility
python -m hermes_lark_streaming verify

# View gateway logs
grep hermes-lark-streaming ~/.hermes/logs/gateway.log
```

---

## Configuration

All configuration items are located under the `streaming:` section in `~/.hermes/config.yaml`.

> **Auto-injection**: When this plugin is first loaded, it automatically adds the `streaming:` section to your `config.yaml` top-level with the defaults below. On uninstall, this section is automatically removed. You only need to manually edit if you want to override specific values.

> **Note**: Hermes also has a native `display.streaming: false` config which controls **CLI/TUI terminal** output. This is unrelated to this plugin's streaming cards.

### Plugin Enable Configuration

Enable this plugin in `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - hermes-lark-streaming
  disabled: []
```

If the `plugins` section doesn't exist, add it manually. You can also enable via command after installation:

```bash
hermes plugins enable hermes-lark-streaming
```

To disable:

```bash
hermes plugins disable hermes-lark-streaming
hermes gateway restart
```

### Available Configuration Options

```yaml
streaming:
  enabled: true              # Enable streaming cards
  linear: true               # Linear mode: single card in-place update with auto card splitting
  panel_expanded: false      # Keep panels (tools, reasoning) expanded in completed cards
  card_ttl_sec: 600         # Card alive detection timeout (seconds)

  footer:
    fields:
      - status
      - elapsed
      - model
      - tokens
      - context
    show_label: true         # Show field labels (true/false)
```

### Feishu Credentials

The plugin reads credentials in the following priority order:

| Priority | Source | Example |
|----------|--------|---------|
| 1 | Environment Variables | `FEISHU_APP_ID`, `FEISHU_APP_SECRET` |
| 2 | File | `~/.hermes/.env` |
| 3 | Config File | `streaming.feishu.app_id` |

```bash
# ~/.hermes/.env example
FEISHU_APP_ID=cli_xxxxxx
FEISHU_APP_SECRET=xxxxxx
FEISHU_BASE_URL=https://open.feishu.cn/open-apis
```

### Reasoning Panel Display

The reasoning panel visibility is controlled by `display.show_reasoning` or `display.platforms.feishu.show_reasoning`:

```yaml
display:
  show_reasoning: true  # Show reasoning panel in Feishu cards
```

---

## Architecture

This plugin uses **runtime monkey patching** instead of AST source injection. When loaded via `hermes plugins install`, it wraps `GatewayRunner`, `AIAgent`, and `Scheduler` methods at runtime — **without modifying any source files on disk**.

### Injection Points

```
hermes-agent
  │
  ├─ gateway/run.py              ← wrapped by hermes-lark-streaming at import
  │   └─ GatewayRunner._handle_message          → NORMALIZE
  │   └─ GatewayRunner._handle_message_with_agent → START + ABORT + INTERRUPT
  │   └─ GatewayRunner._run_agent               → COMPLETE + context
  │
  ├─ agent/conversation_loop.py
  │   └─ run_conversation                       → wraps all 6 callbacks (module-level)
  │
  ├─ run_agent.py
  │   └─ AIAgent.run_conversation               → wraps all 6 callbacks (instance-level backup)
  │       ├─ stream_delta_callback              → ANSWER
  │       ├─ interim_assistant_callback         → THINKING
  │       ├─ tool_progress_callback             → TOOL
  │       ├─ reasoning_callback                 → REASONING
  │       └─ background_review_callback         → BACKGROUND_REVIEW
  │
  ├─ cron/scheduler.py
  │   └─ Scheduler._deliver_result              → CRON (Feishu only)
  │
  └─ hermes-lark-streaming plugin
      ├─ plugin.yaml          — manifest (name, version, hooks)
      ├─ __init__.py          — root package + register export
      ├─ __main__.py          — CLI (status / verify)
      ├─ plugin.py            — register(ctx) entry point (Hermes plugin discovery)
      ├─ monkey_patch.py      — runtime monkey patching (method wrappers)
      ├─ patch.py             — 11 hook functions (called by wrappers)
      ├─ config.py            — config reader
      ├─ controller.py        — StreamCardController (session management)
      ├─ controller_mixin.py  — retry/fallback mixin (non-linear mode)
      ├─ controller_linear_mixin.py — linear mode card orchestration
      ├─ feishu.py            — Feishu Open API client (lark-oapi SDK)
      ├─ cardkit.py           — CardKit v2.0 card builder
      ├─ cardkit_i18n.py      — Chinese/English i18n
      ├─ cardkit_md.py        — Markdown processing
      ├─ linear.py            — linear mode state tracking
      ├─ text.py              — incremental text accumulator
      ├─ tooluse.py           — tool call tracking and visualization
      ├─ image.py             — async image upload
      ├─ flush.py             — throttle refresh scheduler
      └─ unavailable_guard.py — message unavailable protection
```

### Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `lark-oapi` | >= 1.4.0 | Feishu Open API SDK |
| `PyYAML` | >= 6.0 | YAML config parsing |

---

## Linear Mode

Linear mode is the default mode, rendering all content (thinking, tool calls, answer) in a single dynamically updated card.

### How It Works

1. **Segmented Rendering** — Content organized by segments (reasoning, answer, tool)
2. **Auto Card Splitting** — Cards automatically split when approaching Feishu's 200-element limit
3. **Smooth Transition** — Old cards are archived, new cards continue the conversation

### Benefits

- Better user experience with continuous scrolling
- Context preserved in a single view
- Automatic handling of Feishu element limits

### Card Splitting Behavior

The plugin monitors element count and auto-splits in these cases:
- Element count exceeds 180 (reserving 20 for footer and fluctuations)
- Tool segments can split at step boundaries
- Answer segments trigger splitting on overflow

---

## Technical Details

### Runtime Monkey Patching

The plugin injects functionality by wrapping Hermes core class methods instead of modifying source files:

- **Thread Safety** — Uses `contextvars.ContextVar` to propagate message context, with `threading.local()` as fallback (for thread pool scenarios)
- **Idempotency** — Prevents duplicate wrapping via function attribute markers (`_hls_wrapper`)
- **Dual Backup** — Applies patches at both module and instance levels to ensure callbacks are intercepted correctly
- **Delayed Loading** — Retries direct patch after 5 seconds to ensure Hermes modules are fully loaded

### Message Protection (UnavailableGuard)

Terminates update pipeline when messages are deleted/recalled:

- Monitors Feishu API error codes (231003, 1000023, 230011)
- Caches unavailable message states (30-minute TTL)
- Avoids invalid API calls on deleted messages

### Throttle Refresh (FlushController)

Controls card update frequency to avoid API rate limiting:

- CardKit mode: 50ms throttle
- IM PATCH mode: 150ms throttle
- Wait mechanism: Ensures all updates are flushed before completion

### Text State Management (TextState)

Incremental text accumulation and boundary detection:

- Tracks flushed and pending text
- Reasoning tag separation (`<thinking>`/`</thinking>`)
- Avoids duplicate rendering of same content

---

## FAQ

### Plugin Loading Failed

**Problem**: No card effect after installation, Hermes still replies plain text

**Solution**:
1. Check if plugin is correctly installed: `hermes plugins list`
2. View gateway logs: `grep hermes-lark-streaming ~/.hermes/logs/gateway.log`
3. Verify Feishu credentials: `python -m hermes_lark_streaming status`
4. Check for backup directory interference: `ls -la ~/.hermes/plugins/ | grep bak`, delete if exists

### Duplicate Card Content

**Problem**: Reply text appears twice in the card

**Cause**: Hermes calls `run_conversation` multiple times in the same session, causing callbacks to be wrapped repeatedly

**Solution**: Plugin has built-in duplicate wrap guard (v0.8.5), uses `_hls_wrapper` attribute to mark wrapped callbacks

### Card Element Limit Exceeded

**Problem**: Card update fails during long conversations

**Solution**: Enable linear mode (`streaming.linear: true`), plugin will auto-split cards

### Second and Subsequent Messages Have No Streaming Updates

**Problem**: First message is normal, subsequent messages only show marquee and Done

**Cause**: `contextvars.ContextVar` doesn't propagate across threads, message context unavailable in thread pool

**Solution**: Plugin uses `threading.local()` as fallback (v0.8.5), ensuring correct context propagation in cross-thread scenarios

---

## Changelog

### v0.8.5 (2026-05-26)

Contains 5 bug fixes:

| Fix | Date | Issue |
|-----|------|-------|
| fix1 | 2026-05-25 | Plugin root missing `__init__.py` causing load failure |
| fix2 | 2026-05-25 | Callback duplicate wrapping causing content duplication |
| fix3 | 2026-05-25 | `setattr` indentation error causing syntax exception |
| fix4 | 2026-05-26 | `contextvars` not crossing threads causing subsequent messages to lose streaming |
| fix5 | 2026-05-26 | `_set_thread_local_ctx()` undefined + backup directory interference |

---

## Acknowledgments