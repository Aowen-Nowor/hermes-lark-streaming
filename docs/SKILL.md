# 🧠 hermes-lark-streaming — LLM 快速上手指南

> **Purpose**: 项目技能卡片。阅读后应能理解架构、关键设计决策，并高效修改代码。

---

## 1. 项目概述

**hermes-lark-streaming** 是 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 的飞书/Lark CardKit v2.0 流式卡片插件。AI 对话过程中实时更新飞书交互卡片（打字效果、统一面板、工具步骤、推理过程、完成态统计等）。

| 属性 | 值 |
|------|-----|
| 版本 | 1.6.0 (DEV) | 协议 | MIT | Python | ≥3.11 | 与上游 | ⚠️ **不兼容** |

---

## 2. 架构全景

```
用户消息 → GatewayRunner._handle_message ── [Hook 0: on_feishu_normalize]
               ▼
        _handle_message_with_agent ── [Hook 1/8/9: started/aborted/interrupted]
               ▼
        _run_agent ── [Hook 2: on_message_completed]
               ▼
            ├─ stream_delta_callback ── [Hook 4: on_answer_delta]
            ├─ reasoning_callback ──── [Hook 6: on_reasoning_delta]
            ├─ tool_progress_callback [Hook 3: on_tool_updated]
            └─ background_review_cb ── [Hook 7: on_background_review]
Cron: _deliver_result ── [Hook 10: on_cron_deliver] (async)
Background: _run_background_task ── [Hook 1/2]
```

调用链: `patching → hooks → controller → linear_mixin → cardkit → feishu → flush`

> v1.1.0 变更：非线性 `controller/mixin.py` 路径已删除，线性 `linear_mixin.py` 是唯一主路径。`mixin.py` 仅保留 cron/gateway deliver 和共享工具方法。

---

## 3. 文件地图与职责

| 文件 | 职责 | 关键点 |
|------|------|--------|
| **patching/** | **运行时拦截子包** | |
| `├ __init__.py` | 入口 + 共享状态 + 编排 | `apply_patches()` + 延迟补丁 + `_patch_status` 报告 |
| `├ hermes_adapter.py` | Hermes 适配层 (v1.1.0) | `HermesCompat` 类隔离所有 Hermes 内部模块访问 + 版本探测 |
| `├ hooks.py` | Hook 函数层 | `_safe_hook` 统一 enabled 检查 + 异常捕获 |
| `├ gateway.py` | GatewayRunner 包装 | 6 个 wrapper + 时间前缀注入 + cron/background |
| `├ callbacks.py` | 回调包装 | 5 个内部 wrapper + `already_streamed` 透传 + 长度去重 |
| `└ adapter.py` | FeishuAdapter 包装 | send/edit/reaction/clarify 包装 + gateway card 注册 |
| **cardkit/** | **卡片构建子包** | |
| `├ __init__.py` | 重导出门面 | `from .elements/cards/special import *` |
| `├ elements.py` | 原始元素构建器 | 统一面板 + answer streaming + footer + `build_panel_header/children` |
| `├ cards.py` | 卡片组装器 | streaming/complete/IM-fallback 卡片 |
| `├ special.py` | 专用卡片类型 | cron/gateway/clarify 三态卡片 + `normalize_clarify_choices` (v1.3.0) |
| `├ i18n.py` | 中英双语映射 | `_T` dict + `_i18n()`/`_t()` |
| `└ md.py` | Markdown 处理 | 标题/表格降级、长文本分块 |
| **controller/** | **主控制器子包** | |
| `├ __init__.py` | 重导出门面 | StreamCardController + CardSession + 状态常量 |
| `├ core.py` | 主控制器(单例) | 管理生命周期 + 并发限流 (v1.1.0) + epoch 校验 + _sessions/_interrupt_map 线程安全锁 (v1.3.0) |
| `├ mixin.py` | cron/gateway 编排 | `_do_cron_deliver`/`_do_gateway_deliver` + 共享工具方法 |
| `└ linear_mixin.py` | 线性模式编排(主路径) | 统一面板更新、保留式封卡、卡片级安全网、300309 fallback、300313 fallback |
| **state/** | **状态与数据子包** | |
| `├ __init__.py` | 重导出门面 | CardSession + TextState + UnifiedLinearState + CardPhase |
| `├ phase.py` | 卡片生命周期状态机 | `CardPhase`/`TerminalReason` + `PHASE_TRANSITIONS` |
| `├ session.py` | CardSession 数据类 | __slots__ + `_creation_stages` set (v1.1.0) + `card_trace_id` + `transition()`/`should_proceed()` |
| `├ linear.py` | 统一面板状态 | `ReasoningRound` 数据类 + `UnifiedLinearState` 扁平管理 |
| `├ text.py` | 文本增量追踪 | `<think|thinking|thought>` 标签拆分 |
| `└ tooluse.py` | 工具调用追踪 | `ToolStep`/`ToolSession`，敏感信息脱敏 |
| **feishu/** | **飞书 API 客户端子包** | |
| `├ __init__.py` | 重导出门面 | `FeishuClient`, `UnavailableGuard`, 错误码常量 + 判断函数 |
| `├ client.py` | 飞书 API 客户端 | CardKit v2 + IM API，错误码分类 + 瞬态重试 + 300313 专用重试 (v1.1.0) |
| `└ guard.py` | 消息不可用保护 | 删除/撤回检测，30分钟 TTL |
| **flush/** | **节流调度子包** | |
| `├ __init__.py` | 重导出门面 | `FlushController`, `CARDKIT_MS` |
| `└ controller.py` | 节流调度器 | CardKit 80ms 节流，互斥锁 + re-flush |
| **config/** | **配置读取子包** | |
| `├ __init__.py` | 重导出门面 | `Config`, `_get_hermes_config_path` |
| `└ reader.py` | 配置读取 | `_plugin_sec()` 惰性加载 + `/aowen config reload` 手动刷新 |
| **aowen/** | **监控命令子包 (v1.1.0)** | |
| `└ __init__.py` | /aowen 命令体系 | pre_gateway_dispatch hook + metrics 收集 + 卡片构建 |
| **plugin/** | **插件注册子包 (v1.1.0)** | |
| `└ __init__.py` | 注册入口 | `register()`/`unregister()` + 自动备份 config + FeishuClient 预热 + monitor 启动 |
| `__main__.py` | CLI 入口 | status/verify/doctor/cleanup/python |

---

## 4. 关键设计决策

**4.1 版本号唯一真值源**: `plugin.yaml` → 运行时读取(失败→"unknown") / 构建时读取(失败→raise)；`pyproject.toml` 用 `dynamic`。

**4.2 Monkey Patch 非 AST 注入**: 运行时方法替换，不修改源文件，卸载即恢复。代价：需自计时替代不可访问的局部变量。

**4.3 Hermes 适配层 (v1.1.0)**: `patching/hermes_adapter.py` 的 `HermesCompat` 类隔离所有 Hermes 内部模块访问。3 层策略解析 `agent.conversation_loop`（sys.modules → anchor-based → standard import），解决 Apple Silicon 命名冲突。Hermes 升级时只需改这一个文件。`_resolve_hermes_agent_module()` 作为 backward-compat wrapper 委托给 `HermesCompat`。

**4.4 异步 + 双重补丁**: Cron 全链路异步化(禁止 `run_coroutine_threadsafe().result()`)；`run_conversation` 模块级+实例级双重补丁；Cron/后台临时替换 `adapter.send`（卡片替换纯文本）。

**4.5 时间感知格式**: XML 标签 `<time>HH:MM:SS</time>`，LLM 不模仿，无日期/时区后缀。

**4.6 统一面板架构 (v1.0.2)**: 所有推理轮次和工具步骤放在 1 个可折叠面板中（图标 `robot_filled`），回答使用 1 个流式元素。无论对话多长，卡片始终只有 3–4 个元素。面板标题动态显示 `agent loop · N rounds · M tools · Xs`。`display.show_reasoning` 控制推理内容是否出现在面板中。`panel_events` 时间线记录事件发生顺序，面板内容按时间线交错渲染。

**4.7 卡片生命周期 (v1.0.2)**: 4 阶段渐进式卡片构建：Phase 1 用户消息 → 仅创建 "正在加载上下文..." + 加载图标的占位卡片（2 元素）；Phase 2 首 LLM token → 删加载提示、通过 `add_elements` 添加统一面板 + 回答元素；Phase 3 流式更新；Phase 4 完成 → 添加页脚。

**4.8 卡片摘要更新 (v1.0.3)**: `close_streaming` 时同时更新 `summary.content` 和 `summary.i18n_content`（zh_cn + en_us），避免中文用户会话列表永久显示"处理中..."。

**4.9 统一面板超限压缩 (v1.0.6)**: 飞书卡片2.0硬性限制200个元素/组件。当推理轮次或工具步骤过多导致元素数接近200时，`build_unified_panel` 自动裁剪超出部分，折叠为提示行。配置项：`max_tool_steps`（默认20）和 `max_reasoning_rounds`（默认20）。

**4.10 卡片级元素安全网 (v1.0.6)**: 封卡时通过 `_count_tag_objects` 递归计算总 tag objects，超过195（200-5缓冲）则从面板children头部逐项裁剪。两条封卡路径均覆盖。

**4.11 300313 错误码处理 (v1.1.0)**: 生产日志发现 `add_elements` 后 1s 内 `stream_element` 可能返回 300313（飞书服务端元素持久化传播延迟）。`cardkit_stream_element` 内置 200ms×3 次专用重试；drain/seal 阶段 300313 时 fallback 到 `partial_update_element` 写入 answer，避免 full rebuild 导致卡片闪烁。

**4.12 并发限流 (v1.1.0)**: `on_message_started` 时 seal 同 chat_id 的旧活跃卡片为"被新消息取代"，防止多张活跃卡片竞争 API 调用。

**4.13 配置刷新 (v1.1.0)**: 不做自动 mtime 检测（避免每 token 一次 stat()）。配置刷新方式：`/aowen config reload` 命令立即生效，或重启网关生效。`Config.reload()` 清缓存。

**4.14 /aowen 命令体系 (v1.1.0)**: `aowen/` 子包通过 `pre_gateway_dispatch` hook 拦截 `/aowen` 命令，直接回复飞书卡片，不经过 Hermes AI。命令：`/aowen help`、`/aowen status`（含配置折叠面板）、`/aowen monitor`、`/aowen monitor reset`、`/aowen config reload`。零后台内存占用。卡片视觉重构采用统一设计语言（banner→指标列→详情图标行→折叠→footer），颜色语义化（green=success/orange=warning/red=error/blue=info/grey=neutral），全部 column_set 用 flex_mode=stretch 实现响应式，只用 v2 安全标签，不引入 button/form_container/interactive_container。

**4.14.1 /aowen 中断场景提示卡 (v1.1.0)**: AI 回复中（agent 运行中）发送 /aowen 命令时，Hermes 网关走"agent 运行中"快速路径，未知 slash 命令（/aowen 不在白名单）会 fall through 到默认中断路径发给 LLM。借鉴 Hermes 原生 /model 命令的 "Agent is running — wait or /stop first" UX，在 `patching/gateway.py` 的 `_wrap_handle_message` 中检测此场景，发送 `build_interrupt_hint_card()`（橙色 header "AI 正在回复中"）并 return "" 阻止消息进入 agent。

**4.15 状态机标志位收敛 (v1.1.0)**: 8 个布尔标志位合并为 `_creation_stages: set[str]`（含 `"panel"`/`"answer"`/`"hint_removed"`）+ 4 个正交布尔（`_streaming_closed`/`_was_aborted`/`_pending_flush`/`_first_flush_done`）。

**4.16 去重机制收敛 (v1.1.0)**: 从 5 层降到 2 层——保留 `_hls_wrapper` 标记 + `already_streamed` 透传 + `_stream_consumed_len` 长度追踪。移除 `_native_reasoning_active`（用 `bool(state._current_reasoning)` 代替）和 `_force_rewrap`（用 ContextVar 重解析代替）。

---

## 5. CardSession 状态机

### 5.1 阶段转换图 (PHASE_TRANSITIONS)

```
IDLE ──────► CREATING ──────► STREAMING ──────► COMPLETING ──────► COMPLETED
  │               │                 │                 │
  │               │                 │                 ├→ CREATION_FAILED
  │               │                 │                 │
  ├─► ABORTED     ├─► CREATION_FAIL ├─► ABORTED       ├─► ABORTED
  │               │                 │                 │
  └─► TERMINATED  └─► TERMINATED    └─► TERMINATED    └─► TERMINATED
```

**终端阶段** (吸收态，无出边): `{COMPLETED, CREATION_FAILED, ABORTED, TERMINATED}`

> v1.1.0 变更：`FAILED` 作为 `CREATION_FAILED` 的别名保留（值均为 `"creation_failed"`），`CardPhase.FAILED` 类属性仍存在。新代码应使用 `CREATION_FAILED`。

### 5.2 TerminalReason — 终端原因追踪

| TerminalReason | 终端阶段 | 说明 |
|---|---|---|
| `NORMAL` | COMPLETED | 流式正常完成 |
| `ERROR` | COMPLETED | 回复生成期间出错 |
| `ABORT` | ABORTED | 用户主动取消 |
| `UNAVAILABLE` | TERMINATED | 源消息被删除/撤回 |
| `CREATION_FAILED` | CREATION_FAILED | 卡片创建失败 |

### 5.3 CardSession 关键方法

| 方法 | 说明 |
|---|---|
| `transition(to, source, reason)` | 验证转换合法性，自动设置 terminal_reason/terminal_source |
| `should_proceed(source)` | 统一守卫：终端检查 + UnavailableGuard 检查 |
| `is_terminal_phase` | 属性：是否在终端阶段 |
| `is_stale_create(epoch)` | Epoch 机制：检查创建回调是否过期 |
| `enter_terminal(reason, source)` | 统一终端入口：设置原因、来源、递增 epoch |

> v1.2.0：已删除 `CardVisualState`/`PHASE_TO_VISUAL`/`get_visual_state`/`session.visual_state`（生产代码从未读取，死代码）。卡片渲染实际用 `session.state`/`is_error`/`is_aborted` 参数。

### 5.4 COMPLETING 过渡状态

COMPLETING 不在 `_TERMINAL` 集合中——`on_answer`/`on_thinking` 在 COMPLETING 期间仍可更新 `unified_state`，避免晚到回调被静默丢弃。`_do_linear_complete()` 会先 drain 剩余脏数据，再 `mark_completed()` → close streaming → add footer。

`on_interrupted`/`on_aborted` 在旧 session 处于 COMPLETING 时短路——只跳过 abort 逻辑，让 `_do_linear_complete` 自然走完，但新 session 创建和 `_interrupt_map` 更新照常执行。

---

## 6. 卡片 API 降级链

```
CardKit v2 Streaming → CardKit v2 Create+Patch → Hermes 纯文本
```

v1.5.0 删除 IM 降级路径（生产从未触发）。CardKit v2 创建失败直接标记 CREATION_FAILED，hermes 回退纯文本回复。

---

## 7. 统一面板架构

**核心思想**: 1 个可折叠面板承载所有推理轮次和工具步骤，1 个流式元素承载回答文本。无论对话多长，卡片元素总数恒为 3–4 个。

**统一面板结构**:
```
┌─ 统一面板 (robot_filled) ──────────────────────────────┐
│ agent loop · 3 rounds · 5 tools · 12.5s                  │
│                                                          │
│ Round 1 (3.2s)                                          │
│   推理内容...                                             │
│   search("query") ✓ 1.2s                                │
│   read("file.py") ✓ 0.8s                                │
│                                                          │
│ Round 2 (4.1s)                                          │
│   推理内容...                                             │
│   write("file.py") ✓ 2.1s                               │
│                                                          │
│ Round 3 (5.2s)                                          │
│   推理内容...                                             │
│   run("test") ✓ 3.0s                                    │
│   run("lint") ✓ 1.5s                                    │
└──────────────────────────────────────────────────────────┘

┌─ 回答流式元素 ──────────────────────────────────────────┐
│ 这是 AI 的回答文本，流式更新...                            │
└──────────────────────────────────────────────────────────┘
```

**元素 ID**:
- `UNIFIED_PANEL_ELEMENT_ID` — 统一面板
- `ANSWER_ELEMENT_ID` — 回答流式元素

**卡片生命周期 (4 Phases)**:
- **Phase 1** — 用户发送消息 → 创建占位卡片，仅含"正在加载上下文..." + 加载图标（2 个元素）
- **Phase 2** — 首 LLM token 到达 → 删除"正在加载上下文..."，通过 `add_elements` 添加统一面板 + 回答元素
- **Phase 3** — 流式更新面板内容（推理/工具）+ 回答文本
- **Phase 4** — 完成 → 添加页脚

**300309 fallback**: 长对话（>9分钟）遇到飞书自动关闭流式时，通过 `_fallback_write_answer` 将剩余内容写入卡片，不会崩溃。

**保留式封卡**: 封卡时仅删除实际存在的元素，更新统一面板为最终状态。不再有渐进降级。

---

## 8. 配置结构

```yaml
hermes_lark_streaming:
  panel_expanded: false
  streaming_panel_expanded: false
  print_strategy: delay            # "fast" 或 "delay"
  print_step: 4                    # 打字机每次渲染字符数（默认4，范围1~10，需飞书7.23+）
  flush_interval_ms: 200           # 插件发送间隔（默认200ms）
  card_ttl_sec: 600
  max_tool_steps: 20               # 范围 1~100
  max_reasoning_rounds: 20         # 范围 1~100
  footer:
    show_label: false
    fields: [[status, elapsed, model, cost, compression_exhausted]]

# display 节是 Hermes 全局配置，不在 hermes_lark_streaming 下
display:
  show_reasoning: true
```

---

## 9. Hook 索引 (13 个注入点)

| # | Hook | 签名 | 说明 |
|---|------|------|------|
| 0 | `pre_gateway_dispatch` | sync→dict | 消息分发前拦截（v1.1.0 新增）。返回 `{"action":"skip"}` 阻止消息进入 agent，用于 /aowen 命令 |
| 1 | `on_feishu_normalize` | sync | 修正飞书引用消息虚假 thread_id |
| 2 | `on_message_started` | sync | 创建 CardSession |
| 3 | `on_message_completed` | sync→bool | 完成态卡片，返回是否已发卡片 |
| 4 | `on_tool_updated` | sync | 工具调用状态更新 |
| 5 | `on_answer_delta` | sync | AI 回复增量文本 |
| 6 | `on_thinking_delta` | sync | 思考内容（被跳过防重复） |
| 7 | `on_reasoning_delta` | sync | 原生推理增量 |
| 8 | `on_background_review_message` | sync | 后台审查通知 |
| 9 | `on_message_aborted` | sync | 消息异常终止 |
| 10 | `on_message_interrupted` | sync | 新消息打断旧消息 |
| 11 | `on_cron_deliver` | **async** | Cron 推送卡片 |
| 12 | `on_message_completed`(bg) | sync | 后台任务卡片（复用 Hook 3，但用 task_id 作为 message_id，调用场景不同） |

---

## 10. 测试结构

```
tests/
  test_version.py              — 版本号读取逻辑
  test_patch.py                — Hook 函数单元测试
  test_controller.py           — 会话生命周期 + 统一面板模式 + COMPLETING 短路
  test_cardkit.py              — 卡片 JSON 构建
  test_config.py               — 配置读取 + 热更新
  test_flush.py                — 节流调度器
  test_text.py                 — 文本增量追踪
  test_tooluse.py              — 工具调用追踪
  test_linear.py               — 线性模式状态管理
  test_monkey_patch.py         — 时间感知/重入守卫/cron 降级
  test_unavailable_guard.py    — 消息不可用保护
  test_phase.py                — 卡片生命周期状态机
  test_gateway_card.py         — 网关卡片构建
  test_callback_interception.py — 回调拦截
  test_clarify_card.py         — 交互式明义卡片
  e2e/                         — 端到端测试 (v1.1.0)
    framework.py               — E2ETestRunner (mock/真飞书自动切换)
    mock_feishu.py             — MockFeishuServer
    test_e2e_full.py           — 全链路测试用例
    conftest.py                — runner fixture + 模式检测
    .env.example               — 真飞书环境变量说明
  integration/
    test_hermes_compat.py      — Hermes 源码兼容性验证（需 HERMES_SRC_DIR）
```

运行: `HERMES_PYTHON=$(python3 ~/.hermes/plugins/hermes-lark-streaming/__main__.py python) -m pytest tests/`

### CLI 命令参考

| 命令 | 说明 |
|------|------|
| `__main__.py status` | 显示当前配置和凭据状态 |
| `__main__.py verify` | 验证环境兼容性 |
| `__main__.py doctor` | 完整诊断：版本/Python/配置/凭据/补丁状态/日志路径 |
| `__main__.py cleanup` | 清除插件注入的配置（卸载前执行） |
| `__main__.py python` | 自动检测并输出 Hermes Python 解释器路径 |

---

## 11. 开发环境

```bash
# 克隆
git clone -b DEV https://gitee.com/Aowen-Nowor/hermes-lark-streaming.git

# 安装到 Hermes
hermes plugins install /path/to/hermes-lark-streaming

# 查看日志
grep 'HLS:' ~/.hermes/logs/agent.log

# 运行测试（需要 Hermes venv 的 Python，因为依赖 lark-oapi）
HERMES_PYTHON=$(python3 ~/.hermes/plugins/hermes-lark-streaming/__main__.py python)
$HERMES_PYTHON -m pytest tests/

# 真飞书 e2e 测试（可选，需要测试 bot）
cp tests/e2e/.env.example tests/e2e/.env
# 编辑 .env 填入 FEISHU_E2E_APP_ID/APP_SECRET/CHAT_ID
set -a && source tests/e2e/.env && set +a
$HERMES_PYTHON -m pytest tests/e2e/ -v

# 清理 + 重装
$HERMES_PYTHON ~/.hermes/plugins/hermes-lark-streaming/__main__.py cleanup
hermes plugins uninstall hermes-lark-streaming
hermes plugins install https://gitee.com/Aowen-Nowor/hermes-lark-streaming
hermes gateway restart
```

---

## 12. 版本历史

详见 [CHANGELOG.md](CHANGELOG.md)

---

*Last updated: 2026-07-21 | Version: 1.6.0*
