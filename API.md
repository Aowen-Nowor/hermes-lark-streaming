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
| `panel_expanded` | bool | `false` | 状态面板默认展开 |
| `streaming_panel_expanded` | bool | `false` | 流式期状态面板展开 |
| `print_strategy` | str | `"delay"` | 打字机策略（透传 CardKit streaming_config） |
| `print_step` | int | `4` | 打字机步长 |
| `flush_interval_ms` | int | `200` | 兼容保留（V2 节流由 hermes 通道自带） |
| `card_ttl_sec` | int | `600` | 卡片会话 TTL（秒），超时自动回收 |
| `max_tool_steps` | int | `20` | 状态区工具行保留上限 |
| `max_reasoning_rounds` | int | `20` | 兼容保留 |
| `footer.show_label` | bool | `false` | footer 标签显示 |

## 5. 出站帧契约（适配器入口）

`FeishuStreamingAdapter` 在官方 metadata 上路由（`_interim_send` 为 Gateway 内部标记，上线前按官方约定剥离）：

| 帧 | 标记 | 处理 |
|---|---|---|
| answer 流首帧 | `metadata["expect_edits"]=true` | 创建 CardKit 卡片并发出 |
| answer 流更新 | `edit_message(card_message_id, ...)` | CardKit 实体流式更新 |
| 定稿 | `edit_message(..., finalize=true)` 或 `metadata["notify"]=true` | answer 终值落盘 + 关流式 + 摘要 |
| 工具/思考/interim | `metadata["_interim_send"]=true`（+ 文本模板识别） | 卡片状态区（无活跃卡片时暂存并入） |
| 普通消息 / cron | 无标记 | 原生路径（继承行为） |

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
