# 更新日志

## v2.0.27（2026-10-27）

| 类型 | 变更 |
|------|------|
| 文档 | 文档结构重组：SKILL.md、CHANGELOG.md 移入 docs/（与 API.md 同层）；CHANGELOG 全史重写为「版本+日期标题 + 全表格」精简格式 |
| 文档 | README 面向人类重写（特性/配置/CI 表格化）；SKILL.md 面向 agent 保留实战结构；API.md 瘦身并去硬编码版本号、补 `clarify_suspend_sec` 键位 |
| 变更 | CI 现代化：sync-from-gitee 改为全分支+tag 强制镜像（github_sync + refactor/v2.0.0，Gitee 唯一事实源，消除 merge commit 污染），测试切 Python 3.14 |
| 新增 | GitHub PR/Issue 事件实时通知飞书（feishu-notify.yml）；Gitee PR/Issue 每小时增量摘要通知飞书（sync 工作流内嵌，actions/cache 存增量状态） |
| 变更 | hermes-integration-test.yml（V1 集成测试，tests/integration 已于 V2 删除且钉死 Python 3.11 不可用）替换为 hermes-release-watch.yml：hermes 上游发新版 → 飞书提醒人工跑 E2E 验证继承面 |
| 清理 | scripts/notify_feishu.py（V1，含 junitxml 解析缺陷）删除，由 scripts/feishu_notify.py 统一承担（github-event / gitee-digest / ci-report / hermes-release 四模式） |
| 变更 | Gitee 发版流水线 .workflow 触发分支 github_sync → refactor/v2.0.0；Release 描述链接指向 refactor/v2.0.0/docs/CHANGELOG.md |
| 修正 | v2.0.0 日期笔误（2026-02-06 → 2026-10-09，以 refactor 分支 E2E 验证提交为准） |

## v2.0.26（2026-10-27）

| 类型 | 变更 |
|------|------|
| 修复 | clarify 等待期流式卡先变「已完成」、回调后另开新卡（hermes 官方边界语义强制 finalize + re-seed 重开新流，插件忠实承接所致）——finalize 帧进入暂停窗口（`clarify_suspend_sec` 默认 1.2s，0=关闭），`send_clarify` 确认边界后**同一张卡**续流至真回合终态，一次澄清回合一张卡 |
| 新增 | 面板暂停行「⏸ Waiting for your choice...」（i18n `clarify_wait`）；暂停/窗口期豁免看门狗 seal 与 GC 回收（hermes clarify 硬超时 3600s 保证终态必达）；turn_id 防跨回合串卡 |
| 修复 | hermes clarify 占位文案（💬 开头单行 ≤40 字符）此前落入面板思考行——暂停路径不落正文/面板 |
| 测试 | 新增 test_v226_clarify_pause.py 17 用例（窗口/确认/到期/同卡续流/段拼接/双 clarify/防串/豁免），全量 771 passed |

## v2.0.25（2026-10-27）

| 类型 | 变更 |
|------|------|
| 修复 | emoji 开头的 answer 累积快照被整帧误判为进度帧、逐行拆进折叠面板（cron 回合 12 行泄漏事故；缺陷自 v2.0.13 内容依赖型潜伏）——`_split_native_composite` 重写为**行语法扫描**：官方进度帧行仅四类（💬 思考块/围栏命令块/通知轨前缀行/具名工具行），answer 正文行任一不合法即整帧按 answer |
| 修复 | 官方流式光标 ` ▉` 重组前剥除（面板行/正文不再粘光标）；工具行判定限定 emoji 前置（防 "hello world" 误命中工具名） |
| 测试 | 新增 test_v225_native_answer_guard.py 36 用例（事故快照逐字复原/扫描器正反/拆分器两路径/引擎集成），全量 754 passed |

## v2.0.24（2026-10-26）

| 类型 | 变更 |
|------|------|
| 修复 | 纯 answer 回合卡片从建卡到定稿挂空 "agent loop" 面板壳——面板懒渲染：仅状态行非空时渲染，首个状态行到达时全量更新补插（`panel_on_card` 旗标以建/更卡产物实测赋值） |
| 变更 | finalize 不再对从未有过面板的卡发起 delete_elements（省 API、免日志噪声；旗标错位防御性删除保留） |
| 文档 | Clarify 卡片回调控制台配置指引（开发者后台 → 事件与回调 → 卡片交互回调 → 长连接接收；非插件缺陷，代码链路已就绪）；badge 2.0.24 / tests 718 |
| 测试 | 新增 test_v224_lazy_panel.py 9 用例 + 存量断言同步，全量 718 passed |

## v2.0.23（2026-10-26）

| 类型 | 变更 |
|------|------|
| 变更 | 真飞书 E2E 现代化：版本水印动态化（跟随 plugin.yaml 单一事实源）；新增 reply 场景（第 11 个：群聊引用 + 私聊 `FEISHU_E2E_DM_CHAT_ID` 双验证 + 伪锚点降级直发）；full 场景补 v2.0.22 裸围栏块帧；产品代码零变更 |
| 定案 | s170 探针消息为「卡片外独立文本」属机制正解：飞书 reply 锚点本就是一条独立消息，锚点内容不进卡片体——测试设计整改为锚点自解释 + connect/reply 连跑零噪音 |
| 文档 | README 新增「从 V1（1.x）升级」章节；SKILL 新增第 6 节 V1→V2 迁移（安装方式甄别/前置核查/三处配置/行为差异速查/迁移失败速查）；**仓库默认分支 github_sync 仍为 V1 1.8.4** 陷阱警示（clone 不带 `-b refactor/v2.0.0` 会装成 V1） |
| 测试 | 新增 test_v223_e2e_refresh.py 5 用例锁定脚本结构，全量 707 passed |

## v2.0.22（2026-10-26）

| 类型 | 变更 |
|------|------|
| 新增 | 卡片回复引用触发消息（私聊/群聊）：`om_` 锚点 → 飞书原生 reply 形态发卡（hermes 官方 `_reply_anchor_for_event` 默认锚点接线）；锚点非 `om_`（cron/后台合成 id，reply API 必败 99992354）或 reply 失败 → 降级直发，内容永不丢 |
| 修复 | 连续 terminal 调用（hermes 省略重复 header 的裸围栏块）此前渲染为无图标灰字行、耗时钩子挂不上——裸块归属 terminal，每次调用渲染独立带图标步骤行（`Terminal (169 ms)`，耗时 = 官方 post_tool_call 钩子实测） |
| 测试 | 新增 test_v222_reply_and_terminal.py 11 用例，全量 702 passed |

## v2.0.21（2026-10-16）

| 类型 | 变更 |
|------|------|
| 修复 | send_message 短 emoji 通知升格后回合外投递永等不到 answer 帧 → 跑马灯永挂卡——新增**孤儿卡看门狗**（`loop.call_later` 120s 复查：静默达标优雅落定，活跃回合自动重新武装不误伤） |
| 修复 | clarify 线程 fallback 协程只创建不 await（断连窗口期点澄清卡 = 假确认永久挂起）——核心动作同步执行，确认卡更新尽力而为 |
| 修复 | close_streaming 失败上抛致降级/重复消息——对齐 `_seal_footer` 装饰性语义 |
| 修复 | `card_ttl_sec` 负值无钳制（GC 恒真断卡同族）——下限钳制 60s；`footer.fields` 畸形行静默消失——逐行校验 + warning + 全畸形回落默认 |
| 修复 | 未闭合围栏内 `#` 行被标题降级误改写（逐行围栏状态机）；`_split_long_text` 切点落围栏中间（切点围栏闭合校验）；重启/过期后点澄清卡零反馈（「已过期」提示卡）；思考行冻结截断无标记（「……（已截断）」写回缓冲） |
| 兼容 | lark-oapi ≥1.7.x 回调类路径迁移（`CallBackCard/P2CardActionTriggerResponse` 迁至 event.callback.model）——多候选路径加载，消除 ImportError 静默退化 |
| 清理 | V1 死代码 −640 行（11 个面板组装器/错误面板函数 + 16 个死词条），AST 引用计数定案 + 防回引测试 |
| 测试 | 新增 test_v221_lifecycle_ux.py 29 用例，全量 691 passed |

## v2.0.20（2026-10-16）

| 类型 | 变更 |
|------|------|
| 变更 | `card_style.text_size` 改**完全 opt-in**：未配置零注入，卡片 payload 与 v2.0.18 逐字段一致（结构性保证观感零变化）；配置后 answer/aux 两组各 `{default, pc, mobile}` 三档生效，未写档位回落 v2.0.18 字面值（answer→`normal_v2`、aux→`notation`） |
| 测试 | 30 用例重写为 opt-in 语义，全量 674 passed |

## v2.0.19（2026-10-16）

| 类型 | 变更 |
|------|------|
| 新增 | footer `reasoning` 推理等级字段：`display.show_reasoning: true` 且 `agent.reasoning_effort` 有效时默认自动追加（经官方解析器 `resolve_reasoning_config`；显式配置 footer.fields 时不干预） |
| 新增 | 卡片自定义字号 `card_style.text_size`（官方 `config.style.text_size` 三档 token 机制，token 名 `cus-ans`/`cus-aux`；s157 真飞书探针定案 CardKit 接受） |
| 定案 | footer 成本族字段（cost/tokens/cache）V2 官方契约数据不可达，渲染框架保留待数据通道开放；卡片复制 = 飞书平台能力边界（`enable_forward` 官方默认开启，零代码） |
| 测试 | 新增 28 用例，全量 672 passed |

## v2.0.18（2026-10-16）

| 类型 | 变更 |
|------|------|
| 兼容 | hermes-agent v0.22.2 兼容性验证零代码缺陷定案：适配器基类/native streaming 契约/reasoning hooks/cron 投递/平台注册协议全部零变化（服务器解压 diff + 实测 import） |
| 兼容 | plugin.yaml 补 `python_dependencies`（PEP 508）声明——v0.22.x PM「conflicts are refused」契约收紧的机制化收尾（v2.0.13 部署事故闭环） |
| 清理 | docs/API.md 删 4 行幽灵配置键（v2.0.17 已删死键的文档漏同步）；badge tests 修正为 644 |

## v2.0.17（2026-10-14）

| 类型 | 变更 |
|------|------|
| 修复 | P0「一个问题拆 3 张卡」：流首帧模型 content 尚空 → `cardkit_stream_element` 报 99992402 → 连锁降级建卡（s152 真飞书探针二分定案：stream_element content 必填非空）——`_stream_answer` 与 `finalize_card` 两处空串守卫（累积快照语义下空 = 无新内容，跳过无损） |
| 清理 | V1 遗留全仓清理（用户指令「代码干净无冗余」）：死函数×5 + 死配置属性×12（连带孤儿机制）+ 展示死键 + 死测试×28 + 幽灵文件（tests/integration/ 仅剩 __pycache__、无引用截图），AST 全仓交叉引用扫描定案 + 12 属性防回引测试 |
| 测试 | 全量 644 passed |

## v2.0.16（2026-10-13）

| 类型 | 变更 |
|------|------|
| 修复 | 面板视觉四项（用户截图验收）：Terminal/shell 工具行齿轮 → `code_outlined`；流式期最后一组思考轮橙色、定稿收口重渲染全绿（红色不实施——帧流无失败数据源不伪造，失败语义由 ⚠️ 橙色警告行承载）；⚠️/🚨 警告族通报行橙色醒目；cron 卡头部去图标 |
| 定案 | 官方 batch_update 无 update_elements action（s146 真飞书探针，code=10002）→ 面板收口采用 delete+add 同批次重建 |

## v2.0.15（2026-10-13）

| 类型 | 变更 |
|------|------|
| 修复 | 子代理失败/超时通知 >300 字符且活跃卡在场时孤挂原生 post 文本——平台通知轨识别（官方固定前缀 `⚠️ Subagent failed/timed out` 豁免 300 字符上限）：有活跃卡并入面板（warning_outlined 通报行），无活跃卡 early-create 回合卡 |

## v2.0.14（2026-10-13）

| 类型 | 变更 |
|------|------|
| 新增 | cron 定时投递卡片化：官方 `metadata["job_id"]` 精确识别（零启发式）→ 一次性定稿卡（任务名标题行 + markdown 正文 + job_id 溯源页脚）；成功/失败/队列重发三 lane 同源覆盖 |
| 新增 | 回合外 final（notify 无活跃卡）与 ≥200 字无标记长文 → 一次性静态卡；`oneshot_cards` 一键回原生 |
| 修复 | 部署环境影子副本事故：`~/.hermes/plugins/` 顶层历史备份目录与主目录同 plugin key，官方加载器择旧导致部署无效——生产部署须保证插件 key 唯一 |

## v2.0.13（2026-10-12）

| 类型 | 变更 |
|------|------|
| 新增 | **native streaming 通道**：适配器声明官方 `SUPPORTS_NATIVE_STREAMING`，答案流走 `send_stream_frame` 原生帧——工具边界不再封卡，单卡全回合；任何帧失败自动回落 edit/send；`native_streaming: false` 一键回 edit-based |
| 修复 | 纯 answer 回合空面板壳（finalize 清理）|
| 已知 | 复合帧拆分启发式自本版潜伏（answer 以 emoji 开头才触发，v2.0.25 行语法扫描根治）；PM 运行时依赖环境漂移面暴露（v2.0.18 python_dependencies 声明收尾） |

## v2.0.12（2026-10-12）

| 类型 | 变更 |
|------|------|
| 修复 | 折叠面板思考正文 markdown 渲染（`**bold**`/`*italic*`/`` `code` `` 生效，灰色小字号）——此前与命令摘要共用 plain_text 渲染，星号原样显示 |
| 变更 | 仓库根目录布局契约化：运行时 .py 仅剩 `__init__.py`/`__main__.py`（官方加载器以插件目录为包导入，子包布局对加载器不可见） |
| 变更 | `requires-python = ">=3.11,<3.15"` 精确镜像 hermes 官方（唯一受支持运行时 3.14） |

## v2.0.11（2026-10-12）

| 类型 | 变更 |
|------|------|
| 修复 | P0 一轮对话生成多张孤儿卡：burst 帧在建卡窗口内各自建卡（fallback 切换瞬间多 lane 亚秒并发）——**per-chat 帧串行化**，`handle_edit` 接入同一互斥锁（防定稿与建卡交错） |
| 变更 | 仓库层级梳理：API.md 移入 docs/、CHANGELOG 移出 docs/（本版后 v2.0.27 再迁回）、README 补徽章与仓库结构 |

## v2.0.10（2026-10-12）

| 类型 | 变更 |
|------|------|
| 修复 | P1 status 通报建幽灵卡（网关终态错误通报无 metadata 标记 → 升格启发式建占位卡永不定稿）——status 通道语义层识别（官方 `send_or_update_status` 契约实现） |
| 修复 | 通报行裸 markdown 兜底 → 剥 emoji 头 + 官方图标样式；默认工具图标 → `code_outlined`（官方清单 2026-10 版实测在列，19 个面板图标 token ∈ 官方清单冻结集测试防线） |

## v2.0.9（2026-10-12）

| 类型 | 变更 |
|------|------|
| 修复 | P0 300317 sequence 冲突整回合降级纯文本：网络读超时重试用同一 seq，首次请求实际已达服务端时重发必撞——seq 对齐重发自愈（真飞书 sequence 实验：重放/回退必现、+1 自愈、跳号允许） |
| 修复 | P0 长回合（>10 分钟）断卡：`_gc` 运算符优先级 bug 使活跃回合建卡满 TTL 即被回收——修复回收条件 |
| 修复 | P1 幽灵卡竞态（reasoning 尾 flush 执行时无复查重建已终态卡——执行前复查）；P1 双 finalize 重复插页脚（幂等守卫 + finalized 先行置位） |
| 修复 | P2 200 元素上限接线（超阈值裁面板最旧子项 + 折叠提示）；工具耗时配对三缺陷；插件命名空间日志级别 INFO（生产排障证据链） |
| 清理 | 删 `state/`、`flush/` 遗留包 + 338 个遗留测试；全量 508 passed，ruff 净减 50 |

## v2.0.8（2026-10-11）

| 类型 | 变更 |
|------|------|
| 修复 | 面板质量四连修（用户四问题逐项定案）：多行思考吸附（💬 行与其后内容不再被打碎）；答案复读过滤（网关中继把可见正文前 500 字中继成 💬 行的复读消除）；**新增 tool_timing.py**（官方 post_tool_call 钩子 → 单工具耗时 `Terminal (2.3 s)`）；💾 后台通知永不建卡（非对话通知识别） |
| 测试 | 新增 32 用例，全量 712 passed |

## v2.0.7（2026-10-11）

| 类型 | 变更 |
|------|------|
| 新增 | DeepSeek reasoning 隐藏流 → 面板「第 N 轮」思考记录：官方 `on_stream_delta`/`on_stream_end` 观察者钩子，按会话累积 + 节流（1.5s/400 字符）+ 间隔 >8s 断轮重置；线程安全回灌（`submit_progress_from_thread`） |
| 兼容 | 需 config `plugins: {stream_reasoning_deltas: true}`；未配置/失败仅降级为无思考行 |

## v2.0.6（2026-10-10）

| 类型 | 变更 |
|------|------|
| 修复 | E2E 合成帧 ≠ 正式对话真实帧格式（hermes 真实 progress 模板从未被结构化解析，面板原样渲染 emoji 原文）——**新增 progline.py**：terminal 围栏代码块、中/英双语动词行反查工具名；面板官方图标 + 彩色工具标题 + 缩进灰字命令 |
| 测试 | 新增 test_v206_real_formats.py 22 用例回归防线，全量 663 passed |

## v2.0.1（2026-10-10）

| 类型 | 变更 |
|------|------|
| 修复 | 进度帧上下文升格：edit-transport 平台工具进度气泡首条是裸 send（无 metadata 标记）→ 落 CH_PLAIN 产生独立纯文本气泡——按上下文升格进面板 |
| 修复 | 进度快照按行拆分 + 集合去重幂等合并（accumulate 全量快照不再膨胀 blob）；进度帧不回传卡片 message_id（防网关把工具行覆写进答案区） |
| 新增 | `display_tier="high"` 显式声明（防平台默认漂移静默关闭流式）；建卡/定稿生命周期 INFO 日志 |

## v2.0.0（2026-10-09）

| 类型 | 变更 |
|------|------|
| 变更 | **架构重构：standalone 补丁插件 → 正规 platform 插件（零 monkey-patch）**：`FeishuStreamingAdapter` 继承内置 `FeishuAdapter`（能力零损失），新增 composer 帧重组引擎（官方 metadata 四通道分类 → CardKit 生命周期，失败自动降级原生文本永不丢消息）；clarify 卡片闭环正式化；删 `patching/`、`controller/`；平台名 `feishu-streaming` 与原生 `feishu` 二选一，随时回滚 |
| 修复 | 真飞书 E2E 验证随修五处真实缺陷：建卡前占位状态未排除（整 turn 降级）、`_create_card` 丢弃 pending_status（首卡面板恒空）、clarify 相对导入越界（生产澄清卡必静默失效）、`type()` 重建类借用方法 `__class__` cell 指向旧基类（改为导入期绑定真实基类）、tests/e2e V2 移植 |
| 验证 | 云服务器（Ubuntu 26.04 / Python 3.14 / hermes v0.21.6）真飞书 E2E 七场景 25+ 断言全 PASS，全量套件 585/585 |
