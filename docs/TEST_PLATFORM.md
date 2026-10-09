# TEST_PLATFORM.md — V2.0.0 验收文档

> 验收对象：feishu-streaming-platform v2.0.0（refactor/v2.0.0 分支）
> 验收环境：本地审计环境（hermes-agent v0.21.6 源码树 + lark-oapi 1.6.x + Python 3.12）
> 执行工具：`scripts/smoke_v2.py`（可重复）+ pytest 保留测试套件

---

## 1. 测试用例与结果

### 1.1 冒烟 Level 1 — 包结构 / 清单 / 帧分类器（13/13 通过）

| 用例 | 预期 | 结果 |
|---|---|---|
| plugin.yaml kind | `platform` | ✅ |
| plugin.yaml name | `feishu-streaming-platform` | ✅ |
| plugin.yaml version | `2.0.0` | ✅ |
| 平台名推导（loader 规则） | `feishu-streaming` | ✅ |
| answer 流帧分类 | `stream/answer` | ✅ |
| `_interim_send` 标记剥离 | meta 中无残留 | ✅ |
| thinking 气泡分类 | `progress/thinking`（💬 前缀剥离） | ✅ |
| 工具行分类 | `progress/tool` + 工具名提取（`🔧 bash：...` → bash） | ✅ |
| interim 播报分类 | `interim` | ✅ |
| 定稿帧分类（notify） | `final` | ✅ |
| 普通消息分类 | `plain`（走原生路径） | ✅ |
| 防御性输入（None content） | 不抛异常，保守分类 | ✅ |

### 1.2 冒烟 Level 2 — CardKit 引擎（3/3 通过）

| 用例 | 预期 | 结果 |
|---|---|---|
| 流式卡片构建 | schema 2.0 + streaming_mode=true | ✅ |
| 澄清卡片构建 | 交互元素非空 | ✅ |
| FeishuClientConfig | base_url 以 /open-apis 结尾 | ✅ |

### 1.3 冒烟 Level 3 — 真实 hermes 导入链 / 注册协议（12/12 通过）

| 用例 | 预期 | 结果 |
|---|---|---|
| 包导入 + 版本读取 | 2.0.0（自 plugin.yaml） | ✅ |
| 内置 FeishuAdapter 惰性导入 | 真实模块（非占位） | ✅ |
| `register_platform` 官方调用 | name=feishu-streaming + adapter_factory=FeishuStreamingAdapter | ✅ |
| 辅助函数复用内置 | check_fn 等 8 项参数指向内置实现 | ✅ |
| /aowen 注册 | 官方 `register_hook("pre_gateway_dispatch")` | ✅ |
| 继承链完整 | FeishuStreamingAdapter → FeishuAdapter → BasePlatformAdapter | ✅ |
| FrameComposer / clarify / aowen / plugin 导入 | 全链路无 controller/patching 依赖 | ✅ |
| 重写点存在 | send / edit_message / send_clarify | ✅ |
| **零补丁验证** | 内置 `FeishuAdapter.send.__module__` 未被改写 | ✅ |

### 1.4 单元测试套件（584/584 通过）

```
pytest tests/ -q
584 passed in 7.45s
```

保留并适配：cardkit（含澄清卡片转义/i18n/归一化）、state（phase/linear/session/text/tooluse）、config、unavailable_guard、version。
删除（V1 补丁架构专属，随 `patching/`/`controller/` 退役）：monkey_patch / patch / callback_interception / controller / concurrency / gateway_card / v1.2x–v1.8x 版本修复回归 / integration 哨兵。
新增路径：clarify 存储注册表测试改指向 `hermes_lark_streaming.clarify`（语义不变）。

### 1.5 tests/e2e 移植 V2（23/23 通过）

V1 遗留的 `tests/e2e/`（挂在已退役的 StreamingController API 上，改前 2 failed + 20 errors）全部移植到 V2 FrameComposer 驱动：

```
pytest tests/e2e -q
23 passed（mock 模式；real 模式接 FEISHU_E2E_APP_ID/SECRET/CHAT_ID 即用）
```

- `framework.py`：E2ETestRunner 改为驱动 FrameComposer（与 adapter.__init__ 同款接线），start_message/send_segment/feed_answer/feed_reasoning/feed_tool_update/complete 全部经 classify_send/classify_edit 真实分类后进 handle_send/handle_edit
- 覆盖：基本答案流（建卡→分段→edit 全量→两步定稿）、思考+工具面板（含去重）、**建卡前 progress 暂存并入首卡**、20 段长答案 delta 计数、plain/降级与恢复、终态行为（error 定稿/新 turn 新卡）
- 移植中发现并修复 V2 生产缺陷：`clarify.py` 3 处 `..cardkit` 相对导入越界 → 生产澄清卡片必静默降级纯文本；改 `.cardkit` 后 TestClarifySendPath 4 用例全绿

### 1.6 真飞书 E2E 实测（scripts/e2e_v2.py，云服务器 + 真实凭证）

在部署服务器（Ubuntu 26.04 / Python 3.14 / hermes v0.21.6）上用真实飞书应用凭证直驱 FrameComposer + 真实 FeishuClient，七场景全绿（25+ 项断言全 PASS）：

| 场景 | 验证点 | 结果 |
|---|---|---|
| connect | tenant_access_token + send_text_to_chat 直发 | ✅ |
| answer | 建卡 → 打字机流式 → 两步定稿（finalized + 终值落盘） | ✅ |
| progress | 建卡前 thinking/工具行暂存并入首卡 status_lines、重复行去重、cardkit_update 增量 | ✅ |
| interim | 分类断言 + 建卡后并入状态面板 | ✅ |
| full | 仿真实 turn 六步时序（暂存×2 → 建卡带面板 → 流式 → 面板增量 → 定稿） | ✅ |
| clarify | 澄清卡片构建（交互元素非空）+ 直发 + 提交态 | ✅ |
| plain | 分类断言（不进卡片通道）+ 原生 send_text | ✅ |

现场断言摘录：`暂存行并入 status_lines — lines=['💬 正在拆解…', '🔧 bash："echo e2e-progress"']`、`重复工具行去重 — lines=2`、`answer 终值完整 — len=73`、真实卡片 msg_id 全部返回。

## 2. 验收标准核验（对齐 TASK 文档）

| 标准 | 结论 |
|---|---|
| 四通道帧样本分类正确率 100% | ✅（Level 1 全绿） |
| plain 帧行为与内置一致 | ✅（走 `super()` 原生路径，Level 3 零补丁验证佐证） |
| 任何异常不阻断消息投递 | ✅（engine 全部出站路径 try/except → 降级旗标 → 原生） |
| 仓库无 patching/controller 代码引用 | ✅（rg 全量扫描，仅 CHANGELOG 变更说明中提及） |
| 零 monkey-patch | ✅（Level 3 显式断言） |
| 平台插件协议合规 | ✅（kind: platform + 官方 register_platform / register_hook） |

## 3. 已知限制与风险

1. **~~端到端真飞书验证未执行~~ 已完成**：真飞书 E2E 已在云服务器部署环境用真实应用凭证跑通（见 §1.6），CardKit create/stream/close 真实 API 路径全部打通；`tests/e2e/` 已同步移植 V2（§1.5）。遗留：网关全链路（飞书 WS 入站 → agent → 卡片出站）的长时稳定性观察继续在部署环境进行。
2. **/aowen monitor 指标静态化**：V1 由 controller 上报的卡片创建/失败计数在 V2 暂未接线（composer 不依赖 aowen），monitor 卡片显示基础状态。计划 v2.1 通过 composer → metrics 轻量回调恢复。
3. **progress 文本模板解析为启发式**：依赖 hermes locales 的稳定渲染格式（💬 前缀 / emoji 工具行）；格式变化时防御性兜底为 interim 原文展示，不丢失内容。
4. **/aowen 中断提示卡片**：需要"会话运行中"内部状态，V2 未提供（无官方出口），随补丁架构退役。

## 4. 验收结论

**通过**。架构验收（零补丁、协议合规、能力继承、降级保障）全部达成；端到端卡片流已在真实飞书凭证环境实测打通（§1.6），单元 + E2E 套件 584/584 全绿，具备生产替换条件。
