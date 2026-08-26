## v1.7.0 (2026-08-26)

全面审计修复版 — 结合 hermes-agent v0.20.5（tag v2026.8.19）最新源码的兼容性审计 + 5 轮插件深度审计（健壮性/性能/Bug/UX/一致性）+ 真飞书 E2E 铁证。本轮共修复 26 项确认问题（P1×8 / P2×14 / P3×4），删除 3 处交叉验证确认的死代码，新增 46 个回归测试（全套件 888 通过 + 集成 26 通过）。

| 类型 | 问题/功能 | 原因 | 修复/说明 |
|------|-----------|------|-----------|
| 🐛 Bug Fix (P1) | **RELAY 中继部署下 cron 推送退化为纯文本**（用户报告）：hermes v0.20.5 支持 RELAY 前置部署——连接器进程持有飞书凭据，网关进程 `adapters` 字典里没有 native FeishuAdapter，全部飞书出站流量走单一 RelayAdapter（`DeliveryTransport.send` 在 relay 模式下调 `adapter.send_for_platform(...)`（gateway/delivery.py:83），绕过 `feishu_adapter.send`）。插件的 `_wrap_cron_deliver` 找不到 feishu adapter 直接 passthrough → cron 卡片从未生成 | 插件所有出站拦截都打在 FeishuAdapter 类上，对 relay 模式完全无效 | **主链路修复**：①提取共享出站分发 `_dispatch_feishu_outbound()`（turn-final 抑制/​/stop 检测/cron 卡片/gateway 卡片，native 与 relay 共用同一逻辑）；②新增 `_wrap_relay_adapter_send`（按 `_platform_by_chat`/​`_relay_logical_platform` 元数据识别飞书会话，非飞书平台透传，`_interim_send` 帧透传）+ `_wrap_relay_adapter_send_for_platform`（cron/定时车道，按显式平台参数过滤）；③cron 识别用 hermes 投递元数据 `job_id`（cron/scheduler.py route_metadata 穿透 DeliveryRouter 存活到 adapter 层，代码级验证）；④`_apply_relay_adapter_patches` 经 v1.6.0 的 `platform_registry.create_adapter` hook 扩展安装（relay adapter 同样经该工厂创建——gateway/relay/__init__.py register_relay_adapter 注册 factory，代码级验证），外加 apply_patches 时直接 import 兜底；⑤relay 模式下 `_wrap_cron_deliver` 检测 relay-fronts-feishu 并打日志说明链路 (`patching/adapter.py`, `patching/__init__.py`, `patching/gateway.py`) |
| 🐛 Bug Fix (P1) | COMPLETED/ABORTED 两条最常见终态路径全部遗漏 `enter_terminal()`——`terminal_reason`/`terminal_source` 永远为空，`create_epoch` 不递增，监控无法区分正常完成与中断 | 生产代码全部绕过 `transition()` 直接赋值 state（grep 证实 11 处直接赋值 vs 0 处生产调用 transition） | 7 处终态赋值后补 `enter_terminal(reason=..., source=...)`：on_aborted / on_interrupted 3 处 / seal-success ABORTED+COMPLETED / reactivation。新增 `TerminalReason.SUPERSEDED`（续写重激活语义）(`controller/core.py`, `controller/linear_mixin.py`, `state/phase.py`) |
| 🐛 Bug Fix (P1) | `_wait_and_abort` 只检查 COMPLETING——seal 链在 3s flush 等待窗口内完成时（state 已 COMPLETED/CREATION_FAILED），照样覆写为 ABORTED：非法状态转换（PHASE_TRANSITIONS 中 COMPLETED 是吸收态）+ 二次 `_do_linear_complete` → 双重封卡 + `_send_text_fallback` 重复文本回复 + 成功状态被 CREATION_FAILED 覆盖 | v1.3.2 fix (B3-01) 只覆盖一种竞态（flush 期间进入 COMPLETING），漏了"等待期间已完成" | `_wait_and_abort` 增加 `is_terminal_phase` 早退分支 (`controller/core.py`) |
| 🐛 Bug Fix (P1) | `on_completed` 幂等检查漏掉 ABORTED——/stop 后迟到的 on_completed 返回 False，gateway 把缓冲的最终答案又发了一遍纯文本（已停止的卡旁边重复一条答案） | 幂等集合只有 `(COMPLETING, COMPLETED)`，ABORTED 既不在幂等集合也不在 yield-to-gateway 集合 | ABORTED 显式分支：吞掉迟到完成（on_aborted 已封卡）并打 INFO 日志 (`controller/core.py`) |
| 🐛 Bug Fix (P1) | 续写重激活不递增 epoch——重激活窗口内（COMPLETING 但未终态）迟到回调（on_reasoning/on_tool_update）的 epoch 守卫全部失效，可能写入正在封卡的 session | `enter_terminal` 只在终态调用，COMPLETING 期间 epoch 不变 | `_reactivate_session_for_continuation` 设 COMPLETING 时立即调 `enter_terminal(reason=SUPERSEDED)`——epoch 立即失效迟到回调 (`controller/core.py`) |
| 🐛 Bug Fix (P1) | 空内容卡片必败 API：`build_cron_card`/`build_gateway_card` 空 content 时产出 `body.elements=[]`——真飞书 E2E 铁证 230099/200621 "parse card json err"（T7），cron 空白内容触发一次必败 API + 静默降级纯文本 | 飞书 Card 2.0 拒绝空 body；builder 无守卫 | 主流程修复：builders 兜住空 elements——补一个空格 markdown 占位（与 loading hint 占位符同构，schema 合法）(`cardkit/special.py`) |
| ⚡ Perf (P1) | 每次 flush 对全量 answer_text 重跑 5 个正则转义（stream_element 是"set 全量"语义）——8KB 答案 30s 流式 ≈ 8MB 正则扫描/session | `escape_markdown_asterisks` 无缓存，文本越长单次 flush 越慢 | `UnifiedLinearState.escaped_answer_view()` 增量缓存：新增 delta 无 `*`/`` ` `` 且缓存尾部无跨界风险字符时 O(delta) 拼接（含尾随未转义 `*` 的 lookahead 跨界防护——回归测试发现并修正），否则全量重算（最坏=旧行为）；`on_completed` MISMATCH 直写 answer_text 后 `reset_escape_cache()`。4 处 flush 调用点切换 (`state/linear.py`, `controller/linear_mixin.py`) |
| ⚡ Perf (P1) | `reasoning_rounds`/`_panel_events`/`bg_review_messages` 存储无上限——马拉松会话（子代理链/长链推理/审查洪流）内存无限增长（显示侧 max=20 裁剪但存储侧裸奔） | 无 maxlen guard | `_enforce_storage_caps()`：reasoning_rounds 上限 50（seal summary 保留余量）/panel_events 上限 100（丢弃最旧 reasoning 轮时同步重索引）/bg_review_messages 上限 20（保留最新）(`state/linear.py`) |
| 🐛 Bug Fix (P2) | **新发现**：`_submitted_card_response` 调 `build_clarify_submitted_card(choices=...)` 传了不存在的参数——**每次 clarify 点击（select/input_submit/button_submit/retry）都 TypeError**，被上游 except 吞掉只留一条误导性 WARNING | builder 签名是 `(question, selected, clarify_id)`，调用方多了 `choices` kwarg | 删除非法参数。注：clarify 解析本身在异常前已调度成功，所以用户侧功能未断，但 submitted 态响应从未构建过 (`patching/adapter.py`) |
| 🐛 Bug Fix (P2) | clarify 已提交卡（state 2）用 `{"tag":"action","actions":[button]}` 包裹重试按钮——**Card 2.0 已废弃 action 容器**（真飞书 E2E T8b 铁证 230099/200861 "cards of schema V2 no longer support this capability; unsupported tag action"）；生产路径因 SDK stale bound method 丢弃 CallBackCard 而被掩盖，webhook 模式或未来 SDK 修复即触发 | builder 迁移到 Card 2.0 时 button 容器漏改 | button 改顶层元素（E2E A3 修复版真飞书验证通过 SUCCESS）(`cardkit/special.py`) |
| 🐛 Bug Fix (P2) | Phase 2 SCHEMA ERROR 只清 dirty 就返回——卡片永远转圈"正在加载上下文..."（若消息未完成），答案全部静默丢失 | add_elements 被永久拒绝后既不脱离 loading 态也不兜底答案 | ①schema error 时立即 `close_streaming`（幂等，close 后 batch_update 仍可用——E2E B3/B4 验证）让卡脱离加载动画；②`_do_linear_complete` seal 成功后检测"答案存在但 answer 元素从未创建"→ `_send_text_fallback` 兜底文本回复，用户仍能拿到答案 (`controller/linear_mixin.py`) |
| 🐛 Bug Fix (P2) | `_preservative_seal` 最外层 `except Exception: return False` 静默吞一切非 FeishuAPIError 异常——seal 失败无任何日志线索，session 被标 CREATION_FAILED 但没人知道为什么 | 兜底 except 无日志 | WARNING + exc_info 记录后返回 False (`controller/linear_mixin.py`) |
| 🐛 Bug Fix (P2) | 重激活 fire-and-forget 失败时 bare except 静默——stale session 永卡 COMPLETING（非终态→永不被清理→session 泄漏），旧卡新卡都没有 | `except Exception: pass` | WARNING 日志 + 强制 CREATION_FAILED 终态兜底防泄漏 (`controller/core.py`) |
| 🐛 Bug Fix (P2) | `reactivation_count` 提前递增——创建中途失败（id 冲突等）也烧掉唯一一次重激活机会，用户永远拿不到续写卡 | 递增在冲突检查/注册/调度之前 | 计数移到注册+调度全部成功之后；`_maybe_reactivate` 冲突后可重试 (`controller/core.py`) |
| 🐛 Bug Fix (P2) | clarify 授权检查 fail-open——旧版 hermes 无 `_is_interactive_operator_authorized` 方法时整个检查被跳过，**群聊里任何成员可代原作者点击 clarify 卡** | `hasattr` 为 False 直接 fall through | fail-closed：无该方法→拒绝点击+WARNING（hermes v0.20.5 已内置该方法 adapter.py:2771，仅真正旧版受影响）(`patching/adapter.py`) |
| 🐛 Bug Fix (P2) | confirm 阶段 `update_card` 失败后 finally 无条件清理——卡上还显示"已提交+重试按钮"，但重试所需数据已被清空，用户点击无任何反馈（哑火） | `_cleanup()` 在 finally | cleanup 移到 update_card 成功之后；失败时保留 `_clarify_selections` 让重试按钮继续可用，TTL 清理兜底 (`patching/adapter.py`) |
| 🐛 Bug Fix (P2) | select 回调 `int(selected_option)` 与卡片 `"value": str(i)` 跨文件强耦合无契约——未来 value 改成选项文本/哈希即静默吞掉用户点击 | 两文件间唯一契约点是魔法字符串格式 | 解析防御化：先按索引、失败后按选项文本精确匹配，均失败才 WARNING+空响应；卡片端注释指向契约 (`patching/adapter.py`, `cardkit/special.py`) |
| 🔧 Robust (P2) | `id(class)` 做 patch 去重——hermes 热重载模块后旧 class 被 GC，新 class 复用同 id → 去重集合误判"已 patch"，新类裸奔（clarify 退文本复发） | Python id() 是内存地址可复用 | 哨兵属性 `_hls_patched` 为主判据（GC 安全），id 集合降级为兼容镜像（测试/doctor 用）；relay patch 同模式 `_hls_relay_patched` (`patching/__init__.py`, `patching/adapter.py`) |
| 🔧 Robust (P2) | 并发后台任务/cron 共用同一 adapter 时先完成者把后者的 send 拦截器拆掉——后者 card_sent 抑制失效 | finally 无条件 `adapter.send = original_send` 覆盖他人 wrapper | 条件恢复：仅当自己安装的 wrapper 仍是当前属性时才恢复（bg task + cron 两处）(`patching/gateway.py`) |
| 🔧 Robust (P2) | `cardkit_stream_element` 网络错误（_retry_transient 3 次重试耗尽后）传播到 flush 控制器被 DEBUG 级吞掉——持续网络故障静默丢答案、零日志线索 | except 只捕 FeishuAPIError | 网络错误捕获后 WARNING+exc_info 再 raise（可见性）；`except (TimeoutError, Exception)` 元组清理为 `except Exception`+注释 (`feishu/client.py`, `controller/core.py`) |
| 🌐 i18n (P2) | 5 处中文硬编码：折叠提示（panel builder + seal trim 两套格式）、折叠计数解析只认"项"格式（panel builder 格式计数丢失）、截断后缀"已截断共N字"、中断面板正文"Interrupted by new message" 无中文 | v1.x 迁移遗漏 | 全部走 `_T` 字典双语化：`collapse_hint_count`/`collapse_rounds`/`collapse_tools`/`collapse_hint_full`/`truncated_suffix`/`interrupt_by_new_message`；折叠计数解析改为"求和所有数字组"同时支持两套格式（seal "N 项已折叠" 与 panel "X 轮早期推理、Y 步早期操作已折叠"）；`_is_collapse_hint_child()` 双语安全检测（长度+⚡前缀守卫防误报）(`cardkit/i18n.py`, `cardkit/elements.py`, `controller/linear_mixin.py`) |
| ⚡ Perf (P2) | drain 固定 8 轮每轮末尾无条件 20ms sleep——干净 drain 也白等 20ms×N | sleep 在轮末尾不检查是否还需下一轮 | 仅 dirty 仍存在时才 yield；trim 循环预计算 per-child tag 计数（原每次 pop 重算被删子树）；tooluse 3 个内联正则预编译（`_RE_HTML_TAG`/`_RE_PATH_REDACT`/`_RE_BACKTICK_RUN_TOOLUSE`）(`controller/linear_mixin.py`, `state/tooluse.py`) |
| ⚡ Perf (P2) | `record_end` fallback append 绕过 128 步上限——cap 后每个 end 事件都无上限追加，名义 cap 形同虚设 | fallback 分支无 cap 检查 | fallback 分支补 `>= _max_steps: return` (`state/tooluse.py`) |
| 🔧 Config (P2) | answer-only 快车道节流 150ms 硬编码不可调（与 flush_interval_ms 双轨） | `_ANSWER_FAST_STREAM_MS` 常量 | 新增 `answer_flush_interval_ms` 配置（默认 150，范围 70~2000）(`config/reader.py`, `controller/linear_mixin.py`) |
| 🧹 Dead Code | ①`on_cron_deliver` hook（hooks.py + plugin.yaml + 导出 + controller 的 `on_cron_deliver`/`on_cron_deliver_async`）——grep 全仓零生产调用方，hermes 亦无此 hook 名（VALID_HOOKS 不含），`_wrap_cron_deliver` 直接调 `ctrl._do_cron_deliver`；②`client.py` 的 `asyncio.to_thread` fallback + `_use_async_stream_element` 探测——**逐版本交叉验证**：lark-oapi 1.4.0 无任何 cardkit 异步方法（acreate/aupdate/abatch_update/asettings 全缺，插件根本无法运行），1.4.24+ 全套含 `acontent`，即"能跑本插件的 SDK 必有 acontent"，fallback 不可达 | 历史遗留 | 全链删除（含对应测试 TestOnCronDeliver）；`pyproject.toml` 修正 `lark-oapi>=1.4.24`（原 `>=1.4.0` 声明错误——1.4.0 装上即坏）(`patching/hooks.py`, `patching/__init__.py`, `plugin.yaml`, `controller/core.py`, `feishu/client.py`, `pyproject.toml`) |
| 🐛 Bug Fix (P3) | reaction→卡片状态指示功能双重失效：`_REACTION_STATUS_MAP` 键是 unicode emoji，hermes v0.20.5 实际发 Feishu 内部码 "Typing"/"CrossMark"（adapter.py:278-279）永不命中 | 上游 emoji 码制变化 | map 补 Feishu 内部码键（"Typing"→Processing / "CrossMark"→Error），unicode 键保留兼容旧版。已知限制：v0.20.5 reaction 打在用户消息上而非网关卡上，此功能仍需未来 hermes 对卡片消息发 reaction 才会激活 (`patching/adapter.py`) |
| 🧪 Test | 集成测试 OPTIONAL 目标硬编码 `gateway.platforms.feishu` 路径——v0.20.5 该路径已删，`_add_reaction`/`_remove_reaction` 4 项全部误报 skip（实际存在于 plugins.platforms.feishu.adapter） | 候选路径只用于 REQUIRED 目标 | OPTIONAL 测试同样走 `_FEISHU_ADAPTER_MODULE_CANDIDATES` 多路径回退：26 passed/4 skipped → 28 passed/2 skipped（剩余 2 个 skip 是旧 public 名 add/delete_reaction，v0.20.5 确实没有——正确行为）(`tests/integration/test_hermes_compat.py`) |
| 📝 Docs | ①`gateway_cards`/`enabled`/`linear`/`FEISHU_BASE_URL` 环境变量未文档化；②SKILL.md Hook 表 13 项 vs plugin.yaml 12 项且顺序不一致；③create_release.py 顶部 docstring 与函数实现矛盾；④notify_feishu.py 缺环境变量校验（裸 KeyError） | R5 一致性审计 | SKILL.md 配置节补全 4 项+新配置项；Hook 表 12 项与 plugin.yaml 同序；两脚本修复 (`docs/SKILL.md`, `docs/AGENT_GUIDE.md`, `scripts/create_release.py`, `scripts/notify_feishu.py`) |
| 🧪 Test | 新增 46 个 v1.7.0 回归测试 `test_v170_fixes.py` | 防回归 | 覆盖：RELAY 包装器 7 项（cron 重定向/非飞书透传/gateway 卡/未知平台/interim 透传/元数据平台/失败降级）、relay patch 幂等+hook 安装 4 项、终态元数据 3 项、abort 后吞迟到完成、重激活 epoch+计数+冲突重试 3 项、confirm 失败保留重试数据、授权 fail-closed 2 项、select 值解析 3 项、submitted 卡签名契约、escape 缓存正确性（含跨界 hazard 用例——**测试驱动发现并修正了缓存的首版缺陷**）、存储上限 4 项、工具步上限、哨兵去重、schema error 恢复+答案兜底、折叠提示双语+计数、节流配置 3 项、/stop 双语检测 2 项。全套件 888 通过（846 原有调整 + 46 新增 - 4 删除死代码测试） |

**审计方法**: 三方对齐求证（插件代码 + hermes v0.20.5 源码 + 真飞书 E2E）。①拉取 hermes-agent 最新源码（tag v2026.8.19，内部版本 v0.20.5）逐点核对 15 个 wrapper 签名/调用点/import 可达性：RELAY 架构（gateway/delivery.py DeliveryTransport + relay/adapter.py + register_relay_adapter 工厂注册）为代码级验证——cron 投递链路 `_deliver_result → DeliveryRouter._deliver_to_platform → transport.send → send_for_platform` 中 `route_metadata={"job_id":...}` 存活到 adapter 层，此为 relay 模式 cron 识别的依据；`_is_interactive_operator_authorized`（adapter.py:2771）、`/stop` 文案（locales/{zh,en}.yaml stop.*）、reaction 内部码（"Typing"/"CrossMark"）均直接读源码确认。②真飞书 E2E 铁证（上轮 13 张测试卡）：空 body 拒绝（T7 230099/200621）、action 容器废弃（T8b 200861）、button 顶层合法（A3）、close 后 batch_update 仍可用（B3/B4）——四项结论直接驱动本版修复。③死代码删除均经交叉验证：on_cron_deliver 链 grep 全仓（含 hermes 源码 VALID_HOOKS）零调用方；to_thread fallback 逐 lark-oapi 版本下载比对（1.4.0/1.4.24/1.5.x/1.6.x/1.7.x 的 cardkit 异步方法矩阵）。④修复后全套件 888 单元+集成测试通过（含 46 个新回归测试），集成测试 28 passed/2 skipped。

**已知限制**:
- RELAY 模式的修复覆盖 cron 卡片/gateway 卡片/turn-final 抑制//stop 检测四条出站链路；clarify 卡在 relay 模式下走 hermes 原生 relay prompt（连接器渲染按钮）或编号文本，插件的三态 clarify 卡仍是 native-only（需要网关进程持有飞书事件回调）
- RELAY 模式依赖 hermes v0.20.5 的 `_platform_by_chat`（从入站流量学习平台）或 `_relay_logical_platform` 元数据解析平台；从未有过入站消息的会话（纯推送场景）经 `send()` 无法识别平台会透传纯文本（`send_for_platform` 有显式平台参数不受影响）
- reaction→卡片状态指示在 v0.20.5 下仍不会激活（hermes reaction 打在用户消息上，不在网关卡上）——map 已补内部码，待未来 hermes 对卡片消息发 reaction 即自动生效
- escape 缓存的增量快路径要求新增 delta 无 `*`/`` ` `` 且缓存尾部无 `*`/`` ` ``/`\`——含这些字符的 delta 自动全量重算，最坏情况等于旧行为
- `_ANSWER_FAST_STREAM_MS` 常量已被 `_answer_fast_stream_sec(cfg)` 函数取代；answer_flush_interval_ms 文档已同步 SKILL.md
- 面板全量重建（R2-06）为飞书 API 限制（batch_update 无子元素级增量 API，E2E B3/B4 求证），维持现状：显示侧 max=20 裁剪 + v1.7.0 存储侧 50 轮上限控制重建成本


## v1.6.0 (2026-07-21)

Clarify 卡片失效复发修复 — hermes v0.19.0 升级后 clarify 退回纯文本（"❓ 问题 / 编号列表 / Reply with the number..."）。三路调研（插件代码 + hermes v0.19.0 源码 + 服务器日志）确认根因不在 hermes（v0.17.0→v0.19.0 clarify dispatch / deferred loading / 纯文本格式零改动），而在插件 v1.5.0 误删 v1.4.0 的 deferred loading 修复。

| 类型 | 问题/功能 | 原因 | 修复/说明 |
|------|-----------|------|-----------|
| 🐛 Bug Fix (P0) | hermes 升级到 v0.19.0 后，Clarify 交互卡片消失，显示纯文本编号列表（"❓ 测试 Clarify 交互卡片 / 卡片正常 👍 / 卡片异常 ❌ / Reply with the number, the option text, or your own answer."） | v1.5.0 commit cee17bc "架构收敛"误删 v1.4.0 的 `_schedule_direct_patch` + `_apply_feishu_adapter_deferred_repatch`（2s+10s 定时重打），替换为 `_wrap_feishu_adapter_send` 入口 on-demand repatch。该 on-demand repatch 有**两处致命缺陷**：①chicken-and-egg 死结——on-demand 检查代码本身是 wrapper，只装在已 patch 的 class 上，未 patch 的真身（deferred loading 后加载的 `hermes_plugins.feishu_platform.adapter.FeishuAdapter`）永远不触发它；②clarify 走 `send_clarify` 不走 `send`，即使 on-demand 在 send 上能跑也救不了 clarify。结果：gateway 用未 patch 的真身实例，`send_clarify` 走 `BasePlatformAdapter` 默认纯文本实现（`gateway/platforms/base.py:3208`），插件 `_intercepted_send_clarify` wrapper 从未被调用（服务器日志 0 hits `clarify card:` + 1 hit `Routing message to clarify text-intercept` 铁证）。hermes v0.19.0 不是根因——clarify dispatch 路径、deferred loading 机制、纯文本格式在 v0.17.0→v0.19.0 期间零改动（2976 commits） | **主链路修复**：hook `platform_registry.create_adapter`（`gateway/platform_registry.py:278`，public API，v0.17.0→v0.19.0 零 commits，4 条 adapter 创建路径 initial/reconnect/multiplex start/reconnect 全汇聚于此）。新增 `_wrap_platform_registry_create_adapter` + `_apply_create_adapter_hook`：每次 hermes 创建 feishu adapter 实例时，在返回给调用方之前对其 class 跑 `_apply_feishu_adapter_patches`（id 去重）。不依赖定时器、不依赖"先调用才触发"、覆盖 `send_clarify` 和所有其他方法。clarify 回调路径（`_handle_card_action_event` patch，v1.4.2 主链路）v1.5.0 本就正常，无需恢复 (`patching/__init__.py`) |
| 🧪 Test | 新增 7 个 `TestCreateAdapterHookV160` 单元测试 + 2 个 `TestClarifyDispatchPatch` E2E 测试 | v1.5.0 的 `TestOnDemandRepatchV150` 只测 send 触发 repatch，没测 send_clarify 触发，且没测 create_adapter hook，所以 CI 没防住这次回归 | 单元测试覆盖：hook 对未 patch feishu class 触发 patch、对已 patch class O(1) skip、非 feishu 平台透传、orig 返回 None 透传、class module path 兜底检测、hook 安装幂等、import 失败 deferred。E2E 测试覆盖：`_apply_feishu_adapter_patches` 确实把 `send_clarify` wrapper 装上 class（`__name__ == "_intercepted_send_clarify"`）、create_adapter hook 对 deferred-loaded 真身 class patch 后实例的 `send_clarify` 确实是 wrapper（非纯文本 fallback）(`tests/test_monkey_patch.py`, `tests/e2e/test_e2e_clarify.py`) |
| 🧪 Test | E2E 真飞书验证（7 passed） | 用户要求 E2E 真飞书测试 | 用插件实际 `FeishuClient` 向测试群发真实 clarify 卡片（dict_repr/normal/special_chars/submitted/confirmed 五种 + 2 dispatch path），全部通过。非自造卡片，验证插件真实发卡链路 (`tests/e2e/test_e2e_clarify.py`) |

**审计方法**: 三路调研对齐求证。2-a 拉取 DEV 最新代码（HEAD=v1.5.0，用户说的 v1.6.0 远端不存在），git diff v1.4.0→v1.5.0 定位 cee17bc 误删 deferred repatch。2-b 浅克隆 hermes-agent v0.19.0（HEAD=3ef6bbd20，2976 commits），确认 clarify dispatch 路径（`clarify_tool` → `_clarify_callback_sync` → `_status_adapter.send_clarify` → `BasePlatformAdapter.send_clarify` 纯文本）、deferred loading 机制（`platform_registry.py` 零 commits）、纯文本格式（`base.py:3208-3263` 与用户报告逐字吻合）三方面 v0.17.0→v0.19.0 零改动。2-c SSH 服务器抓 agent.log（10:15:18 clarify 测试窗口）：`FeishuAdapter resolved via plugins.platforms.feishu.adapter`（替身）+ `send_clarify patched ✓`（假阳性）+ 0 hits `clarify card:` + 1 hit `Routing message to clarify text-intercept`（走 hermes 原生纯文本）+ 0 hits `re-patched on deferred`（v1.5.0 删了定时重打），铁证 clarify 完全没走插件 patch。2-b 子任务精确定位 hook 点 `platform_registry.create_adapter`（public API，4 条创建路径汇聚，v0.17.0→v0.19.0 零 commits）。修复基于代码+源码+日志三方对齐，未自行构造测试卡片。849 单元测试 + 26 集成测试 + 22 e2e(mock) + 7 e2e(真飞书) 全部通过。

**已知限制**:
- 服务器实际跑的是 v1.5.0（用户说的 v1.6.0 之前不存在），本次作为 v1.6.0 发布
- v1.5.0 的 `_wrap_feishu_adapter_send` 入口 on-demand repatch 保留（对 class A 替身实例仍有效，无害，作为 defense-in-depth）
- `resolve_feishu_adapter_class_fresh()` 方法保留但生产无调用方（v1.4.0 遗留，未删以防未来需要）
- hook 点 `platform_registry.create_adapter` 是 hermes public API，但若 hermes 未来重构 platform 注册机制（如改名/移到其他模块），hook 会失效——集成测试 `test_hermes_compat.py` 已校验该方法存在（REQUIRED），CI 会第一时间报警

---


## v1.5.0 (2026-07-08)

架构收敛版 — 删除 IM 降级路径 + 全量重建路径 + header 颜色切换 + v1.4.1 懒重打 + deferred loading 3 套重打 + `_on_card_action_trigger` patch（SDK bound method 失效）。生产代码从 14,780 行降至 ~10,300 行（-30%）。主链路唯一化：CardKit create → Phase 2 → Phase 3 → preservative seal，无降级分支。兼容 hermes v0.18.1（集成测试 26 passed）。

| 类型 | 问题/功能 | 原因 | 修复/说明 |
|------|-----------|------|-----------|
| 🔧 Refactor (P0) | 删除 IM 降级路径 (`_do_im_fallback_flush`/`_do_im_fallback_seal`/`build_im_fallback_card`/`PATCH_MS`/`use_cardkit` 字段) | 生产日志铁证：`cardkit_create` 从未失败（4 个日志文件 0 次触发），IM 降级是死代码。维护两套 flush + 两套 seal 增加复杂度且从不执行 | 删除整个 IM 降级路径。CardKit create 失败直接 `raise` → 外层 `except Exception` 标记 CREATION_FAILED。主链路唯一化 (`controller/linear_mixin.py`, `cardkit/cards.py`, `state/session.py`, `flush/`) |
| 🔧 Refactor (P0) | 删除全量重建路径 (`build_unified_complete_card`/`_phase2_failed`/`_phase2_never_succeeded`/`_header_driven_rebuild`/`record_full_rebuild`) | 全量重建 22 次触发全是 header 颜色切换（非失败兜底）。放弃 header 颜色切换后，全量重建无存在必要。`_phase2_failed` 标志为全量重建服务，删除后 Phase 2 schema_error/element_not_found 保留 dirty-flag 清空（防 futile retry），不再设标志 | 删除全量重建 fallback 块。`_do_linear_complete` 从 460 行简化到 ~220 行：drain loop → `_preservative_seal` → COMPLETED/CREATION_FAILED。封卡失败直接 CREATION_FAILED，不再原地重建 (`controller/linear_mixin.py`, `cardkit/cards.py`, `aowen/__init__.py`) |
| 🔧 Refactor (P1) | 删除 header 颜色切换功能 (`_build_header`/`_HEADER_STATES`/`header_enabled` 配置) | header 颜色切换依赖全量重建（CardKit batch_update 不支持更新 card-level header）。删除全量重建后 header 颜色无法切换。用户感知小（streaming 蓝色保持），换取主链路简化 | 删除 `_build_header`、`_HEADER_STATES`、`header_enabled` 配置属性、`build_streaming_card_v2`/`build_gateway_card` 的 header_enabled 参数。卡片无 header，内容区域更大 (`cardkit/elements.py`, `config/reader.py`, `cardkit/cards.py`, `cardkit/special.py`) |
| 🔧 Refactor (P1) | 删除 v1.4.1 懒重打 (`lazy_repatch_feishu_adapter` + 60s 节流状态) | v1.4.2 已在 `_handle_card_action_event` 主链路点修复 /card 问题，懒重打治不了 SDK bound method 根因，是 v1.4.1 错误兜底 | 删除 `lazy_repatch_feishu_adapter` + `_lazy_repatch_*` 状态 + `handle_pre_gateway_dispatch` 中的调用 (`patching/__init__.py`, `aowen/__init__.py`) |
| 🔧 Refactor (P1) | 删除 deferred loading 3 套重打 → 1 套按需 repatch | v1.4.0 的 2s+8s 定时重打 + v1.4.1 的 60s 懒重打共 3 套，治标不治本。v1.4.2 的 `_handle_card_action_event` patch + 按需 repatch（`_wrap_feishu_adapter_send` 入口检查 `id(type(self))`）覆盖所有场景 | 删除 `_schedule_direct_patch` + `_apply_feishu_adapter_deferred_repatch`。`_wrap_feishu_adapter_send` 入口新增 6 行按需 repatch 检查：`if id(type(self)) not in _patched_feishu_classes: _apply_feishu_adapter_patches(type(self), is_repatch=True)`。O(1) set lookup，每条消息触发，覆盖任何 deferred loading 时窗口 (`patching/__init__.py`, `patching/adapter.py`) |
| 🔧 Refactor (P1) | 删除 `_on_card_action_trigger` patch (`_wrap_feishu_card_action_trigger`/`_empty_card_action_response`) | SDK WebSocket 模式下 `register_p2_card_action_trigger` 保存 bound method 引用，类属性替换不影响已保存引用 → patch 从未执行（生产日志铁证：patched ✓ 9 次，wrapper 0 次）。`_handle_card_action_event` patch（v1.4.2）通过 `self.` 动态查找可靠覆盖 | 删除 `_wrap_feishu_card_action_trigger` + `_empty_card_action_response` + `_apply_feishu_adapter_patches` 中的注册块。保留 `_wrap_handle_card_action_event`（v1.4.2 主链路修复）(`patching/adapter.py`, `patching/__init__.py`) |
| 🔧 Refactor (P2) | 统一错误处理 + 精简注释 | 7 处散落 `except FeishuAPIError` 各自判 streaming_closed/schema_error/element_not_found。注释占比 23%，含大量"what"叙述和版本历史 | 新增 `_handle_cardkit_error(session, e, phase) -> str` 统一分类（streaming_closed/schema_error/element_not_found/other），7 处调用简化。注释精简到 ~8%，保留"why"和坑警告，删"what"叙述。生产代码 14,780 → ~10,300 行 (-30%) (`controller/linear_mixin.py`, 所有文件) |
| ✅ Compat | hermes v0.18.1 兼容性验证 | hermes 2026.07.07 更新到 v0.18.1 | SSH 服务器读 hermes v0.18.1 源码：所有 patch 目标方法存在（`_on_card_action_trigger`/`_handle_card_action_event`/`send_clarify`/`_build_event_handler`/GatewayRunner 方法/AIAgent.run_conversation` with `persist_user_timestamp`/`cron._deliver_result`）。`_on_card_action_trigger` 仍调用 `self._handle_card_action_event(data)`（动态查找）→ v1.4.2 patch 有效。`/card` 仍不在 COMMAND_REGISTRY → `_handle_card_action_event` patch 仍需。集成测试 26 passed, 4 skipped |

**审计方法**: 架构收敛经生产日志 + 代码 + hermes 源码三方对齐。①SSH 服务器取 agent.log（4 个 rotated 文件）grep：IM 降级 0 次触发（死代码）、全量重建 22 次全是 header 颜色切换（非失败兜底）、`_on_card_action_trigger patched ✓` 9 次但 wrapper 0 次（SDK bound method 失效铁证）。②读 hermes v0.18.1 源码确认所有 patch 目标方法存在且 `_on_card_action_trigger` 仍通过 `self.` 动态查找调用 `_handle_card_action_event`。③集成测试（HERMES_SRC_DIR=/usr/local/lib/hermes-agent）26 passed, 4 skipped。858 单元测试通过，0 回归。生产代码 14,780 → ~10,300 行 (-30%)，主链路唯一化（CardKit create → Phase 2 → Phase 3 → preservative seal，无降级分支）。

**已知限制**:
- 放弃 header 颜色切换：卡片 streaming 时蓝色保持，不再切到 completed(绿)/error(红)/stopped(红)。用户感知小，换取主链路简化
- CardKit create 失败直接 CREATION_FAILED（不再降级 IM 卡片）。生产从未触发，但极端飞书服务端故障时用户看不到卡片。可接受（`update_card` 大概率也失败）
- 全量重建删除后，preservative seal 失败直接 CREATION_FAILED。生产 22 次全量重建全是 header 切换（非 seal 失败），删除后无影响
- 总代码 ~10,300 行（目标 ≤10,000，略超 300 行，主要是 aowen 卡片构建器和 CLI 无法再精简而不丢功能）

---

## v1.4.2 (2026-07-07)

紧急修复 v1.4.1 未解决的 `/card` 问题 — 用户升级 v1.4.1 后卡片交互仍返回 "Unknown command /card"。经原始日志（agent.log 3.6MB + gateway.log 140KB）+ hermes core 源码 + lark_oapi SDK 源码三方对齐，定位 v1.4.1 patch 错了拦截层。

| 类型 | 问题/功能 | 原因 | 修复/说明 |
|------|-----------|------|-----------|
| 🐛 Bug Fix (P0) | v1.4.1 升级后卡片交互（select_static/input）仍返回 "Unknown command /card"，wrapper 从未执行（生产日志铁证：`_on_card_action_trigger patched ✓` 9 次，`"no known marker"` 0 次，原生 `Routing card action` 11 次） | **v1.4.1 patch 错了拦截层。** 飞书 SDK 在 `register_p2_card_action_trigger(self._on_card_action_trigger)`（`adapter.py:1639`）时保存 **bound method 引用**——`P2CardActionTriggerProcessor.__init__` 执行 `self.f = f`（`processor.py:14`），`do()` 调用 `self.f(data)`（`processor.py:19`）即存储的引用，**非动态查找**。之后替换 `FeishuAdapter._on_card_action_trigger` 类属性**不影响** SDK 已持有的 bound method（Python 机制：bound method 的 `__func__` 在创建时绑定，类属性替换不改变已创建 bound method 的 `__func__`）。v1.4.1 的 `_wrap_feishu_card_action_trigger` + 懒重打都打在类属性上 → SDK 永远调用注册时的旧 bound method → wrapper 从未执行 → clarify action 落入 hermes core 原生 `_handle_card_action_event` → 生成 `/card {tag} {json}` 合成命令 → Gateway 不认识 → "Unknown command /card" | **patch `_handle_card_action_event` 而非 `_on_card_action_trigger`**（`patching/adapter.py`）。hermes core `_on_card_action_trigger` 方法体调用 `_handle_card_action_event` 时用 `self._handle_card_action_event(data)`（`adapter.py:2615`，**动态查找**）。即使 SDK 持有 `_on_card_action_trigger` 的 stale bound method，该方法体仍通过 `self.` 动态查找当前类属性上的 `_handle_card_action_event`。新增 `_wrap_handle_card_action_event`：①`hermes_clarify_action` → 复用 `_handle_clarify_card_action`（授权检查 + resolve_gateway_clarify + schedule_confirm_card），丢弃返回的 CallBackCard（async 路径无 sync 响应），suppress /card；②其他无 marker action → suppress /card。注册到 `_apply_feishu_adapter_patches`，与 `_on_card_action_trigger` patch 并存（后者对 webhook 模式 / SDK 重新注册仍有效，提供 instant CallBackCard，两者互补）(`patching/adapter.py`, `patching/__init__.py`) |
| 🧪 Test | 新增 7 个回归测试，含 2 个端到端 stale bound method 模拟 | v1.4.1 的 wrapper 从未执行是生产环境才暴露的 SDK 机制问题，单元测试需模拟该场景 | `TestWrapHandleCardActionEventV142`（5 个：wrapper 可调用、unknown action 抑制、empty action_value 抑制、clarify 处理+原生不调用、clarify 异常不放行）；`TestStaleBoundMethodSimulation`（2 个关键端到端模拟：①stale `_on_card_action_trigger` bound method + patched `_handle_card_action_event` + clarify action → clarify 被正确解析；②stale bound method + unknown action → 原生 `_handle_card_action_event` 不被调用 = /card 被抑制）(`tests/test_clarify_card.py`) |

**审计方法**: 三方对齐求证，读原始日志未盲信分析文档。①用户上传原始 `agent.log`（3.6MB）+ `gateway.log`（140KB），grep 确认分析文档的日志计数：`_on_card_action_trigger patched ✓` 9 次、`"no known marker"` 0 次、`"clarify card: callback"` 0 次、`"lazy repatch"` 0 次、`"Routing card action"` 11 次、`"Unknown command /card"` 11 次——铁证 v1.4.1 wrapper 从未执行。日志含 v1.4.0（314 次）和 v1.4.1（45 次）版本字符串，确认用户已升级 v1.4.1 但问题依旧。②SSH 读 hermes core 源码（`/usr/local/lib/hermes-agent/plugins/platforms/feishu/adapter.py`）：`_on_card_action_trigger`（:2582）方法体 `self._submit_on_loop(loop, self._handle_card_action_event(data))`（:2615）——`self.` 动态查找。③SSH 读 lark_oapi SDK 源码：`processor.py` `P2CardActionTriggerProcessor.__init__` `self.f = f` + `do()` `return self.f(data)`——存储 bound method 引用非动态查找；`dispatcher_handler.py:218` `P2CardActionTriggerProcessor(f)` 注册时求值。根因闭环：SDK 注册时保存 `_on_card_action_trigger` bound method（`__func__` = 注册时的原函数），插件 2s 后 patch 类属性不影响已保存引用 → wrapper 从未执行。修复：patch `_handle_card_action_event`（`self.` 动态查找可达）。913 单元测试通过（906 + 7 新增），0 回归。

**已知限制**:
- clarify 卡片在 SDK stale bound method 路径下失去 instant CallBackCard（"已提交"态）sync 响应——用户看到卡片从"待选择"~1s 后跳到"已确认"（由 `_schedule_confirm_card` server-side `update_card` 完成）。`_on_card_action_trigger` wrapper 生效时（webhook 模式）仍提供 instant CallBackCard
- `_on_card_action_trigger` patch（v1.4.1）保留——对 webhook 模式（`adapter.py:3495` `self._on_card_action_trigger(data)` 动态查找）和未来 SDK 重新注册场景仍有效，与 `_handle_card_action_event` patch 互补
- 懒重打补丁（v1.4.1 `lazy_repatch_feishu_adapter`）保留——对 deferred loading 边缘场景（真身 class 未 patch）仍有效，但**不能**修复 SDK bound method 引用问题（bound method 在注册时创建，类属性替换不影响）
- E2E 真飞书实机验证待用户配合（服务器 hermes v0.18.0 不复现 /card 问题，需 v0.17.0 环境实机盖章）。修复基于 SDK 源码 + hermes core 源码 + 生产日志三方对齐 + stale bound method 端到端模拟测试

---

## v1.4.1 (2026-07-07)

两个用户反馈 issue 修复 — 卡片交互返回 "Unknown command /card" + Phase 3 batch_update 300315 死循环。根因求证经 SSH 服务器日志 + hermes core 源码 + 插件代码三方对齐，未盲信 bug 报告根因分析。

| 类型 | 问题/功能 | 原因 | 修复/说明 |
|------|-----------|------|-----------|
| 🐛 Bug Fix (P0) | 飞书 DM 中点击统一面板/clarify 卡片交互元素（select_static 下拉框、input 输入框）后返回 **"Unknown command /card"**，卡片交互完全无反应（用户反馈 8/8 次复现） | **插件没有 `/card` 命令**——这是 hermes core 行为。hermes core 原生 `FeishuAdapter._on_card_action_trigger`（`plugins/platforms/feishu/adapter.py:2582`）对无 `hermes_action`（审批）/ `hermes_update_prompt_action`（改提示词）的卡片 action 一律路由到 `_handle_card_action_event`（`:2913`），后者生成合成 COMMAND `f"/card {action_tag}"`（`:2933`）并投递给 Gateway；Gateway 不认识 `/card`（不在 `COMMAND_REGISTRY`）→ "Unknown command /card" + unknown-command 提示卡。本插件 clarify 卡片只携带 `hermes_clarify_action`（`cardkit/special.py:307`），若 `_on_card_action_trigger` 补丁未打到 gateway 运行时真身类（deferred loading 边缘场景，v1.4.0 的 2s/10s 固定调度未覆盖其环境的真身加载时机），clarify action 落入原生路径 → `/card` 被拒。bug 报告 "Solution A: 注册 /card 为已知命令" 是错误方向 | ①**防御性拦截** (`patching/adapter.py`)：`_wrap_feishu_card_action_trigger` 不再无条件放行非 clarify action 给原生。改为检查三种已知 marker——`hermes_clarify_action`（插件 clarify）→ 插件处理；`hermes_action` / `hermes_update_prompt_action`（hermes core 审批/改提示词）→ 放行原生；**无任何 marker 的 action → 返回空 `P2CardActionTriggerResponse`**（抑制 `/card` 合成命令生成，WARNING 日志记录 action_value）。插件卡片（clarify）已自带 marker，streaming 卡片无交互元素（已 grep 求证），无 marker 的 action 多为残留/竞态/旧卡，放行只会产生噪音错误。②**懒重打补丁** (`patching/__init__.py`)：新增 `lazy_repatch_feishu_adapter(force=False)`，60s 节流，从 `handle_pre_gateway_dispatch`（每条消息触发）调用，覆盖 2s/10s 固定调度漏掉的延迟加载窗口——第一条消息到来时若真身已加载但未 patch，立即补打。`force=True` 供 `/aowen doctor` 跳过节流 |
| 🐛 Bug Fix (P1) | 飞书消息反复显示「正在加载上下文...」无法正常完成，日志每轮刷 `unified flush batch_update failed: code=300315, msg=ErrMsg: not find elementID : context_loading_hint;`（用户「诺玛🌙」反馈） | `controller/linear_mixin.py` Phase 3 batch_update 的 except 块有 `is_schema_error` 分支但**缺 `is_element_not_found_error` 分支**（Phase 2 有，line 645）。最常见场景：Phase 2 在飞书侧成功删除了 `context_loading_hint` 元素（例如 Phase 2 await 抛网络异常但飞书已处理 batch），本地 `_creation_stages` / `existing_elements` 未同步 → Phase 3 safety net（line 793-798）检测到 loading_hint 还在 `existing_elements` 中 → 添加 `delete_elements` 操作 → 飞书 API 返回 300315（元素已被删除）→ 错误落入通用 `warning` 分支（line 851）→ `hint_removed` 仍未同步 + `existing_elements` 未 discard → 下轮 safety net 再次添加 delete → **死循环** → batch 原子失败导致 partial_update（面板内容）丢失 → 卡片卡在「正在加载上下文...」 | Phase 3 except 块 `is_schema_error` 之后、通用 warning 之前，**新增 `is_element_not_found_error` 分支**（与 Phase 2 对齐）：300315 = 元素不存在 = 非致命。同步本地标志位（`_creation_stages.add("hint_removed")` + `existing_elements.discard(_LOADING_HINT_ELEMENT_ID)`）→ 下次 flush safety net 不再触发 delete，死循环打破。**保留** `panel_dirty` / `tool_steps_dirty` → batch 中其他操作（partial_update）下轮重试。`info` 级别日志（不是 warning/error）— 不是真正的故障 (`controller/linear_mixin.py`) |
| 🧪 Test | 新增 11 个回归测试覆盖 v1.4.1 三个修复点 | 防御性拦截 / 懒重打 / Phase 3 element_not_found 均为用户反馈的生产问题，需回归保障 | `TestWrapFeishuCardActionTriggerV141`（5 个：unknown action 抑制、hermes_action 放行、hermes_update_prompt_action 放行、空 action_value 抑制、clarify 仍拦截）；`TestPhase3ElementNotFoundV141`（2 个：300315 同步 hint + 保留 dirty、二次 flush 不再添加 delete 验证死循环打破）；`TestLazyRepatchV141`（4 个：函数存在、60s 节流、force 跳过节流、None class 不崩溃）(`tests/test_clarify_card.py`, `tests/test_controller.py`, `tests/test_monkey_patch.py`) |

**审计方法**: 三方对齐求证，未盲信 bug 报告根因分析。①克隆 Gitee DEV 分支（HEAD=v1.4.0）分析插件代码，确认插件无 `/card` 命令（grep 全仓 "Routing card action|synthetic command|/card " 无匹配），clarify 卡片 select_static/input 全部携带 `hermes_clarify_action`，streaming 卡片无交互元素。②SSH 服务器（47.239.199.25, hermes v0.18.0 + 插件 v1.4.0）取证：服务器 clarify 拦截正常工作（`callback received action=select` → `confirmed (hard lock)`），无 `/card` 错误，无 300315 错误；启动日志铁证 deferred repatch 工作正常——替身 class_id=94212263706992，真身 class_id=94212274640752（不同对象），两者都 patched ✓。bug 报告者环境（hermes v0.17.0）2s/10s repatch 可能未覆盖其真身加载时机。③读 hermes core 源码（`/usr/local/lib/hermes-agent/plugins/platforms/feishu/adapter.py`）：`_on_card_action_trigger`（:2582）检查 `hermes_action`/`hermes_update_prompt_action`，其余路由 `_handle_card_action_event`（:2913）生成 `f"/card {action_tag}"` 合成 COMMAND（:2933），Gateway 不认识 → "Unknown command /card"。Phase 3 300315 根因经代码行级确认（Phase 2 line 645 有 is_element_not_found_error，Phase 3 line 840-851 缺）。906 单元测试通过（895 原有 + 11 新增回归），0 回归。

**已知限制**:
- Issue 1（/card）的懒重打补丁仅在 `pre_gateway_dispatch` 触发时生效。若用户首条交互是点击卡片（而非发消息），懒重打不会运行；但 v1.4.0 的 2s/10s 固定调度仍作为兜底。服务器 v0.18.0 实测 deferred repatch 正常，bug 报告者 v0.17.0 环境建议升级到 v0.18.0+
- 防御性拦截对无 marker 的 action 返回空响应（不更新卡片）。若未来 hermes core 新增卡片 action 类型（新 marker），需同步更新插件白名单。当前白名单：`hermes_clarify_action`（插件）/ `hermes_action`（审批）/ `hermes_update_prompt_action`（改提示词）
- Phase 3 `is_element_not_found_error` 修复假设 batch_update 中 partial_update 与 delete_elements 的失败是独立的（partial_update 可能在 300315 前已生效）。保留 `panel_dirty`/`tool_steps_dirty` 供下轮重试，partial_update 是幂等的（全量替换 header+elements），重试安全
- E2E 真飞书实机验证未执行（服务器 hermes v0.18.0 不复现两个 issue）。修复基于代码 + hermes core 源码 + 服务器日志三方对齐，建议 bug 报告者升级后实机盖章

---

## v1.4.0 (2026-06-30)

Hermes v0.17.0 兼容性修复 — Clarify 卡片消失 + delegate_task 卡片降级两个生产问题，根因同为 hermes bundled platform deferred loading 导致 FeishuAdapter 补丁打在替身类。

| 类型 | 问题/功能 | 原因 | 修复/说明 |
|------|-----------|------|-----------|
| 🐛 Bug Fix (P0) | hermes update 升级到 v0.17.0 后，Clarify 交互卡片消失，变成纯文本编号列表 | hermes v0.17.0 引入 bundled platform **deferred loading**（延迟加载）。插件 `apply_patches()` 在启动早期运行，此时 `hermes_plugins.feishu_platform.adapter`（真身，gateway 运行时用）还没被加载，`HermesCompat` fallback 到 `plugins.platforms.feishu.adapter`（替身，源码路径）。插件把 `send_clarify` / `_on_card_action_trigger` 补丁打在替身类上。2 秒后 `_schedule_direct_patch` 触发，真身类已被 deferred loader 加载，但该函数**只重新 patch AIAgent，没有重新 patch FeishuAdapter**。gateway 用的真身类没补丁 → clarify 退回 `BasePlatformAdapter.send_clarify` 纯文本路径。日志铁证：`10:04:17 FeishuAdapter resolved via plugins.platforms.feishu.adapter`（替身）→ `10:04:19 FeishuAdapter resolved via hermes_plugins.feishu_platform.adapter`（真身，2s 后，但只 re-patch AIAgent） | 抽取 `_apply_feishu_adapter_patches(FeishuAdapter, *, is_repatch=False)` 函数；`_schedule_direct_patch` 在 2s 延迟后除 re-patch AIAgent 外，**新增 FeishuAdapter primary repatch**（resolve 真身 → 若 `id(cls)` 不在 `_patched_feishu_classes` 则 repatch + WARNING 日志）；再 sleep 8s（启动后 10s）做 secondary 兜底，防慢加载环境。`_patched_feishu_classes: set[int]` 用 `id(cls)` 去重避免重复打补丁 (`patching/__init__.py`) |
| 🐛 Bug Fix (P0) | `delegate_task` 子代理返回后，主代理续写输出从卡片降级为纯文本（单线程正常，仅并发场景触发） | 双重根因：①hermes 在调用任何工具前发 `stream_delta_callback(None)` 流式边界信号。普通工具执行快（毫秒级），卡片流式会话未进入终态。但 `delegate_task` 派子代理跑很久（几秒~几十秒），期间卡片流式被服务端关闭（`_streaming_closed=True`）。子代理返回、主代理续写 answer token 时 `cardkit_stream_element` 失败（300309）→ fallback → 退回 `adapter.send`。②`adapter.send` 因同一 deferred loading 根因补丁打错类 → 纯文本 fallback | ①新增 `_reactivate_session_for_continuation(stale_session)`：用旧 session 的 chat_id/anchor_id 创建新 CardSession（`new_message_id = {anchor_id}-cont-{seq}`），设 `_is_continuation=True`，预置 `unified_state` 防 on_answer 早到丢 token，fire-and-forget `_do_create_linear_card` 建新卡；旧 session 转 COMPLETING 异步封卡保留 partial 内容。②`on_answer` 入口检测终态+`_streaming_closed=True` 时触发重激活，`_continuation_map[old_msg]=new_msg` 透明重定向后续 token，`on_completed` pop 映射让最终封卡落新卡。③`_continuation_reactivation_count` 限制最多 1 次，防递归。④`_apply_feishu_adapter_patches` 的 deferred repatch（见 P0 上条）同时治好 `adapter.send` 补丁错类根因 (`controller/core.py`, `state/session.py`, `controller/linear_mixin.py`) |
| 🐛 Bug Fix (P1) | `send_clarify` / `_on_card_action_trigger` 在兼容性测试中标记为 OPTIONAL，hermes 升级移除/改名时 CI 仍绿，问题只在生产暴露 | `OPTIONAL_CLASS_METHOD_TARGETS` 缺失时只 `pytest.skip` 不 fail。开发者已知道 hermes 不同版本接口可能不同，但 clarify 是用户可见功能，不应该 optional | 两个目标从 `OPTIONAL_CLASS_METHOD_TARGETS` 移到 `CLASS_METHOD_TARGETS`（REQUIRED）。AST fallback 增强：多路径回退（`gateway.platforms.feishu` → `plugins.platforms.feishu.adapter`）+ 继承链回退（`BasePlatformAdapter` @ `gateway.platforms.base`，覆盖 `send_clarify` 这种 FeishuAdapter 继承自基类的方法）(`tests/integration/test_hermes_compat.py`) |
| ✨ Feature | FeishuAdapter 补丁身份运行时校验——`/aowen doctor` 可主动检测补丁是否打在 gateway 实际使用的类上 | deferred loading 根因隐蔽，启动日志显示 `patched ✓` 但实际打错类，用户无感知 | 新增 `_verify_feishu_patch_identity(adapter_instance) -> bool`：检查 `id(type(adapter_instance))` 是否在 `_patched_feishu_classes` 里，mismatch 时 ERROR 日志提示 `/aowen doctor`。本次仅实现+导出，未集成到每条消息调用链（避免性能开销），供 doctor 命令或手动诊断调用 (`patching/__init__.py`) |
| 🔧 Improvement | `HermesCompat` FeishuAdapter 解析增加 deferred loading 感知日志 | 启动早期真身未加载时静默 fallback 到替身，无日志线索 | 真身路径 import 失败时新增 DEBUG 日志说明 fallback + 提示将在 deferred 阶段重打。新增 `resolve_feishu_adapter_class_fresh()` 方法供延迟重打阶段重新 resolve（不复用缓存）(`patching/hermes_adapter.py`) |

**审计方法**: 四路调研对齐求证。2-a 克隆 DEV 分支（HEAD=v1.3.7）分析插件代码，定位 clarify 走 `FeishuAdapter.send_clarify` patch、panel 走 `partial_update_element` 全量替换。2-b 浅克隆 hermes-agent v0.17.0 源码，锁定 deferred loading 机制（`hermes_cli/plugins.py:1329` deferred loader + `gateway/platform_registry.py:173` 注释）导致 FeishuAdapter 真身/替身类对象分离，以及 `stream_delta_callback(None)` 在工具前触发的 delegate_task 续写问题。2-c SSH 取服务器 agent.log（4 个 rotated 文件 + gateway.log），对比升级前后日志：升级前有 `clarify card: send_clarify intercepted` + `card sent successfully`，升级后 0 行 clarify card 日志 + 出现 `gateway.platforms.base: Routing message to clarify text-intercept`，铁证 clarify 走文本路径。3 E2E 真飞书复现因提供的凭证（`cli_a951f158a1b89bd7`）与 agent 实际 app（`cli_aa8f3742bba55cba`）不匹配无法触发，但前三路证据链已闭合。所有修复基于代码+源码+日志三方对齐，未自行构造测试卡片。871 单元测试 + 26 集成测试通过（4 skipped 为 OPTIONAL reaction 方法）。

**已知限制**:
- E2E 真飞书实机验证因凭证不匹配未能执行 delegate_task 降级修复的端到端确认。源码层面根因链路（`stream_delta_callback(None)` → 长任务 → `_streaming_closed=True` → fallback）已成立，建议用户配合蒲正宇真人在 agent 监听的 chat 发触发消息做最终实机盖章
- `_verify_feishu_patch_identity` 仅实现+导出，未自动集成到每条消息调用链（避免性能开销）。可通过 `/aowen doctor` 手动调用，后续可考虑在 gateway adapter 创建时 hook 一次
- 会话续写重激活最多 1 次（`_continuation_reactivation_count` 限制），极端场景（连续多次长任务）下仍可能走文本 fallback 作为兜底
- 折叠面板内推理流式（CardKit API 限制：`collapsible_panel` 不支持 `stream_element`）本次未修，保留 v1.3.7 行为，待后续版本评估"推理临时外置打字机"方案

---

## v1.3.7 (2026-06-25)

E2E 测试优化 — 真飞书模式群聊消息数减少 ~85%。

| 类型 | 问题/功能 | 原因 | 修复/说明 |
|------|-----------|------|-----------|
| 🔧 Improvement | E2E 真飞书模式群聊测试消息过多（每次跑完群聊收到 ~21 条 anchor 文本 + ~21 张卡片） | `runner` fixture 是函数级，每个测试方法都重新 setup 发一条 anchor 消息。3 个测试文件共 21 个测试方法 = 21 条 anchor 消息 | `runner` fixture 改为 module 级（`_module_runner`），同一测试文件的所有方法共享 1 个 runner（1 条 anchor）。函数级 `runner` fixture 从 `_module_runner` 拿实例，每个测试方法前清理 controller sessions + 重置配置保证隔离 (`tests/e2e/conftest.py`) |
| 🐛 Bug Fix | module 级 runner 共享导致 `test_header_disabled_no_header` 失败（前一个测试 `enable_header()` 的配置残留） | `Config` 是单例，module 级 runner 的 `header_enabled=True` 残留到下一个测试 | 函数级 `runner` fixture 每次重置 `header_enabled=False` 到默认值 |
| 📊 统计 | 群聊 anchor 消息：21 条 → 3 条（减 85%）；群聊总消息（anchor+卡片）：~42 条 → ~24 条 | test_e2e_full.py 12→1, test_e2e_clarify.py 5→1, test_e2e_header.py 4→1 | 私聊测试不受影响（本来就是 3 条） |

**审计方法**: 统计 E2E 测试在真飞书模式下的群聊消息数，定位到 `runner` fixture 函数级 setup 是消息重复的根因。改为 module 级后用 mock 模式（24 passed）+ 真飞书模式（header + session 7 passed）验证无回归。配置残留问题由 mock 模式 `test_header_disabled_no_header` 失败发现并修复。

---

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
