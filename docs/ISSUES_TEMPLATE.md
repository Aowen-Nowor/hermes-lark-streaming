# Issue 提交模板

> 感谢你提交 Issue！无论是 Bug 报告、功能建议还是使用疑问，以下模板都能帮助我们更高效地定位和解决问题。

---

## Issue Title — 标题格式

请使用以下格式命名 Issue：

```
[类型] 简要描述
```

**类型**（对应下方 Category Labels）：

| 类型标签 | 标题前缀 | 说明 |
|----------|----------|------|
| Bug | `[Bug]` | 功能异常、报错、卡片显示错误等 |
| Feature | `[Feature]` | 新功能请求 |
| Improvement | `[Improvement]` | 现有功能的优化建议 |
| Question | `[Question]` | 使用疑问、配置咨询 |

**示例**：
- `[Bug] 统一面板内容不更新`
- `[Feature] 支持自定义卡片主题颜色`
- `[Improvement] 页脚字段支持自定义排序`
- `[Question] 多 Profile 部署如何配置飞书凭据`

---

## Description — 问题描述

<!-- 请清晰描述你遇到的问题或期望的功能 -->

**如果是 Bug**：请描述异常现象——你看到了什么？发生了什么不该发生的事？

**如果是 Feature/Improvement**：请描述你期望的行为——你希望实现什么效果？解决什么痛点？

**如果是 Question**：请描述你的使用场景和困惑。

---

## Steps to Reproduce — 复现步骤

<!-- 仅 Bug 需要，其他类型可删除此节 -->

1. ...
2. ...
3. ...

**复现频率**：[每次 / 偶尔 / 仅一次]

---

## Expected Behavior — 期望行为

<!-- 描述你期望正常情况下的行为 -->

---

## Actual Behavior — 实际行为

<!-- 描述实际发生的行为，与期望行为的差异 -->

---

## Environment — 环境信息

<!-- 请完整填写以下信息，这对排查问题至关重要 -->

| 项目 | 值 |
|------|-----|
| 插件版本 | <!-- 如 1.0.7，可通过 `hermes plugins list` 查看 --> |
| Hermes 版本 | <!-- 如 0.6.x --> |
| Hermes 安装方式 | <!-- 如 pip install / pip editable / 源码运行 / Docker；可通过 `which hermes` 和 `pip show hermes-agent` 确认 --> |
| Python 版本 | <!-- 如 3.11.5 --> |
| 操作系统 | <!-- 如 Ubuntu 22.04 / macOS 14 / Termux --> |
| 飞书/Lark | <!-- 国内版 / 国际版 --> |
| 线性模式 | <!-- 开启 / 关闭（默认开启，统一面板架构） --> |
| 统一面板 | <!-- 是否显示推理内容（display.show_reasoning） --> |

---

## Logs — 日志附件

> ⚠️ **必须提供**：没有日志的 Bug 报告几乎无法定位问题。请务必附上相关日志。

请运行以下命令获取插件相关日志：

```bash
# 自动检测 Hermes Python 路径：
HERMES_PYTHON=$(python3 ~/.hermes/plugins/hermes-lark-streaming/__main__.py python)
grep hermes_lark_streaming ~/.hermes/logs/agent.log | tail -200
```

如果有报错，也可以查看完整日志：

```bash
# 查看最近 500 行日志
tail -500 ~/.hermes/logs/agent.log

# 搜索特定错误码（如 300313、300317、300305、300309）
grep -E "300313|300317|300305|300309|element_limit" ~/.hermes/logs/agent.log | tail -50

# 查看 HLS: 前缀的插件日志（v1.1.0+ 统一前缀）
grep 'HLS:' ~/.hermes/logs/agent.log | tail -100
```

**贴日志时请注意**：
- 移除敏感信息（如 `app_id`、`app_secret`、`img_key` 等）
- 保留时间戳和日志级别
- 如果日志很长，请使用 `<details>` 折叠

---

## Screenshots / Recordings — 截图或录屏

<!-- 如果问题涉及卡片显示异常，请附上截图或录屏 -->

---

## Category Labels — 分类标签

请为你的 Issue 选择一个标签（维护者会最终确认）：

- [ ] **Bug** — 功能异常、报错、行为不符合预期
- [ ] **Feature** — 新功能请求
- [ ] **Improvement** — 现有功能的优化（性能、体验、代码质量等）
- [ ] **Question** — 使用疑问、配置咨询

---

## /aowen 命令自查（推荐）

在提交 Issue 前，建议先在飞书中发送以下命令自查插件状态：

```
/aowen status    — 查看插件配置和凭据状态
/aowen monitor   — 查看运行指标（卡片创建数、API调用数、错误码分布）
```

如果上述命令无响应，说明插件补丁未成功应用或控制器未初始化——请在日志中搜索 `apply_patches` 和 `patches applied` 确认。

---

## Debug Tips — 快速定位

| 症状 | 优先检查 | 关键日志 |
|------|----------|----------|
| 卡片不出现 | 补丁是否成功应用 | `apply_patches`、`GatewayRunner` |
| 内容重复 | 回调是否被双重包装 | `_maybe_wrap_callbacks`、`consumed` |
| 面板思考内容重复（DeepSeek） | 是否同时检查两个回调的 `_hls_wrapper` | `_thinking_wrapper` |
| 会话列表永久显示"处理中..." | `close_streaming` 是否传入 `summary` + `i18n_content` | `close_streaming`、`summary` |
| 流式关闭 (300309) | 300309 fallback 是否生效（长对话>9分钟飞书自动关闭流式） | `300309`、`_fallback_write_answer` |
| 300317 序列冲突 | `_streaming_closed` 守卫是否生效 | `300317`、`_streaming_closed` |
| 页脚早于内容出现 | drain 步骤是否执行 | `drain`、`answer_dirty` |
| 回答内容不显示 | `already_streamed` 处理 + 去重长度追踪 | `already_streamed`、`_stream_consumed_len` |
| 完成卡片被覆盖成"已停止" | `on_interrupted`/`on_aborted` 是否错误触发于 COMPLETING 状态 | `abort old msg=... (after flush wait)` + `on_aborted: ... state=ABORTED` + `linear complete with fallback failed` |

> 详细的架构背景和调试指南请参阅 [SKILL.md](SKILL.md)。

---

*感谢你的耐心填写！完整的信息能大幅缩短问题定位时间。*
