# Changelog

## v2.0.15 (2026-10-13)

**hermes 平台通知轨卡片化——子代理失败通知 >300 字符漏洞（Task 21 用户追问「子代理的消息是否也做成卡片消息了」举一反三增量）。**

背景：v2.0.14 十二条输出路径盘点未单列子代理（delegate_task）专项。本轮取证（hermes 最新源码逐行）：子代理架构天然受限——官方设计铁律「The parent only ever sees the delegation call and the summary result」（delegate_tool.py:9-11），且 `DELEGATE_BLOCKED_TOOLS` 硬禁子代理调 `send_message`（delegate_tool_toolsets.py:14-23 "no cross-platform side effects"）——子代理中间过程与自发消息**不存在出站路径**。实际出站仅五条：delegate 工具进度行（面板 ✓）/ 失败超时通知（本条修复）/ 后台完成结果（`[IMPORTANT:…]` 合成消息注入父会话唤醒新回合 → 主流卡片 ✓）/ 批量 per-task 失败通告（同注入 ✓）/ cron 会话子代理（结果并入 cron 投递 → R1 cron 卡 ✓）。漏洞仅在失败通知：`_deliver_platform_notice` 直发 `adapter.send`（run_notifications.py:208），飞书 thread_id=None 时 metadata=None 无任何标记 → CH_PLAIN；官方 `format_subagent_failure_line`（delegate_tool_progress.py:64-93，f-string 硬编码前缀 `⚠️ Subagent failed/timed out`）的 reason 经 `_clean_error_text` cap 200 字符，整行最长 ≈359 字符——超出 `_is_progress_text` 工具行 300 上限：「长通知 + 活跃卡在场」既不升格进面板、又命中不了 oneshot R3（要求无活跃卡）→ `super().send()` 原生 post，孤挂卡片旁。timeout 行（max ≈265）与 credits 警告族（AgentNotice 唯一生产方 credits_tracker.py，全行 <100 字符）现状已覆盖。

| 类型 | 问题/功能 | 原因 | 修复/说明 |
|---|---|---|---|
| P2 | 子代理失败/超时通知 >300 字符且活跃卡在场时，以原生 post 文本孤挂卡片旁 | 通知轨无 metadata 标记 → CH_PLAIN；`_is_progress_text` 300 上限拦截升格；oneshot R3 要求无活跃卡（通知被官方 gate `_run_still_current` 锁定回合内发出，活跃卡大概率存在） | **平台通知轨识别（方案 A）**：`_is_progress_text` 对官方固定前缀 `⚠️ Subagent failed/timed out`（容忍裸 ⚠ 变体）豁免 300 上限（600 防御，与围栏块一致）→ 复用既有升格路径：有活跃卡 → 并入面板（v2.0.10 通报行样式，⚠️ → `warning_outlined` 官方图标）；无活跃卡 → early-create 回合卡 + 暂存（后续 answer 帧接管，单卡全回合）。**拒绝 oneshot R4**（通知 → 独立 gateway 卡）：通知锁定回合内，独立静态卡与回合卡并存 = v2.0.11 多卡片观感回归；升格路径与现状 ≤300 字符通知行为完全一致。回合外唯一可能是调度 race 窗口 → 暂存并入下一卡（Task 18 P2-C 已知限制家族，不丢消息不多卡）。hermes 通知行硬编码英文（locales 全库无中文通知文案），正则不猜测中文变体 |

**审计方法**: hermes 源码逐行（delegate_tool.py 架构注释 / delegate_tool_toolsets.py DELEGATE_BLOCKED_TOOLS / delegate_tool_progress.py format_subagent_failure_line + _clean_error_text cap / run_turn_runner.py:182-198 `_progress_subagent_notice` gate + :1185-1197 notice_callback 同轨 / run_notifications.py:176-208 `_deliver_platform_notice` 终点 + :1267-1348 `_inject_watch_notification` 合成注入 + :98 `_mark_internal_notification` / credits_tracker.py AgentNotice 全部生产方枚举）+ v2 adapter/classify/engine/progline/cards 全链重读 → 识别层单点豁免实现 → 全量 620/620（v2.0.15 新增 24 用例：启发式豁免 9 / 分类层不变 2 / 升格层 5 + 渲染先例 2 / composer 集成 3 / adapter 接线 3）→ ruff 基线相等（144=144，新代码零新增）。真飞书 E2E 走 full 回归（⚠️ 行与 v2.0.10 已验收的 credits 通报行渲染路径完全一致，无新渲染代码）。

---

## v2.0.14 (2026-10-13)

**cron 定时投递卡片化 + 回合外正式投递一次性定稿卡——用户 2026-10-13 截图事故修复 + hermes→飞书输出路径全量盘点（举一反三）。**

背景：用户云服务器 cron 任务 `daily-morning-news-830`（job 967430ee4ab2）08:31:15 投递，飞书收到富文本而非卡片（im GET 实锤 `om_x100b63e3f3f570a…` msg_type=`post`）。取证定案：hermes cron 投递经 live adapter lane（cron/scheduler_delivery.py:2107 `_deliver_via_live_adapter` → gateway/delivery.py:322 `transport.send`）以 `adapter.send` 落地，metadata 携带官方标记 `{"job_id", "notify": True}`（scheduler_delivery.py:1458）；插件 classify → CH_FINAL → `engine._on_final_send` 回合外无活跃卡返回 None → `super().send()` 原生 post 文本。V1 曾以 monkey-patch `_deliver_result` 实现 cron 卡片化（DEV patching/gateway.py:853 `_wrap_cron_deliver` 无条件拦截 + `build_cron_card`），v2 移除补丁层后能力断档。举一反三全量盘点 hermes→飞书 12 条输出路径：流式回复（native 卡）/状态通报（v2.0.10 advisory）/思考中继（面板）/澄清卡/媒体/降级红线（原生保底）均有既定语义，仅 3 条**回合外**路径落原生文本——cron 投递族、回合外 final、长文主动投递（send_message 工具族）。三者同源同修。

| 类型 | 问题/功能 | 原因 | 修复/说明 |
|---|---|---|---|
| P1 | cron 定时任务响应以富文本（post）而非卡片投递（成功/失败/队列排水三 lane 同源） | live lane `adapter.send` → CH_FINAL → 回合外无活跃卡 → 原生 post；V1 补丁层能力未随零补丁架构迁移 | **一次性定稿卡（R1）**：`adapter._send_oneshot_card`——metadata 带 `job_id`（官方标记，scheduler_delivery.py:1458，零启发式）即渲染 `build_cron_card`：官方包装头 `parse_cron_delivery` 解析（scheduler_delivery.py:2025-2031 格式逐行镜像；wrap_response=false 无头时兜底标题 "Cronjob"）→ 标题行（time_outlined + 任务名粗体）+ hr + 正文 markdown（表格降级/长文切分沿用 V1 遗产）+ 灰色 job_id 溯源页脚（`_build_notice_row` 同构）；thread_id 为 om_ 消息 id 时线程内回复卡。一次性卡**无状态**：不注册 composer state、不取 per-chat 锁、与回合卡互不干扰（v2.0.11 多卡片根因不涉及） |
| P2 | 回合外 final（notify 无活跃卡）落原生文本（非流式单发回复等） | 同通道 | **R2**：`build_gateway_card` 静态卡；有活跃卡 = 回合定稿语义，保持既有 finalize 路径不变 |
| P2 | ≥200 字无标记主动投递（send_message 工具族）落原生文本 | CH_PLAIN → 原生 | **R3**：长文静态卡；<200 字保持原生（短消息卡片化属过度设计） |

**审计方法**: hermes 源码逐行（scheduler_delivery.py `_deliver_result` 双 lane / `_live_route_metadata` :1458 / `delivery_queue` drain 同收敛 :1376 / send_message_tool live metadata）+ 服务器日志取证（agent.log:2530 live adapter lane + message_id；config.yaml 无 `cron:` 节 → notify 默认 True）+ im GET 消息类型实锤 → V1（DEV 分支）考古（`_wrap_cron_deliver` + relay lane job_id 识别先例）→ 插件 adapter/cardkit/config 重读后实现 → 全量 596/596（v2.0.14 新增 26 用例：包装头解析 7 / cron 卡扩展 4 / 三规则门控 11 / send 接线 4）→ ruff 基线相等（新代码零新增）。真飞书 E2E 新增 `oneshot` 场景（包装头解析断言 + cron 卡/gateway 卡双直发）。

**⚠️ 部署环境事故（本轮发现并修复，影响 v2.0.13）**: 首次部署后生产 lane 验证仍发 post——取证发现 `~/.hermes/plugins/` 顶层存在历史部署备份副本 `hermes-lark-streaming.bak-v211/.bak-v212`，与主目录同 plugin key（feishu-streaming-platform）；官方加载器对同名 key 选择 **.bak-v212（v2.0.12 代码）**（agent.log `Loading plugin 'feishu-streaming-platform' (path=…bak-v212)` 铁证）→ **v2.0.13 自 2026-10-12 部署起从未真正上线**（Task 19 的探针 25/25 与 E2E 全绿为假象：探针 sys.path 直挂主目录、E2E 直构 composer，均未经过加载器选择）。修复：全部影子副本挪出 `~/.hermes/plugins/`（→ `~/.hermes/plugin-backups/`），重启后 `Loading … path=…/hermes-lark-streaming` 铁证 v2.0.14 真身加载，生产 lane 临时 cron 任务实证 `msg_type=interactive`。**部署卫生规则（永久生效）**：①部署备份必须放 plugins 目录之外；②部署验证必须包含「Loading plugin … path=」日志行路径检查（磁盘 md5 / sys.path 探针都无法代表运行时真身）。

**已知限制**:
- cron 卡为一次性静态卡（无流式/无状态面板）——cron 投递无回合上下文，属投递物非对话；执行期工具过程仍只在服务端日志可见
- `cron.wrap_response: false` 的裸投递走 R1 时标题为兜底 "Cronjob"（无任务名可解析）
- cron 投递与活跃流式卡并发时为两张独立卡（投递物与回合答案语义不同，刻意不合并进面板）

---

## v2.0.13 (2026-10-12)

**答案流迁移 hermes native streaming 通道（P1-B）+ 空面板壳清除（P1-A）——Task 18 取证报告两项修复落地（用户拍板「两个都做了吧」）。**

背景：Task 18 取证（2026-10-12 16:54 飞书实测轮次日志 + hermes v0.21.6 官方源码逐行核对）定案——「同一段话一次进正文一次进折叠面板」非插件路由缺陷：edit-based 平台上模型在工具调用前的可见前奏正文被官方 segment-break finalize seal 成独立卡（stream_consumer.py:842-844 `finalize=tick.got_done or tick.got_segment_break`，官方注释自认 *"it closes a preamble, not the answer"*——为 DingTalk AI Cards 显式关闭设计）；同族文本在后续轮经 💬 思考中继进面板行。两次渲染各符合通道语义，形态不一致；429 fallback 续跑放大观感。

| 类型 | 问题/功能 | 原因 | 修复/说明 |
|---|---|---|---|
| P1 | 同一段话一次进正文（工具边界被 seal 成独立卡 + 空面板 + 已完成页脚）一次进折叠面板——跨轮归属不稳定 | edit-based 通道：官方 segment-break finalize 在「模型前奏 + 工具调用」组合下把前奏封成第一卡（native 路径不存在此问题：`finalize = finalize and is_turn_final` 吞掉 segment-break，但 hermes 无任何平台开启 native，feishu 走默认 edit 路径） | **native streaming 迁移（P1-B）**：适配器声明 `SUPPORTS_NATIVE_STREAMING`（类常量）+ `supports_native_streaming` probe + `send_stream_frame`（官方契约范例 plugins/platforms/wecom/streaming.py:261；探测点 stream_consumer_transport.py:151-164）。native 帧通道上官方吞掉 segment-break finalize（transport:389）且 native = cumulative transport（stream_consumer.py:906 `_end_segment` 直接 return）→ 工具边界不再封卡，**单卡全回合**。composer 新增 `handle_stream_frame`：种子帧（`text=""`）建卡骨架 / 全量快照更新（cumulative，非分段追加）/ finalize 幂等封卡；降级红线官方契约保证——任何帧 False/异常 → consumer 禁 native 回落 edit/send 现有链路（transport:399-426），帧永不丢。灰度开关 `feishu_streaming.native_streaming`（默认 true；false 一键回到 v2.0.12 edit-based 行为） |
| P1 | 纯 answer 回合卡上遗留 "agent loop" 空面板壳（折叠头无内容） | cards.py `include_unified_panel` 无条件渲染 collapsible_panel 骨架——流式期作「卡片先现」视觉锚，整轮无工具/思考行时终态后成空壳（Task 18 取证 16:54:57 卡 `created status_lines=0` → `finalized status_lines=0`） | **finalize 空面板删除（P1-A）**：`finalize_card` 在 status_lines 为空时追加第四步 `batch_update delete_elements [UNIFIED_PANEL_ELEMENT_ID]`（装饰性步骤：失败仅 debug 留痕，绝不阻断定稿；有过程记录的面板原样保留）。流式期面板骨架不变（「卡片先现」体验保留） |
| 伴生 | native 激活后网关工具 preview 行改走流内复合帧（run_turn_runner.py:310 `_progress_emit` native 分支 → `sc.on_tool_progress`），不再作为进度气泡到达适配器——面板失去网关工具行数据源 | 官方 native 语义：工具行复合进答案流帧（`_compose_frame_content` = `"{answer}\n\n---\n{progress}"`，stream_consumer.py:251-255；真实 delta 到达时清空 overlay :290-291） | `handle_stream_frame` 内置复合帧拆分器 `_split_native_composite`：拆最后一个官方分隔符，拆出段经 `split_progress_segments`（围栏块保一体）逐段全部命中进度启发式才拆，工具行并入面板——V1 风格图标/耗时/思考轮次渲染不变，保持本平台状态面板特色；正文自含 markdown 分隔线不误拆，误拆由下一快照帧自愈。💬 思考中继（run_turn_runner.py:152）与 status 通报通道不受 native 影响，reasoning_feed / tool_timing 官方钩子照常 |

**审计方法**: hermes 官方源码逐行核对（stream_consumer_transport.py 帧原语 :46-70 / native 探测 :151-164 / `_native_push` :373-426；stream_consumer.py `_push_update` :824-844 / `_end_segment` :529-543 / `_finalize_turn` :857-874 / `_compose_frame_content` :246-255；run_turn_runner.py 进度改道 :310-327；WeCom native 实现范例 streaming.py:261-340）→ 插件 engine/adapter/finalize/cards/classify/client 重读后实现 → 全量 570/570（v2.0.13 新增 28 用例：复合帧拆分 10 / handle_stream_frame 11 / 适配器 native 面 7 / P1-A 空面板 2；既有 2 处幂等守卫断言按 P1-A 新语义更新：finalize batch_update 调用数 2→3）→ ruff 基线相等（144=144，新代码零新增）。

**已知限制**:
- 复合帧拆分是启发式：正文以「markdown 分隔线 + emoji 工具行」结尾的极端形态可能误拆进面板——官方在下一个真实 delta 到达时清空工具行 overlay（stream_consumer.py:290-291）且帧为全量快照，自愈窗口一个 delta；finalize 帧纯正文（got_done 时 `is_interim=False` 不复合）无此风险
- `supports_native_streaming` probe 无 per-chat 维度（官方签名仅 chat_type/metadata）；建卡失败回落为帧级（无 state 下一帧重试）+ 回合级（False 禁 native 回落 edit/send）
- native 激活后 CH_STREAM（expect_edits 分类）在正常流程中闲置，仅作为回落路径保留；进度气泡 accumulate 快照仍经 CH_PROGRESS 进面板（💬 中继/工具行 [回落时]）
- **部署环境（hermes v0.21.x PM 运行时）**：PM 会把进程 site-packages 切到它选代的代次 venv（`~/.hermes/installs/<id>/environments/<gen>/venv`，pm/environments.py::activate_dependencies），插件依赖（PyYAML/lark-oapi）必须装进该环境——manifest `python_dependencies` 官方契约仅校验提示不自动安装；PM 代次重建（hermes update）后需重装（本次部署排障实录：v2.0.12 时代同环境插件加载正常、此后 PM 状态差异触发切换，插件报 `ModuleNotFoundError: yaml`——详见 README 快速开始的环境说明）

---

## v2.0.12 (2026-10-12)

**面板思考正文 markdown 渲染 + 仓库根目录布局契约化 + Python 版本对齐 hermes 官方（用户 2026-10-12 三连反馈）。**

背景：①折叠面板思考正文自 V1 起走命令摘要同款 `plain_text` 灰字渲染——对命令正确，对模型生成的 markdown 语义文本错误，`**Planning tool use**` 原样显示星号；②pyproject 声明 `requires-python = ">=3.11"` 与 hermes 官方漂移（官方 pyproject.toml:10-15 明示 `">=3.11,<3.15"` 且注释原文 *"we \*only\* support 3.14"*，服务器 gateway 实际运行 Python 3.14）；③仓库根目录仍平铺 5 个 .py（adapter / clarify / reasoning_feed / tool_timing / setup），与「根目录只应保留入口与文档配置」的整洁标准不符。

| 类型 | 问题/功能 | 原因 | 修复/说明 |
|---|---|---|---|
| P2 | 折叠面板思考正文 markdown 未渲染：`**bold**` / `*italic*` / `` `code` `` 原样显示 | 思考正文自 V1 起与命令摘要共用 `_build_tool_step_detail`（plain_text 灰字）；命令是代码非 markdown，思考文本是模型 markdown 语义输出——同一渲染函数服务两种语义是根因 | 新增 `_build_reasoning_body_detail`：思考正文改 **lark_md** 渲染（markdown 语义生效），视觉不变（22px 缩进 + notation 字号 + 灰色逐行 `<font color='grey'>` 包裹——卡片 2.0 文本元素 `text_color` 仅 plain_text 支持，`lark_md` + `text_size` + `<font>` 是轮次/工具标题行生产已验证原语，逐行包裹规避多行 `<font>` 跨空行无契约问题）；命令摘要保持 plain_text；s116 真飞书探针 5 变体（lark_md+text_color / lark_md+font-grey / markdown+text_color / markdown / plain_text 现状对照）create + PUT 全过，比对卡 om_x100b63fd18545884c37387393b82173 |
| P2 | Python 版本声明与 hermes 官方漂移 | v1.x 时代对齐后 hermes 官方收紧未跟：官方唯一受支持运行时为 3.14（wheels 仅保证 3.14），`>=3.11` 元数据下限是官方更新器契约（旧安装升级步骤） | pyproject `requires-python = ">=3.11,<3.15"` 精确镜像官方并注释理由；README 徽章 python-3.11+ → **python-3.14** |
| P2 | 根目录平铺 5 个 .py，层级不整洁（用户：「根目录下除了 `__init__.py`、`__main__.py` 就不应该有其他 py 脚本」） | v2.0.11 曾以「运行时平铺 = 官方内置插件同构」为结论保留——经官方加载器源码重新核对，该结论过度保守：平台插件装载器 `_load_directory_module`（hermes_cli/plugins_loader.py:698）仅做 `spec_from_file_location` + `__path__=[插件目录]`，之后全部相对导入走标准 import 机制（cardkit/ 等子包同机制早已生产验证），加载器唯一契约是根 `__init__` 暴露 `register`；`plugins/plugin_loader.py` 的根级 `*.py` 兄弟预注册仅服务 cron_providers/context_engine/memory 发现包，与平台插件无关 | `adapter.py` / `clarify.py` / `reasoning_feed.py` / `tool_timing.py` `git mv` 收进 `plugin/`（hermes 集成层，跨包子包导入 `from ..xxx`）；`setup.py` 删除（动态版本改 `[tool.setuptools.dynamic] version = {attr = "hermes_lark_streaming.__version__"}`，plugin.yaml 仍是版本单一来源，pip wheel 实构建验证 2.0.12 解析成功）；根目录仅余 `__init__.py` + `__main__.py` + 文档/配置；布局契约冻结为回归测试（根仅两入口 .py）；顺带修复 `__main__ cleanup` 子命令导入不存在的 `_cleanup_config`（实为 `cleanup_streaming_config`）的死命令 bug |

**审计方法**: 官方加载器源码逐行核对（hermes_cli/plugins_loader.py vs plugins/plugin_loader.py 双装载器辨析）→ V1（DEV 2e90f9fe）渲染考古（思考正文自 V1 即 plain_text，属既定视觉而非回归）→ 真飞书 s116 五变体探针 → hermes 官方 pyproject 原文引用（hermes-agent @ 本地审计克隆）→ 全量 542/542（v2.0.12 新增布局契约 + md 渲染 11 用例）→ ruff 143（基线 148，净减 5，新代码零新增）→ pip wheel 动态版本实构建。

**已知限制**:
- lark_md 是 markdown 组件的子集：列表/表格/围栏等扩展语法在思考正文中仍按字面显示（思考段的围栏内容本就经 progline 分流到独立 markdown 兜底行渲染，不受影响）
- 思考正文行内既有 `<font>` 的行不再包裹灰色（避免嵌套干扰），该行保持原色
- pip 构建的动态版本经 attr 导入解析：`__init__` 解析 plugin.yaml 不依赖第三方库，构建环境缺 yaml/hermes 时 adapter 导入失败被捕获、版本解析不受影响

---

## v2.0.11 (2026-10-12)

**一轮对话生成多张卡片（孤儿卡）——per-chat 帧串行化（用户飞书验收反馈：「一个问题生成了多张卡片」）。**

事故链（2026-10-12 gateway21 生产日志 + 用户截图逐卡比对）：用户发送「几点了」→ 主模型 deepseek-v4-flash 流式 429003 中断（429 in 1.5s，流未交付）→ 官方 fallback 0.04s 内切换 sensenova-6.8-flash-lite → 工具回合完成瞬间（response #1 intake + 工具行 + 思考中继 + status 通报）多 lane 亚秒级齐发 6 帧 → `_on_progress` early-create 路径（active_state 检查 → cardkit_create + send_card_by_id ≈0.5-1s 网络挂起 → 注册状态）的挂起窗口内，后续并发帧各自看到「无活跃卡」→ 1.2 秒内连建 3 张卡（seeds 1/1/2 行，日志 `composer: card created` ×3），仅末张被定稿，前两张成为无页脚孤儿卡。10-11 23:13 已有同族先例（两卡相隔 145ms，其一为 0 行空面板孤儿卡）。

| 类型 | 问题/功能 | 原因 | 修复/说明 |
|---|---|---|---|
| P0 | 一轮对话生成多张卡片：burst 帧在建卡窗口内各自建卡，仅末张存活，其余成无页脚孤儿卡（2026-10-12 14:30「几点了」3 卡；10-11 23:13 同族） | engine 模块头假设「hermes 对单 chat 的出站天然串行」不成立——reasoning 回灌/工具耗时经 `ensure_future`/`call_soon_threadsafe` 火忘任务投递，网关多 lane（进度气泡/思考中继/status 通报/流式消费者）在回合事件密集期亚秒级齐发；`_on_progress`/`_on_stream` 的「检查活跃状态 → 建卡 → 注册」跨 await 无互斥（check-then-act 竞态） | FrameComposer 新增 **per-chat `asyncio.Lock`**：`handle_send`/`handle_edit` 全帧路径串行化——建卡窗口内后续帧在锁上排队，锁内 `active_state` 复查即双重检查；首帧建卡、后续帧合并进同一卡（或暂存随建卡并入面板种子）。锁随 chat 惰性创建、仅 `clear()` 全量清空（forget 不清，保证在途持有者与后来者共用同一把锁）。降级红线/幂等定稿/自愈语义不变 |
| P0 伴生 | 定稿与下一回合建卡共用同一状态对象，无锁并发时 `finalize_card` 挂起段可与 `_create_card` 状态重置交错（页脚落错卡风险） | 同上——`handle_edit` 未与 `handle_send` 互斥 | `handle_edit` 接入同一 per-chat 锁 |
| 文档 | 仓库层级与 README 老化：API.md 游离根目录、CHANGELOG 藏于 docs/、无引用头像资产、README 死链（docs/DESIGN.md 已于 v2.0.9 删除）、缺 v2.0.6-v2.0.11 特性记载、安装路径与真实部署不符、无徽章无仓库结构说明 | 多版本迭代只增不梳 | `API.md → docs/`、`docs/CHANGELOG.md → CHANGELOG.md`（开源惯例位）、删除无引用 `assets/avatars/`、README 全量重写（徽章/实景主图/架构图/真实配置键/仓库结构及「根=包」加载器契约说明/测试与 E2E 指引）；`create_release.py` 的 CHANGELOG 链接同步 |

**审计方法**: gateway21.log + agent.log 全帧取证（3 条 `card created` / 1 条 `card finalized`，无 degrade/finalize/建卡失败日志夹于其间 → 并发重叠排除法）→ engine/adapter/reasoning_feed 源码逐行核对（fire-and-forget 任务投递点清单）→ 10-11 23:13 历史同族事故回溯 → 竞态确定性复现测试（慢速 mock client 放大建卡窗口：无锁 6 帧 = 6 卡，有锁恒 1 卡，7 用例）→ 全量 530/530 → 真飞书 e2e_v2 新增 `burst` 场景（并发帧 gather 投递，断言单卡片 + 全行并入）。

**已知限制**:
- 已产生的孤儿卡（历史事故遗留）不自动删除——删除用户可见历史的风险大于留置；新竞态已从根因闭合
- per-chat 锁使同 chat 帧严格排队：建卡窗口（≈0.5-1s）内的帧延迟到达属预期（原语义即串行合并，无功能损失）
- `submit_tool_duration_from_thread`（装饰性耗时）不经锁、自带终态守卫，极端并发下耗时标签可能缺失（诚实不伪造，CHANGELOG v2.0.9 P2-8 已知限制同族）

---

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
