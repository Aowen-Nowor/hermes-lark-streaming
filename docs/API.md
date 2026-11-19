# API.md — 配置与当前对外契约

> 版本以 `plugin.yaml` 为单一事实源。本文对应 **v2.0.35（2026-11-12）**。安装与人类操作见 [`../README.md`](../README.md)，Agent 执行见 [`SKILL.md`](SKILL.md)。

## 1. 标识与注册契约

| 项 | 契约 |
|---|---|
| 仓库 / 主分支 | `https://gitee.com/Aowen-Nowor/hermes-lark-streaming.git` / `github_sync` |
| 插件 ID | `feishu-streaming-platform` |
| 平台名 | `feishu-streaming` |
| 内置对应平台 | `feishu` |
| 插件形态 | 独立 `kind: platform` 插件；`FeishuStreamingAdapter` 继承内置 `FeishuAdapter` |
| 扩展方式 | 官方 `ctx.register_platform(...)`；零 monkey-patch，`provides_hooks: []` |
| 出站职责 | 将内置出站消息/编辑重组为 CardKit 流式卡、状态面板与一次性静态卡 |
| standalone | 官方 standalone sender 构造本插件适配器并调用其 `send()`，不绕回内置纯文本发送 |
| 注册副作用 | 只做 SDK 门槛检查与注册；不自动创建、注入或迁移 `config.yaml` / `.env` |

V1 的显式 migration/cleanup 入口不是注册期行为。V2 的可选配置由用户创建，凭证由 Hermes 官方凭证链提供。

## 2. 路径、凭证与平台启用

### 2.1 当前 profile 路径

```bash
hermes config path
hermes config env-path
```

- 代码使用 `hermes_constants.get_hermes_home()` 与 `hermes_home_key()`，遵循 context-local profile → `HERMES_HOME` → 平台默认目录。
- 配置缓存按 profile home 隔离；不得把默认 profile 的路径、环境变量或缓存用于另一个 profile。
- Windows/Linux/macOS 使用同一契约；不得写死用户名、盘符、安装 ID 或 generation 路径。

### 2.2 凭证链

- 主流程：`hermes config env-path` 返回的当前 profile `.env`。
- 运行时通过官方 scoped secret 读取；插件配置读取使用 `gateway.platforms._shared.get_scoped_secret`，不是跨 profile 的裸 `os.environ` 查找。
- 平台注册声明官方 `EnvCred` 与 `env_enable_credentials=("FEISHU_APP_ID", "FEISHU_APP_SECRET")`，将 env 映射到 `PlatformConfig.extra.app_id/app_secret`，并桥接 domain、connection mode、webhook 安全参数与 home channel。
- **不要求、也不建议把 `app_secret` 写入 `config.yaml`。** 旧 YAML 读取仅为 V1 兼容，不是新安装主流程。
- `hermes plugins install ... --yes-deps --enable` 只自动确认依赖与启用插件；凭证仍需交互式安装提示或预先配置 `.env`。

两个飞书平台共享凭证时必须显式二选一：

```bash
hermes config set platforms.feishu.enabled false
hermes config set platforms.feishu-streaming.enabled true
hermes config set streaming.enabled true
```

显式 `platforms.<name>.enabled: false` 优先于 env-enable。CLI 会把 `gateway.platforms.*` 规范化到顶层 `platforms.*`，且顶层优先；不要同时维护两种形状。

## 3. 跨平台路径与 profile

所有路径以当前 Hermes profile 为准。使用 `hermes config path` 和 `hermes config env-path` 获取配置位置，不要在脚本、文档或 issue 中写死 `~/.hermes`、`C:\\Users\\...` 或 `/home/...`。多个 profile 存在同名插件副本时，先逐份核对分支、版本和本地改动，再执行同步。


### 3.1 平台与显示键

| 键 | 默认/建议 | 说明 |
|---|---|---|
| `platforms.feishu.enabled` | 使用本插件时 `false` | 内置平台开关，防止双回复 |
| `platforms.feishu-streaming.enabled` | `true` | 本插件平台开关 |
| `streaming.enabled` | Hermes 默认 `false`；建议 `true` | 全局流式总开关；平台覆盖只能收窄 |
| `display.platforms.feishu-streaming.streaming` | 未设置，跟随全局 | `false` 可关闭本平台流式预览 |
| `display.tool_progress` | 注册 `display_tier="high"`，为 `all` | 工具进度通道；`off` 关闭 |
| `display.platforms.feishu-streaming.thinking_progress` | `false` | `true` 允许实时思考进度进入面板 |
| `display.show_reasoning` | Hermes 配置 | 推理显示；有效 effort 可使默认页脚追加 `reasoning` |
| `display.platforms.feishu-streaming.interim_assistant_messages` | high tier 为 `true` | 对话性 interim 入面板；非对话通知另有分类 |

### 3.2 环境变量

| 变量 | 默认/必填 | 说明 |
|---|---|---|
| `FEISHU_APP_ID` | **必填** | 应用 ID |
| `FEISHU_APP_SECRET` | **必填** | 应用密钥，敏感 |
| `FEISHU_DOMAIN` | `feishu` | `feishu` / `lark` |
| `FEISHU_CONNECTION_MODE` | `websocket` | `websocket` / `webhook` |
| `FEISHU_ENCRYPT_KEY` | 空 | webhook 加密键，敏感 |
| `FEISHU_VERIFICATION_TOKEN` | 空 | webhook 验证令牌，敏感 |
| `FEISHU_HOME_CHANNEL` | 空 | cron/通知默认 chat ID |
| `FEISHU_ALLOWED_USERS` | 空 | 逗号分隔 open_id 白名单；`allowlist_suppresses_pairing=True` |
| `FEISHU_ALLOW_ALL_USERS` | `false` | 允许所有用户，生产慎用 |
| `FEISHU_ALLOW_BOTS` | `none` | `none` / `mentions` / `all` |
| `FEISHU_GROUP_POLICY` | `allowlist` | `allowlist` / `open` / `disabled` |
| `FEISHU_REQUIRE_MENTION` | `true` | 群内是否要求 @机器人 |
| `FEISHU_REACTIONS` | `true` | 内置 reaction 开关 |
| `FEISHU_WEBHOOK_HOST` | `127.0.0.1` | webhook 监听地址 |
| `FEISHU_WEBHOOK_PORT` | `8765` | webhook 监听端口 |
| `FEISHU_WEBHOOK_PATH` | `/feishu/webhook` | webhook 路径 |
| `FEISHU_BOT_OPEN_ID` / `FEISHU_BOT_USER_ID` / `FEISHU_BOT_NAME` | 自动获取 | 特殊迁移覆盖项，通常留空 |

### 3.3 `feishu_streaming` 配置节

所有键均可缺省；未配置节时使用下表默认值，注册不会自动创建该节。

| 键 | 类型 | 默认与约束 | 说明 |
|---|---|---|---|
| `streaming_panel_expanded` | bool | `false` | 流式状态面板默认是否展开 |
| `print_strategy` | str | `delay`；允许 `fast/delay` | 透传 CardKit streaming config，非法值回退 `delay` |
| `print_step` | int | `4`；钳制 `1..10` | 打字机步长 |
| `card_ttl_sec` | int | `600`；最低 `60` | 活跃卡状态 TTL，超时回收 |
| `watchdog_idle_sec` | float | `300`；钳制 `60..1800` | 无活动卡看门狗阈值，超时执行 best-effort 收口 |
| `oneshot_cards` | bool | `true` | 一次性静态卡总开关；`false` 回内置原生路径 |
| `clarify_suspend_sec` | float | `1.2`；`0` 关闭 | 等待澄清边界的窗口；到期无确认则真定稿 |
| `footer.show_label` | bool | `false` | 页脚字段标签 |
| `footer.fields` | list | `[[status,elapsed,model]]` | 支持 `status/elapsed/model/tokens/context/api_calls/history_offset/compression_exhausted/cache/cost/reasoning`；兼容单层列表，畸形行剔除，全畸形回退默认 |
| `card_style.text_size.answer` | map | 未配置 | 正文 `{default,pc,mobile}`；启用后缺档回退 `normal_v2`，token `cus-ans` |
| `card_style.text_size.aux` | map | 未配置 | 辅助行三档；启用后缺档回退 `notation`，token `cus-aux` |

补充约束：

- 未配置 `card_style` 时不注入 `config.style`。
- 统计字段只有在官方帧提供对应数据时才显示，无数据自动省略；`cost/tokens/cache/compression_exhausted` 在当前官方 V2 帧契约中通常不可达，默认页脚不收录它们。
- `display.show_reasoning=true` 且能解析出有效 effort 时，默认页脚为 `status/elapsed/model/reasoning`。
- `native_streaming` 已退役；V1 的 `linear/panel_expanded/flush_interval_ms/answer_flush_interval_ms/feishu_base_url/show_reasoning/gateway_cards/max_tool_steps/max_reasoning_rounds` 不是有效 V2 配置。
- Clarify 在本平台按“一次调用一个问题”运行；多题调用由 Hermes 串行成多张卡。单选走内置按钮卡，多选走 CardKit 2.0 `multi_select_static` 表单，开放题走 `input` 表单；`multi_select=true` 必须提供 `choices`。

非机密示例：

```yaml
platforms:
  feishu:
    enabled: false
  feishu-streaming:
    enabled: true
streaming:
  enabled: true
display:
  platforms:
    feishu-streaming:
      thinking_progress: true
feishu_streaming:
  streaming_panel_expanded: false
  print_strategy: delay
  print_step: 4
  card_ttl_sec: 600
  watchdog_idle_sec: 300
  oneshot_cards: true
  clarify_suspend_sec: 1.2
  footer:
    show_label: false
    fields:
      - [status, elapsed, model]
```

## 4. 出站帧契约

当前生产声明 `SUPPORTS_NATIVE_STREAMING=False`，与内置飞书一样走 edit/send 流。`send_stream_frame` 保留兼容承接与失败回退，但不是网关优先选择的通道。

| 输入 | 官方标记/条件 | 当前处理 |
|---|---|---|
| answer 首帧 | `send(..., metadata["expect_edits"]=True)` | 创建 CardKit 卡并发送；同聊天后续帧串行处理 |
| answer 更新 | `edit_message(card_message_id, ..., finalize=False)` | CardKit 流式更新，正文使用全量快照语义 |
| 回合定稿 | `edit_message(..., finalize=True)` 或活跃卡上的 `metadata["notify"]=True` | 写入终值、收口面板/页脚并关闭流式；重复终帧按幂等处理 |
| 工具/思考 | `_interim_send=True` 且匹配官方进度文本 | 进入状态面板；建卡前暂存，建卡后合并更新 |
| 对话性 interim | `_interim_send=True`，非工具/思考 | 活跃卡并入面板；有确认回合锚时可 early-create，无确认回合锚走原生，插件独立通知不创建流式卡 |
| 后台/生命周期通知 | 已识别的通知模板 | 活跃卡并入面板，无卡原生文本；不创建孤儿流式卡 |
| cron | `metadata["job_id"]` | `build_cron_card`：任务名、正文与 job_id 溯源；长内容受分块与卡片预算约束 |
| 无活跃卡 final | `notify=True` 或无预览标记的 final | `oneshot_cards=true` 时发送一次性静态卡 |
| 主动 plain | 无标记、无活跃卡、正文非空 | 一次性静态卡；**没有 200 字门槛**，短回复同样可入卡 |
| 活跃卡中的 plain | 无标记且符合进度/回合语义 | 可升格进面板；不能满足时回内置路径 |
| 预览/进度帧 | `expect_edits` 或 `_interim_send` | 永不进入一次性卡，防止静态卡与后续编辑双贴 |
| `oneshot_cards=false` | 上述一次性场景 | 回内置原生发送路径 |

### 4.1 回复锚点

1. 优先使用显式 `reply_to`；其次读取 `metadata.reply_to_message_id`。
2. 当 `send()` 无显式锚且 metadata 为空时，回退到最近入站用户消息锚；缓存按 chat 隔离、带 TTL 并只接受 `om_` 前缀。
3. cron/notify 等带 metadata 的投递不使用入站锚回退，避免错误引用旧消息。
4. `om_` 锚使用飞书 reply 发卡；非 `om_` 或已确认锚点不可用时直发同一张卡。reply/直发的传输结果未知时返回**不可自动重试**的失败，禁止 Hermes 自动重试和原生二次投递。
5. standalone sender 返回 `platform="feishu-streaming"` 与实际 `message_id`，文本与媒体错误经脱敏后返回。

### 4.2 面板与正文边界

- 纯 answer 回合不创建空面板；首个有效状态行到达后再补插。
- 连续 terminal 的无头围栏块归属于 terminal 步骤，不作为正文重复出现。
- 面板更新支持合并；定稿、停机与看门狗收口前强制冲刷。
- 卡片受元素和字节预算约束，优先裁最旧面板行，不裁 answer；截断提示如实说明“省略”。
- 推理预影只在官方最终 response 提供时搬入面板；插件不新增隐藏 reasoning 钩子，也不伪造工具完成态。

### 4.3 澄清

- 澄清卡与回调使用内置 `_CARD_ACTION_ROUTES` / clarify 协议，操作者校验保持 fail-closed。
- Hermes 在澄清前可能先 finalize 当前流；插件用 `clarify_suspend_sec` 等待紧随的 `send_clarify` 来区分边界与真终态。
- 确认边界后保留流式状态并显示暂停行，回调后拼接 resume 基线，尽量同卡续流。
- 窗口到期无确认则真定稿；`0` 表示关闭暂停语义。turn_id 改变时不跨回合串卡。

## 5. 指令与配置刷新

| 指令 | 说明 |
|---|---|
| `/aowen help` | 功能卡片 |
| `/aowen status` | 版本、客户端、活跃卡、架构与配置状态 |
| `/aowen config reload` | 重读当前 profile 的插件配置；不重载平台注册、`.env` 或适配器连接 |
| `/aowen reset` | 重置指标 |

修改平台开关、凭证或依赖后，需要在网关外部重启。不要从 gateway 内工具调用执行重启。

## 6. 错误与降级契约

| 场景/错误码 | 当前行为 |
|---|---|
| 一般网络/限流瞬态错误 | 有界重试与退避；耗尽后当前回合回内置原生路径 |
| `200850` 流式会话过期 | 重开流式会话并重发；会话年龄达到续期阈值时主动续期 |
| `200860` 内容/卡片过大 | 不可重试错误；前置体积预算与面板裁剪避免无意义重试 |
| `300305` 原子 batch 拒绝 | 建卡和定稿重建都先过元素预算，避免整批失败 |
| `300309` 定稿后迟到 edit | 已定稿的非定稿 edit 幂等成功，不误伤全通道 |
| `300313` 元素未就绪 | 定时重试 |
| `300317` sequence 冲突 | 对齐 sequence 后重发自愈 |
| `99991400` / HTTP 429 | 使用较长退避，避免限频窗口内快速重试 |
| `99992354` 非真实 reply 锚 | 直发降级，不丢内容 |
| `99992402` 空 stream content | 空串守卫跳过 |
| 卡片创建/更新/一次性发送失败 | 已确认未投递时记录原因并回内置原生路径；结果未知时返回 `retryable=False`，禁止自动重试和原生二次投递。已建远端卡尽量先优雅关流 |
| 网关 disconnect | 对未定稿/暂停卡 best-effort 收口，防止永久 spinner |
| 内置继承面失配 | 启动 fail-fast 或明确警告，不静默产出坏适配器 |
| `lark-oapi<1.6.4` | 启动依赖门槛日志；依赖统一由官方插件安装/更新流程解析 |

## 7. E2E 模式契约

| 变量 | 必填/默认 | 说明 |
|---|---|---|
| `FEISHU_E2E_REAL` | 默认非 `1`，即 mock | 只有精确 `1` 是 real opt-in；`true/yes/on` 不启用 real |
| `FEISHU_E2E_APP_ID` | real 必填 | 专用测试应用 ID |
| `FEISHU_E2E_APP_SECRET` | real 必填 | 专用测试应用密钥，敏感 |
| `FEISHU_E2E_CHAT_ID` | real 必填 | 专用测试会话 chat ID |
| `FEISHU_E2E_OPEN_ID` | 可选 | pytest real 私聊场景用户 open_id |
| `FEISHU_E2E_DM_CHAT_ID` | 可选 | 独立 reply 脚本私聊 chat ID |
| `FEISHU_E2E_BASE_URL` | 飞书中国站 API | real API 地址覆盖项 |

只有 `FEISHU_E2E_REAL=1` 与三个 real 必填值同时存在才使用真实 `FeishuClient`；其他情况一律内存 mock。真实 pytest 和独立 `scripts/e2e_v2.py` 都会发消息，必须经用户当次明确授权并在专用测试会话执行。

默认安全验证：

```bash
FEISHU_E2E_REAL=0 python -m pytest tests -q
python scripts/smoke_v2.py
```

Issue 入口：https://gitee.com/Aowen-Nowor/hermes-lark-streaming/issues 。提交时按 [`ISSUE_TEMPLATE.md`](ISSUE_TEMPLATE.md) 提供环境、版本、复现、期望/实际、时序、脱敏日志、`om_` 与 `log_id`，不得贴密钥。
