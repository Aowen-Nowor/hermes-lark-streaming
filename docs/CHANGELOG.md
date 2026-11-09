# 更新日志

## v2.0.32（2026-11-09，未发布）

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

## v2.0.31（2026-11-07，未发布）

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
