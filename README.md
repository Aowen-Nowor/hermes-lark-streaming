<div align="center">

# hermes-lark-streaming

**Hermes Agent 飞书 CardKit v2.0 流式卡片平台插件**

[![platform](https://img.shields.io/badge/platform-feishu--streaming-teal)](#快速开始)
[![hermes](https://img.shields.io/badge/hermes--agent-%E2%89%A50.23-grey)](https://github.com/NousResearch/hermes-agent)
[![python](https://img.shields.io/badge/python-%3E%3D3.11%2C%3C3.15-informational)](#环境与路径)
[![license](https://img.shields.io/badge/license-MIT-yellow)](#license)

**v2.0.35 · 2026-11-12**

*回答流式入卡 · 工具与思考进入状态面板 · 澄清同卡续流 · 全程零 monkey-patch*

<img src="assets/screenshots/img1.png" alt="流式卡片：agent loop 状态面板、流式回答与定稿页脚" width="560"/>

</div>

---

## 这是什么

`hermes-lark-streaming` 是一个独立分发的 Hermes **platform 插件**。插件 ID 是 `feishu-streaming-platform`，注册的平台名是 `feishu-streaming`，仓库主分支是 **`github_sync`**。

它与 Hermes 内置 `feishu` 的关系是「**内置适配器 + 出站卡片封装层**」：

- 继承内置 `FeishuAdapter` 的连接、入站、媒体、threads、群门控、配对与 cron 等能力。
- 通过官方 `ctx.register_platform(...)` 注册独立平台，不修改 Hermes 源码，不替换或包装核心函数，**零 monkey-patch、零注册钩子**。
- 只重组出站消息：回答进入 CardKit 流式卡片，工具/思考进入状态面板，回合外投递进入一次性静态卡。
- `register()` 只做依赖检查与平台注册，**不自动写入或迁移 `config.yaml` / `.env`**。
- 已确认未投递的卡片失败回退内置原生路径；投递结果未知时返回**不可自动重试**的失败，避免 Hermes 或原生链路盲目重发。切回 `feishu` 即可恢复原生行为。

## 能做什么

| 能力 | 行为 |
|---|---|
| 流式回答 | 使用与内置飞书一致的 edit/send 通道，将回答以打字机效果写入 CardKit 卡片并定稿 |
| 状态面板 | 工具进度、思考与对话性 interim 信息进入同一卡片；纯回答回合不创建空面板 |
| 引用回复 | 有真实 `om_` 锚点时以回复形式发卡；已确认锚点不可用时直发，传输结果未知时禁止自动重试和原生二次投递 |
| 澄清续流 | 澄清等待期暂停卡片，回答后尽量在同一张卡上继续；交互协议复用内置实现 |
| 回合外投递 | cron、命令回复、主动发送与无活跃卡的回合外定稿使用一次性静态卡；普通文本/媒体回合即使无工具调用也建 turn 卡（reasoning 进折叠面板），短回复也不另设字数门槛 |
| standalone 投递 | 无常驻网关的官方 standalone 发送也经过本插件适配器，而非绕回内置纯文本路径 |
| 稳定性 | 每聊天帧串行化、sequence 自愈、流式会话续期、停机收口、孤儿卡看门狗与体积/元素预算 |

## 环境与路径

- Hermes Agent：**`>=0.23`**。
- Python 元数据范围：**`>=3.11,<3.15`**；实际运行时使用 Hermes 管理的 Python。
- 飞书/Lark 自建应用：启用机器人能力，配置 CardKit 与消息权限，并将机器人加入授权测试会话。
- 安装、升级、依赖解析都走 Hermes 官方插件命令；不需要手工克隆或逐环境执行 `pip`。

Windows、Linux、macOS 使用相同的 Hermes CLI。不要猜 profile 目录，也不要把路径写死为某个用户目录：

```bash
hermes config path       # 当前 profile 的 config.yaml
hermes config env-path   # 当前 profile 的 .env
```

文档中的 `$HERMES_HOME` 表示**当前 profile 的实际 Hermes home**。Linux/macOS/Windows Git Bash 可用 `export HERMES_HOME=...`；Windows PowerShell 用 `$env:HERMES_HOME = "..."`。命名 profile 必须使用该 profile 对应的 CLI 上下文，不能复用默认 profile 的凭证或配置路径。

## 快速开始

### 1. 准备 `.env` 凭证（主流程）

先运行 `hermes config env-path`，在该命令返回的文件中配置：

```dotenv
FEISHU_APP_ID=cli_replace_me
FEISHU_APP_SECRET=replace_me
FEISHU_DOMAIN=feishu
FEISHU_CONNECTION_MODE=websocket
# FEISHU_HOME_CHANNEL=oc_replace_me
```

**不要把 `app_secret` 写入 `config.yaml`，不要提交或粘贴真实 `.env`。** 插件通过 Hermes 的 profile-scoped secret 与官方 `EnvCred` / env-enable 契约读取凭证。交互式安装也会询问缺失的 `requires_env` 并写入 `.env`；`--yes-deps` 只回答 Python 依赖确认，不代表提供凭证。

### 2. 用官方安装器安装

```bash
hermes plugins install https://gitee.com/Aowen-Nowor/hermes-lark-streaming.git --yes-deps --enable
```

该命令从真实 Gitee 仓库安装，解析 `plugin.yaml` 声明的依赖并启用 `feishu-streaming-platform`。默认跟踪仓库主分支 `github_sync`；不要把旧的 `master` 或开发分支当作主分支。

### 3. 选择流式平台并开启流式输出

两个飞书平台必须二选一。由于两边都可由同一组 `.env` 凭证自动启用，**显式关闭内置 `feishu`** 是防止双回复的必要步骤：

```bash
hermes config set platforms.feishu.enabled false
hermes config set platforms.feishu-streaming.enabled true
hermes config set streaming.enabled true
```

可选：让实时思考进度进入面板。

```bash
hermes config set display.platforms.feishu-streaming.thinking_progress true
```

对应的非机密配置形状如下；**这里不含、也不需要应用密钥**：

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
```

`feishu_streaming` 配置节完全可选；未创建时使用文末表格中的默认值，安装和注册不会自动注入它。

### 4. 重启并做无消息健康检查

在**网关进程之外的终端**重启网关（桌面端也可用其外部管理方式）：

```bash
hermes gateway restart
hermes gateway status
hermes plugins show feishu-streaming-platform
hermes config get platforms.feishu.enabled
hermes config get platforms.feishu-streaming.enabled
hermes config get streaming.enabled
```

预期：插件为 enabled、内置平台为 false、流式平台为 true、全局流式开关为 true，状态与日志中没有平台校验或适配器创建错误。仅重开桌面窗口不等于重启网关。

首次真实聊天验收属于真实飞书操作，应在获得会话所有者授权的专用测试会话进行；日常安装验证可以先完成下方的 mock 测试。

## 完整配置

### 平台与显示开关

| 配置键 | 推荐/默认值 | 说明 |
|---|---:|---|
| `platforms.feishu.enabled` | 使用本插件时 `false` | 内置平台；与流式平台同时启用会双回复 |
| `platforms.feishu-streaming.enabled` | `true` | 启用本插件平台；也可由完整 `.env` 凭证触发官方 env-enable，显式值优先 |
| `streaming.enabled` | 建议 `true`；Hermes 默认 `false` | 全局流式总开关；per-platform 设置只能收窄，不能绕过它 |
| `display.platforms.feishu-streaming.streaming` | 未设置（跟随全局） | 设为 `false` 可只关闭本平台的流式预览 |
| `display.tool_progress` | 插件 high tier 为 `all` | 工具进度数据源；`off` 会关闭工具进度通道 |
| `display.platforms.feishu-streaming.thinking_progress` | `false` | 设为 `true` 后，实时思考进度可进入状态面板 |
| `display.show_reasoning` | 由 Hermes 配置决定 | 控制推理显示；也影响默认可用的 `reasoning` 页脚字段 |
| `display.platforms.feishu-streaming.interim_assistant_messages` | high tier 为 `true` | 对话性 interim 信息进入面板；后台通知仍按通知语义处理 |

CLI 会将 `gateway.platforms.*` 规范化到顶层 `platforms.*`，且顶层配置优先。本文统一只用顶层形状，避免同一平台出现两份互相覆盖的配置。

### `.env` 与继承的飞书环境变量

| 变量 | 默认/必填 | 说明 |
|---|---|---|
| `FEISHU_APP_ID` | **必填** | 应用 ID；由 Hermes 当前 profile 的 secret scope 读取 |
| `FEISHU_APP_SECRET` | **必填** | 应用密钥；只放 `.env` 或受支持 secret 源，不写 YAML |
| `FEISHU_DOMAIN` | `feishu` | `feishu` 或 `lark` |
| `FEISHU_CONNECTION_MODE` | `websocket` | `websocket` 或 `webhook` |
| `FEISHU_ENCRYPT_KEY` | 空 | webhook 加密键；webhook 模式按内置要求配置 |
| `FEISHU_VERIFICATION_TOKEN` | 空 | webhook 验证令牌；属于敏感信息 |
| `FEISHU_HOME_CHANNEL` | 空 | cron/通知默认投递 chat ID |
| `FEISHU_ALLOWED_USERS` | 空 | 逗号分隔的用户 open_id 白名单；设置后会抑制陌生 DM pairing |
| `FEISHU_ALLOW_ALL_USERS` | `false` | 是否允许所有用户；生产环境不要为了排障长期放开 |
| `FEISHU_ALLOW_BOTS` | `none` | 机器人消息准入：`none` / `mentions` / `all` |
| `FEISHU_GROUP_POLICY` | `allowlist` | 群准入策略：`allowlist` / `open` / `disabled` |
| `FEISHU_REQUIRE_MENTION` | `true` | 群内是否要求 @机器人 |
| `FEISHU_REACTIONS` | `true` | 是否使用内置处理中/失败表情反馈 |
| `FEISHU_WEBHOOK_HOST` | `127.0.0.1` | webhook 监听地址；公网部署需同时正确配置反向代理与安全边界 |
| `FEISHU_WEBHOOK_PORT` | `8765` | webhook 监听端口 |
| `FEISHU_WEBHOOK_PATH` | `/feishu/webhook` | webhook 路径 |
| `FEISHU_BOT_OPEN_ID` / `FEISHU_BOT_USER_ID` / `FEISHU_BOT_NAME` | 自动获取 | 旧迁移或特殊部署的覆盖项；通常留空，由内置适配器补水 |

### `feishu_streaming` 插件配置节（全部可选）

| 键 | 类型 | 默认值与约束 | 说明 |
|---|---|---|---|
| `streaming_panel_expanded` | bool | `false` | 流式期间状态面板是否默认展开 |
| `print_strategy` | str | `delay`；`fast` / `delay` | CardKit 打字机策略，非法值回退 `delay` |
| `print_step` | int | `4`；钳制到 `1..10` | 每次渲染的步长 |
| `card_ttl_sec` | int | `600`；最低 `60` | 活跃卡片状态 TTL，过小会被钳制以防断卡 |
| `watchdog_idle_sec` | float | `300`；钳制到 `60..1800` | 无活动卡片的看门狗静默阈值 |
| `oneshot_cards` | bool | `true` | cron、命令回复、主动 plain、无活跃卡回合外 final 是否使用一次性静态卡；普通文本/媒体回合 final 始终建 turn 卡；`false` 回原生路径 |
| `clarify_suspend_sec` | float | `1.2`；`0` 关闭 | 等待澄清边界的窗口；关闭后澄清前卡先定稿，后续可能另开卡 |
| `footer.show_label` | bool | `false` | 页脚字段是否显示文字标签 |
| `footer.fields` | list | `[[status, elapsed, model]]` | 支持 `status/elapsed/model/tokens/context/api_calls/history_offset/compression_exhausted/cache/cost/reasoning`；无数据字段省略，畸形行回退默认 |
| `card_style.text_size.answer` | map | 未配置 | 正文 `{default, pc, mobile}` 字号；配置后缺省档回退 `normal_v2`，token 为 `cus-ans` |
| `card_style.text_size.aux` | map | 未配置 | 辅助行三档字号；配置后缺省档回退 `notation`，token 为 `cus-aux` |

未配置 `card_style` 时不注入 `config.style`。`cost/tokens/cache/compression_exhausted` 在当前 V2 官方帧数据契约下通常不可达，配置了也会省略。`native_streaming` 及 V1 的 `linear/panel_expanded/flush_interval_ms/answer_flush_interval_ms/feishu_base_url/show_reasoning/gateway_cards/max_tool_steps/max_reasoning_rounds` 不是当前有效配置。

## 从 V1（1.x）迁移到 V2

V1 是 standalone 钩子形态并可能向配置注入内容；V2 是独立 platform 插件。**不能原地换代码。顺序固定为：备份 → 显式 cleanup → 卸载 V1 → 安装 V2 → 配置 → 验证。**

1. **备份并确认 cleanup 可用**
   - 用 `hermes config path` / `hermes config env-path` 找到当前 profile 文件，分别复制到安全备份位置。
   - 记录 V1 版本、安装来源和当前平台开关。
   - 确认 V1 目录中的 `__main__.py` 仍在；cleanup 必须在 V1 代码尚存时运行。若已丢失，先从原备份恢复 V1，不要手工猜删配置。
2. **显式执行 V1 cleanup**

   Linux/macOS/Git Bash 示例（Windows 可将 `python3` 换成 `python`）：

   ```bash
   export HERMES_HOME="${HERMES_HOME:-$(dirname "$(hermes config path)")}"
   V1_DIR="$HERMES_HOME/plugins/hermes-lark-streaming"
   python3 "$V1_DIR/__main__.py" cleanup
   ```

   使用曾运行 V1 的 Python 环境。cleanup 的目标只是 V1 注入的插件配置；它不应删除你的飞书凭证。
3. **卸载 V1**

   ```bash
   hermes plugins uninstall hermes-lark-streaming
   ```
4. **安装 V2**

   ```bash
   hermes plugins install https://gitee.com/Aowen-Nowor/hermes-lark-streaming.git --yes-deps --enable
   ```
5. **重新配置**
   - 将应用凭证放在当前 profile 的 `.env`；不要把旧 YAML 中的 secret 原样保留为新配置依据。
   - 执行快速开始第 3 节，显式设置 `feishu=false`、`feishu-streaming=true`、`streaming.enabled=true`。
   - 按需重新创建 `feishu_streaming` 非机密配置；不要恢复 V1 死键。
6. **验证**
   - 从外部终端重启网关，检查 `gateway status`、`plugins show` 和三个开关。
   - 先跑默认 mock 测试；真实卡片验收只在授权的专用测试会话进行。

**迁移回滚**：先切回内置 `feishu` 并验证原生可用，再卸载 V2，恢复迁移前的配置与 `.env` 备份，按 V1 原始来源和原版本重装，最后重启并验证。不要用未记录版本的“最新 V1”代替原版本。

## 日常更新

```bash
hermes plugins check-updates
hermes plugins update feishu-streaming-platform
```

然后在网关外部重启并复查：

```bash
hermes gateway restart
hermes plugins show feishu-streaming-platform
hermes gateway status
```

- 不要手工改写 Git 工作区或逐个运行环境装依赖；更新与依赖由官方插件流程负责。
- 若更新被安全扫描阻止，先审查变更，不要把 `--force` 当默认选项。
- 本地修改过的安装先备份；官方强制重装对 tracked 修改与未跟踪文件有各自处理规则，不能把更新当作备份。

### 回到指定旧版本

先备份并切到内置 `feishu`，确认原生可用后，使用**完整 40 位 commit SHA**进行官方固定版本重装：

```bash
hermes plugins install https://gitee.com/Aowen-Nowor/hermes-lark-streaming.git --force --ref <完整的40位commitSHA> --yes-deps --enable
```

随后恢复与该版本匹配的非机密配置，重启并验证。固定版本不会被普通 `plugins update` 自动移动；要换版本需显式选择新 SHA。

## 安全卸载与回滚到内置平台

仅想停用卡片时，不必卸载，直接切回内置平台：

```bash
hermes config set platforms.feishu-streaming.enabled false
hermes config set platforms.feishu.enabled true
hermes config set streaming.enabled true   # 内置平台需要流式时保留；否则按原设置恢复
```

在外部终端重启并用 `hermes gateway status` 确认原生平台正常。需要完全卸载时，在以上回滚成功后执行：

```bash
hermes plugins uninstall feishu-streaming-platform
# 仅当你不再需要自己创建的可选插件配置时：
hermes config unset feishu_streaming
```

再次重启并检查状态。**保留 `.env` 中的飞书凭证**，内置 `feishu` 仍需要它们；不要删除整个 profile、其他平台配置或 Hermes 托管环境。

## 测试与真实 E2E

### 默认：mock，绝不因“有凭证”而自动发消息

在仓库目录运行：

```bash
FEISHU_E2E_REAL=0 python -m pytest tests -q
python scripts/smoke_v2.py
```

Windows PowerShell 可这样强制 mock：

```powershell
$env:FEISHU_E2E_REAL = "0"
python -m pytest tests -q
Remove-Item Env:FEISHU_E2E_REAL -ErrorAction SilentlyContinue
```

只有 **`FEISHU_E2E_REAL=1` 精确值** 且 `FEISHU_E2E_APP_ID`、`FEISHU_E2E_APP_SECRET`、`FEISHU_E2E_CHAT_ID` 全部非空时才允许 real 模式；`true/yes/on` 不算授权。凭证单独存在仍然是 mock。

### 危险：真实飞书 E2E 会发送真实文本和卡片

> **Agent 不得自行执行。必须获得用户当次明确授权，使用专用测试应用和专用测试会话，并先确认目标 chat ID。** 它会创建真实消息/卡片、产生可见通知并消耗 API 配额；生产群、生产机器人和未知会话一律不要用。

Bash 示例：

```bash
FEISHU_E2E_REAL=1 \
FEISHU_E2E_APP_ID=cli_test_replace_me \
FEISHU_E2E_APP_SECRET=test_secret_replace_me \
FEISHU_E2E_CHAT_ID=oc_test_replace_me \
python -m pytest tests/e2e -q
```

独立场景脚本同样必须带显式 opt-in，例如 `FEISHU_E2E_REAL=1 FEISHU_E2E_APP_ID=cli_test_replace_me FEISHU_E2E_APP_SECRET=test_secret_replace_me FEISHU_E2E_CHAT_ID=oc_test_replace_me python scripts/e2e_v2.py --scenario connect,reply`；`--list` 只用于查看场景。私聊测试按需使用 `FEISHU_E2E_OPEN_ID`，独立 reply 脚本可使用 `FEISHU_E2E_DM_CHAT_ID`，API 地址可用 `FEISHU_E2E_BASE_URL` 覆盖。真实验收后记录相关 `om_` 消息 ID 和 `log_id`，不要记录或上传密钥。

## 故障排查

| 症状 | 优先检查与处理 |
|---|---|
| `config validation failed` / `No adapter available` | 用 `hermes config env-path` 确认凭证落在**当前 profile**；检查两个必需变量、插件 enabled、平台 enabled，再重启。注册不会替你写配置 |
| 卡片与原生文本双回复 | 检查 `platforms.feishu.enabled=false` 与 `feishu-streaming=true`。共享 `.env` 可触发两个平台 env-enable，显式关闭内置平台不可省略 |
| 插件已启用但依赖/API 缺失 | 运行 `hermes plugins doctor feishu-streaming-platform --ci`；需要用同一 Gitee URL 的官方安装流程修复依赖，不要向每个 generation 手工 pip |
| `install out of sync` 或桌面端环境异常 | 先保留报错全文并用官方插件 doctor/install 流程诊断；不要删除锁文件、杀进程或改写 Hermes 托管 venv |
| 改了配置仍不生效 | 确认修改的是 `hermes config path/env-path` 返回的当前 profile 文件；在网关外部真正重启。`/aowen config reload` 只重读插件配置，不替代平台/凭证重启 |
| 有卡片但没有打字机 | 检查 `streaming.enabled=true`，以及 `display.platforms.feishu-streaming.streaming` 未被设为 false |
| 工具行或思考行缺失 | 检查 `display.tool_progress` 未关闭；实时思考另需 `thinking_progress=true`。没有官方完成事件的工具不会由插件伪造完成态 |
| 澄清按钮提示未配置回调 | 飞书开放平台 → 应用 → 事件与回调 → 卡片交互回调，选择“使用长连接接收回调”；webhook 部署需按内置模式另行配置 |
| 卡片永久进行中 | 收集时间线、卡片 `om_`、`log_id` 与停机/限流日志；检查看门狗和错误码。不要以删除网关锁或强杀进程作为修复 |
| 回复没有引用提问 | cron/后台合成 ID 不是 `om_`，直发是预期；已确认真实 `om_` 锚不可用时可直发，结果未知时禁止自动重试和原生二次投递。保留触发与卡片 `om_` 便于查证 |
| `hermes plugins config` 报未知命令 | 该子命令不存在。插件操作用 `hermes plugins ...`，配置操作用顶层 `hermes config set/get/unset/path/env-path` |

需要提交问题时，保留最小复现和脱敏日志，不要贴整份 `.env`、未脱敏 `config.yaml` 或 `config get --raw` 输出。

## Issue 与文档

- [Gitee Issues](https://gitee.com/Aowen-Nowor/hermes-lark-streaming/issues)
- [Issue 信息模板](docs/ISSUE_TEMPLATE.md)
- [Agent 执行手册](docs/SKILL.md)
- [配置与出站/API 契约](docs/API.md)
- [更新日志](docs/CHANGELOG.md)

## License

MIT
