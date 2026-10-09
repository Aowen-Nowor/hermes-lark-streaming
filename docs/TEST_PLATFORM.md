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

### 1.4 单元测试套件（561/561 通过）

```
pytest tests/ --ignore=tests/e2e
561 passed in 5.40s
```

保留并适配：cardkit（含澄清卡片转义/i18n/归一化）、state（phase/linear/session/text/tooluse）、config、unavailable_guard、version。
删除（V1 补丁架构专属，随 `patching/`/`controller/` 退役）：monkey_patch / patch / callback_interception / controller / concurrency / gateway_card / v1.2x–v1.8x 版本修复回归 / integration 哨兵。
新增路径：clarify 存储注册表测试改指向 `hermes_lark_streaming.clarify`（语义不变）。

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

1. **端到端真飞书验证未执行**：本环境无飞书应用凭证（`FEISHU_APP_ID/SECRET`），CardKit create/stream/close 的真实 API 路径未在本次验收中打通；`tests/e2e/` 框架保留，可在凭证环境一键复验。风险缓解：CardKit API 封装自 V1 生产验证过的 `feishu/client.py` 原样继承，未改协议。
2. **/aowen monitor 指标静态化**：V1 由 controller 上报的卡片创建/失败计数在 V2 暂未接线（composer 不依赖 aowen），monitor 卡片显示基础状态。计划 v2.1 通过 composer → metrics 轻量回调恢复。
3. **progress 文本模板解析为启发式**：依赖 hermes locales 的稳定渲染格式（💬 前缀 / emoji 工具行）；格式变化时防御性兜底为 interim 原文展示，不丢失内容。
4. **/aowen 中断提示卡片**：需要"会话运行中"内部状态，V2 未提供（无官方出口），随补丁架构退役。

## 4. 验收结论

**通过（附条件）**。架构验收（零补丁、协议合规、能力继承、降级保障）全部达成；端到端卡片流需在真实飞书凭证环境做最终复验（E2E 框架已就位），通过后方可用于生产替换。
