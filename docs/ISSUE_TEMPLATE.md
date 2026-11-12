# Feishu Streaming Issue 信息模板

提交入口：<https://gitee.com/Aowen-Nowor/hermes-lark-streaming/issues>

> **先脱敏，再提交。** 不得粘贴 App Secret、tenant/access token、API key、完整 `.env`、`auth.json`、未脱敏 `config.yaml`、`hermes config get --raw` 输出或任何认证头。不要贴无关用户隐私、群成员信息或整段私聊内容。若凭证已经泄露，请立即轮换，不要继续在 Issue 中传播。
>
> 可以保留排障所需的触发/卡片消息 `om_...`、CardKit `card_id`、飞书 `log_id`、错误码、job_id、turn_id 与精确时间线。截图同样要检查是否含 secret 或隐私。

## 标题

<!-- 例：[卡片] clarify 回调后另开新卡；或 [安装] 命名 profile 下 .env 未触发平台启用 -->

## 问题摘要

<!-- 2–4 行说明影响范围、首次出现版本、是否可稳定复现。一个 Issue 只描述一个根因。 -->

## 环境

| 项 | 填写 |
|---|---|
| OS 与版本 | <!-- Windows 11 / macOS xx / Linux 发行版与版本 --> |
| Hermes 形态与版本 | <!-- Desktop / CLI / 服务；`hermes --version` 的实际输出 --> |
| 插件版本 | <!-- `hermes plugins show feishu-streaming-platform` --> |
| 安装来源与分支/commit | <!-- Gitee URL；github_sync；完整或短 commit --> |
| Python 版本 | <!-- 实际运行 Hermes/测试的解释器 --> |
| `lark-oapi` 版本 | <!-- 实际运行时版本 --> |
| profile 类型 | <!-- default / named；无需贴敏感路径 --> |
| 连接模式 | <!-- websocket / webhook --> |
| 部署形态 | <!-- 本机 / 容器 / 远程服务器 / 其他 --> |
| 测试模式 | <!-- mock；或经授权的 FEISHU_E2E_REAL=1 real --> |

可附以下**只读、非机密**命令输出：

```bash
hermes --version
hermes plugins show feishu-streaming-platform
hermes gateway status
hermes config get platforms.feishu.enabled
hermes config get platforms.feishu-streaming.enabled
hermes config get streaming.enabled
```

不要附完整配置 dump 或任何 `--raw` 输出。

## 最小复现步骤

<!-- 从干净状态开始写最小步骤。安装问题给出实际安装/配置命令；卡片问题给出消息类型、是否群聊/私聊、是否流式、是否 clarify/cron/standalone。 -->

1.
2.
3.

复现频率：<!-- 每次 / 偶发（约多少分之一） / 仅一次 -->

若是 real E2E：确认已获当次明确授权，使用专用测试应用与专用测试会话。**不要为了复现 Issue 在生产群或未知会话发测试消息。**

## 期望行为

<!-- 应该看到什么？例如同一张卡续流、内置 feishu 保持关闭、默认只运行 mock。 -->

## 实际行为

<!-- 实际看到什么？保留原始错误文本与错误码，不要改写。 -->

## 时序与关联 ID

<!-- 时间必须含时区；按先后列出首帧、工具/思考、edit/finalize、clarify、重连、停机等关键事件。 -->

| 时间（含时区） | 事件/帧 | 结果与错误码 | 关联 ID |
|---|---|---|---|
|  |  |  |  |

| 标识 | 填写（没有则写“无”） |
|---|---|
| 触发用户消息 `om_...` |  |
| 卡片消息 `om_...` |  |
| 回复引用 parent/root `om_...` |  |
| CardKit `card_id` |  |
| 飞书 `log_id`（可多个，标明对应请求） |  |
| `turn_id` / `job_id` / `clarify_id` |  |
| 相关 HTTP 状态与飞书错误码 |  |

## 脱敏日志与证据

<!-- 只截取故障前后相关时间窗。secret 用 `<REDACTED>`，用户正文可概括；om_、log_id、错误码和事件顺序请保留。 -->

```text
<在此粘贴脱敏后的日志>
```

- [ ] 已检查日志、截图和命令输出，没有 secret/token/认证头。
- [ ] 已保留与故障相关的 `om_`、`log_id`、错误码和时间顺序。
- [ ] 已区分“插件加载/注册成功”和“真实适配器/卡片调用成功”，没有仅凭加载日志下结论。

## 配置摘要（仅非机密项）

<!-- 只列相关键，例如两个平台 enabled、streaming.enabled、thinking_progress、oneshot_cards、clarify_suspend_sec。不要贴 extra.app_secret。 -->

```yaml
<在此填写最小非机密配置>
```

## 已尝试的排查与结果

<!-- 写明执行过的只读检查、mock 测试、回滚或配置变化及实际结果；不要只写“都试过了”。 -->

## 临时绕过方式（如有）

<!-- 例如暂时切回内置 feishu。注明是否已实际验证。 -->

## 补充材料

<!-- 最小 mock 用例、脱敏截图/录屏、相关上游版本差异或文档链接。大日志请使用安全的文件分享方式并确保访问权限。 -->
