<h1 align="center">hermes-lark-streaming</h1>

<p align="center">
  <img src="https://img.shields.io/badge/Project-Vibe%20Coding-ff69b4" alt="Vibe Coding">
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/License-MIT-4caf50.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/python-3.11+-3776AB.svg" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/version-1.3.6-ff9800.svg" alt="Version">
</p>

<p align="center">
<a href="mailto:zhengyu.pu@petalmail.com"><img src="https://img.shields.io/badge/Email-zhengyu.pu%40petalmail.com-9C27B0?logo=gmail&logoColor=white" alt="Email"></a>
<a href="https://applink.feishu.cn/client/message/link/open?token=AmoQJk5dwczIahKlW78ADLU%3D"><img src="https://img.shields.io/badge/The_Only_Official_Group-China-red" alt="The Only Official Group"></a>
<a href="https://larkcommunity.feishu.cn/wiki/DKkpwgMcJiglIhk88N4cqJEan5f?from=from_copylink"><img src="https://img.shields.io/badge/docs-Knowledge_Base-3370FF?logo=feishu&logoColor=white" alt="Knowledge Base"></a>
</p>

<p align="center">
English | <a href="README.zh-CN.md">中文版</a>
</p>

Feishu/Lark CardKit v2.0 streaming cards plugin for Hermes Agent — real-time AI response display with typing effect, unified collapsible panel, chronological reasoning/tool display, and more.

> Based on [Cheerwhy/hermes-lark-streaming](https://github.com/Cheerwhy/hermes-lark-streaming) v0.7.0, with extensive refactoring and optimizations
>
> ⚠️ **Incompatible with the upstream plugin** — if you have the original `Cheerwhy/hermes-lark-streaming` installed, please uninstall it first before installing this version.

---

## Effect Preview

<table align="center">
  <tr>
    <td><img src="assets/screenshots/img1.png" width="200px" /></td>
    <td><img src="assets/screenshots/img2.png" width="200px" /></td>
    <td><img src="assets/screenshots/img3.png" width="200px" /></td>
    <td><img src="assets/screenshots/img4.png" width="200px" /></td>
  </tr>
</table>

---

## Quick Start

### Prerequisites

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) (running, with Feishu platform configured)
- Hermes CLI with plugin system support (`hermes plugins` command available)

### Installation

> **💡 Smart Install Prompt**: Copy the following prompt to Hermes Agent, and it will automatically complete the installation:
> 
> ```
> Help me install Feishu Ao-style Cards:
> - Gitee: https://gitee.com/Aowen-Nowor/hermes-lark-streaming/raw/github_sync/docs/AGENT_GUIDE.md
> - GitHub: https://raw.githubusercontent.com/Aowen-Nowor/hermes-lark-streaming/github_sync/docs/AGENT_GUIDE.md
> ```

> The plugin automatically reads the `HERMES_HOME` environment variable to locate the installation path (`~/.hermes` by default). No extra steps are needed for non-default paths.

**Gitee**
> Choose either SSH or HTTPS:
```bash
# Gitee (SSH)
hermes plugins install git@gitee.com:Aowen-Nowor/hermes-lark-streaming.git
# Gitee (HTTPS)
hermes plugins install https://gitee.com/Aowen-Nowor/hermes-lark-streaming
```
**GitHub**
> Choose either SSH or HTTPS:
```bash
# GitHub (SSH)
hermes plugins install git@github.com:Aowen-Nowor/hermes-lark-streaming.git
# GitHub (HTTPS)
hermes plugins install https://github.com/Aowen-Nowor/hermes-lark-streaming
```

Enter `Y` when prompted to enable the plugin, then restart the gateway:

```bash
hermes gateway restart
```

### Update

```bash
hermes plugins update hermes-lark-streaming
hermes gateway restart
```

### Uninstallation

```bash
# 1. Clean up injected config (while plugin code is still available)
# Auto-detect Hermes Python path:
HERMES_PYTHON=$(python3 ~/.hermes/plugins/hermes-lark-streaming/__main__.py python)
$HERMES_PYTHON ~/.hermes/plugins/hermes-lark-streaming/__main__.py cleanup

# 2. Remove plugin
hermes plugins uninstall hermes-lark-streaming

# 3. Restart gateway
hermes gateway restart
```

### Verify Installation

```bash
hermes plugins list
grep hermes_lark_streaming ~/.hermes/logs/agent.log
# Auto-detect Hermes Python path:
HERMES_PYTHON=$(python3 ~/.hermes/plugins/hermes-lark-streaming/__main__.py python)
$HERMES_PYTHON ~/.hermes/plugins/hermes-lark-streaming/__main__.py status
$HERMES_PYTHON ~/.hermes/plugins/hermes-lark-streaming/__main__.py verify
$HERMES_PYTHON ~/.hermes/plugins/hermes-lark-streaming/__main__.py doctor
```

> **Troubleshooting**: If no card effect appears, check: (1) `hermes plugins list` shows enabled; (2) no `*.bak` directories under `~/.hermes/plugins/`; (3) Feishu credentials are configured. The `doctor` command provides a one-stop diagnostic covering plugin version, Python environment, config, Feishu credentials, patch status, and log paths.

---

## Configuration

All settings go under the `hermes_lark_streaming:` section in `~/.hermes/config.yaml`. The plugin auto-injects defaults on first load; run `cleanup` before uninstalling to remove them.

```yaml
hermes_lark_streaming:
  panel_expanded: false            # Keep panels expanded in completed cards
  streaming_panel_expanded: false  # Keep panels expanded during streaming
  print_strategy: delay            # "fast" (instant) or "delay" (smoother typewriter, default)
  print_step: 4                    # Typewriter chars per render (default 4, range 1-10, Feishu 7.23+)
  flush_interval_ms: 200           # Plugin send interval in ms (70-2000, default 200)
  card_ttl_sec: 600               # Card alive detection timeout (seconds)
  max_tool_steps: 20               # Max tool steps shown in panel (default 20, range 1–100)
  max_reasoning_rounds: 20         # Max reasoning rounds shown in panel (default 20, range 1–100)
  header:
    enabled: false                  # Card header (blue Processing → green Completed / red Error-Stopped). Default off. See below

  footer:
    show_label: false              # Show field labels
    fields:
      - [status, elapsed, model, cost, compression_exhausted]
      # Available fields:
      #   status      — Reply status (Completed / Error / Stopped)
      #   elapsed     — AI response elapsed time
      #   model       — Model name used
      #   cost        — Estimated cost with trust indicator ($0.023 est. / $0.023 actual / Free)
      #   compression_exhausted — Context window is full (⚠ Context Full)
      # Fields below are not shown by default — add them to the fields list to enable:
      #   cache       — Cache hit rate (cache_read/total_input hit%)
      #   tokens      — Token usage (↑ input ↓ output 💭 reasoning)
      #   context     — Context window usage (used/total percentage)
      #   api_calls   — Number of API calls in this session
      #   history_offset — Conversation history offset; larger = longer history, sudden decrease = context compression
      # Each inner list is one row in the footer; fields only shown when they have values
```

### Card Header (`header.enabled`)

When `header.enabled: true`, the plugin displays a status header at the top of agent reply cards:

- **Streaming**: blue header, "Processing..."
- **Completed**: green header, "Completed"
- **Error**: red header, "Error"
- **Stopped** (interrupted): red header, "Stopped"

Default is `false` (off) — cards have no header, matching v1.1.x behavior.

> **Note**: Due to a Feishu CardKit API limitation, the settings/batch_update interfaces **cannot update the card-level header** during streaming or incremental seal. Only a full card rebuild (`cardkit_update`) can change the header color. Therefore, **when `header.enabled` is on, the seal path switches to full card rebuild** (instead of the default incremental seal) so the header color transitions correctly (blue → green/red). When off, the default incremental seal is used (better performance).

> **Scope**: `header.enabled` only affects agent streaming/completed cards. Cron push cards and gateway-internal message cards are unaffected. `/aowen` command cards always have their own banner-style header (part of the v1.1.0 design language) and are not controlled by this option.


### Reasoning Panel Display

```yaml
display:
  show_reasoning: true  # Show reasoning content in the unified panel
```

### Unified Panel Overflow Compression

Feishu Card 2.0 has a **hard limit of 200 elements/components** per card. Exceeding it triggers error `300305 (element exceeds the limit)`, which causes card sealing to fail and triggers a plain-text fallback — resulting in duplicate content visible to users.

> **Element counting rule**: Every JSON object with a `tag` property counts as 1 element, including deeply nested ones like `standard_icon`, `plain_text`, `lark_md`, etc.

#### Element Cost Breakdown

| Component | Elements | Notes |
|-----------|----------|-------|
| Panel container | 1 | `collapsible_panel` |
| Panel title | 2 | `plain_text` + `standard_icon` |
| Each reasoning round (max) | 4 | Title row `div`+`standard_icon`+`lark_md` + reasoning text `markdown` |
| Each tool step (max) | 7 | Title row `div`+`standard_icon`+`lark_md` + detail row `div`+`plain_text` + result row `div`+`lark_md` |
| Fold hint (when triggered) | 1 | 1 `markdown` element |
| Answer text | 1–3 | `markdown`; long text may be split |
| Footer | 2 | `hr` + `markdown` |
| Card header (when enabled) | ~3 | `plain_text` + `standard_icon` |
| Error panel (when present) | ~4 | `collapsible_panel` + inner elements |

**Example calculation**: 20 reasoning rounds + 20 tool steps = 20×4 + 20×7 + fixed overhead ≈ 223 (exceeds 200)

Hence the defaults `max_tool_steps=20` + `max_reasoning_rounds=20`, combined with a fold mechanism, ensure most scenarios stay within limits. Even if a higher config value or an extreme case still exceeds the cap, a built-in **card-level element safety net** kicks in — at seal time all elements are known (panel + answer + footer + error), the actual tag object count is recursively computed, and if it exceeds 195 (200 − 5 buffer), the oldest panel children are trimmed first. This guarantees the card never exceeds 200 elements. Answer, footer, and error panel are never trimmed.

#### Configuration

```yaml
hermes_lark_streaming:
  max_tool_steps: 20           # Max tool steps shown in unified panel (default 20, range 1–100)
  max_reasoning_rounds: 20     # Max reasoning rounds shown in unified panel (default 20, range 1–100)
```

When the limit is exceeded, early items are collapsed into a single summary line, e.g.: `⚡ 10 early reasoning rounds, 5 early tool steps collapsed`

The panel title always shows the **actual total** (e.g. "3 rounds · 44 tools"); the fold hint only affects what is displayed inside the panel.

### /aowen Commands

Send `/aowen` commands in Feishu, the plugin replies with cards directly (bypassing Hermes AI):

| Command | Description |
|---------|-------------|
| `/aowen help` | Show all available commands |
| `/aowen status` | Show plugin status + current config (collapsible panel) |
| `/aowen monitor` | Show metrics dashboard (cards created, API calls, error codes, etc.) |
| `/aowen monitor reset` | Reset metrics counters |
| `/aowen config reload` | After modifying `~/.hermes/config.yaml`, send this command in Feishu to apply immediately, or restart the gateway |
| `/aowen` | Same as `/aowen help` |

> `/aowen` is the plugin's command prefix; all `/aowen` commands are handled by the plugin, not Hermes.

### Feishu Credentials

The plugin reuses Hermes's existing Feishu credentials — no separate configuration needed. Hermes already configures these in `~/.hermes/.env` during installation:

```bash
# ~/.hermes/.env (configured by Hermes, reused by plugin)
FEISHU_APP_ID=cli_xxxxxx
FEISHU_APP_SECRET=xxxxxx
FEISHU_DOMAIN=feishu          # feishu=China, lark=International
```

> The plugin automatically reads Hermes's Feishu credentials and domain settings. If the Hermes Feishu channel works, the plugin works too.

---

## Developer Guide & Changelog

> 📖 **[SKILL.md](docs/SKILL.md)** — LLM quick-start guide. Architecture, key design decisions, efficient code modification guide.

> For the full version history, see [CHANGELOG.md](docs/CHANGELOG.md)

> ⚠️ **Important Notice:** If upgrading from v1.0.1 or below, please follow the uninstallation process to remove the old version and freshly install the new one. Do NOT upgrade via the update command!

---

## How to Submit Issues
> Please refer to the template [ISSUES_TEMPLATE.md](docs/ISSUES_TEMPLATE.md)

## Acknowledgments

<a href="https://github.com/joshcheng820222"><img src="https://avatars.githubusercontent.com/u/26886147?v=4&s=66" alt="joshcheng820222" width="66" height="66"></a> <a href="https://github.com/xuu1998"><img src="https://avatars.githubusercontent.com/u/40609659?v=4&s=66" alt="xuu1998" width="66" height="66"></a> <a href="https://gitee.com/joshchengjoshcheng"><img src="assets/avatars/joshchengjoshcheng.png" alt="joshchengjoshcheng" width="66" height="66"></a> <a href="https://github.com/hmhmdcy"><img src="https://avatars.githubusercontent.com/u/163143682?v=4" alt="hmhmdcy" width="66" height="66"></a>
