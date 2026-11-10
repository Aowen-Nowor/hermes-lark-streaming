# 更新日志

## v2.0.34（2026-11-10）

> 审计方法：用户三症状（2026-11-10 日志包 logs-v33 + hermes-agent@8b50838 源码 + 插件 v2.0.33 代码三方互证，原始帧形状逐字节取证，hermes locales 模板直接核验）→ 按用户裁决收敛修复面（第一原则不注册钩子）→ 实施。

| 类型 | 变更 |
|------|------|
| 修复 | **en locale 工具预览行全部失效（症状②，用户报 tool_search 未识别）**：`_TOOL_PREVIEW_RE` 分隔符类补 ASCII 半角冒号——hermes preview 模板 zh 全角 `：`（locales/zh.yaml:841）、en 半角 `:`（locales/en.yaml:864），bridge 工具（tool_search/tool_describe）无 curated 动词必走 preview 模板，en 环境（用户实际环境）此前全部切不出工具名 → 灰色通报行兑底（"Searching tools · session search past..." 未识别成 tool_search 标题+内容；agent.log:21570 生产实证）。配套 `tool_search` 补官方 search 图标（原落默认 code_outlined 语义不符） |
| 修复 | **网关停机/重启通知建流式卡永久跑马灯（症状③）**：`_NOTICE_RES` 补 gateway.shutdown.* 生命周期通知规则（en+zh，含 cron 中断通告，模板逐字节核验）——通知 `⚠️ Hermes is shutting down — …` 此前命中进度启发式（⚠️ emoji 开头 ≤300）→ 回合外 early-create 流式卡（面板标题误计「agent loop · 1 个工具」同源）→ 网关 ~1.1s 后进程退出、无 finalize → 永久跑马灯（agent.log:21619 card=7706182798772669698 全日志唯一命中，13 起 shutdown notification 全部无 finalized）。归入通知语义：活跃卡并入面板 / 无卡原生文本，永不建卡。进程退出兑底由 v2.0.33 FIX-1 disconnect→seal_all_on_shutdown 承担（plugin-check 回执实证事故时服务器跑 b0c38d0 未含该修复，升级 v2.0.33+ 即得） |
| 已知 | 症状①（Mem0 add 橙色不变绿、折叠面板无内容）按用户裁决主动不修：工具真实完成态（绿/红+耗时）与结果内容仅存在于钩子/结构化帧通道（gateway tool.complete / post_tool_call），本插件零钩子架构（用户裁决：第一原则不注册钩子）——hermes 文本流对 mem0 类工具只发 pending 行 `⚙️ mem0_add...`（route-trace 逐字节实证），结果 90 chars 从不入流（executor 完成日志 vs 卡 status_lines 恒 1 双证）。mem0 类工具行保持 pending 语义为最终行为 |
| 版本 | `plugin.yaml: version 2.0.34`；README 徽章 2.0.34 + tests 771→857（补上 v2.0.33 漏更的测试徽章）；SKILL checkout 示例同步 |
| 验证 | pytest 857 passed（830 基线 + 27 新回归：en/zh preview 行解析与假阳性护栏、生命周期通知 6 模板分类、tool_search 图标/标题、mem0 pending 现状锁定）；ruff 零新增（改动文件仅 3 项存量）；smoke 13/13 |

## v2.0.33（2026-11-09）

> 审计方法：三路并行深审（健壮性/时序竞态、性能、UX+官方语义）产出 16 项发现，逐项 file:line + 生产日志双证据；15 项实施、1 项主动暂缓（见「已知」）。全部改动带回归测试，其中 9 例对修复前代码验证为红（防回归实效）。

| 类型 | 变更 |
|------|------|
| 修复 | **网关停机弃卡（P1）**：适配器补 `disconnect()` 收口路径——先走内置断开再 `seal_all_on_shutdown()`，全部未定稿/暂停态卡（含 clarify 暂停卡）按 `_seal_live_card` 优雅关流式（5s 总预算、逐卡容错）。生产日志实证 3 起「卡创建后进程退出、永不定稿」跑马灯（agent.log:14228/14596/9749），重启后新进程注册表为空连看门狗都无法追踪——此前这类卡只能手动删 |
| 修复 | `_pause_expire` 吞 finalize 异常：异常体改走 `_degrade`+`_seal_degraded_card` 同帧路径（原 final 置位后远端卡永挂流式态且被看门狗豁免） |
| 修复 | 定稿后迟到 edit 撞 300309 误伤全通道：finalized 态的非定稿 edit 直接幂等成功返回（定稿 edit 已携全量文本，迟到帧按构造是重复帧） |
| 修复 | 旧卡 `_by_message` 键残留劫持活跃回合：`_create_card` 覆写前逐出旧键 + `delete_message` 身份校验（原删历史卡会把进行中回合的状态整个踢掉） |
| 修复 | `_gc` 逐出未定稿态先封卡再 pop（原 `card_ttl_sec < watchdog_idle_sec` 配置下确定性跑马灯） |
| 修复 | 看门狗重挂步长改用配置值（原 `call_later` 硬编码 300s，`watchdog_idle_sec` 只生效一半） |
| 优化 | **面板更新合并（P1）**：每聊天 0.2s trailing-edge 合并思考增长/interim/时长回填热路径（生产实证 499s 回合 ~70 次全量快照 ~1.3MB 上行 → 合并落定）；首面板懒插入保持即时；finalize/看门狗/收口前强制冲刷 |
| 优化 | 30KB 卡上限预算护栏：`cardkit_update` 前 27KB 体积预算，超限逐出最旧面板行（答案永不裁）；200860 分类为不可重试（原 3 连快重试白等） |
| 优化 | 进度行解析 memo（原每 patch 全量重解析 ×3）+ 答案归一化每吸收批一次（原每行 O(answer) 全量重扫，25KB 答案实测 6.05ms/帧） |
| 优化 | `_chat_locks` 增长面收敛（gc/forget 时空闲锁逐出——最后一个无上界增长面） |
| 优化 | 定稿尾字节相同答案跳过重传（`last_streamed_answer` 基线；推理前移自然失效）——收口关键路径少一次全量 RTT |
| 优化 | 流式路径 markdown 卫生：答案/面板接入 `optimize_markdown_style` + >20 表格降级（原仅 oneshot 生效，风格噪声逐帧放大上传体积），内容等值缓存零重复变换 |
| 修复 | 200 元素裁剪提示诚实化双语化：「⚡ 还有 N 项已折叠」→「⚡ 前 N 项已省略 / N earlier steps omitted」（内容是删除不是折叠，展开面板即可证伪）+ `_reseal_panel` 入 batch 前过元素上限（原可复超 200 → 300305 整批拒绝面板卡死） |
| 修复 | 面板标题/思考轮次标题 i18n 接线：en_us 用户不再看到「agent loop · 2 轮 · 3 个工具」混排（接线 rounds/tools_count 死键 + round_n 英文标签不再取而弃之）+ 空答案摘要双语 |
| 修复 | oneshot/cron 投递 reply 失效弃卡：`reply_card` 失败改直发群（镜像引擎生产实证「内容永不丢」路径）+ cron 分块上限 10 块双语截断标记（原无限分块必撞 200 元素/30KB 硬上限） |
| 版本 | `plugin.yaml: version 2.0.33`；README 徽章 2.0.33；SKILL checkout 示例同步 |
| 验证 | pytest 830 passed（761 基线 + 69 新回归）；ruff 零新增（净 −1）；smoke 16/16（脚本三处过期断言随修：硬编码版本 2.0.0/独立加载 hack 遇相对导入必炸/零 hook 架构）；CI 侧本轮先行修复（不入版本号）：飞书通知卡 200861 拒收（tag:action → lark_md 链接）、送达失败显红、布局分段化、测试树错位（详见 workflow 头注与 CI 提交） |
| 已知 | 审计#5（clarify 暂停卡跨回合合并判别）主动暂缓——「新消息 vs 澄清回答」判别器需真飞书 E2E 验证，误判会打断 v2.0.26 同卡续流特性；零生产事故，等 E2E 窗口 |

## v2.0.32（2026-11-09）

> 审计方法：压缩包最新代码（v2.0.31 未提交工作区）+ 服务器日志（agent.log 19377 行 / errors.log 6695 行）五轮审计（主链静态 → 时序竞态 → 性能 → 用户体验 → 官方语义核对），全部结论带命令级证据；飞书 2.0 参数以开放平台 cardkit 文档 + 历史真飞书探针（s96/s98/s146/s152）为准，无经验改动。

| 类型 | 变更 |
|------|------|
| 修复 | **200850 流式会话超时无自愈（P0-1 + P1-3 方案B，用户确认）**：官方 CardKit 流式会话寿命 ~10min，生产实证 11-07 两起（12:01 建卡 → 12:11:08 撞码，600s 整；15:08 建卡 → 15:18:35 同族）——长回合第 10 分钟整回合降级纯文本。双层主链修复：① 事后自愈：`call_with_seq_recovery` 捕获 200850 → `client.cardkit_reopen_streaming`（settings `streaming_mode:true`，官方恢复动作，R5 文档核对 + s98 跳号实证）→ 重发同内容；② 主动续期：会话龄 ≥540s 时下一帧更新前先重开（`engine._renew_stream_if_due`），防患于未然。长回合不再中断打字机 |
| 修复 | **降级后远端卡永久流式态（P0-2）**：`_degrade` 此前只置本端旗标，远端卡 streaming_mode 仍开、spinner 永挂直到用户手动删卡（11-07 两起 200850 事故的可见面）。现在降级瞬间 `_seal_degraded_card` 复用 `finalize_card` 优雅收口（answer 终值 + 页脚 + 关流式），与看门狗 seal 同语义；收口失败仅 debug（官方会话超时自愈为最终防线） |
| 修复 | **看门狗误封活跃回合（P1-1，用户确认阈值 300s）**：v2.0.21 硬编码 120s 在生产误封——11-08 16:05 铁证：回合存活、网关上下文压缩 300s+ 静默（16:00:33 压缩启动 → 16:05:01 封卡 → 16:05:33 日志「compression still streaming after 300s」）；11-07 12:19 同族。阈值提到 300s（5×429 冷却 60s 裕度、与压缩首个检查点同量级），新增配置键 `watchdog_idle_sec`（60~1800 钉制，默认 300） |
| 优化 | **长文分块切点策略（P2-3，用户确认）**：`_split_long_text` 切点升级「段落 > 标题行前 > 换行」三级优先，候选点同时过围栏闭合与表格完整双检查（表格从中间切，前后两半都渲染坏）；块大小维持 2400 生产验证值。cron/一次性长卡按小节边界断块，阅读不断层 |
| 优化 | 限频退避分道：99991400（接口频率限制）从 0.1/0.3/0.6 亚秒级改走 1.0/3.0/6.0 秒级退避——限频窗口内三连快重试只会继续撞墙然后放弃；流式更新官方 10 QPS/卡豁免，能撞限频的是 create/batch 突发 |
| 清理 | 入站锚/会话键缓存上限 256（超限淘汰最旧一半）——长命网关进程内无上界增长面收敛 |
| 版本 | `plugin.yaml: version 2.0.32`；README 徽章 2.0.30 → 2.0.32（补上 v2.0.31 漏更的发布三处同步） |
| 验证 | 全量 pytest 回归 + ruff（见发布说明）；200850 自愈/主动续期的真飞书 E2E 待服务器可用后补探针（预期：长回合 >10min 打字机不断流，日志出现 `streaming session renewed` 或 `200850 … reopen streaming`） |

## v2.0.31（2026-11-07，未独立发布——内容随 v2.0.32 入库）

| 类型 | 变更 |
|------|------|
| 行为变更 | 回退原生流式：`SUPPORTS_NATIVE_STREAMING=False`、`supports_native_streaming()`一律`False`，与内置飞书同款 edit-based 流——网关最终拼好的全量 `response`（含推理预影）才能进卡片收口，不再被原生快照吞掉 |
| 修复 | 推理搬运：`composer/_finalize` 从最终 `answer` 最前剥网关推理预影（中英文码块模板均识别），改写成`💬`思考行并入折叠面板，正文干净——与内置“定稿一次性补”的可见面一致，只是落点在面板而不是正文 |
| 修复 | 流式工具双贴：`_split_native_composite`尾把兜底清洗——分隔符存在即官方复合帧信号，尾行里有一条`_is_named_tool_line`或≥50% emoji首行就兜底拆，不再回滚整帧为正文污染答案区 |
| 修复 | 双贴第二根因（route-trace 18:08 实锤）：`🔧 Editing C:/...`等带全路径/长命令的工具行超启发式上限（300/600字）漏判 → 落原生散文本。回合内宽吸收：活跃卡期间无标记 plain 帧一律升格进面板（4000字兜底，notify/非对话通知仍豁免） |
| 修复 | 短回复卡片化：收`≥200`门槛内置“发啥收啥”——无活跃卡的 plain 定稿一律走一次性卡，`reply_to=om_锚点`走引用发表；预览帧（`expect_edits`/`_interim_send`）永不进一次性卡，防静态卡先行+预览编辑双贴 |
| 清理 | 收`≥200`门槛后，面板12行截断与推理2000二次截断一并收掉（200元素飞书硬上限保留，_enforce兜底）；网关侧15行那刀在先，插件不再叠加 |
| 版本 | `plugin.yaml: version 2.0.31`（与本条对齐）；`requires_hermes>=0.23`、`provides_hooks: []`维持 |
| 修复 | 卡片引用锚点：入站锚存根 + 出站回退——取证实锤网关进度/流式通道 `send(reply_to=None, metadata=None)`（anchor-trace 17:52:55），metadata 回路覆盖不了进度首帧 → 卡片永远直发。现在入站事件记锚（`_handle_message_with_guards` 覆写），出站无显式锚且无 metadata 时回退最近入站用户消息 ID（`_resolve_outbound_anchor`，10 分钟 TTL，om_ 前缀校验，cron/notify 带标记投递不回退）——与内置飞书"回复触发消息"同语义 |
| 修复 | 页脚 model 字段会话化：`_resolve_gateway_model` 只读 config.yaml，`/model` 会话切换（model=auto provider=owen）对它不可见 → 页脚显示过期模型。现在入站时记录 session_key（官方 `build_session_key`），定稿页脚优先查会话级 override（`session_store.get_model_override`），无 override 回退 config 值 |
| 验证 | `hermes plugins validate` Passed（仅剩`private core imports`/`dump_all_env`两⚠）；`pytest tests/` 759 passed（2 deselected存量POSIX断言，含新增 test_v233_inbound_anchor 7例）；**真机闭环：卡片引用用户消息（reply_card branch）已验收、页脚 model 显示会话实际模型（auto）已验收**，trace 诊断日志已删 |

## v2.0.30（2026-11-06）

| 类型 | 变更 |
|------|------|
| 修复 | clarify 逻辑归位内置：删除插件自建的 clarify 平行协议（自有状态字典 + `select`/`input_submit`/`retry_submit` token + `_handle_card_action_event` 旁路入口）。根因：Hermes 0.23.0 内置按 `_CARD_ACTION_ROUTES` 同步路由 `hermes_clarify_action`，先于插件入口命中内置 `_handle_clarify_card_action`，其查 `self._clarify_state[clarify_id]` 为空 → `logger.debug` 静默丢弃返回空响应，用户点击零反馈、134s 超时兜底。修复后 `send_clarify` 仅做 `composer.suspend_for_clarify()`（流式卡挂起）再 `super().send_clarify()`，发卡/状态/回调/retire 全走内置协议 |
| 修复 | 继承面探测更新：`_probe_override_surface` 改查 `_CARD_ACTION_ROUTES` 存在性，替代已过时的 `_handle_card_action_event` 入口检查（该方法现仅兜底未命中路由的动作） |
| 行为变更 | clarify 卡形态由插件 CardKit 2.0（下拉框 + 输入框 + 三态）变为内置编号按钮 + Other；自定义输入走内置「Other → 打字下一条」路径（网关拦截文本 clarify 回答，行为一致）；流式卡暂停/续流不受影响 |
| 清理 | `plugin/clarify.py` 成为孤儿模块（adapter 不再引用），本版保留文件、待线上验证后移除，连同 CardKit 的 clarify 卡构建一并退役 |
| 验证 | 真飞书实测：点击 resolve 11.94s（修复前 134.36s 超时），日志 `Feishu clarify button resolved` + 同卡续流；新增回归测试 `tests/test_clarify_delegation.py`；相关 pytest 101 passed（另有 2 个 `test_config.py` POSIX 路径断言在 Windows 下的存量失败，与本次无关） |

## v2.0.29（2026-10-27）

| 类型 | 变更 |
|------|------|
| 说明 | V2 线唯一发版条目，合并 v2.0.0 → v2.0.28 全部迭代；V1（1.8.4）→ V2 升级步骤见 README「从 V1 升级」，agent 手册见 SKILL.md 第 6 节 |
| 重构 | standalone 钩子插件（运行时 monkey-patch GatewayRunner / FeishuAdapter）→ 正规 platform 插件：`FeishuStreamingAdapter` 继承内置 `FeishuAdapter`（WebSocket/webhook、媒体、threads、群门控、cron 投递能力零损失），官方 `register_platform` 协议注册；删除 `patching/`、`controller/`（钩子与 token 级控制器）；平台名 `feishu-streaming` 与原生 `feishu` 二选一，改回即整体回滚，无运行时残留 |
| 重构 | 新增 composer 帧重组引擎：官方 metadata 四通道分类（answer 流 / progress 面板 / interim / final）→ CardKit v2.0 卡片生命周期，per-chat 帧串行化；clarify 交互卡片闭环（select/input/button/retry 四分支 + 操作者授权 fail-closed） |
| 新增 | native streaming 通道：适配器声明官方 `SUPPORTS_NATIVE_STREAMING`，答案流走 `send_stream_frame` 原生帧——工具边界不封卡、单卡全回合；任何帧失败自动回落 edit/send；`native_streaming: false` 一键回 edit-based |
| 新增 | agent loop 状态面板：工具行/思考行实时渲染（真实 progress 模板结构化解析、思考按轮分组、单工具耗时、官方图标 token、思考正文 markdown 渲染）；纯 answer 回合面板懒渲染（无空壳）；DeepSeek reasoning 隐藏流经官方 `on_stream_delta` 观察者钩子回灌面板（`stream_reasoning_deltas: true`） |
| 新增 | 卡片 reply 引用触发消息（私聊/群聊；锚点非 `om_` 或 reply 失败自动降级直发）；cron 定时投递卡片化（任务名标题 + job_id 溯源页脚）；回合外投递与 ≥200 字长文一次性静态卡（`oneshot_cards` 可关）；子代理失败/超时通知并入面板或 early-create 回合卡；页脚 `reasoning` 推理等级；`card_style.text_size` 自定义字号（完全 opt-in，未配置零注入） |
| 新增 | clarify 暂停/续流：澄清等待期卡片保持「进行中」+ 面板暂停行，选择后**同一张卡**续流至定稿，一次澄清回合一张卡（`clarify_suspend_sec`，默认 1.2s，0 = 关闭） |
| 修复 | 稳定性主线（真飞书实测沉淀）：300317 sequence 冲突对齐重发自愈、长回合断卡（GC 回收条件）、burst 帧多孤儿卡（帧串行化）、一问多卡（stream_element 空串守卫）、emoji 开头 answer 快照误拆进面板（行语法扫描根治）、status 通报幽灵卡、孤儿卡看门狗（回合外投递永不挂卡）、clarify fallback 假确认、双 finalize 幂等、200 元素上限裁剪、流式光标剥除、思考行吸附与答案复读过滤、围栏内文本不被误改写、长文切点围栏闭合校验 |
| 兼容 | hermes-agent ≥ v0.22（`SUPPORTS_NATIVE_STREAMING`；启动 fail-fast 探测，失配即报点位）；plugin.yaml 声明 `python_dependencies`（PEP 508）；`requires-python >=3.11,<3.15`（官方运行时 3.14）；lark-oapi ≥ 1.6.4（≥1.7.x 回调类路径迁移已多候选兼容） |
| 清理 | V1 遗留死代码/死配置/死测试分批清除（钩子层约 2700 行 + 后续累计约千行）；删除 `state/`、`flush/` 遗留包 |
| 文档 | 文档体系重组：README（人类向：项目说明 + 部署/升级）、SKILL.md（agent 向：安装/验证/升级手册）、API.md（配置键位 + 出站帧契约）、CHANGELOG.md 同归 docs/；CI：Gitee → GitHub 全分支 + tag 每小时强制镜像、PR/Issue 双向飞书通知、hermes 上游发版飞书监控 |
| 已知 | 页脚 cost/tokens/cache 字段在 V2 数据契约下不可达（配置自动省略）；卡片转发复制为飞书平台默认能力（`enable_forward`），插件不额外干预 |
| 验证 | 真飞书 E2E 11 场景（`scripts/e2e_v2.py`，hermes v0.21.6/v0.22 + Python 3.14）；全量 pytest 771 passed |
