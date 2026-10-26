# API.md — 配置与接口说明

v2.0.0 · 本文档描述 feishu-streaming 平台插件的全部可配置项与对外契约。

---

## 1. 通用说明

- **配置文件**：`~/.hermes/config.yaml`（路径以 `hermes setup` 实际安装为准）
- **插件配置节**：`feishu_streaming`（顶层键；V1 遗留的 `hermes_lark_streaming` 节在注册时自动迁移）
- **环境变量**：与内置 `feishu` 平台完全一致（插件复用其校验与装配逻辑）

## 2. 环境变量

### 必需

| 变量 | 说明 |
|---|---|
| `FEISHU_APP_ID` | 飞书/Lark 应用 ID |
| `FEISHU_APP_SECRET` | 飞书/Lark 应用密钥 |

### 可选

| 变量 | 默认 | 说明 |
|---|---|---|
| `FEISHU_DOMAIN` | `feishu` | `feishu`（国内）/ `lark`（国际） |
| `FEISHU_ALLOWED_USERS` | — | 允许对话的用户 ID 白名单（逗号分隔） |
| `FEISHU_ALLOW_ALL_USERS` | `false` | 允许所有用户（仅开发环境） |
| `FEISHU_HOME_CHANNEL` | — | cron / 通知投递的默认会话 |

其余（webhook 模式、加密键、群规则等）与内置 feishu 平台相同，见 hermes 官方文档。

## 3. Gateway 配置

```yaml
gateway:
  platforms:
    - name: feishu-streaming
      type: feishu-streaming
```

| display / streaming 键 | 建议值 | 作用 |
|---|---|---|
| `display.thinking_progress` | `true` | 思考气泡进入 progress 通道（卡片思考行数据源） |
| `display.tool_progress` | `all` / `new` | 工具进度行（卡片状态区数据源） |
| `streaming.enabled` | `true` | answer 流式通道（卡片打字机数据源） |

## 4. 插件配置（`feishu_streaming` 节）

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `streaming_panel_expanded` | bool | `false` | 流式期状态面板展开 |
| `native_streaming` | bool | `true` | native streaming 通道（v2.0.13）：适配器声明官方 `SUPPORTS_NATIVE_STREAMING`，答案流走 `send_stream_frame` 原生帧（工具边界不封卡，单卡全回合）；`false` 回 edit-based 流 |
| `oneshot_cards` | bool | `true` | 回合外正式投递一次性卡（v2.0.14）：cron 投递族（metadata `job_id`）→ build_cron_card 标题+溯源页脚；回合外 final（notify 无活跃卡）与 ≥200 字无标记长文 → build_gateway_card 静态卡；`false` 全部落回原生路径（v2.0.13 行为） |
| `print_strategy` | str | `"delay"` | 打字机策略（透传 CardKit streaming_config） |
| `print_step` | int | `4` | 打字机步长 |
| `card_ttl_sec` | int | `600` | 卡片会话 TTL（秒），超时自动回收；v2.0.21 下限钳制 60s（负值/极小值会让活跃回合状态被 GC——断卡事故同族） |
| `footer.show_label` | bool | `false` | footer 标签显示 |
| `footer.fields` | list | 见说明 | 页脚字段行（`[["status","elapsed","model"]]` 语法，V1 兼容单层列表）；支持字段 `status/elapsed/model/cost/tokens/cache/reasoning`（无值字段自动省略；v2.0.21 畸形行剔除 + warning + 全畸形回落默认；默认表为 `status/elapsed/model(+reasoning)`——cost/tokens/cache 等在 V2 契约下数据不可达，配置了也自动省略）。`reasoning`（v2.0.19）= 推理等级，需 hermes config `display.show_reasoning: true` 且 `agent.reasoning_effort` 有效（经官方解析器 `resolve_reasoning_config`）；默认字段在条件满足时自动追加，显式配置时不干预 |
| `card_style.text_size.answer` | map | 未配置（零注入） | v2.0.20 正文（answer 元素）字号，完全 opt-in：配置后按 `{default, pc, mobile}` 三档生效，未写的组/档位回落 v2.0.18 字面值（answer→`normal_v2`、aux→`notation`）；值域官方 13 档 token（heading-0/heading-1/…/normal/notation/xxxx-large/…/x-small），非法值回落 normal。落卡为 `config.style.text_size.cus-ans`（s157 真飞书探针定案 CardKit 接受）；未配置时不注入 `config.style`，payload 与 v2.0.18 逐字段一致 |
| `card_style.text_size.aux` | map | 未配置（零注入） | v2.0.20 辅助行字号（面板思考/工具行/警告/页脚/折叠提示），同上三档结构，未写档位回落 `notation`，落卡 token `cus-aux` |

## 5. 出站帧契约（适配器入口）

`FeishuStreamingAdapter` 在官方 metadata 上路由（`_interim_send` 为 Gateway 内部标记，上线前按官方约定剥离）：

| 帧 | 标记 | 处理 |
|---|---|---|
| answer 流首帧 | `metadata["expect_edits"]=true` | 创建 CardKit 卡片并发出 |
| answer 流更新 | `edit_message(card_message_id, ...)` | CardKit 实体流式更新 |
| 定稿 | `edit_message(..., finalize=true)` 或 `metadata["notify"]=true` | answer 终值落盘 + 关流式 + 摘要（无活跃卡时 → 一次性静态卡，v2.0.14） |
| 工具/思考/interim | `metadata["_interim_send"]=true`（+ 文本模板识别） | 卡片状态区（无活跃卡片时暂存并入） |
| cron 投递 | `metadata["job_id"]`（官方标记，scheduler_delivery.py:1458） | 一次性定稿卡：包装头解析（任务名标题行 + job_id 溯源页脚），v2.0.14 |
| 长文无标记 | 无标记且 ≥200 字 | 一次性静态卡（send_message 工具族长文），v2.0.14 |
| 普通消息 | 无标记（<200 字） | 原生路径（继承行为） |

**回复引用（v2.0.22）**：建卡投递时 `reply_to` 锚点为 `om_` 前缀 → 卡片以飞书原生 reply 形态引用触发消息（`reply_card_by_id`，hermes 官方 `_reply_anchor_for_event` 默认锚点 = 触发消息 message_id，私聊/群聊自动生效）；锚点非 `om_`（cron/后台合成 task_id，reply API 必败 99992354）或 reply 失败 → 降级直发（内容永不丢）。流式更新按 card_id 走，与消息发送方式无关。

**连续 terminal 调用（v2.0.22）**：hermes 对 back-to-back terminal 调用省略重复 header（裸围栏块）；面板渲染将裸块归属 terminal，每次调用渲染独立带图标步骤行（`Terminal (xxx ms)`，耗时 = 官方 post_tool_call 钩子实测）。

## 6. 指令

| 指令 | 说明 |
|---|---|
| `/aowen help` | 功能卡片 |
| `/aowen status` | 状态卡（版本/客户端/活跃卡片/架构状态/配置） |
| `/aowen config reload` | 重载配置 |
| `/aowen reset` | 重置指标 |

## 7. 错误码

| 场景 | 行为 |
|---|---|
| CardKit 瞬态错误（网络/限流） | 自动重试（指数退避），重试耗尽 → 本回合降级原生文本 |
| CardKit 300313（元素未就绪） | 定时重试（见 composer/engine） |
| 继承面失配（hermes 重构） | 启动 fail-fast，ERROR 日志列出缺失点 |
| lark-oapi < 1.6.4 | 启动 ERROR/WARNING 指纹（WS 断连风险） |
