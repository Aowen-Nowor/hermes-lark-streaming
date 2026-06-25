## v1.3.6 (2026-06-25)

紧急修复 v1.3.5 遗留的两个生产问题 — GitHub Actions 真飞书 E2E 测试竞态 + /aowen 命令无法识别。

| 类型 | 问题/功能 | 原因 | 修复/说明 |
|------|-----------|------|-----------|
| 🐛 Bug Fix (P0) | `/aowen` 命令无法识别，Hermes 网关返回 `Unknown command /aowen`，插件 `pre_gateway_dispatch` hook 从未注册 | `plugin/__init__.py` 的 `register()` 函数 pre-warm 段 `except RuntimeError: return` 提前退出，导致后续 `/aowen` hook 注册代码（第 269-275 行）从不执行。v1.3.2 引入 pre-warm 功能时加的早期 `return`，意图是"无事件循环就跳过预热"，但误跳过了 hook 注册。用户报告：网关日志只有 `patches applied` + `no running event loop, skipping pre-warm`，缺失 `/aowen commands registered` 日志 | 移除 pre-warm `except` 块里的 `return`，改为 `loop = None` 仅跳过预热，`/aowen` hook 注册代码总能到达 (`plugin/__init__.py`) |
| 🐛 Bug Fix (P1) | GitHub Actions 真飞书 E2E `test_prune_skips_streaming_session` 持续超时（v1.3.5 修复只在 mock 模式验证，真飞书模式仍失败） | concurrency seal 循环遍历 `_sessions` 时，同一 session 对象会被 `message_id` key 和 `anchor_id` key 两次命中。第一次 `on_interrupted` 创建 session2 时 `_sess_put(anchor_id, session2)` 覆盖了 `_sessions[anchor_id]`，导致循环再次遇到 anchor_id key 时把刚创建的 session2 当作 old_session abort 掉（session2 的 `_card_ready` 从未 set → 10 秒超时）。日志证据：`_complete_session: non-linear session` 的 session id 与第一次 `_do_create_linear_card ENTER` 的 session id 不同 | concurrency seal 循环加 `seen_sessions: set[int]` 跟踪已处理的 session 对象 id，同一对象只处理一次 (`controller/core.py`) |
| 🧪 Test | 新增 `test_register_always_registers_aowen_hook` 回归测试 | v1.3.6 P0 修复的回归测试——验证无事件循环时 `register()` 仍注册 `pre_gateway_dispatch` hook | 模拟 `asyncio.get_running_loop` 抛 RuntimeError，断言 `mock_ctx.register_hook` 被调用且 hook_name == `pre_gateway_dispatch` (`tests/test_monkey_patch.py`) |
| 📝 Docs | 补充根因分析：为何单元测试/E2E 未发现 P0 | `register()` 函数只在网关启动时调用，单元测试和 E2E 测试都直接 import controller 调用方法，不经过 `register()`。`test_register_logs_version` 只检查版本号日志，未检查 hook 注册。真飞书 E2E `test_prune_skips_streaming_session` 在 mock 模式下 `anchor_id == message_id`（不会有两个 key），只有真飞书模式 `anchor_id != message_id` 才触发竞态 | 新增的回归测试覆盖 `register()` 的 hook 注册路径；E2E 测试在真飞书模式下验证 concurrency seal 的 anchor_id 去重 |

**审计方法**: 两个问题都由用户在生产环境发现并报告。P0 通过读 `register()` 代码定位到 pre-warm 的 `return` 语句（issue 提供了完整的根因分析和修复方案）。P1 通过真飞书 E2E 复现 + 加调试日志追踪 session 对象 id，发现 concurrency seal 循环中同一 session 被 anchor_id key 和 message_id key 两次命中，第二次把刚创建的 session2 误 abort。两处修复均在本地 + 真飞书 E2E 验证通过。

---

## v1.3.5 (2026-06-25)

紧急修复 v1.3.4 引入的两个生产问题 — markdown 占位符泄漏导致飞书卡片显示乱码 + E2E 测试竞态超时。

| 类型 | 问题/功能 | 原因 | 修复/说明 |
|------|-----------|------|-----------|
| 🐛 Bug Fix (P0) | AI 回复含粗体包裹行内代码时，飞书卡片显示 `P12P`、`P13P` 等乱码（如 `_msg_ctx` → `P12P`、`inspect.signature()` → `P13P`） | `escape_markdown_asterisks` 的 Step 5 还原保护区域用 `re.sub` 从左到右匹配，当粗体包裹行内代码时（外层粗体占位符索引 > 内层代码占位符索引），`re.sub` 跳过替换文本中的内容，导致内层占位符 `\x00P{i}P\x00` 泄漏。飞书渲染时吃掉 null byte，显示 `P12P`。这是 v1.3.0 perf 优化（re.sub 替代 str.replace）引入的回归 | 改用逆向 `str.replace` 遍历（高索引→低索引）：先恢复外层（含内层占位符），后恢复内层。`str.replace` 扫描全串不会跳过替换内容中的匹配，确保嵌套占位符正确还原 (`cardkit/md.py`) |
| 🐛 Bug Fix (P1) | E2E 测试 `test_prune_skips_streaming_session` 在 GitHub Actions 超时（`_card_ready` 永远等不到） | v1.3.4 P0 修复（concurrency seal 复用 session）中，`on_interrupted` 创建 session2 后 fire-and-forget `_do_create_linear_card`，若被旧 session 的 `_wait_and_abort` + `_complete_session` + `_do_linear_complete_with_fallback` 级联任务链延迟执行，`_card_ready` 超时 | `on_message_started` 复用路径兜底：若 `existing._card_ready` 未 set，重新 fire-and-forget `_do_create_linear_card`。`_do_create_linear_card` 内部有 `state != IDLE` 守卫，已运行的调用不会被重复执行 (`controller/core.py`) |

**审计方法**: 用户报告飞书卡片乱码后，先发真飞书 E2E 测试卡排除飞书渲染问题（纯 markdown 正常），再用本地复现定位到 `escape_markdown_asterisks` 占位符泄漏。E2E 测试超时问题由云服务器实际运行 GitHub Actions 工作流发现，根因是 v1.3.4 concurrency seal 修复的竞态边缘场景。两处修复均在本地 + 真飞书 E2E 验证通过。

---

## v1.3.4 (2026-06-25)

全面代码审计修复版 — 两轮深度审计（第一轮 5 模块并行 + 第二轮 3 路径深挖生产主流程），共修复 2 P0 + 10 P1 + 4 P2 问题，新增飞书 log_id 排查能力。

| 类型 | 问题/功能 | 原因 | 修复/说明 |
|------|-----------|------|-----------|
| 🐛 Bug Fix (P0) | 用户在卡片流式中发送新消息时，concurrency seal 创建重复 session，导致孤儿占位卡片永久卡在"正在加载上下文..." | `on_message_started` 的 concurrency seal 循环调用 `on_interrupted(new_message_id=message_id)`，`on_interrupted` 发现 `new_message_id` 无 session 就创建一个并 fire `_do_create_linear_card`。回到 `on_message_started` 后又创建第二个 session 覆盖第一个，导致：两张卡片被创建，第一张成为孤儿永远停在 placeholder | `on_message_started` 在 concurrency seal 循环后检查 `self._sess_get(message_id)`，如果已存在（由 `on_interrupted` 创建）则复用，仅补记 metrics，不重复创建 (`controller/core.py`) |
| 🐛 Bug Fix (P0) | `/aowen` 命令 handler 异常时返回 `None`，命令落入 LLM 被当作用户 prompt 处理 | `handle_pre_gateway_dispatch` 的 `except Exception` 返回 `None` 而非 `_skip(...)`，导致 `/aowen foo` 进入 agent。日志级别 DEBUG 在生产不可见 | 异常时 `return _skip(...)` 阻止 agent dispatch，日志升级到 `exception` 级别 (`aowen/__init__.py`) |
| 🐛 Bug Fix (P1) | Phase 2 `schema_error` / `element_not_found` 路径导致 ~15 次/秒无效 API 调用 + 掩盖 `_phase2_never_succeeded` 守卫 | `add_elements` 失败后 mark "answer" as created → Phase 3 在不存在的元素上 `partial_update` / `stream_element` 返回 300313 无限重试；且 `"answer" in _creation_stages` 使完成时 `_phase2_never_succeeded=False`，走 preservative seal（再失败 2 次）而非全量重建 | 新增 `session._phase2_failed` 标志。schema_error / element_not_found 时设置标志 + 清空脏数据 + return，不再 mark as created。`_do_unified_flush` 顶部检查标志跳过 Phase 2/3。`_phase2_never_succeeded` 检查 `_phase2_failed`。完成时走全量重建写内容 (`controller/linear_mixin.py`, `state/session.py`) |
| 🐛 Bug Fix (P1) | v1.3.3 注释声称捕获 `asyncio.CancelledError` 但 `except Exception` 无法捕获（Python 3.8+ CancelledError 是 BaseException 子类） | flush 任务被取消时 `_first_flush_done` 不被重置，下次内容走节流而非立即 flush，增加"占位卡卡住"风险 | 新增 `except asyncio.CancelledError` handler，重置 `_first_flush_done` 后 re-raise 传播取消语义 (`controller/linear_mixin.py`) |
| 🐛 Bug Fix (P1) | `_retry_transient` 只捕获 `FeishuAPIError`，httpx 网络错误（ConnectError/ReadTimeout）和 `ObtainAccessTokenException` 裸传播，导致 `cardkit_create` 失败走 IM 降级 | lark_oapi SDK 的 `Transport.aexecute` 不捕获网络异常，httpx 错误直接传播出 `_retry_transient` 的 `except FeishuAPIError` | 新增 `except httpx.RequestError/TimeoutException` 和 `except ObtainAccessTokenException` handler，瞬态网络错误指数退避重试 (`feishu/client.py`) |
| 🐛 Bug Fix (P1) | 飞书频控错误码 99991400 不在 `CARDKIT_TRANSIENT_CODES` 中，频控时直接失败传播 | 原 transient_codes 只含 {2200, 1663, 300000}。飞书对单个 API 设频率限制（如 batch_update 每秒 5 次），超限返回 99991400。controller 仅 reset `_first_flush_done` 而不重试，增加内容延迟 | 将 99991400 加入 `CARDKIT_TRANSIENT_CODES`，client 层指数退避重试。官方文档：https://open.feishu.cn/document/server-docs/api-call-guide/frequency-control (`feishu/client.py`) |
| 🐛 Bug Fix (P1) | `_wrap_handle_message_with_agent` / `_wrap_run_agent` / `_wrap_run_background_task` 的 `orig()` 调用无 try/finally，异常时 `_msg_ctx` / `_started_msg_ids` 泄漏 | cleanup 在函数末尾或 try/finally 块外，`orig()` 异常跳过 cleanup。`_msg_ctx` 保留 stale `event_message_id`，下一条消息的 `FeishuAdapter.send()` 被静默抑制（"卡片不出现" bug） | 三处 wrapper 均增加 try/except BaseException：异常时执行 cleanup 后 re-raise。`_wrap_handle_message_with_agent` 提取 `_hls_cleanup_ctx()` helper 统一清理。`_wrap_run_background_task` 将 COMPLETE hook + cleanup 移入 try/finally (`patching/gateway.py`) |
| 🐛 Bug Fix (P1) | `inspect.signature()` 未防御，C 扩展/wrapped callable 抛 ValueError/TypeError 导致 `apply_patches` 崩溃、插件加载失败 | `inspect.signature(orig).parameters` 对某些 callable 会抛异常，无 try/except | 两处 `inspect.signature` 调用增加 `try/except (ValueError, TypeError)`，失败时 `_has_persist_ts = False` (`patching/gateway.py`, `patching/__init__.py`) |
| 🐛 Bug Fix (P1) | 被 abort 的 session 完成后状态被覆盖为 COMPLETED（ABORTED→COMPLETED 非法转换） | `_do_linear_complete` 在 seal 成功后无条件设 `session.state = COMPLETED`，覆盖 `on_aborted` 设的 ABORTED | 检查 `session._was_aborted`：True 设 ABORTED，False 设 COMPLETED (`controller/linear_mixin.py`) |
| 🐛 Bug Fix (P1) | 错误面板 `friendly_en` 是死代码，英文 locale 用户看到中文错误消息 | `body_content` 只用 `friendly_zh`，markdown 元素无 `i18n_content` | 构建 `body_content_en` + `body_content_zh`，markdown 元素添加 `i18n_content=_i18n(en, zh)` (`cardkit/elements.py`) |
| 🐛 Bug Fix (P1) | `bg_review_messages` 在 preservative seal 路径不渲染，默认配置下后台审查功能完全失效 | v1.3.4 第一轮只修了 `build_unified_complete_card`（全量重建路径），遗漏 `build_preservative_seal_actions`（默认 seal 路径）。`_build_background_review_panel` 只在全量重建调用 | `build_preservative_seal_actions` 在 error panel 之后、footer 之前插入 bg_review panel（与全量重建路径一致）(`cardkit/elements.py`) |
| 🐛 Bug Fix (P1) | `_preservative_seal` retry 路径 300309 `raise` 与主路径 `return False` 语义不一致 | retry 循环内 `except FeishuAPIError` 对 300309 执行 `raise`（依赖外层 `except Exception` 兜底），主路径对 300309 直接 `return False` 走全量重建 | retry 路径 300309 改为 `return False`，与主路径一致，显式走全量重建 (`controller/linear_mixin.py`) |
| 🐛 Bug Fix (P1) | `UnavailableGuard` 在生产环境是死代码——消息被删/撤回时 guard 从未被触发，插件继续对已删除卡片发 API 调用浪费配额 | `mark_unavailable` 只在 `guard.terminate()` 内部调用，而 `terminate` 只在 `should_skip` 发现 `is_unavailable` 为 True 时才调用，形成循环依赖：要 terminate 必须 is_unavailable，要 is_unavailable 必须 mark_unavailable。没有任何 API 错误处理路径调用 `terminate` | `_do_create_linear_card` 的 `except Exception` 兜底中，如果异常是 `FeishuAPIError` 且 `is_terminal_api_code(e.code)` 为 True（消息被删 231003/1000023、撤回 230011），调用 `session.guard.terminate(source, err=e)` 激活 guard，后续 `should_skip` 返回 True 跳过无用 API 调用 (`controller/linear_mixin.py`) |
| 🐛 Bug Fix (P2) | `bg_review_messages` 在 state 中累积但从未传给 `build_unified_complete_card`，后台审查功能静默失效 | `footer_data = session.footer` 不含 `bg_review_messages`（存在 `state.bg_review_messages` 中），`has_dirty()` 包含检查会触发 flush 但 flush 路径不渲染它 | `_do_linear_complete` 构造 `footer_data` 时从 `state.bg_review_messages` 注入 (`controller/linear_mixin.py`) |
| 🐛 Bug Fix (P2) | `FlushController` 的 `call_soon(asyncio.create_task, ...)` 未持有 Task 强引用，可能被 GC 回收 | Python 文档："Save a reference to the result of this function"。`core.py:_fire_and_forget` 已修复此问题，但 `flush/controller.py` 未同步 | 新增 `_pending_flush_tasks: set[Task]`，通过 `_create_flush_task` helper 创建 Task 并 add 到 set，`add_done_callback(discard)` 清理 (`flush/controller.py`) |
| 🐛 Bug Fix (P2) | `linear_mixin.py` 两处首字即显 `create_task` 未持有 Task 强引用，可能被 GC 回收 | `controller/linear_mixin.py:274` 和 `:352` 直接 `loop.create_task(...)` 未保存引用，与 `core.py:_fire_and_forget` 模式不一致 | 改用 `self._fire_and_forget(coro, loop)` 持有强引用 (`controller/linear_mixin.py`) |
| 🔧 Fix (P2) | `_do_create_linear_card` 的 `except FeishuAPIError` 未捕获异常对象，log_id 丢失 | `except FeishuAPIError:` 没有 `as e`，日志只输出固定文本不输出异常详情和 log_id | 改为 `except FeishuAPIError as e:` 并在日志中输出 `%s` 异常对象（自动带 log_id）(`controller/linear_mixin.py`) |
| ✨ Feature | 飞书 log_id 排查能力——所有 `FeishuAPIError` 携带 `[log_id=...]`，可在飞书开放平台后台查请求链路 | 插件日志只输出错误码和消息，无法定位飞书服务端的具体请求链路 | `FeishuAPIError` 新增 `log_id` 字段 + `__str__` 自动附加。`_check` 从 SDK 响应提取 log_id（含 async 路径 SDK bug 兜底：`get_log_id()` 返回 None 时从 `response.error` dict 提取）。Issue 模板新增 log_id 排查章节 (`feishu/client.py`, `docs/ISSUES_TEMPLATE.md`) |
| 🧪 Test | 新增 3 个回归测试 | 覆盖 v1.3.4 关键修复 | `test_v134_concurrency_seal_no_duplicate_session`（concurrency seal 不重复创建）、`test_v134_aborted_session_keeps_aborted_state`（ABORTED 状态保持）、`test_v134_aowen_handler_exception_returns_skip_not_none`（/aowen 异常返回 skip） |
| 🧪 Test | 更新 `test_im_fallback_seal_aborted_with_header` | v1.3.4 的 ABORTED 状态保持修复影响该测试 | 断言从 `COMPLETED` 改为 `ABORTED` |
| 📝 Docs | `docs/ISSUES_TEMPLATE.md` 新增 Feishu log_id 排查章节 | 用户报 Bug 时无法提供飞书请求链路 ID，维护者难以定位是插件问题还是飞书服务端问题 | 新增章节说明 log_id 含义、日志提取命令、Issue 提交建议 (`docs/ISSUES_TEMPLATE.md`) |

**审计方法**: 两轮深度审计。第一轮 5 模块并行（controller/cardkit/patching/feishu/state+aowen），覆盖全部 ~13.7k 行代码。第二轮聚焦生产主流程 3 路径深挖（agent 卡片生命周期 / patching 层与 hermes 交互 / 网关卡片+cron+异常路径）。所有发现均有 file:line 证据 + 代码片段，每个修复前重新读代码确认。飞书相关结论经真飞书 E2E 验证（log_id 提取、column_set 可用性）。审计前 891 tests passed，审计后 894 tests passed（+3 回归测试，0 回归）。

**已知限制（本次未修复，评估为低优先级或需更大范围改动）**:
- `_preservative_seal` retry 路径（300317 触发）未应用 195 元素安全网和内容守卫——retry 失败会走全量重建（已应用安全网），风险较低
- `_wrap_run_agent` 的 COMPLETE hook 段未纳入 try/except BaseException 覆盖——COMPLETE hook 内部已 `except Exception`，仅 BaseException（CancelledError/KeyboardInterrupt）泄漏，hermes 崩溃场景下非首要问题
- patching 层并发 background task / cron deliver 的 `adapter.send` per-call 替换竞态——需重构为 per-task context flag，影响范围大
- `_wrap_handle_message_with_agent` 入口 `event.message_id` / `self._reply_anchor_for_event` 无 try/except——hermes Event schema 变化才会触发，需 hermes 多版本兼容性测试

---

## v1.3.3 (2026-06-25)

P0 紧急修复 — 占位卡片永久卡在"正在加载上下文..."问题（issue: placeholder_card_stuck）

| 类型 | 问题/功能 | 原因 | 修复/说明 |
|------|-----------|------|-----------|
| 🐛 Bug Fix (P0) | 占位卡片永久卡在"正在加载上下文..."，永远看不见回答内容 | Phase 2 的 `cardkit_batch_update(add_elements + delete_loading_hint)` 调用失败后，`_creation_stages` 永远不写入 `"answer"`/`"panel"`/`"hint_removed"`。而所有后续写入路径（drain 循环、seal 内容守卫、seal Step 1/2）都通过 `"xxx" in session._creation_stages` 判断元素是否存在——判断为 False 则跳过写入。seal 最终"成功"关闭了流式模式但未写入任何内容，全量重建 fallback 永远不被触发。这是**标志位死锁**：没有元素存在标志 → 不写入内容 → 没有元素存在标志。 | **检测 Phase 2 失败并强制全量重建**：`_do_linear_complete` 在调用 `_preservative_seal` 前检查 `"answer" not in _creation_stages and (answer_text or panel_visible or reasoning_rounds)`，如果为 True 说明 Phase 2 从未成功，跳过 preservative seal（会静默"成功"不写内容），直接走全量重建 fallback（`build_unified_complete_card` + `cardkit_update`），用完整内容替换整张占位卡片。 |
| 🐛 Bug Fix (P0) | Phase 2 网络异常/超时未捕获，`_creation_stages` 为空且 `_first_flush_done` 已设 True | Phase 2 的 `cardkit_batch_update` 只捕获 `FeishuAPIError`，`asyncio.TimeoutError`/`aiohttp.ClientError` 等网络异常未被捕获，传播到 `FlushController._do_flush` 的 `except Exception` 被静默吞掉。`_creation_stages` 为空 + `_first_flush_done=True`（设值过早）→ 后续内容到达时走节流 flush 而非立即 flush，增加延迟。 | 1) Phase 2 增加外层 `except Exception` 捕获非 `FeishuAPIError` 异常，记录 warning 日志并重置 `_first_flush_done=False` 让下次内容到达时走立即 flush 重试。2) FeishuAPIError 的 transient 分支（非 schema/非 element_not_found）也重置 `_first_flush_done=False`。 |
| 🧪 Test | 新增回归测试 `test_phase2_failure_forces_full_rebuild_not_stuck` | 验证 Phase 2 失败后 `_do_linear_complete` 走全量重建而非 preservative seal，且 `cardkit_update` 收到的完整卡片包含 answer 文本 | 模拟 Phase 2 从未成功的场景（`_creation_stages` 为空 + `answer_text` 有内容），断言 `_preservative_seal` 不被调用、`cardkit_update` 被调用且卡片内容包含回答文本。 |
| 🧪 Test | 3 个 header 相关测试更新 `_creation_stages` 设置 | v1.3.3 的 Phase 2 失败检测会影响未设置 `_creation_stages` 的测试 | `test_header_disabled_uses_preservative_seal`、`test_header_enabled_skips_preservative_seal`、`test_header_rebuild_does_not_pollute_full_rebuilds_metric`、`test_real_failure_rebuild_counts_full_rebuilds` 增加 `session._creation_stages.add("answer")` 模拟 Phase 2 已成功。 |

**根因分析**（issue: placeholder_card_stuck）:
```
占位卡创建成功 → _creation_stages = {}（空）
      ↓
LLM 首字到达 → Phase 2（_do_unified_flush）
      ↓  执行 cardkit_batch_update(add_elements + delete_loading_hint)
      ↓
  ❌ API 失败（网络超时/频控/auth刷新/非瞬态错误）
      ↓
  _creation_stages 无变化 → 无 "answer", "panel", "hint_removed"
      ↓
on_completed → _do_linear_complete
      ↓
  Step 2 Drain 循环：检查 "answer" in _creation_stages → False → 跳过
  Step 5 _preservative_seal：
    → 内容守卫检查 "panel"/"answer" in _creation_stages → False → 全部跳过
    → Step 1 更新面板：检查 "panel" → False → 跳过
    → Step 2 更新回答：检查 "answer" → False → 跳过
    → Step 5 batch_update（只有 footer + delete）→ 成功
    → Step 6 close_streaming → 成功
    → 返回 True（"成功"）
      ↓
seal_ok = True → 全量重建 fallback 不触发
      ↓
卡片永久停留在：只有加载提示 + 加载图标
```

---

## v1.3.2 (2026-06-25)

全面代码审计修复版 — 3-5 轮审计共发现 35 个问题（0 P0, 5 P1, 15 P2, 15 P3），本次修复全部 P1/P2 和主要 P3 问题。

| 类型 | 问题/功能 | 原因 | 修复/说明 |
|------|-----------|------|-----------|
| 🔧 Fix (P1) | 配置文件读取未捕获 OSError/UnicodeDecodeError | `config/reader.py` 的 `_load()` 和 `_reload_cached()` 只捕获 `yaml.YAMLError`，权限不足/磁盘错误/编码问题会导致未捕获异常 | 两处文件读取增加 `except (OSError, UnicodeDecodeError)` 分支，返回空配置并 warning 日志 |
| 🔧 Fix (P1) | `_to_float` 允许 nan/inf 值通过 | `float('nan')`/`float('inf')` 是合法 Python float，不触发 ValueError。下游 `max()`/`min()` 比较因 NaN 传播失效，节流逻辑永远不触发或永远立即触发 | `_to_float` 增加 `math.isnan()`/`math.isinf()` 检查，nan/inf 值返回 default 并 warning |
| 🔧 Fix (P1) | `_to_int` 的 float 路径未捕获 OverflowError/ValueError | `int(float('inf'))` 抛 OverflowError，`int(float('nan'))` 抛 ValueError，均未捕获 | `_to_int` 的 float 分支增加 try/except (OverflowError, ValueError) |
| 🔧 Fix (P1) | `__init__.py` 模块 docstring 与代码矛盾 | docstring 声称"默认 flush 间隔 100ms"和"主动 TTL 延长"，二者分别已在 v1.3.1 恢复为 200ms 和删除 | 更新 docstring：100ms→200ms，"Proactive TTL extension"→"300309 stream-closed fallback" |
| 🔧 Fix (P1) | `docs/SKILL.md` 引用已删除的 `on_reload()` 回调 | v1.3.0 删除了 `on_reload()` 回调（死代码），但 SKILL.md 4.13 节仍写"`on_reload` 回调注册" | 移除"`on_reload` 回调注册"描述 |
| 🐛 Bug Fix (P2) | `_wait_and_abort` 异步路径绕过 COMPLETING 短路 | `on_interrupted` 的同步路径检查 `state == COMPLETING` 跳过 abort，但异步 `_wait_and_abort` 在 await 后直接设 `state = ABORTED` 未重新检查，竞态导致完成流程被中断 | `_wait_and_abort` 在 await 后增加 COMPLETING 重新检查，与同步路径一致 |
| 🐛 Bug Fix (P2) | `/stop` 响应检测使用脆弱子串匹配 | `any(kw in content for kw in ("已停止", "stopped", "Stopped"))` 会误匹配 AI 回答中的"已停止"等词，错误 abort 活跃卡片 | 改为三重条件：内容 <50 字符 + 以 ⚡ 开头 + 包含关键词，避免误触发 |
| 🐛 Bug Fix (P2) | `_fire_and_forget` 未持有 Task 引用 + 协程泄漏 | `loop.create_task(coro)` 返回的 Task 未保存强引用，GC 可能在完成前回收。fallback 失败时协程未 close，产生 "coroutine was never awaited" 警告 | 新增 `_pending_tasks` 集合持有强引用，task 完成后自动移除。fallback 失败时 `coro.close()` |
| 🐛 Bug Fix (P2) | `aowen/__init__.py` 使用废弃 `asyncio.get_event_loop()` | Python 3.14 将移除 `get_event_loop()` 的隐式创建行为，届时会抛 RuntimeError | 改为 `asyncio.get_running_loop()`，显式获取运行中的事件循环 |
| 🐛 Bug Fix (P2) | `aowen` 命令 `loop.create_task` 未持有 Task 引用 | 与 `_fire_and_forget` 相同的 GC 回收风险 | 新增 `_aowen_pending_tasks` 集合持有强引用 |
| 🔧 Fix (P2) | 多处 `asyncio.get_event_loop()` 废弃 API | `plugin/__init__.py:253`、`controller/linear_mixin.py:271` 使用废弃 API | 改为 `asyncio.get_running_loop()`（在 async 上下文中安全） |
| 🔧 Fix (P2) | `docs/SKILL.md` 引用已删除的 TTL 延长功能 | v1.3.1 删除了 `cardkit_extend_ttl`，但 SKILL.md 仍写"TTL 延长" | 更新为"300309 fallback"描述 |
| 🔧 Fix (P2) | `docs/AGENT_GUIDE.md` 手动更新指令用错分支名 | 写 `git pull origin master`，但主仓库使用 `DEV` 分支 | 改为 `git pull origin DEV` |
| 🔧 Fix (P3) | `_stream_consumed_len` 字典无清理，持续增长 | interrupt-reuse 场景下每个新 eid 追加到字典永不清理，长时间运行内存泄漏 | thinking_wrapper 检测到消息完成时调用 `_cleanup_consumed_len(_eid)` 清理 |
| 🔧 Fix (P3) | aowen 指标记录 `except Exception: pass` 静默吞异常 | 7 处指标记录调用完全静默，指标系统故障不可见 | 改为 `except Exception: _logger.debug(...)` 记录异常 |
| 🔧 Fix (P3) | `_INTERRUPT_MAP_MAX` 定义在函数体内 | 每次调用 `on_interrupted` 都重新创建局部变量 | 提升为模块级常量 |
| 🔧 Fix (P3) | `upload_image` API 调用缺少 try/except | 与 `upload_local_image` 不一致，网络/认证错误会传播未捕获 | 增加 try/except 包裹上传调用，返回 None 并 debug 日志 |
| 🔧 Fix (P3) | `_schedule_confirm_card` 冗余 `import asyncio` | asyncio 已在模块级导入，函数内重复导入 | 移除冗余 import |
| 🔧 Fix (P3) | `_hls_bg_sending`/`_hls_cron_sending` 默认值不对称 | 递增时 default=0，递减时 default=1，语义不一致 | 统一为 default=0 |
| 📝 Docs | `docs/CHANGELOG.md` 附录 F3 引用已废弃 TTL 延长 | F3 "主动 TTL 延长" 作为经验教训，但功能已删除 | 标注删除线 + v1.3.1 移除说明 |
| 📝 Docs | `docs/CHANGELOG.md` F1 默认刷新间隔仍写 100ms | v1.3.1 已恢复 200ms | 更新为 200ms |
| 📝 Docs | `docs/ISSUES_TEMPLATE.md` 引用已废弃 TTL 主动延长 | Debug Tips 表格写"卡片 TTL + 主动延长" | 更新为"300309 fallback" |
| 📝 Docs | `docs/ISSUES_TEMPLATE.md` 未引导用户使用 /aowen 自查 | 用户提交 issue 前无自助排查手段 | 新增"/aowen 命令自查"章节，引导用户先发 /aowen status/monitor |

**审计方法**: 3-5 轮全面审计，涵盖代码健壮性、性能、Bug、用户体验、文档对齐五个维度。所有发现均有文件:行号证据，不基于经验猜测。飞书 CardKit v2.0 相关优化已对照官方文档验证。

---

## v1.3.1 (2026-06-24)

| 类型 | 问题/功能 | 原因 | 修复/说明 |
|------|-----------|------|-----------|
| 🐛 Bug Fix (P0) | 封卡时末尾内容被裁剪（用户反馈 issue-streaming-tail-clip） | v1.3.0 加的 `_answer_finalized_via_stream` 守卫在 answer 已通过 `stream_element` 推送后跳过 `_preservative_seal` Step 2 的最终 `partial_update_element`。但飞书打字机队列是异步的——`close_streaming` 立即终止流式会话，未渲染完的字符被永久丢弃。用户表现为回答在最后一句话中间突然截断（如"服务 + 守护进程 +"丢失后半句）。 | **移除守卫**：`_preservative_seal` 和 retry 路径的 Step 2 现在 ALWAYS 发送最终 `partial_update_element` 作为"权威定稿"。`partial_update_element` 的即时替换效果是值得接受的代价（视觉跳动 P2 < 内容截断 P0）。删除 `_answer_finalized_via_stream` 标志（session.py `__slots__` + linear_mixin.py 4 处 setter + 2 处守卫）。新增 1 个回归测试验证守卫移除后 seal 永远写入最终 answer。 |
| 🐛 Bug Fix | `cardkit_extend_ttl` 功能完全失效 | `feishu/client.py:cardkit_extend_ttl` 使用 `streaming_config.ttl_seconds` 参数，**真飞书 API 实测返回 300122 "unknown property"**（5 种参数变体 + 对照组 `print_strategy` code=0 全部验证）。飞书 CardKit v2.0 settings API 不支持任何形式的 TTL 延长参数。错误被 `except Exception: _logger.debug(...)` 静默吞掉，功能完全无效且不可见。 | **删除功能**：移除 `cardkit_extend_ttl` 方法 + `controller/linear_mixin.py` 中的 TTL 延长调用点 + `_TTL_EXTEND_THRESHOLD_SEC`/`_TTL_EXTEND_DELTA_SEC` 常量 + 3 个测试文件中的 mock。长对话（>9分钟）遇到飞书自动关闭流式时仍有 300309 fallback 处理（`_fallback_write_answer`），不会崩溃。 |
| 🔧 Fix | `flush_interval_ms` 默认值恢复为 200ms | v1.2.1 (commit 264f32c) 将默认值从 100→200（生产日志显示 200ms 与 100ms 打字机效果无差别，但 API 调用量减半）。v1.3.0 (commit 8313b76) 回退为 100（commit message 说"确认 100ms"），但 `flush_interval_sec` docstring 残留"200ms"未同步回退，CHANGELOG 也未记录回退。 | **恢复 200ms**：`config/reader.py` 默认值 100→200 + docstring 更新；`plugin/__init__.py` 注入配置 100→200；`tests/test_config.py` 断言 100→200；`README.md`/`README.zh-CN.md`/`SKILL.md`/`AGENT_GUIDE.md` 文档同步。6 处一致。 |
| 🔧 Fix | CHANGELOG v1.2.1 条目与代码不一致 | CHANGELOG v1.2.1 P1-01 声称"默认值从 100 上调至 200"，但 v1.3.0 代码回退为 100 时 CHANGELOG 未更新。 | v1.3.1 恢复 200ms 后，CHANGELOG v1.2.1 条目与代码一致，无需修改历史条目。v1.3.0 的回退在 v1.3.1 被"再回退"，最终状态与 v1.2.1 原始意图一致。 |
| ✨ Feature | 回归测试覆盖 | `_preservative_seal` 末尾内容裁剪 bug 无测试覆盖 | 新增 `test_seal_always_writes_final_answer_after_stream_element` — 使用 `CardSession` 子类模拟 v1.3.0 bug 场景（`_answer_finalized_via_stream=True`），验证 seal 仍发送最终 `partial_update_element`。已验证：reintroduce bug 时测试 FAIL，fix 后 PASS。 |
| 🐛 Bug Fix (P0) | FeishuAdapter import 路径错误导致补丁无法应用 | `hermes_adapter.py` 用 `from gateway.platforms.feishu import FeishuAdapter`，但 Hermes v0.17.0 生产环境实测此路径 **No module named**（`gateway/platforms/feishu.py` 文件已移除）。Gateway 实际通过 `hermes_plugins.feishu_platform.adapter` 命名空间加载 adapter。补丁打在错误的类上，对 gateway 实例无效果。用户反馈"feishu_adapter未就绪"。 | **修复 import 路径**：改为 3 路径 fallback 优先 `hermes_plugins.feishu_platform.adapter` → `plugins.platforms.feishu.adapter` → `gateway.platforms.feishu`（legacy）。生产环境实测验证 `feishu_adapter_class` 正确解析为 `hermes_plugins.feishu_platform.adapter.FeishuAdapter`。 |
| 🐛 Bug Fix | `_intercepting_send` / `_card_sending_send` 参数名不匹配 | Hermes `_send_with_retry` 用关键字参数 `self.send(chat_id=..., content=...)` 调用，但插件 wrapper 用 `chat_id_send` / `content_text` 参数名，导致 `TypeError: missing required positional arguments`。Cron 推送和 background task 路径受影响。 | **参数名对齐**：`_intercepting_send`: `chat_id_send`→`chat_id`；`_card_sending_send`: `chat_id_send`→`chat_id`, `content_text`→`content`。与 `FeishuAdapter.send(self, chat_id, content, ...)` 签名一致。 |
| 🐛 Bug Fix | `/new` / `/reset` 命令确认消息被抑制 | Agent 运行中发 `/new`，gateway 返回 `EphemeralReply`（确认消息），但 `_intercepted_send` 的 `card_sent` 守卫抑制了所有文本回复（包括 `EphemeralReply`）。用户看到红色 ❌ 无确认反馈。 | **EphemeralReply 直通**：`_intercepted_send` 入口检查 `isinstance(content, EphemeralReply)`，如果是则直接调用原始 send 不经过 `card_sent` 守卫。`EphemeralReply` 是 gateway 内部确认消息，不是重复 agent 回复。 |
| 🔧 Fix | 数值配置项非数字字符串导致崩溃 | `print_step`/`max_tool_steps`/`max_reasoning_rounds`/`flush_interval_ms`/`card_ttl_sec` 用 `int()`/`float()` 直接转换，用户写 `print_step: abc` 时抛未捕获 `ValueError`。 | **新增 `_to_int` / `_to_float` helper**：类似 `_to_bool` 的类型容错，非数字输入返回默认值 + WARNING 日志。5 个数值配置项全部接入。 |
| 🔧 Fix | `_gateway_cards` 字典内存泄漏 | `_register_gateway_card` 生产代码调用 2 次，`_unregister_gateway_card` 从未在生产代码调用。字典只增不删，无 TTL，无容量限制。 | **容量限制**：`_register_gateway_card` 内添加 500 条上限，超限时按 `registered_at` 清理最旧 20%。新增 `registered_at` 时间戳字段。 |
| 🔧 Fix | `_send_text_fallback` 在数据释放后执行 | `_do_linear_complete` 失败时先执行 `_release_session_data`（清空 `session.text`），然后返回 False，接着 `_send_text_fallback` 读取 `session.text.display_text` 已为空。 | **提前快照**：`_do_linear_complete_with_fallback` 在调用 `_do_linear_complete` 前保存 `answer_text`/`error_message` 快照，通过 `fallback_text` 参数传给 `_send_text_fallback`。 |
| 🐛 Bug Fix | `header=true` 全量重建路径遗漏 `escape_markdown_asterisks` | `build_unified_complete_card`（header=true 时使用）未调用 `escape_markdown_asterisks`，而 `_preservative_seal`（header=false）调用了。`header=true` 时 AI 回复中的乘号 `*`（如 `2*4000+4*3000`）不被转义，飞书 markdown 把 `*4000+4*` 配对为斜体，乘号消失、数字拼合。 | **添加 escape**：`build_unified_complete_card` 的 answer 处理增加 `escape_markdown_asterisks`，与 `_preservative_seal` 路径一致。真飞书 API 实测验证：close_streaming 后 cardkit_update 全量替换可行（code=0）。 |

---

## v1.3.0 (2026-06-24)

| 类型 | 问题/功能 | 原因 | 修复/说明 |
|------|-----------|------|-----------|
| 🐛 Bug Fix (P0-01) | Clarify 追问卡片选项乱码（用户反馈） | LLM 传 dict 格式 choices（如 `{"id":1,"path":"/mnt/nas/backup1"}`），在 Hermes 工具调度层被 `str()` 序列化为 dict-repr 字符串后穿透到插件。插件直接将其放入 markdown 元素，飞书 `lark_md` 把 `{'` 当模板语法处理，导致单引号消失、`}` 变形为 `)`，显示为 `{id':1)` | **三层防御**：① `cardkit/special.py` 新增 `_normalize_choice()` — 用 `ast.literal_eval` 解析 dict-repr 字符串，按字段优先级（label>description>text>title>name>path>value>id）提取可读文本；② markdown 元素用 `_escape_md` 转义 `{` `}` `[` `]` `<` `>` 等特殊字符；③ 解析失败兜底原样转义显示。`question` 文本同样转义（Round 2 审计补充）。生产日志求证 + E2E 真飞书测试验证 |
| 🐛 Bug Fix | `_clarify_*` 5个共享字典无并发锁 | 多线程（事件循环/飞书webhook/定时协程）并发访问 `_clarify_choices`/`_clarify_questions`/`_clarify_card_msg_ids`/`_clarify_selections`/`_clarify_timestamps` 可导致用户选择丢失、重试失败 | 新增 `_clarify_lock = threading.Lock()`，30+ 访问点全部加锁。`_schedule_confirm_card` 锁内快照数据后释放锁再做网络调用，避免持锁 await |
| 🐛 Bug Fix | `_sessions` 会话字典无并发锁 | `_sessions` 被事件循环线程和 worker 线程并发访问，`_sessions.values()` 遍历时另一线程修改可触发 `RuntimeError: dictionary changed size during iteration` | 新增 `_sessions_lock = threading.RLock()` + 7个线程安全 helper 方法（`_sess_get`/`_sess_put`/`_sess_pop`/`_sess_items_snapshot`/`_sess_values_snapshot`/`_sess_active_count`/`_sess_clear`）。全项目 15+ 访问点改用 helper。RLock 允许重入（`on_message_started` 调 `on_interrupted`） |
| 🐛 Bug Fix | `_interrupt_map` 重定向字典无并发锁 | 与 `_sessions` 同类问题，Round 1 审计遗漏，Round 2 补充 | 新增 `_interrupt_map_lock = threading.Lock()`，3处访问点（写入/弹出/清理）全部加锁 |
| 🐛 Bug Fix | `_sessions.values()` 遍历无快照 | `aowen/__init__.py:414` 和 `controller/core.py:196` 直接遍历 `_sessions.values()`，并发修改时崩溃 | 改用 `_sess_active_count()` / `_sess_items_snapshot()` 线程安全快照 |
| 🐛 Bug Fix | 推理文本去重 30 字符前缀比较丢内容 | `on_reasoning_delta` 的 post-stream 去重用 `min(30, ...)` 只比较前 30 字符，共享 30+ 字符前缀的合法增量块被误判为重复而丢弃 | 改为完整前缀比较 `text[:len(self._current_reasoning)] == self._current_reasoning`。6 个回归测试验证 |
| 🐛 Bug Fix | `config/reader.py` `_logger` 未定义 | `_load()` 和 `_reload_cached()` 的 YAML 错误分支引用 `_logger`，但 `_logger` 仅在 `reload()` 内部用 `__import__` 定义，模块级无定义 → YAML 语法错误时 NameError 崩溃 | 模块级 `import logging` + `_logger = logging.getLogger("hermes_lark_streaming")`，删除 `__import__` hack |
| 🐛 Bug Fix | `escape_markdown_asterisks` 空字节泄漏 | AI 回复中含 `\x00` 空字节时（如 AI 复述源码占位符模式 `\x00P{i}P\x00`），还原步骤可能 IndexError 崩溃或占位符泄漏到飞书渲染为方框 `□P0P□` | 三层防御：① 函数入口剥离所有 `\x00`；② `re.sub` 包 `try/except`，IndexError 回退 per-block replace；③ 返回前再次剥离残余 `\x00`。5 个回归测试 |
| 🐛 Bug Fix | Clarify 流式中断 + answer 瞬间输出 | ① Clarify 卡片出现时流式仍在更新（flush controller 有 pending timer）；② Clarify 选择后 answer 瞬间输出（seal step 2 `partial_update_element` 绕过打字机队列） | ① `send_clarify` 前 `flush_now` + 取消 pending timer；② 新增 `_answer_finalized_via_stream` 标志，answer 通过 `stream_element` 发送时跳过 seal 的 `partial_update_element` |
| 🐛 Bug Fix | E2E framework 缺少 `print_step` | `MagicMock(spec=Config)` 未设置 `print_step`，`build_streaming_card_v2` 访问时返回 MagicMock 对象，`json.dumps` 序列化失败 | framework.py 新增 `cfg.print_step = 4` |
| 🔧 Fix | `strip_reasoning_tags` 热路径 2 个死正则 | 步骤1已移除所有 `<think>` 标签后，步骤2/3 试图匹配 `<think>...</think>` 块——永远不匹配。每个 answer token 都执行，2000 token = 4000 次无效正则扫描 | 删除步骤2/3（`state/text.py`），保留步骤1（移除标签保留内容，符合函数语义） |
| 🔧 Fix | `inspect.signature` 每条消息调用 | `patching/__init__.py` 和 `patching/gateway.py` 在 per-message wrapper 内调用 `inspect.signature()` 检查 `persist_user_timestamp` 参数——签名运行时不变，每条消息浪费 10-50μs | 在 wrap time 计算一次 `_has_persist_ts`，闭包捕获，per-message 只做布尔判断 |
| 🔧 Fix | flush-cycle INFO 日志刷屏 | 每个 flush 周期（150-200ms）打 2 条 INFO 日志，长对话 500+ 条 | 2 处 per-flush INFO 降为 DEBUG。状态转换和错误日志保持 INFO |
| 🔧 Fix | `on_completed` 正常完成日志降噪 | 每次成功完成打 INFO 日志，生产环境刷屏 | 正常完成日志降为 DEBUG，yield-to-gateway 边缘情况保持 INFO |
| 🚀 Performance | `escape_markdown_asterisks` 保护区域还原 O(K×N)→O(N) | 每个 protected block 用 `str.replace` 全文扫描，K 个 block × N 文本长度 | 改为单次 `re.sub` + lambda 查表还原（`_RE_PROTECTED_PLACEHOLDER`） |
| 🚀 Performance | `UnavailableGuard._prune_cache` 每 token 全量扫描 | `is_unavailable()` 每个 token 调用，每次遍历整个缓存。100 条 × 2000 token = 20 万次遍历 | 改为阈值触发：缓存 >50 条才清理。小缓存 `in` 检查天然 O(1) |
| 🚀 Performance | `Config._load()`/`_reload_cached()` 无锁 | Config 单例跨线程共享，并发首次访问重复解析 YAML，`reload()` 清缓存时可能读到陈旧配置 | 新增 `threading.Lock`，`_load`/`_reload_cached`/`reload` 全部加锁 |
| 🏗️ Architecture | 删除 Config `on_reload()` 死代码 | `on_reload()` 注册回调和 `_on_reload_callbacks` 列表全项目无调用方，`reload()` 里的 for 循环永远不执行 | 删除 `on_reload()`、`_on_reload_callbacks`、循环。`Callable` import 一并移除 |
| 🏗️ Architecture | 简化 `patching._get_config()` 缓存 | v1.2.0 Config 改单例后，外层 `_config` 全局缓存冗余 | 删除 `_config` 全局，`_get_config()` 直接 `return Config()`。conftest 同步移除 `_config` 重置 |
| 🏗️ Architecture | 删除 TextState 死方法 | `is_dirty()`/`mark_flushed()`/`last_flushed` 在 v1.1.0 被 UnifiedLinearState 的 dirty 标志替代后从未被调用 | 删除方法 + 属性 + 2 个相关测试 |
| 🏗️ Architecture | 移除 `inject_time` 配置项 | Hermes v0.17.0+ 内置 `gateway.message_timestamps.enabled`，功能重叠 | 移除 `_inject_time_prefix` 函数、`_inject_time_guard` 重入守卫、config 属性、诊断日志、测试、文档说明。安装时不再注入此配置项 |
| 🏗️ Architecture | `enabled`/`linear` 不再写入默认配置 | 从来不会修改这两个字段的值，做成配置项没有必要 | 代码默认值改为 True，安装时不再注入 `enabled: true` 和 `linear: true` |
| 🔧 Fix | prune 日志 msg_id 截断过短 | `[:12]` 截断后无法与飞书后台完整 ID 关联 | 改为 `[:20]`，排障更高效 |
| ✨ Feature | `print_step` 配置项 | 飞书打字机每次 70ms 渲染 1 字符太慢，LLM 输出完成后卡片还要渲染很久 | 新增 `print_step` 配置项（默认 4，范围 1~10），每次 70ms 渲染 N 字符，速度 N 倍。真飞书测试验证 `print_step=4` 生效。需飞书 7.23+ 客户端 |
| ✨ Feature | Clarify 选项长文本截断 | LLM 传超长 choice 文本时下拉框换行难看 | `_normalize_choice` 超过 80 字符自动截断 + `…` 省略号 |
| ✨ Feature | 并发测试覆盖 | `_sessions`/`_clarify_*`/`_interrupt_map`/Config 锁无并发测试 | 新增 `tests/test_concurrency_v130.py` — 12 个并发测试（线程安全 + RLock 重入 + 无 RuntimeError） |
| ✨ Feature | Clarify E2E 真飞书测试 | Clarify 卡片无 E2E 测试覆盖 | 新增 `tests/e2e/test_e2e_clarify.py` — 5 个 E2E 测试（dict-repr 选项/特殊字符/question 转义/正常选项），真飞书验证通过 |
| ✨ Feature | Clarify 单元测试覆盖 | normalize/escape 逻辑无单元测试 | 新增 48 个单元测试（字段优先级/解析失败兜底/截断/转义/defense-in-depth） |

---

## v1.2.1 (2026-06-24)

| 类型 | 问题/功能 | 原因 | 修复/说明 |
|------|-----------|------|-----------|
| 🐛 Bug Fix (P0-01) | 乘号 `*` 被飞书 Markdown 吃掉 | 飞书 Markdown 解析器将 `2*4000+4*3000` 中的 `*4000+4*` 配对为斜体，导致乘号消失、数字拼合 | 新增 `escape_markdown_asterisks()` 函数（`cardkit/md.py`）：先保护合法 Markdown 结构（代码/粗体/合法斜体），再转义剩余 `*`。判断逻辑：CJK 字符后 `*` = 排版（斜体），ASCII 字母/数字后 `*` = 运算符（转义）。25 个测试用例验证。`controller/linear_mixin.py` 全部 7 处答案输出路径已接入 |
| 🐛 Bug Fix (P0-02) | 工具耗时显示 17 亿毫秒 | `started_at` 默认值 `0.0`（1970 年），未收到开始事件时 `time.time() - 0.0` 产生天文数字 | `ToolStep.started_at` 和 `ToolSession.started_at` 改为 `None`；`elapsed_ms` 属性和计算处加 `is None` 守卫 |
| 🐛 Bug Fix (P0-03) | 卡片元素裁剪计数偏差 + 折叠提示拼接错误 | 折叠提示预留空间硬编码为 1（不随模板变化）；`rstrip("已折叠")` 逐字符匹配会多删 CJK 字符 | 预计算折叠提示模板的实际 tag 数量（`_count_tag_objects`）；`rstrip` → `removesuffix`（精确后缀匹配） |
| 🐛 Bug Fix (P0-04) | 监控计数器高并发丢数 | `_metrics["cards_created"] += 1` 非原子操作，多线程并发可丢失计数 | 新增 `threading.Lock`（`_metrics_lock`），所有 `record_*`/`set_*`/`get_metrics`/`_do_reset` 函数加锁 |
| 🐛 Bug Fix (P0-05) | 可用性缓存无锁保护 + code=0 逻辑错误 | `_unavailable_cache` 字典并发读写可触发 `RuntimeError: dictionary changed size during iteration`；`or` 运算符吞掉 `code=0` | 新增 `threading.Lock`（`_unavailable_cache_lock`），`mark_unavailable`/`is_unavailable`/`_prune_cache` 加锁；新增 `_get_cached_code()` 线程安全读取函数；`or` → `is None` 精确判断 |
| 🔧 Fix (P1-01) | 刷新频率过高 | 生产日志显示单次对话 571 次 API 调用（~500ms 间隔），用户感知上 200ms 与 100ms 打字机效果无差别 | `flush_interval_ms` 默认值从 100 上调至 200；answer-only 间隔从 70ms 上调至 150ms |
| 🔧 Fix (P1-02) | 推理文本截断保护不一致 | 仅 timeline 路径的 in-progress reasoning 有 2000 字截断，其余 3 条渲染路径无截断保护，超长推理文本可致飞书 API 报错 | 提取 `_truncate_reasoning()` 函数（`cardkit/elements.py`），4 处推理文本渲染全部调用 |
| 🔧 Fix (P1-03) | `hasattr()` 不检查可调用性 | `feishu/client.py` 用 `hasattr()` 检查 SDK `acontent` 方法，仅验证属性存在不验证可调用 | `hasattr()` → `callable(getattr(..., 'acontent', None))` |
| 🔧 Fix (P1-04) | 后台任务标志并发不安全 | `_hls_bg_sending`/`_hls_cron_sending` 布尔标志在并发场景下，先结束的任务会重置标志，后结束的任务失去保护 | 布尔标志 → 计数器（`getattr(adapter, '_hls_bg_sending', 0) + 1`），读取处改为 `> 0` 判断 |
| 🔧 Fix (P1-05) | 追问选项超过 26 个时标签变特殊符号 | `chr(ord("A") + 26)` = `[`，不是有效字母 | `i < 26` 时用字母标签，否则用数字标签（27, 28...） |
| 🔧 Fix (P1-06) | 配置文件语法错误导致插件崩溃 | `yaml.safe_load()` 无 try/except，YAML 语法错误直接崩溃 | `_load()` 和 `_reload_cached()` 加 `try/except yaml.YAMLError`，解析失败用空配置继续运行 + 警告日志 |
| 🔧 Fix (P1-07) | 流式 API 日志过于 verbose | 每次 `stream_element` 成功打 INFO 日志，单次会话可产生数十条 | `stream_element OK` 日志从 INFO 降为 DEBUG |
| 🔧 Fix (P1-08) | 会话字典无上限导致内存泄漏 | `_interrupt_map` 无容量限制；`_clarify_*` 字典无 TTL 清理 | `_interrupt_map` 加 200 条上限；`_clarify_*` 加 30 分钟 TTL 清理（`_prune_expired_clarify()`） |

---

## v1.2.0 (2026-06-22)

| 类型 | 问题/功能 | 原因 | 修复/说明 |
|------|-----------|------|-----------|
| ✨ Feature (P0) | agent 卡片头部（head）可配置 | `header.enabled` 配置项与 builder/controller 接线在 v1.0.x 已存在但为"半成品"：无文档、无测试、封卡不变色、降级不覆盖，用户根本不知道有此功能 | 全面补全：① 文档化（README/README.zh-CN/SKILL/AGENT_GUIDE）；② 默认配置注入 `header: {enabled: false}`；③ `/aowen status` 展示 header 状态；④ 启动诊断日志加 header；⑤ 新增 header 测试覆盖 |
| 🐛 Bug Fix (P0) | 开启 header 后封卡头部颜色不变 | 飞书 CardKit settings/batch_update 接口不支持更新 card-level header（官方文档求证），增量封卡后头部永远停留在"处理中"蓝色。只有全量重建（cardkit_update）能改 header | **方案 B**：`_do_linear_complete` Step 5 加分支——`header_enabled=True` 时跳过增量封卡 `_preservative_seal`，直接走全量重建 fallback，用 `build_unified_complete_card` 生成正确状态色 header（蓝→绿/红）。关闭时（默认）仍走增量封卡，性能不变 |
| ✨ Feature | IM 降级卡片支持 header | `build_im_fallback_card`/`build_gateway_card` 不支持 header，开启 header 后降级会突然丢失头部 | **方案 A**：两个 builder 加 `header_enabled` 参数，降级调用点传 `self._cfg.header_enabled`，保持视觉一致 |
| 🐛 Bug Fix (P0) | 开 header 后 agent 报错时头部颜色与正文矛盾 | `is_error` 仅基于 `session.state` 判断，但 agent 报错时 `on_completed(error_message=...)` 只存 error_message，state 设为 COMPLETING（非 error 态）→ `is_error=False` → header 绿色"已完成"，但正文显示红色错误面板 | `is_error` 兼顾 error_message：`session.state in (CREATION_FAILED, TERMINATED) or (bool(error_message) and not is_aborted)`。报错时 header 正确用红色。**副作用（正向改善）**：默认路径（未开 header）agent 报错时，footer 状态文本也从"已完成"改为"出错"（红字），与红色 error panel 视觉一致（v1.1.x 是 footer"已完成"+红色 error panel 的矛盾） |
| 🔧 Fix | H6 header 主动重建污染"全卡重建"指标 + 误导性日志 | 开 header 正常完成走全量重建（设计行为），但复用 fallback 路径导致打"preservative seal failed"日志 + 调 `record_full_rebuild()`，/aowen monitor 的"全卡重建"计数被污染 | fallback 入口加 `_header_driven_rebuild` 标志区分"header 主动重建"vs"真实失败重建"：前者用专属日志"header rebuild succeeded"、不计入 full_rebuilds 指标；后者保持原日志+指标 |
| 🐛 Bug Fix (P0) | `/aowen config reload` 对 `_plugin_sec()` 属性不生效 | Config 无单例机制，`Config()` 每次新建实例。aowen reload 新建 Config 调 `reload()` 只清新实例缓存，controller/patching 持有的旧实例缓存不清 → 改 `header.enabled`/`enabled`/`linear`/`flush_interval_ms` 等配置 + reload 后不生效，必须重启网关 | Config 改单例模式（`__new__` + `_instance`），所有 `Config()` 返回同一实例，`reload()` 全局生效。conftest 加 `_reset_config_singleton` fixture 保证测试隔离 |
| 🔧 Fix | header 主动重建日志噪音 | 开 header 用户每次完成打 2 条 INFO 日志（skip + succeeded），生产中刷屏 | 两条日志降为 DEBUG（正常路径诊断信息） |
| 🔧 Fix | L1 日志去重在 drain 循环中遗漏 | `_streaming_closed_logged` 只覆盖 `_do_unified_flush` 3 处，drain 循环（最多 8 轮）2 处 streaming closed INFO 日志未去重，最多 16 条重复 | drain 阶段 2 处也加 `_streaming_closed_logged` 检查（300313 仍每次打，非重复事件） |
| 🔧 Fix | "streaming closed" 日志刷屏 | 生产中长对话末尾同一张卡 45 秒内打 15 条重复 INFO 日志 | `CardSession` 加 `_streaming_closed_logged` 标志，第一次打 INFO 后降为 DEBUG |
| 🏗️ Architecture | 删除 `CardVisualState` 死代码 | `CardVisualState`/`PHASE_TO_VISUAL`/`get_visual_state`/`session.visual_state` 在生产代码中从未被读取，卡片渲染实际用 `session.state`/`is_error`/`is_aborted` | 删除类、映射、函数、属性及相关导出和测试 |
| 📝 Docs | CHANGELOG v1.1.0 P0-3 mtime 描述勘误 | v1.1.0 先加了 mtime 自动检测（P0-3），后于同一版本周期内提交 0d468cd 明确删除（有意设计：避免高频 stat 系统调用），但 CHANGELOG 未同步更新，仍写"移到 `_plugin_sec()`" | 补勘误说明：mtime 检测已被有意移除，配置刷新靠 `/aowen config reload` 或重启；仅 inject_time/show_reasoning/gateway_cards 走 60s TTL 缓存。v1.2.0 不补回 mtime（尊重原决策） |
| 📝 Docs | panel 拆分函数现状说明 | v1.1.0 称 `build_panel_header`/`build_panel_children` 拆分支持"只重建 children"优化，但优化从未实现（两函数仅内部调用） | `build_unified_panel` docstring 补注释明确"单独入口当前仅内部使用，优化预留未启用"，避免维护者误解 |
| 🔧 Fix | 错误卡片未显示调试 ID | `_build_error_panel` 调用时未传 `card_trace_id`，错误卡片不显示调试 ID，用户报 issue 时无法提供 trace 关联日志 | `build_unified_complete_card`/`build_preservative_seal_actions` 加 `card_trace_id` 参数，传给 `_build_error_panel`。错误卡片显示"调试 ID: xxx"并提示"如果反复出错，请把调试 ID 反馈给开发者" |
| 🔧 Fix | 错误卡片技术详情区显示 HTML 标签乱码 | v1.1.0 错误面板用 `<details><summary>` HTML 标签实现折叠，但飞书 markdown 组件不支持 HTML 标签，标签会显示成乱码文本 | 去掉 `<details>` 标签，技术详情用分隔线 + 标题区分。外层 `collapsible_panel` 已提供折叠能力，无需嵌套 HTML 标签 |

> **延后到 v1.3.0（已完成）**：TextState 死方法精简（C3）、prune 日志显示更长 msg_id（M1）、on_completed 日志降级（M2）、`_sessions` 并发锁（M3）、Config `on_reload()` dead code 清理、`patching._get_config()` 缓存简化。均在 v1.3.0 完成。

---

## v1.1.3 (2026-06-21)

| 类型 | 问题/功能 | 原因 | 修复/说明 |
|------|-----------|------|-----------|
| 🐛 Bug Fix (P0) | CardKit 创建失败降级到 IM 卡片后内容全丢 | 降级代码设 `linear=False` + `unified_state=None`，导致 on_answer/on_thinking 直接跳过，内容从未写入卡片；_do_linear_complete 检查 `card_id=None` 直接返回 False | 降级时保留 `unified_state` 和 `linear=True`；新增 `_do_im_fallback_flush` 用 `update_card`（IM PATCH）全量更新内容；新增 `_do_im_fallback_seal` 用 `update_card` 封卡；_do_linear_complete 的 `card_id=None` 检查跳过 IM 降级模式 |
| ✨ Feature | IM 降级测试覆盖 | 之前没有任何测试覆盖 CardKit 创建失败后的内容写入路径，`test_cardkit_failure_falls_back` 只验证降级设置（还断言 `unified_state is None` 验证了 bug 存在） | 新增 `TestIMFallbackPath` 5 个测试：保留 unified_state / on_answer 写入 / flush 用 update_card / 封卡用 update_card / on_thinking 写入 |

---

## v1.1.2 (2026-06-20)

| 类型 | 问题/功能 | 原因 | 修复/说明 |
|------|-----------|------|-----------|
| ✨ Feature | Hermes v0.17.0 兼容性验证 | Hermes v0.17.0 (v2026.6.19) 发布，`_run_agent` 新增 `persist_user_message` 参数，`run_conversation` 内部重构 | 新增 3 个集成测试：验证 `_run_agent` 的 `persist_user_timestamp`/`persist_user_message` 参数存在，验证 `run_conversation` 仍可调用 |
| 📝 Docs | inject_time 与 message_timestamps 关系说明 | Hermes v0.17.0 内置 `gateway.message_timestamps.enabled`，和插件 `inject_time` 功能重叠 | README/AGENT_GUIDE 补充说明：建议优先使用官方 `message_timestamps`，开启时关闭插件 `inject_time` |
| 🔧 Fix | hermes-integration-test cron 时间调整 | `cron: '0 2 * * *'`（整点）GitHub Actions 延迟严重（5 小时） | 改为 `cron: '33 0 * * *'`（UTC 0:33 = 北京时间 8:33），避开整点高负载 |

---

## v1.1.1 (2026-06-20)

| 类型 | 问题/功能 | 原因 | 修复/说明 |
|------|-----------|------|-----------|
| 🐛 Bug Fix | drain 遇 300309（streaming closed）直接 skip 答案丢失 | `linear_mixin.py` drain 阶段 `stream_element` 遇 300309 时直接 skip，没有 fallback，答案内容从未写入卡片 | 统一 fallback：300309 和 300313 都改用 `batch_update` + `partial_update_element`（不带 tag）写入答案 |
| 🐛 Bug Fix | drain/seal fallback 带 tag 导致 300312 | `partial_update_element` 的 `partial_element` 带了 `tag`/`text_align`/`text_size`，飞书按官方文档拒绝（300312 "tag cannot be updated"） | 去掉 tag 等字段，只保留 `content`；新增 `_fallback_write_answer` 辅助函数统一处理 |
| 🐛 Bug Fix | `_prune_stale_sessions` 误清理 STREAMING session | 之前不检查 session 状态，只看 `created_at > TTL`，STREAMING 状态的 session 也会被清理，导致 AI 回调找不到 session、卡片永远卡在"流式中" | 只清理 `is_terminal_phase` 的 session，活跃 session 超 TTL 只打日志不清理 |
| 🔧 Fix | `_release_session_data` 死代码 | 函数定义了释放 `unified_state`/`text`/`tool_use` 重数据的逻辑，但从未被调用，封卡后 session 仍持有 AI 回答全文等重数据 | 封卡成功/失败后调用 `_release_session_data`，释放重数据，减少内存占用 |
| ✨ Feature | E2E 支持 open_id + chat_id | 之前只支持 `FEISHU_E2E_CHAT_ID`，用户给的 open_id 无法跑真飞书测试 | 新增 `FEISHU_E2E_OPEN_ID`，chat_id 和 open_id 都必填（分别测群聊和私聊） |
| ✨ Feature | E2E 时间模拟工具 | 长场景（TTL 超时等）测试需要真等 600 秒，耗时过长 | 新增 `simulate_session_age` 方法，修改 `session.created_at` 模拟超时，不用真等 |
| ✨ Feature | E2E 生命周期覆盖完善 | 现有测试只覆盖基本流程，缺少 300309 fallback/TTL 超时/中断/错误/长答案等场景 | 新增 8 个 E2E 测试：300309/300313 fallback、prune 保护/清理、release 数据、错误/中断/长答案生命周期 |
| ✨ Feature | sync-from-gitee 工作流支持真飞书 E2E | GitHub Actions 只跑 mock 测试，不跑真飞书测试 | 工作流分三步：单元测试（始终跑）+ E2E mock（始终跑）+ E2E 真飞书（有 secrets 才跑）；注入 4 个 GitHub Secrets 到 E2E 环境变量 |
| 🔧 Fix | E2E 测试间加延迟避免触发飞书 API 限制 | 飞书 CardKit API 限制 1000 次/分 & 50 次/秒（流式豁免），create/send/close 计入配额 | 真飞书模式下测试间加 1 秒延迟；mock 模式不延迟 |
| 📝 Docs | README GitHub 链接分支修正 | 智能安装提示的 GitHub 链接指向 `master` 分支，但 GitHub 备份仓库主分支是 `github_sync` | `master` → `github_sync` |

---

## v1.1.0 (2026-06-17)

| 类型 | 问题/功能 | 原因 | 修复/说明 |
|------|-----------|------|-----------|
| 🐛 Bug Fix (P0-1) | 并发限流调用 `on_message_interrupted` 方法不存在 | `controller/core.py` 在 `on_message_started` 并发限流分支中调用 `self.on_message_interrupted(...)`，但实际方法名为 `on_interrupted`，导致同一 chat_id 多消息场景抛 AttributeError，新消息也无法创建卡片 | 改为 `self.on_interrupted(...)`；并发限流正常触发旧卡 seal |
| 🐛 Bug Fix (P0-2) | FeishuClient 自定义 `base_url` 不生效 | `feishu/client.py` `__init__` 只用 `app_id`/`app_secret` 构建 client，未调用 `.domain(config.base_url)`，导致自建飞书/Lark 海外域名用户无法访问 API（永远走默认 open.feishu.cn） | `__init__` 中追加 `builder = builder.domain(config.base_url)`，`feishu.base_url` 配置项真正生效 |
| 🐛 Bug Fix (P0-3) | 部分配置属性 mtime 热更新失效 | `_check_mtime_and_invalidate()` 只在 `enabled` 属性中调用，其他属性（`linear`/`flush_interval_ms`/`max_tool_steps` 等）走 `_plugin_sec()` 不检测 mtime，改完配置文件后这些属性最多延迟 60s（TTL）才生效 | 将 mtime 检测从 `enabled` 属性移到 `_plugin_sec()`，所有走该方法的属性都检测文件变化 |

> **勘误（v1.2.0 补）**：上述 mtime 自动检测机制已在 **v1.1.0 内部**（提交 `0d468cd`，2026-06-18）被**有意移除**——删除了 `_check_mtime_and_invalidate()` 方法和 `_config_mtime` 字段，`_plugin_sec()` 不再调 `stat()`。原因：流式输出期间高频读配置，每次 stat 系统调用开销不可接受。配置刷新方式改为：`/aowen config reload` 立即生效，或重启网关。仅 `inject_time`/`show_reasoning`/`gateway_cards` 三个属性走 60s TTL 缓存（`_reload_cached()`）。**此为有意设计，非 bug**，v1.2.0 不补回 mtime 检测。
| ✨ Feature (P0-3) | `/aowen config reload` 命令 | 改完 config.yaml 后必须等最多 60 秒 mtime 检测才生效，调试不便 | `aowen/__init__.py` 新增 `/aowen config reload` 命令，立即清缓存，配置秒级生效。（注：v1.3.0 移除了 `on_reload` 回调——该回调从未被任何模块调用，属死代码） |
| 🐛 Bug Fix (P0-4) | pyproject.toml packages 列表遗漏 5 个子包 | `[tool.setuptools.packages.find].include` 只列了 controller/cardkit/patching/state，缺 feishu/config/monitor/plugin/flush，pip install 时这 5 个子包不会被打包，运行时 ImportError | packages 列表补全 9 个子包（含 `feishu*`/`config*`/`aowen*`/`plugin*`/`flush*`） |
| 🐛 Bug Fix (P0-5) | `unregister()` 未清理活跃会话 | `plugin/__init__.py` `unregister()` 只清理 config，未清空 `ctrl._sessions`，卸载/重装后旧会话残留导致内存泄漏与潜在竞态 | `unregister()` 新增 `ctrl._sessions.clear()` 清空活跃会话 |
| 🏗️ Architecture (P0-6) | 删除 `cardkit/theme.py` | v1.1.0 引入的主题系统实际未被任何业务代码引用，颜色/图标仍硬编码在 `elements.py` 中，主题配置项无效 | 删除 `cardkit/theme.py` + `cardkit/__init__.py` 中的 `from .theme import *`；README/AGENT_GUIDE/SKILL 同步移除 `theme.*` 配置项说明 |
| 🏗️ Architecture (P0-7) | 删除 `assets/card_templates/` | v1.0.5 导出的 13 个卡片模板 JSON 文件未被任何代码引用，卡片逻辑全在 Python 源码中 | 删除 `assets/card_templates/` 目录（13 个 JSON 文件） |
| 🐛 Bug Fix | 300313 "not find elementID" 短回复卡片闪烁 | add_elements 后 1s 内 stream_element 返回 300313（飞书服务端元素持久化传播延迟），源码无此错误码处理，drain 8 轮全失败后 full rebuild 导致卡片闪烁 | 新增 `CARDKIT_ELEMENT_NOT_FOUND = 300313` 常量 + `is_element_not_found_error()` 判断；stream_element 内置 200ms×3 次专用重试；drain/seal 阶段 300313 时 fallback 到 `partial_update_element` 写入 answer |
| ✨ Feature | stream_element 成功日志 | 生产日志 22h 内 0 次成功的 stream_element 日志，无法判断是否工作 | 新增 INFO 级 `HLS: stream_element OK` 日志，记录 card/element/len/seq |
| 🔧 Fix | 日志前缀混乱 | HLS_DIAG/HLS_WRAP/HLS_CALLED/HLS_FIX 四个前缀无规范，22 处散落 | 统一为 `HLS:` 前缀；诊断日志全部降为 DEBUG；WARNING 只留给功能受损 |
| ✨ Feature | card_trace_id | 同一张卡片的日志散落不同时间点，靠 msg_id 人工串联 | CardSession 新增 `card_trace_id`（msg_id 后 6 位），关键生命周期日志统一带 trace |
| ✨ Feature | 启动补丁应用报告 | Hermes 升级后补丁静默失效，无结构化状态 | `apply_patches()` 结束时记录 `_patch_status` 字典（6 个补丁目标 + Hermes layout） |
| ✨ Feature | `doctor` 命令 | 用户排障需要手动跑多个命令 | `__main__.py doctor`：6 步检查（版本/Python/配置/凭据/补丁状态/日志路径） |
| 🔧 Fix | 文档错误 | ISSUES_TEMPLATE 让用户 grep `gateway.log`（实际在 `agent.log`）；AGENT_GUIDE 配置项名写错 | 修正日志路径；配置项名对齐代码；show_reasoning 从 hermes_lark_streaming 节移到 display 节 |
| 🔧 Fix | 19 处 `except Exception: pass` | 异常被静默吞掉，排查时看不到 | 替换为 `_logger.debug("HLS: suppressed exception", exc_info=True)` |
| 🏗️ Architecture | 删除非线性 ControllerMixin 主路径 | 631 行代码几乎不用（CardKit 创建失败时直接降级到 IM 卡片） | 删除 `_do_create_card`/`_do_update_card`/`_do_tool_use_status_update`/`_do_reasoning_update`/`_do_complete`/`_do_complete_inner`（−407 行）；core.py 始终走线性路径 |
| 🔧 Fix | 去重机制 5 层叠加 | `_hls_wrapper` + `already_streamed` + `_stream_consumed_len` + `_native_reasoning_active` + `_force_rewrap`，逻辑难追踪 | 移除 `_native_reasoning_active`（用 `bool(state._current_reasoning)` 代替）和 `_force_rewrap`（用 `_resolve_eid()` ContextVar 重解析代替）；简化 late_reasoning_wrapper（58→17 行） |
| 🔧 Fix | 状态机 8 个布尔标志位 | `_panel_element_created`/`_answer_element_created`/`_loading_hint_removed` 等标志位组合爆炸 | 合并为 `_creation_stages: set[str]`（含 `"panel"`/`"answer"`/`"hint_removed"`），24 处机械替换 |
| 🏗️ Architecture | 删除 backward-compat 别名 | `LinearState`/`Segment`/`linear_state`/`FAILED`/`LinearControllerMixin` 占据维护成本 | 全部删除，源码引用改为 `UnifiedLinearState`/`ReasoningRound`/`unified_state`/`CREATION_FAILED`/`UnifiedControllerMixin` |
| ✨ Feature | 拆分 build_unified_panel | 每次 flush 重建整个 panel JSON | 拆为 `build_panel_header()` + `build_panel_children()`，支持只重建 children |
| ✨ Feature | 错误卡片友好化 | 错误卡片直接显示技术细节（如 `300315 unknown property 'icon'`） | 改为"AI 回复出错，请重试"+ 调试 ID + 可折叠技术详情 |
| ✨ Feature | 并发限流 | 同一 chat_id 多张活跃卡片竞争 API 调用 | `on_message_started` 时 seal 同 chat_id 的旧活跃卡片为"被新消息取代" |
| 🏗️ Architecture | Hermes 适配层 | Hermes 内部接口散落在 patching/__init__.py，升级时改多处 | 新建 `patching/hermes_adapter.py`，`HermesCompat` 类封装所有 Hermes 内部模块访问 |
| ✨ Feature | 版本探测 + 适配 | Hermes 升级后无法自动选择正确的适配实现 | `HermesCompat._detect_version()` 探测 Hermes 版本，`_resolve_modules()` 3 层策略解析 conversation_loop |
| ✨ Feature | 完整端到端测试框架 | 无"发消息→看卡片 JSON"全链路测试 | `tests/e2e/` 新增 MockFeishuServer + E2ETestRunner + 14 个测试用例；mock/真飞书自动切换（有 FEISHU_E2E_* 环境变量→真飞书，无→mock） |
| ✨ Feature | 配置项运行时热更新 | 改配置要重启网关 | `Config.reload()` 清缓存 + mtime 自动检测 + `on_reload` 回调注册；`/aowen config reload` 命令秒级生效 |
| ✨ Feature | 监控面板 | 无实时插件健康指标 | `aowen/` 子包通过 pre_gateway_dispatch hook 拦截 /aowen 命令，直接回复飞书卡片，不经过 Hermes AI |
| 🏗️ Architecture | 根目录文件模块化 | hermes_adapter.py/monitor.py/plugin.py/conftest.py 散落在根目录，不便维护 | hermes_adapter.py → patching/hermes_adapter.py；monitor.py → monitor/__init__.py；plugin.py → plugin/__init__.py；conftest.py 合并到 tests/conftest.py |
| 📝 Docs | README/AGENT_GUIDE/SKILL 文档同步 | v1.1.0 架构改动后文档未更新 | SKILL.md 删除"常见陷阱"章节（迁移到 CHANGELOG 附录），重写架构/文件地图；README 监控面板归入配置说明；验证安装加 doctor 命令 |
| ✨ Feature | /aowen 卡片视觉重构 | 6 张 /aowen 卡片（help/status/monitor/reset/config reload/unknown）用纯 markdown 列表+1:2 column_set，视觉层次单薄，PC+移动端观感一般 | 引入统一设计语言：banner(图标+标题) → 关键指标列 → 详情图标行 → 折叠次要信息 → 灰色 footer；新增 7 个辅助函数（_icon_div/_metric_block/_two_col/_three_col/_section_title/_fold/_footer_note）；颜色语义化（green=success/orange=warning/red=error/blue=info/grey=neutral）；全部 column_set 用 flex_mode=stretch 实现响应式；只用 v2 安全标签（div/lark_md/plain_text/hr/column_set/column/collapsible_panel/standard_icon/markdown），不引入 button/form_container/interactive_container |
| ✨ Feature | /aowen 中断场景提示卡 | AI 回复中（agent 运行中）发送 /aowen 命令时，Hermes 网关走"agent 运行中"快速路径，未知 slash 命令（/aowen 不在白名单）fall through 到默认中断路径，命令文本被当普通消息发给 LLM；pre_gateway_dispatch hook 不在该路径上触发 | 借鉴 Hermes 原生 /model 命令的 "Agent is running — wait or /stop first" UX；新增 `build_interrupt_hint_card()`（橙色 header "AI 正在回复中" + 警告图标 banner + 蓝色 info 图标提示"等待完成或 /stop"+ 灰色 footer"命令已忽略"）；在 `patching/gateway.py` 的 `_wrap_handle_message` 中检测 agent 运行中 + /aowen 命令时发送提示卡并 return ""，阻止消息进入 agent |

---

## v1.0.7 (2026-06-16)

| 类型 | 问题/功能 | 原因 | 修复/说明 |
|------|-----------|------|-----------|
| 🐛 Bug Fix | Cron/Gateway 静态卡片表格超限 | `build_cron_card()` 和 `build_gateway_card()` 调用 `_downgrade_tables()` 时不传 `limit`，默认用 20。但飞书静态卡片（非流式）硬限 5 张表格，超限被截断或报错 | 新增 `_MAX_CRON_TABLES = 5` 常量，Cron/Gateway 卡片改用 `limit=_MAX_CRON_TABLES`；流式卡片仍用 20 阈值 |
| 🔧 Fix | 工具步骤标题显示冗余状态文字 | 工具步骤标题有状态文字（Running/Succeeded/Failed），应去掉，只靠颜色区分；推理轮次标题没加粗、没颜色区分 | `_tool_status_info()` 去掉 label，`running` 颜色改 `orange-300`；`_build_tool_step_title()` 改为颜色+加粗统一格式；新增 `_build_reasoning_round_title()` 辅助函数统一推理标题渲染（orange-300 进行中、green 已完成、red 失败） |
| 🔧 Fix | 推理内容缺少缩进 | 推理轮次的思考内容（markdown tag）和标题左对齐，没有缩进；而工具步骤的 detail/output 都有 22px 缩进 | 推理内容从 `markdown` tag 改为 `div` + `lark_md` + `margin: "0px 0px 0px 22px"`，与工具内容对齐 |
| 🔧 Fix | Schema Error 300315 日志缺少关键细节 | 飞书返回 300315 错误时包含具体哪个属性非法，但日志只记录整个异常字符串，需人工翻找 | `FeishuAPIError` 新增 `extract_schema_detail()` 方法，3 处 schema error 日志新增 `detail:` 字段，一眼可见非法属性 |
| 🐛 Bug Fix | 并发消息可能污染新卡片内容 | 用户快速连发多条消息时，旧消息的回调可能在旧 session 上继续写入，导致新卡片内容被污染 | `on_reasoning`/`on_tool_update`/`on_answer` 三个回调入口添加 epoch 校验，检测到 stale epoch 自动跳过 |
| ✨ Feature | 新增 AGENT_GUIDE.md | Agent（AI 助手、自动化脚本）需要高信息密度文档了解安装/配置/排障，现有 README token 消耗高 | 新增 `docs/AGENT_GUIDE.md`，~2KB 高密度机器可读文档 |

## v1.0.6 (2026-06-15)

| 类型 | 问题/功能 | 原因 | 修复/说明 |
|------|-----------|------|-----------|
| 🐛 Bug Fix | 卡片超限300305导致内容重复 | 当 AI 调用大量工具（如44步）时，卡片元素数超过飞书200上限，封口报错300305后触发文本兜底，导致卡片和文本重复展示同一内容 | 统一面板自动裁剪超出的推理轮次和工具步骤，折叠为提示行（`⚡ 还有X步早期操作已折叠`），确保卡片元素永不超限 |
| 🔧 Fix | 封口顺序导致卡片被"冻住"缺页脚 | `_preservative_seal` 中先 `close_streaming` 再 `batch_update`，若封口失败则卡片流式已关闭但缺页脚 | 将 `close_streaming` 移到 `batch_update` 之后执行，先写入内容+页脚再关闭流式模式 |
| ✨ Feature | 新增面板裁剪配置项 | 无法控制统一面板中显示的推理轮次和工具步骤数量 | 新增 `max_tool_steps`（默认20）和 `max_reasoning_rounds`（默认20）配置项，超出部分自动折叠为提示行，范围1~100 |
| ✨ Feature | 卡片级元素安全网 | 面板内部安全网无法感知面板外部元素（answer、footer、error），只能用保守160阈值猜测 | 安全网上移到卡片层：封卡时已知所有元素（面板+answer+footer+error），精确递归计数总 tag objects，超过195（200-5缓冲）自动从面板children最老项开始裁剪；两条封卡路径（`_preservative_seal` 逐增量 + `build_unified_complete_card` 全卡重建）均覆盖 |

## v1.0.5 (2026-06-14)

| 类型 | 问题/功能 | 原因 | 修复/说明 |
|------|-----------|------|-----------|
| 🐛 Bug Fix | notify_feishu.py 提交消息重复 | Gitee MR 合并产生重复提交消息（如 `!42 fix: xxx` 与 `fix: xxx`） | `notify_feishu.py` 新增提交消息去重逻辑，基于规范化消息文本（去除 `!N ` 前缀）去重 |
| 🐛 Bug Fix | 简单对话显示空白 agent loop 面板 | 无工具/推理的简单对话在 Phase 2 创建了空面板，用户看到无内容的可折叠区域 | Phase 2 拆分为两条路径：有面板（工具/推理存在时）和无面板（简单对话）；晚到的推理/工具通过动态添加面板处理 |
| 🐛 Bug Fix | 正常完成的卡片被新消息覆盖成"已停止" | `on_interrupted`/`on_aborted` 不检查 COMPLETING 状态，导致正在收尾（drain）的 session 被误标 ABORTED，触发 fallback 发送 26 字符短文本覆盖完整卡片 | `on_interrupted`/`on_aborted` 入口新增 COMPLETING 短路：仅跳过 abort 逻辑，新 session 创建和 `_interrupt_map` 更新照常执行；`on_aborted` 标记 `_was_aborted` 让封卡显示"已停止"状态 |
| 🔧 Fix | `.hermes-last-release` 被 sync-from-gitee 反复覆盖 | 该文件被 git 追踪，GitHub Actions 写入新版本后，sync-from-gitee 每小时同步将 Gitee 侧的 `none` 覆盖回 GitHub，导致集成测试每天重复运行 | 将 `.hermes-last-release` 从 git 追踪移除（加入 `.gitignore`），改用 GitHub Actions Cache 持久化版本状态，不受同步工作流影响 |
| 🔧 Fix | FeishuAdapter 反应拦截在 Hermes 新版本静默失效 | Hermes 新版本将 `add_reaction`/`delete_reaction` 改为私有方法 `_add_reaction`/`_remove_reaction`，插件补丁使用 `try/except AttributeError` 静默跳过 | 补丁逻辑增加 fallback：先尝试公共方法名，失败后尝试私有方法名，兼容新旧版本 |
| 🔧 Fix | 3 个单元测试与 v1.0.5 Phase 2 拆分不同步 | Phase 2 拆分后简单对话不再创建空面板，新增 `_answer_element_created` 标志，部分测试缺少该标志导致测试路径错误 | 补全 `_answer_element_created = True`；修正 `_panel_element_created` 断言为 `_answer_element_created`；新增简单对话（无面板）完整生命周期测试 |
| 🔧 Fix | 集成测试在 sync-from-gitee 工作流中 25 个 skipped | `pytest tests/` 包含集成测试目录，但该工作流不设置 `HERMES_SRC_DIR`，导致全部 skip | `pyproject.toml` 新增 `norecursedirs = ["tests/integration"]`，集成测试由 `hermes-integration-test.yml` 单独运行 |
| ✨ Feature | Hermes Agent 集成测试工作流 | 需要自动检测 Hermes 新版本并验证插件兼容性 | 新增 GitHub Actions 工作流，每日上海时间 10:00 运行，检查 Hermes 新版本发布、运行兼容性测试、通知飞书 |
| ✨ Feature | 飞书卡片模板导出 | 卡片模板分散在代码中，不便维护和复用 | 所有飞书卡片模板导出至 `assets/card_templates/` 目录，集中管理 |

## v1.0.4 (2026-06-13)

| 类型 | 问题/功能 | 原因 | 修复/说明 |
|------|-----------|------|-----------|
| 🐛 Bug Fix | GitHub Actions 测试失败 (37/780) | test_phase.py: `asyncio.get_event_loop()` 在 Python 3.10+ 无事件循环时抛 RuntimeError；test_monkey_patch.py: guard 日志从 `_logger.debug("HLS_WRAP: guard check")` 变更为 `_logger.warning("HLS_DIAG: ...")` 但测试未同步；test_version.py: `importlib.reload` 在 Python 3.11+ 要求 `__spec__` 非 None | 1) `FlushController.__init__` 惰性获取事件循环（`_loop=None` + `_get_loop()`）；2) 测试设置 `asyncio.set_event_loop()`；3) 测试断言更新为 `_logger.warning` + `HLS_DIAG`；4) `importlib.reload` 替换为 `spec_from_file_location` 重注册 |
| 🐛 Bug Fix | 验证安装/卸载步骤 HERMES_PYTHON 路径错误 | 文档硬编码 `~/.hermes/hermes-agent/venv/bin/python3`，仅适用于 Hermes Desktop；CLI/服务器安装路径为 `/usr/local/lib/hermes-agent/venv/bin/python3` | 新增 `__main__.py python` 命令自动检测路径；文档改用 `$(python3 ... __main__.py python)` 自动检测；补充手动设置说明 |
| ✨ Feature | 新增 `python` CLI 命令 | 用户需要知道 Hermes venv Python 的路径才能运行 status/verify/cleanup | `__main__.py python` 自动搜索常见安装路径并输出，简化文档指令 |
| 🔧 Fix | FlushController 在无事件循环环境初始化失败 | Python 3.10+ 中 `asyncio.get_event_loop()` 在无事件循环时抛 RuntimeError | `__init__` 捕获双重 RuntimeError 设 `_loop=None`，新增 `_get_loop()` 惰性获取 |

## v1.0.3 (2026-06-12)

| 类型 | 问题/功能 | 原因 | 修复/说明 |
|------|-----------|------|-----------|
| 🏗️ Architecture | 卡片生命周期状态机优化 | 参考 openclaw-lark 设计，需显式状态转换、终端原因追踪、epoch 机制 | 新增 `CardPhase`/`TerminalReason`/`CardVisualState` + `PHASE_TRANSITIONS` 转换图 + `transition()`/`should_proceed()`/`is_stale_create()`/`enter_terminal()` 方法；`CREATION_FAILED` 替代旧 `FAILED`；新增 `TERMINATED` 阶段；88 个测试覆盖 |
| 🐛 Bug Fix | 会话列表永久显示"处理中..."（中文用户） | `cardkit_close_streaming` 只更新 `summary.content`，未更新 `summary.i18n_content`；飞书根据用户语言显示 `i18n_content.<locale>` | `cardkit_close_streaming` 同时更新 `content` 和 `i18n_content`（zh_cn + en_us）；新增 `_build_summary()` 辅助函数；4 个回归测试 |
| 🐛 Bug Fix | 重复 `close_streaming` 导致 300317 序列冲突 | `_preservative_seal` 主路径和重试路径都调用 `close_streaming`，第二次调用时 sequence 已过期 | `CardSession` 新增 `_streaming_closed` 守卫标志，确保 `close_streaming` 只调用一次 |
| 🐛 Bug Fix | `UnboundLocalError: 'panel'` 导致恢复路径崩溃 | 300317 重试路径引用 try 块中 `panel` 变量，但 `panel` 在 `close_streaming` 之后才赋值 | 重试路径始终从当前状态重建 `retry_panel`，而非引用 try 块局部变量 |
| 🐛 Bug Fix | 折叠面板思考内容重复（DeepSeek 模型） | `stream_delta_callback` 为 None 时，守卫仅检查其 `_hls_wrapper` 标记，`interim_assistant_callback` 被双重包装 | 守卫同时检查两个回调的 `_hls_wrapper` 标记；去重逻辑从精确匹配升级为长度追踪 |
| 🐛 Bug Fix | 会话列表完成后仍显示"处理中..." | 飞书 settings API 不稳定处理同一请求中的 `summary` + `streaming_mode: false` | 两步更新：先 `close_streaming`（不含 summary），再 `cardkit_update_summary`；流式已关闭时仍更新摘要 |
| 🐛 Bug Fix | 封卡时内容丢失 | `_preservative_seal` 的完整性守卫只清除 dirty 标记未实际 flush | 守卫升级为在 `close_streaming` 前实际 flush 剩余脏数据 |
| 🐛 Bug Fix | 页脚早于回答内容出现 | `COMPLETING` 在 `_TERMINAL` 集合中，晚到回调被丢弃 | 移除 `COMPLETING` 出终端集；drain 步骤确保内容完整输出 |
| 🐛 Bug Fix | 流式参数低于官方默认值 | `print_frequency_ms` 为 10ms（官方默认 70ms） | `print_frequency_ms` 提升至 70ms；`_ANSWER_FAST_STREAM_MS` 提升至 70ms；`flush_interval_ms` 范围改为 70–2000ms |
| ✨ Feature | 打字机效果 | 流式卡片输出按字符渲染，匹配飞书 CardKit v2.0 文档行为 | `print_frequency_ms=70`、`print_step=1`、默认 `flush_interval_ms=100ms`、仅回答快流 70ms |
| 🚀 Performance | 延迟 Markdown 优化 | 流式期间每次 flush 都执行 `optimize_markdown_style` 开销大 | 流式期间发送原始文本，仅在封卡时执行完整 Markdown 优化 |
| 🚀 Performance | 间隔计时器优化 | `LONG_GAP_MS` 和 `BATCH_AFTER_GAP_MS` 过长 | `LONG_GAP_MS` 2.0s → 1.0s，`BATCH_AFTER_GAP_MS` 300ms → 100ms；瞬态重试延迟缩减 |

---

## 附录：历史陷阱与经验教训

> 以下内容记录了插件开发过程中遇到的关键陷阱和修复经验，按主题分类。这些经验已融入代码设计，记录于此供后续维护参考。

### A. 异步与线程安全

| # | 陷阱 | 教训 |
|---|------|------|
| A1 | 事件循环死锁 | 在 async 函数中绝不用 `run_coroutine_threadsafe().result()`，直接 `await` |
| A2 | contextvars 不跨线程 | 用 `_thread_local_ctx` 手动传递；`_run_agent` 中设置 thread-local |
| A3 | FlushController 线程安全 | worker 线程必须用 `call_soon_threadsafe()`，`call_soon()` 不唤醒事件循环→flush 永不执行 |

### B. 内容去重

| # | 陷阱 | 教训 |
|---|------|------|
| B1 | `already_streamed` 忽略导致双重投递 | Hermes 调用 `interim_assistant_callback(text, already_streamed=True)` 时，必须跳过 `on_thinking_delta`，直接透传给原始回调 |
| B2 | 精确字符串去重失败 | `interim_assistant_callback` 投递累积文本，与增量块长度不同，精确匹配永远失败。改用 `_stream_consumed_len` 按 eid 追踪已消费总长度 |
| B3 | `_maybe_wrap_callbacks` 双重包装 | 当 `stream_delta_callback` 为 None 时，守卫必须同时检查 `stream_delta_callback` AND `interim_assistant_callback` 的 `_hls_wrapper` 标记 |
| B4 | 推理内容重复（DeepSeek 模型） | 当原生 `reasoning_callback` 已激活时，`_linear_on_thinking` 必须跳过 `on_reasoning_delta`，避免累积文本再次追加 |

### C. 状态机与竞态

| # | 陷阱 | 教训 |
|---|------|------|
| C1 | `on_interrupted` 误触发于 COMPLETING | 旧 session 处于 COMPLETING 时，只跳过 abort 逻辑，但新 session 创建和 `_interrupt_map` 更新仍照常执行 |
| C2 | `card_sent` 区分完成与中断 | 返回 None 两种含义：`card_sent=True`→正常完成抑制文本；`card_sent=False`→真正 abort/error |
| C3 | Epoch 机制防止过期创建回调 | 创建前快照 `epoch = session.create_epoch`，创建后检查 `is_stale_create(epoch)`——epoch 已变则跳过转换 |
| C4 | 幂等守卫 | COMPLETING 状态同步转移 + 300317 容错，适用于异步回调竞态 |

### D. 封卡与流式关闭

| # | 陷阱 | 教训 |
|---|------|------|
| D1 | `close_streaming` 重复调用 | 对同一张卡片只能调用一次。重复调用导致 300317 sequence conflict。`CardSession` 新增 `_streaming_closed` 布尔标志 |
| D2 | 重试路径引用 try 块局部变量 | `_preservative_seal` 的 300317 重试路径引用了 `panel["header"]`，但 `panel` 仅在 try 块中赋值。重试路径必须从当前状态重建变量 |
| D3 | 封卡只删除实际存在的元素 | v1.0.2 之前盲目删除所有已知元素 ID，导致 300314 失败。现在只删除 `existing_elements` 中的元素 |
| D4 | 状态标志必须在 API 成功后设置 | `_loading_hint_removed` 等标志在 `batch_update` 成功后才设置，否则 API 失败时标志已设但实际未生效 |
| D5 | 完成前排空剩余脏数据 | `on_completed` 触发时可能还有脏数据未 flush。drain 步骤显式 flush 剩余内容，再 `mark_completed()` → close streaming → add footer |
| D6 | 关闭流式时必须更新摘要（含 i18n_content） | `close_streaming` 时同时更新 `summary.content` 和 `summary.i18n_content`（zh_cn + en_us），否则中文用户会话列表永久显示"处理中..." |

### E. Monkey Patching

| # | 陷阱 | 教训 |
|---|------|------|
| E1 | 签名确认 | 必须确认目标是类方法还是模块级函数；签名不匹配 = 静默失败 |
| E2 | `add_reaction` 改名 | Hermes 新版本将 `add_reaction`/`delete_reaction` 改为 `_add_reaction`/`_remove_reaction`，补丁需 fallback 尝试两种命名 |

### F. 性能与参数

| # | 陷阱 | 教训 |
|---|------|------|
| F1 | 性能参数应可配置 | 性能敏感参数不应硬编码。默认 200ms 刷新间隔（v1.3.1 恢复，可配置 70~2000ms，最低 70ms 对齐飞书官方 `print_frequency_ms`） |
| F2 | 流式参数不低于官方推荐值 | `print_frequency_ms` 官方默认 70ms，`print_step` 官方默认 1，不可低于此值 |
| F3 | ~~主动 TTL 延长~~ (v1.3.1 已移除) | 原设计：卡片生存时间接近 540s 时自动延长 600s。**v1.3.1 真飞书 API 实测发现 `streaming_config.ttl_seconds` 参数返回 300122 "unknown property"，飞书 CardKit v2.0 settings API 不支持 TTL 延长参数**。功能已删除，改为 300309 fallback（`_fallback_write_answer`）处理长对话流式关闭 |
| F4 | 延迟 Markdown 优化 | 流式期间发送原始文本，仅在封卡时执行完整 Markdown 优化 |
| F5 | 卡片未就绪时的延迟 flush | `card_message_ready=False` 时标记 `_pending_flush`，卡片创建完成后立即执行 |

### G. 架构设计

| # | 陷阱 | 教训 |
|---|------|------|
| G1 | 统一面板消除元素爆炸 | v1.0.2 之前每个 reasoning round 创建独立面板（4 元素/面板），元素数线性增长。统一面板架构集中在 1 个面板 + 1 个回答元素 = 3–4 元素恒定 |
| G2 | 按时间线交错渲染 | `panel_events` 时间线记录事件顺序，面板内容按时间线交错渲染（reasoning→tool→reasoning→tool） |
| G3 | 卡片生命周期 4 阶段渐进构建 | Phase 1 占位卡片（2 元素）→ Phase 2 首 token 添加面板/回答 → Phase 3 流式更新 → Phase 4 添加页脚 |
| G4 | `CREATION_FAILED` 替代 `FAILED` | 旧的 `FAILED` 是 catch-all，拆分为 `CREATION_FAILED`（创建失败）和 `TERMINATED`（消息删除） |
| G5 | 外部参数 NoneType 防护 | 外部字符串做切片/下标时必须防御 None：`(message_id or "?")[:12]` |
