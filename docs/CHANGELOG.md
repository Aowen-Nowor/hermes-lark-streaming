# Changelog

## v2.0.10 (2026-10-12)

**面板通报行官方图标统一 + status 通道语义层（用户飞书验收反馈：限流提示行样式不一致且用 emoji，违反"面板一律飞书官方图标"标准）。**

事故链（2026-10-12 gateway20 生产日志 + 用户截图文字逐字比对）：主模型 deepseek-v4-flash 撞 429003 三连（会话 ~75K tokens 吃满账户 TPM）→ 重试耗尽 → agent 发出 lifecycle 状态 → 网关 `_prepare_gateway_status_message` 判定 provider 错误信封 → 官方转换 `_gateway_provider_error_reply` → i18n 文案「⏱️ The AI model service is rate-limiting requests. Wait a moment, then use /retry.」→ status 通道（`send_or_update_status` 兜底 `send()`，metadata 无 notify）→ v2.0.1 升格启发式把 emoji 短行误判为进度 → 无活跃卡时建占位卡（spinner 永不定稿，用户"卡住了"体感）→ 渲染层不匹配任何已知模式 → 裸 markdown 兜底（字体与其他行不一致 + emoji 原样保留）。

| 类型 | 问题/功能 | 原因 | 修复/说明 |
|---|---|---|---|
| P1 | status 通报建幽灵卡（"卡住了"根因）：终态错误通报被当进度行，无活跃卡时建占位卡且永不定稿 | 网关 `send_or_update_status` 契约此前无平台实现 → 兜底 plain `send()`（metadata 无 notify/`_interim_send` 标记）→ CH_PLAIN → v2.0.1 升格启发式（emoji 短行）→ CH_PROGRESS → `_on_progress` 占位分支建卡 | 适配器实现官方 `send_or_update_status` 契约：status 通报 = advisory——活跃卡并入面板、无卡原生文本气泡（用户立即可见）、**永不建卡/永不升格**；刻意偏离官方「按 status_key 编辑气泡」：面板即本平台的原地状态面 |
| P2 | 面板未识别通报行裸 markdown 兜底：字体与结构化行不一致 + emoji 原样保留 | `_render_status_children` 只结构化工具行/思考行，emoji 开头且解析不出工具结构的行（限流/警告/后台通报）落到 `{"tag":"markdown"}` 兜底 | 新增通报行样式：`split_notice_lead` 剥 emoji 头 → `_build_notice_row` 渲染 div + official `standard_icon` + notation 灰字（与面板其余行同字体体系）；emoji→官方图标映射表（⏱/⏳/⏰→`time_outlined`、⚠/🚨→`warning_outlined`、💾→`storage_outlined`、🔄→`refresh_outlined`、ℹ/💡→`info_outlined`、未知→`info_outlined`）；非 emoji 未知行维持 markdown 兜底 |
| P2 | 默认工具图标 `tool_02` 不在官方 enumerations-for-icons 清单内 | V1(1.8.1) 移植遗留；V1 时代未对照官方清单 | `DEFAULT_TOOL_ICON` → `code_outlined`（官方清单 2026-10 版实测在列）；测试新增官方 token 冻结集，断言面板引用的全部 19 个图标 token ∈ 官方清单（回归防线） |

**审计方法**: 用户反馈文字与 gateway20.log 逐字比对（i18n 文案 `_gateway_provider_error_reply` 定位）→ hermes 源码链路求证（`_TELEGRAM_NOISY_STATUS_RE` 静默语义 / `send_or_update_status` 官方契约与调用点 / `_mark_notify_metadata` 键名）→ 官方 enumerations-for-icons 全量抓取（215KB / 1141 token）存档核对 → 本地 15 用例回归（渲染层纯函数 + 适配器桩语义层）→ 全量 523/523。

**已知限制**:
- 限流重试过程进度（「⏱️ Rate limited. Waiting Xs (attempt N/3)...」）被网关 `_TELEGRAM_NOISY_STATUS_RE` 官方静默（设计：retry chatter 不进聊天），V2 层无钩子可达——重试窗口内面板无新增行属预期；fallback 链配置后的「⚠️ Model fallback...」通报可见
- 未知 emoji（映射表外）以 `info_outlined` 兜底渲染（官方清单内通用图标）；国旗等多码位 emoji 行维持裸 markdown 兜底
- 升格启发式（v2.0.1）对非 status 通道的裸进度文本仍生效；cron/standalone 场景下 progress 样式文本仍会建卡（v2.0.8 通知清单已拦截已知通知族，其余文本无可靠判据，诚实保留）

---

## v2.0.9 (2026-10-12)

**五轮全面审计修复：P0/P1/P2 全量落地（用户确认 2026-10-12，先审计后修复流程首次执行）。**

审计证据链：gateway19.log 生产事故（23:09:50 整回合降级纯文本）+ hermes 源码考古 +
真飞书 sequence 实验（卡 om_x100b63f0d183a880b1f89b86413d0d1：重放/回退 seq 必现
300317、seq+1 立即自愈、跳号允许）+ 代码静态审查。全部修复经主流程方案（不新增兜底）。

| 类型 | 问题/功能 | 原因 | 修复/说明 |
|---|---|---|---|
| P0 | 300317 sequence 冲突 → 整回合降级纯文本（gateway19 生产事故） | `client._retry_transient` 对网络读超时的重试用**同一 sequence** 重发；首次请求实际已达服务端时重发必撞 300317（`_is_transient_error` 判非瞬态直接上抛 → composer `_degrade`） | **新增 `cardkit/seq.py`**：`call_with_seq_recovery` 捕获 300317 → 本地 seq 对齐服务端（+1）→ 重发同内容（全量语义幂等）。engine 全部带 seq 调用（answer 流×2/状态更新/建卡 seed）+ finalize 三步（answer 终值/页脚插入/关流式）统一接线；上限 3 次逐次 +1，超出仍上抛交降级红线。主流程自愈，非兜底 |
| P0 | 长回合（>10 分钟）断卡：答案编辑失败 + 第二张卡 + 旧卡跑马灯永挂 | `_gc` 条目 `st.finalized and idle>60 or created_at>ttl` 运算符优先级实际为 `(finalized and idle>60) or (created_at>ttl)`——活跃回合建卡满 TTL（默认 600s）即被弹出；后续 answer edit 落到内置适配器 `im.v1.message.update` 打卡片消息必败、进度帧新建第二张卡 | `_gc` 重写为**空闲回收**：已定稿 60s 冷却回收；未定稿仅 `last_activity` 空闲超 TTL 才回收（活跃回合每帧续命，长回合永不误杀；空闲超 TTL 的只能是已中断孤儿回合）。占位状态暂存帧同步续命 |
| P1 | 幽灵卡：回合封卡后冒出带跑马灯新卡 | agent 层 `on_stream_end` 先于网关 finalize edit（竞态窗实验证实）；reasoning 尾 flush 入队时守卫通过、执行时无复查 → `_on_progress` 占位分支对已终态状态重建卡片（复用重置 finalized） | `reasoning_feed.on_stream_end` 尾 flush 标记 `final=True` 透传；composer `_feed_progress` **执行时**复查：终态/无卡一律丢弃（不占位不建卡）；流中 flush 语义不变 |
| P1 | 双 finalize 重复插页脚 | `handle_edit(finalize=True)` 无 `st.finalized` 幂等守卫；网关重试/重复事件重复执行三步定稿 | `_finalize` 开头幂等短路（已定稿直接返回成功）+ `finalized` 先行置位；异常路径由 `_degrade` 置终态，行为一致 |
| P2 | 200 元素上限无安全网 | `_enforce_card_element_limit` 定义+导出但从未调用（当前 ~30 元素安全，未来膨胀无防线） | 接线 `build_streaming_card_v2` 返回路径：超阈值裁面板最旧子项 + 「⚡ 还有 N 项已折叠」提示；answer/页脚/spinner 永不裁 |
| P2 | 工具耗时误挂/失联 | ① 钩子尾反查任意未挂行：钩子丢失时耗时误挂到更早调用；② pending 行挂上耗时后 preview 取代 → key 失联标签丢失；③ 建卡复用状态未重置 `tool_durations` → 同命令重跑继承旧耗时 | ① 只认尾数第一个同工具行（已挂→pending 等新行认领）；② `_migrate_tool_durations`：同位置同工具 pending→preview 取代时迁移标签；③ `_create_card` 重置 `tool_durations`/`pending_tool_durations` |
| P2 | 生命周期日志不可见（审计 R2 可观测性缺口） | 网关默认日志级别 WARNING 吞掉插件 INFO（card created/finalized 无迹可查，生产排障缺证据链） | `register()` 内联 `logging.getLogger("hermes_lark_streaming").setLevel(INFO)`——插件命名空间不受网关全局级别影响，第三方日志零污染；300317 自愈用 WARNING 恒可见 |
| P2 | V1 遗留代码/文档过时 | `state/`（5 文件）`flush/`（2 文件）运行时零引用，338 遗留测试占套件近半；`docs/` 遗留 TASK_*.md×5/TEST_PLATFORM.md/DESIGN.md/API.md；README 徽章停 v2.0.0 | 删除 `state/`、`flush/` + 5 个纯遗留测试文件 + `test_cardkit.py` 遗留用例改写为 V2 语义（`ReasoningRound` TYPE_CHECKING 死导入清除）；删除 7 个过时文档；README 徽章 → v2.0.9 |

**审计方法**: 5 轮只读审计（R1 本地/Gitee/服务器三方一致性 → R2 服务器日志取证 → R3 代码静态审查 → R4 hermes 源码对齐 + 真飞书 sequence 实验 → R5 汇总分级），全部结论「机制 + 证据」双实锤后才动手；修复全程遵守主流程原则（300317 是协议语义自愈、GC 是回收语义修正、幽灵卡是时序语义补全，均非 try/except 式兜底）；真飞书 E2E 实验先于修复定案。

**已知限制**:
- 工具耗时配对残余窗口：某调用钩子丢失且下一调用的行尚未插入快照时，耗时缺失（诚实不伪造）；跨回合不继承
- 钩子重复触发（hermes 层异常）会把耗时标签送 pending 认领到下一个同工具行（概率极低，与旧实现相同）
- 新回合建卡前的 reasoning delta 暂存丢失窗口（v2.0.7 既有边界，1.5s 节流后下一次 flush 自愈进面板）
- 用户 10 秒内连发消息时，上一回合终态残留下首个 progress 帧可能被 final-flush 防线误丢（answer 首帧仍正常建卡）
- elements.py 内 V1 渲染函数（build_unified_panel/build_panel_header/build_panel_children）已零运行时引用，保留至下一轮专项清理（本轮边界：只删审计点名的 state/flush）
- 429003（SenseNova 账户级限流）与模型上游截断（no finish_reason）为外部约束，V2 层不可修

套件 508/508（存量 712 − 338 遗留 + 新增 21 + test_cardkit 改写 6）；ruff 全仓 209→159（净减 50 来自遗留删除），新代码零新增告警。

---

## v2.0.8 (2026-10-11)

**流式面板质量四连修：多行思考吸附 / 答案复读过滤 / 单工具耗时 / 非对话通知永不建卡。**

用户验收（2026-10-11）四问题：①「思考部分只有前面格式对，后面格式不对」②
「"SpaceX买" 不知道是什么」③「每一条都没有耗时记录，而插件都有的」④「Self-
improvement review 新起一个卡片，跑马灯一直在」。逐项根因与修复：

| 问题 | 根因 | 修复 |
|---|---|---|
| ① 多行思考后段格式乱 | reasoning_feed / 网关中继的 `💬 {text}` 自带换行，`split_progress_segments` 按行直拆把首行后的内容打碎成独立 raw 行（仅首行保留思考样式） | `progline.split_progress_segments` 多行思考吸附：💬 行与其后非 emoji/非围栏连续行合成单一逻辑段；思考正文整段渲染（超 2000 字截断，V1 `_REASONING_DISPLAY_LIMIT` 同款）；`reasoning_feed` 冻结上限 600→2000（600 会把推理正文腰斩） |
| ② "SpaceX买" 碎片 | 模型可见间歇文本（commentary）经 CH_INTERIM 直 append 成无样式裸行；且网关 `_relay_thinking` 把**每次响应**（含最终答案）的可见正文前 500 字中继成 💬 行 | 非 emoji 播报归入思考轮次（V1 on_thinking 语义，灰色 notation + 合并幂等）；`_is_answer_duplicate` 前缀过滤最终答案中继复读（V1 从不把 final answer 展示进面板；≥40 字符才判，隐藏推理绝不误伤） |
| ③ 无每条耗时 | V2 帧协议无工具事件；V1 由工具事件实测 | **新增 `tool_timing.py`**：官方 `post_tool_call` 生命周期钩子（payload 自带 `duration_ms`，`model_tools.py::_emit_post_tool_call_hook`）→ 线程安全回灌 → 工具标题「Terminal (2.3 s)」（V1 `_format_duration_label` 同款）；钩子先于行时入 pending 队列由行追加认领。思考轮次标题「第 N 轮 · X.Xs」：行到达时间戳对账（`_reconcile_line_times`，增长取代继承起点不重置），轮次在下一工具行到达时定稿 |
| ④ 💾 通知新卡 + 跑马灯不止 | 后台通知（`_make_bg_review_callbacks` → `_send_status_text`，metadata 仅 `_interim_send`；`non_conversational` 标记仅 Discord 生效）带 💾 emoji → 命中进度启发式 → 卡片定稿后仍触发 `_on_progress` 建新卡（streaming_mode + spinner，永无 finalize） | `classify` 官方非对话通知清单（对齐内置 Discord 适配器 `_DISCORD_NONCONVERSATIONAL_*` 正则子集：💾 Self-improvement review / ⏳ Working / Hermes update / Gateway restart）→ CH_INTERIM：活跃卡并入面板（V1 `defer_background_review` 语义）/ 无卡走原生文本；`_upgrade_plain_to_progress` 增加 metadata 守卫（notify/non_conversational/通知文本永不升格） |

套件 712/712（新增 32 用例）；ruff 新代码零新增告警。

**审计方法**: 服务器 gateway18.log 真实回合取证 + hermes 源码逐行定位（`turn_response_intake.py::_relay_thinking` / `model_tools.py` 钩子 / `gateway/run.py:476` Discord-only 标记）+ V1(1.8.1) worktree 逐项对照（`on_thinking`/`defer_background_review`/`_format_duration_label`/`_REASONING_DISPLAY_LIMIT`）。

**已知限制**:
- 模型上游截断（"Response truncated — server ended the stream without finish_reason"，sensenova 端点侧现象）V2 层不可修
- 429003 账户级限流为外部约束
- v2.0.9 审计发现本版存在三处缺陷（300317 无自愈 / GC 优先级 / 幽灵卡竞态），已在下一版修复

---

## v2.0.7 (2026-10-11)

**思考行修复：DeepSeek reasoning 隐藏流 → 面板「第 N 轮」思考记录（零 monkey-patch）。**

用户验收（2026-10-11）：「折叠面板里为何没有思考、推理的记录，只有工具的记录」。
根因（日志+源码+端点实测三重定位）：hermes 网关 `thinking_progress` 只中继模型在
工具调用之间的**可见间歇文本**（`_relay_thinking` 读 assistant content）；而
DeepSeek-v4-flash 的思考走 `reasoning_content` 隐藏流（端点流式实测 62 reasoning
块/次回答）——网关主代理路径不接 `reasoning_callback`（仅 api_server 接），
`_fire_reasoning_delta` 静默丢弃，面板永远收不到 💬 行。

| 变更 | 文件 | 说明 |
|---|---|---|
| **新增** reasoning 流桥 | `reasoning_feed.py` | 注册官方 `on_stream_delta`/`on_stream_end` 观察者钩子；`kind="reasoning"` delta 按会话（`gateway.session_context` ContextVar 定位 chat）累积、节流（1.5s/400 字符）、间隔 >8s 断轮重置，回灌 composer 的 CH_PROGRESS/thinking 通道 → 面板按既有合并语义（增长原地替换）渲染「第 N 轮」思考行；600 字符冻结上限保前缀增长幂等 |
| **新增** 线程安全回灌 | `composer/engine.py` | `submit_progress_from_thread()`：捕获事件循环引用（构造期/帧入口），`call_soon_threadsafe` → handle_send 消费；已定稿/已降级丢弃 |
| **新增** 适配器接线 | `adapter.py` | 实例化时 `bind_composer()`；`register(ctx)` 注册观察者钩子（需 config `plugins.stream_reasoning_deltas: true` 才有投递；未配置/失败仅降级为无思考行） |

**部署要求**：config.yaml 增加 `plugins: {stream_reasoning_deltas: true}`。

套件 674/674（新增 11 用例）；ruff 新代码零新增告警。

## v2.0.6 (2026-10-10)

**真实帧格式对齐：E2E ≠ 正式对话根因修复（用户验收反馈驱动）。**

用户验收（2026-10-10）：「E2E 群聊消息已接近理想效果，但私聊正式对话面板里是
`💻 terminal` + 行号代码块」。根因：v2.0.5 的 E2E 用合成帧（`🖥️ terminal："…"`）
恰好命中解析正则，而 hermes v0.21.x 真实 progress 模板（`agent/display.py` +
`gateway/run_turn_runner.py` + locales）完全不同——正式对话的行从未被结构化解析，
面板只能原样渲染 emoji 原文。

| 变更 | 文件 | 说明 |
|---|---|---|
| **新增** 真实格式解析 | `cardkit/progline.py` | ① terminal 围栏代码块（`💻 terminal` + ` ``` ` 块）→ (tool, 首行命令)；② 中文/英文动词行（`🔍 搜索网页 · q` / `💻 Running uname`）→ 动词反查工具名（hermes locales display.verb 双语动词表）；③ 裸围栏块（连续 terminal 调用省略 header）；④ 网关去重后缀 `(×N)` 剥除；⑤ `split_progress_segments`：accumulate 快照按逻辑段拆分，围栏块与 header 保持一体（直拆 splitlines 会把块拆成破碎 ` ``` ` 行——正是正式对话面板破碎的直接原因） |
| **修复** 面板渲染 | `cardkit/cards.py` | 真实格式 → V1 风格元素：官方图标 + 彩色粗体工具标题 + 缩进灰字命令；emoji / ` ``` ` 围栏永不进面板元素；包裹引号在 `preview_text` 内剥除（不再误伤命令尾部引号） |
| **修复** 合并语义 | `composer/engine.py` | `(×N)` 幂等；pending → 代码块原地取代；同工具再次调用追加新步骤；建卡前暂存行过同一套分段 |
| **修复** 分类通道 | `composer/classify.py` | 裸围栏块判入 progress 通道（≤600 字符），真实会话裸 send 升格路径可用 |
| **新增** 回归防线 | `tests/e2e/test_v206_real_formats.py` | 固化真实格式的解析/分段/合并/渲染/引擎端到端语义（22 用例），防再次回退 |

套件 663/663；ruff 新代码零新增告警。

## v2.0.1 (2026-10-10)

**生产部署夜修复：进度帧上下文升格 + 进度气泡快照合并 + 网关咬合加固。**

云服务器实测（hermes 2026.9.24 快照 + v0.21.6）暴露三个生产缺陷，本版全部修复：

| 变更 | 文件 | 说明 |
|---|---|---|
| **新增** 进度帧上下文升格 | `adapter.py` | edit-transport 平台（feishu）的工具进度气泡首条是**裸 send**（hermes 只给 native-stream / 预览路径打 `expect_edits`，`_send_progress_text` 不带任何标记）→ 原分类落 CH_PLAIN 产生独立纯文本气泡。现在：活跃卡片期 + 进度启发式（💬 前缀 / emoji 短行）→ 升格 CH_PROGRESS 吸收进状态面板 |
| **修复** 进度快照合并 | `composer/engine.py` | 网关 accumulate 模式下气泡文本是「当前全部进度行」的全量快照（每 tick 重发累积全文）——原实现整块 append 会产生膨胀 blob。现在按行拆分 + 集合去重幂等合并（accumulate / separate 两种模式都正确） |
| **修复** 进度帧返回契约 | `composer/engine.py` | `_on_progress` 不再回传卡片 message_id——防止网关 `_send_progress_text` 把卡片 id 记作 `progress_msg_id` 后用 `_edit_progress_message` 把工具行**覆写进答案区**（卡片内容损坏事故预防） |
| **新增** `display_tier="high"` | `adapter.py` | 显式声明显示基线（streaming 跟随全局 + tool_progress "all" + interim 开），防插件平台默认漂移静默关闭流式 |
| **新增** 生命周期日志 | `composer/engine.py` | 建卡 / 定稿 INFO 日志（chat/card/行数），生产日志可观测 |

**兼容性**：hermes v0.21.6（官方最新稳定版）注册协议全兼容（`PlatformEntry.display_tier` 官方字段）；v0.21.6 已移除 PM/uv-sync 准入机制，插件以目录插件形态直接加载（`~/.hermes/plugins/<name>/`）。

## v2.0.0 (2026-02-06)

**架构重构：standalone 补丁插件 → 正规 platform 插件（零 monkey-patch）。**

V2.0.0 是一次彻底重写：插件从「运行时补丁集合」变为「可安装的平台」，与内置 `feishu` 平台并存、随时切换。V1 文档与补丁架构全部退役。

| 变更 | 说明 |
|---|---|
| **新增** `adapter.py` | `FeishuStreamingAdapter` 继承内置 `FeishuAdapter`（能力零损失），重写 `send` / `edit_message` / `delete_message` / `send_clarify` / `_handle_card_action_event` |
| **新增** `composer/` | 帧重组引擎：官方 metadata 帧分类（`expect_edits` / `_interim_send` / `notify` / progress 模板）→ CardKit 生命周期（create → stream_element → close_streaming）；卡片 API 失败自动降级原生文本，永不丢消息 |
| **新增** `clarify.py` | 澄清卡片闭环正式化（选择/输入/重试/确认三态 + fail-closed 授权） |
| **变更** `plugin.yaml` | `kind: standalone` → `kind: platform`，平台名 `feishu-streaming` |
| **变更** 配置节 | `hermes_lark_streaming` → `feishu_streaming`（自动迁移） |
| **删除** `patching/` | 全部运行时 monkey-patch（v2.0.0 验收标准：0 补丁） |
| **删除** `controller/` | token 级流式控制器——事件源（agent 回调钩子）随补丁架构一并退役，由帧重组引擎替代 |
| **保留** `cardkit/` `feishu/` `state/` `flush/` `aowen/` `config/` | 卡片引擎、CardKit 客户端、会话状态、/aowen 指令原样继承 |
| **已知限制** | ① `/aowen` 在 agent 运行中的"中断提示卡片"随补丁架构退役（无官方状态查询接口）；② 终态卡片状态区折叠与 footer 计费字段为 v2.1 规划；③ 非 clarify 的卡片按钮点击被抑制 `/card` 合成命令（继承 V1 行为） |

**升级**：安装本插件后把 gateway 平台名 `feishu` → `feishu-streaming` 即可；回滚改回即恢复原生。

**v2.0.0 E2E 验证补充（refactor/v2.0.0 @ 0334cc9）**：

- 真飞书 E2E 实测打通：云服务器（Ubuntu 26.04 / Python 3.14 / hermes v0.21.6）+ 真实应用凭证，`scripts/e2e_v2.py` 七场景（connect/answer/progress/interim/full/clarify/plain）25+ 项断言全 PASS——含建卡前 progress 暂存并入首卡状态面板、重复工具行去重、打字机流式、两步定稿、澄清卡片直发
- 随验证修复的三处 V2 真实缺陷：① composer `active_state()` 未排除建卡前占位状态（工具先行场景整 turn 降级）② `_create_card()` 丢弃 pending_status（首卡状态面板恒空）③ `clarify.py` 3 处 `..cardkit` 相对导入越界（生产澄清卡片必静默降级纯文本）
- 部署实测再修两处适配器引导缺陷：④ `type()` 重建类借用的方法函数 `__class__` cell 指向旧占位基类 → 生产首次实例化 super() 身份错乱（改为**导入期绑定真实基类**，rebuild 退化为 no-op；`__bases__` 原地赋值在 CPython 3.12+ 直接 TypeError，不可用）⑤ 插件平台凭证需在 config `platforms.feishu-streaming.extra` 显式种入 `app_id`/`app_secret`（内置平台的 `_ENV_STEPS` 环境桥不覆盖插件平台，`_is_connected` 据此判定）
- `tests/e2e/` 完成到 V2 的移植（23 用例，mock/real 双模式）+ 导入期绑定回归测试，全量套件 585/585
- 部署形态（已上线）：hermes gateway 常驻 + feishu-streaming 平台接管 + 真飞书 WS + SenseNova LLM；插件准入需配国内 PyPI 镜像 + 放宽 pm 超时（`uv sync` 全量重建 venv 在国内网络必超时）
