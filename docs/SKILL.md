---
name: hermes-lark-streaming-install
description: 在 Windows/Linux/macOS 上安装、配置、验证、更新、迁移、卸载 hermes-lark-streaming（feishu-streaming-platform）。当用户要求部署飞书流式卡片插件、从 V1 迁移 V2、升级回滚或排查平台/卡片/E2E 问题时使用；默认只做 mock，真实飞书 E2E 必须获得当次明确授权。
---

# feishu-streaming-platform：Agent 执行手册

人类说明见 [`../README.md`](../README.md)，配置与帧契约见 [`API.md`](API.md)。当前文档对应 **v2.0.35（2026-11-12，本地验证中，未发布）**。

## 0. 先锁定事实与安全边界

| 项 | 值 |
|---|---|
| Gitee 仓库 | `https://gitee.com/Aowen-Nowor/hermes-lark-streaming.git` |
| 主分支 | `github_sync` |
| 插件 ID | `feishu-streaming-platform` |
| 注册平台名 | `feishu-streaming` |
| 内置平台名 | `feishu` |
| V1 插件 ID / 常见目录 | `hermes-lark-streaming` |
| 最低 Hermes | `>=0.23` |
| Python 元数据 | `>=3.11,<3.15` |
| Issue | https://gitee.com/Aowen-Nowor/hermes-lark-streaming/issues |
| Issue 模板 | [`ISSUE_TEMPLATE.md`](ISSUE_TEMPLATE.md) |

架构是**独立 platform 插件 + 内置 `FeishuAdapter` 继承 + 出站 CardKit 封装**：零 monkey-patch、零注册钩子。`register()` 只做依赖检查和注册，不自动写入或迁移配置。standalone 发送也经过本适配器。

执行纪律：

1. **默认禁止真实飞书消息。** 安装健康检查和测试先用 mock；只有用户当次明确授权、专用测试应用、专用测试会话、正确 `FEISHU_E2E_CHAT_ID` 与 `FEISHU_E2E_REAL=1` 同时满足，才可执行 real E2E。
2. 不修改 Hermes 核心，不 monkey-patch，不删除锁文件，不强杀网关，不逐个运行环境手工安装依赖，不用手工 Git 操作代替官方更新。
3. 不把 App Secret、token、完整 `.env`、`auth.json`、未脱敏配置或 `config get --raw` 输出贴进聊天/Issue。凭证只写当前 profile 的 `.env`，不写 `config.yaml`。
4. 安装、更新、卸载、配置变更和重启前先确认目标 profile；备份含有 secret 的文件时保持同等访问权限，不上传备份。
5. 网关重启只能从**网关进程之外**的终端或桌面外部管理入口执行。Agent 不得从 gateway 内的工具调用重启自己。

## 1. 先核实 CLI，不凭记忆造命令

本手册的命令形状已用 Hermes 0.23.1 的实际 `--help` 核实。目标机版本不同，先重跑：

```bash
hermes plugins install --help
hermes plugins update --help
hermes plugins uninstall --help
hermes config --help
```

关键结论：

- 安装：`hermes plugins install <identifier> [--force] [--ref <40位SHA>] [--yes-deps] [--enable]`。
- 更新：`hermes plugins update [name]`；`--yes` 主要用于无名批量更新，不要画蛇添足加到命名单插件流程。
- `uninstall` 是 `remove` 的别名：`hermes plugins uninstall <name>`。
- **`hermes plugins config` 不存在。** 配置使用顶层 `hermes config set/get/unset/path/env-path`。
- 依赖确认与凭证是两件事：`--yes-deps` 不代替 `.env` 中的 `FEISHU_APP_ID/SECRET`。
- 诊断：`hermes plugins doctor feishu-streaming-platform --ci`、`hermes gateway status`。

## 2. 跨平台路径与前置核查

Windows、Linux、macOS 都先用官方命令解析当前 profile：

```bash
hermes --version
hermes config path
hermes config env-path
hermes plugins show feishu-streaming-platform
hermes config get platforms.feishu.enabled
hermes config get platforms.feishu-streaming.enabled
```

`plugins show` 在全新安装前报未安装是正常信息，不是继续执行的失败理由。其余输出若指向另一个 profile，先修正 CLI profile/HERMES_HOME 上下文。

需要 shell 变量时：

**Linux/macOS/Windows Git Bash**

```bash
export HERMES_HOME="${HERMES_HOME:-$(dirname "$(hermes config path)")}"
export CONFIG_FILE="$(hermes config path)"
export ENV_FILE="$(hermes config env-path)"
export PLUGIN_DIR="$HERMES_HOME/plugins/feishu-streaming-platform"
```

**Windows PowerShell**

```powershell
if (-not $env:HERMES_HOME) {
  $env:HERMES_HOME = Split-Path -Parent (hermes config path)
}
$ConfigFile = hermes config path
$EnvFile = hermes config env-path
$PluginDir = Join-Path $env:HERMES_HOME "plugins\feishu-streaming-platform"
```

不要写死默认 home、某个用户的系统目录、安装 ID、环境 hash 或盘符。代码运行时应使用官方 `get_hermes_home()` / profile-scoped secret；文档命令只使用上述解析结果。

前置条件：Hermes 版本满足、飞书应用权限与机器人会话已配置、网络可到飞书服务、当前 profile 的 `.env` 可写。插件依赖由官方安装器解析，不预装到每一个运行环境。

## 3. 全新安装

### 3.1 备份已有配置

已有配置或凭证时，先备份。Bash：

```bash
STAMP="$(date +%Y%m%d-%H%M%S)"
cp -- "$CONFIG_FILE" "$CONFIG_FILE.bak-$STAMP"
if [ -f "$ENV_FILE" ]; then cp -- "$ENV_FILE" "$ENV_FILE.bak-$STAMP"; fi
```

PowerShell：

```powershell
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
Copy-Item -LiteralPath $ConfigFile -Destination "$ConfigFile.bak-$Stamp"
if (Test-Path -LiteralPath $EnvFile) {
  Copy-Item -LiteralPath $EnvFile -Destination "$EnvFile.bak-$Stamp"
}
```

全新且无配置可保留时仍需知道备份位置；备份不得进入仓库或 Issue。

### 3.2 配置 `.env` 凭证（主流程）

让操作者在 `$ENV_FILE` 中填写，Agent 不索取真实 secret：

```dotenv
FEISHU_APP_ID=cli_replace_me
FEISHU_APP_SECRET=replace_me
FEISHU_DOMAIN=feishu
FEISHU_CONNECTION_MODE=websocket
# FEISHU_HOME_CHANNEL=oc_replace_me
```

交互式安装也能询问缺失的 `requires_env` 并写入 `.env`。凭证通过 Hermes 官方 scoped secret、`EnvCred` 与 env-enable 进入 `PlatformConfig.extra`；**不要添加 `platforms.*.extra.app_secret` 到 YAML。**

### 3.3 官方安装

```bash
hermes plugins install https://gitee.com/Aowen-Nowor/hermes-lark-streaming.git --yes-deps --enable
```

该 URL 是唯一本文使用的安装源；主分支是 `github_sync`。不要改成占位 `REPO_URL`，不要用旧 `master` 或开发分支。

### 3.4 显式选择平台

共享凭证可能同时满足内置和流式平台的 env-enable，必须显式二选一：

```bash
hermes config set platforms.feishu.enabled false
hermes config set platforms.feishu-streaming.enabled true
hermes config set streaming.enabled true
# 可选：实时思考进度进入面板
hermes config set display.platforms.feishu-streaming.thinking_progress true
```

CLI 会把 `gateway.platforms.*` 规范化到顶层 `platforms.*`，顶层优先。本手册只写顶层，避免双配置源。

可选 `feishu_streaming` 节只能由用户显式创建；安装/注册不会注入。需要改多项时可用官方 `hermes config edit`，单项用 `hermes config set`。

### 3.5 重启与无消息验收

从外部终端：

```bash
hermes gateway restart
hermes gateway status
hermes plugins show feishu-streaming-platform
hermes plugins doctor feishu-streaming-platform --ci
hermes config get platforms.feishu.enabled
hermes config get platforms.feishu-streaming.enabled
hermes config get streaming.enabled
```

验收点：插件 enabled、版本符合预期、内置 false、流式 true、全局流式 true、无凭证/注册/依赖/适配器错误。桌面窗口重开不等于网关重启。不要为“验证安装”擅自发飞书消息；先跑第 9 节 mock。

## 4. 完整配置表

### 4.1 平台与显示

| 键 | 默认/建议 | 作用 |
|---|---|---|
| `platforms.feishu.enabled` | 本插件启用时 `false` | 防内置与流式双回复 |
| `platforms.feishu-streaming.enabled` | `true` | 本插件平台开关；显式 disable 优先于 env-enable |
| `streaming.enabled` | Hermes 默认 `false`，本插件建议 `true` | 全局流式总开关 |
| `display.platforms.feishu-streaming.streaming` | 跟随全局 | 只可进一步关闭本平台流式 |
| `display.tool_progress` | high tier 为 `all` | 工具进度数据源；`off` 关闭 |
| `display.platforms.feishu-streaming.thinking_progress` | `false` | `true` 允许实时思考进度 |
| `display.show_reasoning` | Hermes 配置 | 推理显示与 `reasoning` 页脚数据来源 |
| `display.platforms.feishu-streaming.interim_assistant_messages` | high tier 为 `true` | 对话性 interim 入面板 |

### 4.2 `.env` 与继承变量

| 变量 | 默认/必填 | 作用 |
|---|---|---|
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | **必填** | 当前 profile 的应用凭证；只放 `.env` 或受支持 secret 源 |
| `FEISHU_DOMAIN` | `feishu` | `feishu` / `lark` |
| `FEISHU_CONNECTION_MODE` | `websocket` | `websocket` / `webhook` |
| `FEISHU_ENCRYPT_KEY` / `FEISHU_VERIFICATION_TOKEN` | 空 | webhook 安全参数，按内置要求配置 |
| `FEISHU_HOME_CHANNEL` | 空 | cron/通知默认 chat ID |
| `FEISHU_ALLOWED_USERS` | 空 | 用户 open_id 白名单，逗号分隔；设置后抑制陌生 DM pairing |
| `FEISHU_ALLOW_ALL_USERS` | `false` | 允许所有用户；不要为排障长期放开 |
| `FEISHU_ALLOW_BOTS` | `none` | `none` / `mentions` / `all` |
| `FEISHU_GROUP_POLICY` | `allowlist` | `allowlist` / `open` / `disabled` |
| `FEISHU_REQUIRE_MENTION` | `true` | 群内是否必须 @机器人 |
| `FEISHU_REACTIONS` | `true` | 内置处理中/失败 reaction |
| `FEISHU_WEBHOOK_HOST` / `PORT` / `PATH` | `127.0.0.1` / `8765` / `/feishu/webhook` | webhook 监听配置，变量名分别为完整 `FEISHU_WEBHOOK_*` |
| `FEISHU_BOT_OPEN_ID` / `FEISHU_BOT_USER_ID` / `FEISHU_BOT_NAME` | 自动获取 | 特殊迁移覆盖项，通常留空 |

### 4.3 `feishu_streaming` 全部有效键

| 键 | 类型 | 默认/约束 | 作用 |
|---|---|---|---|
| `streaming_panel_expanded` | bool | `false` | 流式面板默认展开状态 |
| `print_strategy` | str | `delay`；`fast/delay` | 打字机策略，非法值回退 |
| `print_step` | int | `4`；`1..10` 钳制 | 打字机步长 |
| `card_ttl_sec` | int | `600`；最低 `60` | 卡片状态 TTL |
| `watchdog_idle_sec` | float | `300`；`60..1800` 钳制 | 看门狗静默阈值 |
| `oneshot_cards` | bool | `true` | 一次性静态卡总开关；false 回原生 |
| `clarify_suspend_sec` | float | `1.2`；0 关闭 | 澄清边界暂停窗口 |
| `footer.show_label` | bool | `false` | 页脚标签 |
| `footer.fields` | list | `[[status,elapsed,model]]` | 可含 `tokens/context/api_calls/history_offset/compression_exhausted/cache/cost/reasoning`；无数据省略 |
| `card_style.text_size.answer` | map | 未配置 | `{default,pc,mobile}`；缺档回退 `normal_v2`，token `cus-ans` |
| `card_style.text_size.aux` | map | 未配置 | 三档辅助字号；缺档回退 `notation`，token `cus-aux` |

页脚基础字段为 `status/elapsed/model`，其余统计字段取决于官方帧是否供数。`card_style` 未配置时不注入样式。`native_streaming` 已退役；V1 死键为 `linear/panel_expanded/flush_interval_ms/answer_flush_interval_ms/feishu_base_url/show_reasoning/gateway_cards/max_tool_steps/max_reasoning_rounds`，不要迁移。

## 5. V1 → V2：一次性迁移

严格按以下顺序，不能原地覆盖：

### 5.1 备份与取证

1. 执行第 3.1 节备份，记录 V1 版本、原始安装来源、安装目录、平台开关。
2. 确认 `$HERMES_HOME/plugins/hermes-lark-streaming/__main__.py` 存在。cleanup 必须使用 **V1 自己的代码**，在卸载前执行。
3. 若代码已丢失，停止迁移并先从原备份/原来源恢复；不要手工猜删 YAML 或拿其他版本 cleanup 碰运气。
4. Hermes 必须满足 V2 的 `>=0.23`，不满足先按官方方式升级 Hermes。

### 5.2 显式 cleanup

Bash（Windows 可将 `python3` 换成 `python`，使用曾运行 V1 的环境）：

```bash
export HERMES_HOME="${HERMES_HOME:-$(dirname "$(hermes config path)")}"
V1_DIR="$HERMES_HOME/plugins/hermes-lark-streaming"
python3 "$V1_DIR/__main__.py" cleanup
```

cleanup 只清 V1 注入的插件配置，不应删除飞书凭证。失败就停止，保留现场与备份，不进入卸载步骤。

### 5.3 卸载 V1、安装 V2

```bash
hermes plugins uninstall hermes-lark-streaming
hermes plugins install https://gitee.com/Aowen-Nowor/hermes-lark-streaming.git --yes-deps --enable
```

### 5.4 配置与验证

1. 确认凭证只在当前 profile `.env`，不把旧 YAML secret 复制到新 YAML。
2. 执行第 3.4 节三个必需开关；需要思考进度再开 `thinking_progress`。
3. 只按第 4.3 节重建非机密 `feishu_streaming`；不要恢复 V1 死键。
4. 从外部重启，执行第 3.5 节无消息验收和第 9 节 mock。
5. 真实卡片验收需要单独明确授权，不以迁移完成为由自动发消息。

### 5.5 迁移回滚

先切回内置 `feishu` 并在外部重启确认原生可用；卸载 V2；恢复迁移前配置和 `.env` 备份；按记录的原始来源与版本重装 V1；再次重启验证。不得用未记录的“最新 V1”替代原版本。

## 6. 日常更新

```bash
hermes plugins check-updates
hermes plugins update feishu-streaming-platform
```

随后从外部重启并检查：

```bash
hermes gateway restart
hermes plugins show feishu-streaming-platform
hermes gateway status
```

更新前后都要保留非机密配置与备份。扫描报 caution/danger 时先审查，不默认 `--force`。不要手工换分支、删目录、清缓存或逐 generation pip。

### 固定到旧版本

先备份并切回内置平台，确认原生可用。使用完整 40 位 commit SHA：

```bash
hermes plugins install https://gitee.com/Aowen-Nowor/hermes-lark-streaming.git --force --ref <完整的40位commitSHA> --yes-deps --enable
```

恢复与该版本匹配的非机密配置，重启验证。pinned 安装不会被普通 update 自动移动；换版本要显式选新 SHA。

## 7. 安全卸载与回滚

### 7.1 首选：回滚到内置，不卸代码

```bash
hermes config set platforms.feishu-streaming.enabled false
hermes config set platforms.feishu.enabled true
# streaming.enabled 按内置平台原有设置恢复，不盲目改值
```

外部重启，`hermes gateway status` 确认原生可用。保留 `.env`，因为内置平台也使用同一凭证。

### 7.2 完全卸载

先完成 7.1，再执行：

```bash
hermes plugins uninstall feishu-streaming-platform
# 仅当用户确认不再需要自己创建的可选配置：
hermes config unset feishu_streaming
```

再次从外部重启并检查。不要删除 profile、`.env`、其他平台配置、锁文件或 Hermes 托管 venv。

## 8. 故障排查

| 症状 | 取证与安全处置 |
|---|---|
| 平台校验失败 / 无适配器 | 核对当前 profile 的 `env-path`、两个必需变量、插件 enabled、平台 enabled；注册不写配置。修改后外部重启 |
| 双回复 | 核对 `platforms.feishu.enabled=false`。两个平台共享凭证，env-enable 不会替你完成二选一 |
| 依赖缺失或 API 不存在 | `hermes plugins doctor feishu-streaming-platform --ci`，保留原始错误。需要修复时经用户确认走同源官方安装器；不要手工逐个 venv 装包 |
| `install out of sync` | 保存完整输出，先用官方 doctor/install 诊断。不得删锁、强杀进程或改写托管环境 |
| 本地修改后需同源修复 | 先备份并说明 tracked/untracked 风险，获确认后才可 `hermes plugins install <同一Gitee URL> --force --yes-deps --enable` |
| 配置不生效 | 核对 `config path/env-path` 是否当前 profile，是否同时存在顶层与 gateway 配置；外部重启。`/aowen config reload` 只重读插件配置 |
| 无流式打字 | `streaming.enabled=true`，并检查 per-platform streaming 未关闭 |
| 工具/思考缺失 | `display.tool_progress` 不可为 off；实时思考需 `thinking_progress=true`。插件不伪造上游不存在的完成态 |
| 卡片不停转 | 保存起止时间、时区、触发/卡片 `om_`、`log_id`、限流/停机日志，按时间线定位，不强杀、不删锁 |
| 澄清按钮无回调 | 开放平台配置“卡片交互回调 → 使用长连接接收回调”；webhook 部署按内置模式另查 |
| 无引用线 | 非 `om_` 的 cron/后台 ID 直发是预期；已确认真实 `om_` 锚不可用时可直发，结果未知时不二次投递。记录两类 om_ 查证 |
| `hermes plugins config` 失败 | 改用顶层 `hermes config ...`，该 plugins 子命令不存在 |

## 9. 测试：默认 mock，real 双重 opt-in

在 `$PLUGIN_DIR` 或源码检出目录执行。

### 9.1 默认 mock（Agent 默认唯一可自执行路径）

```bash
FEISHU_E2E_REAL=0 python -m pytest tests -q
python scripts/smoke_v2.py
```

PowerShell：

```powershell
$env:FEISHU_E2E_REAL = "0"
python -m pytest tests -q
Remove-Item Env:FEISHU_E2E_REAL -ErrorAction SilentlyContinue
```

凭证单独存在不会触发 real。只有精确 `FEISHU_E2E_REAL=1` 且 APP_ID/SECRET/CHAT_ID 全部非空才会 real；`true/yes/on` 都保持 mock。不要在报告中写死测试数量，记录实际命令与退出结果即可。

### 9.2 真实 E2E（会发真实消息）

> 先停下取得当次明确授权。确认专用测试应用、专用测试会话、目标 chat ID、机器人权限与可见消息接受者。真实测试会发连通性文本、建卡、更新卡、消耗配额。生产环境或未确认会话禁止执行。

Bash：

```bash
FEISHU_E2E_REAL=1 \
FEISHU_E2E_APP_ID=cli_test_replace_me \
FEISHU_E2E_APP_SECRET=test_secret_replace_me \
FEISHU_E2E_CHAT_ID=oc_test_replace_me \
python -m pytest tests/e2e -q
```

独立场景脚本也必须显式带同一 opt-in，例如 `FEISHU_E2E_REAL=1 FEISHU_E2E_APP_ID=cli_test_replace_me FEISHU_E2E_APP_SECRET=test_secret_replace_me FEISHU_E2E_CHAT_ID=oc_test_replace_me python scripts/e2e_v2.py --scenario connect,reply`；`--list` 仅列场景。可选变量：`FEISHU_E2E_OPEN_ID`（pytest 私聊）、`FEISHU_E2E_DM_CHAT_ID`（独立 reply 私聊）、`FEISHU_E2E_BASE_URL`（API 地址）。完成后清理进程级测试变量，记录 `om_` 与 `log_id`，不上传 secret。

## 10. Issue 交付

入口：https://gitee.com/Aowen-Nowor/hermes-lark-streaming/issues

必须按 [`ISSUE_TEMPLATE.md`](ISSUE_TEMPLATE.md) 提供：环境、Hermes/插件/Python/SDK 版本与 commit、最小复现、期望/实际、带时区时序、脱敏日志、触发和卡片 `om_`、`log_id`。一个 Issue 一个根因。

不得贴密钥、完整 `.env`、认证文件、未脱敏配置、个人隐私或群消息全文；允许保留排查所需的消息 `om_`、CardKit `card_id`、飞书 `log_id`、错误码与相对时间线。若 secret 已泄露，先轮换，不要用 Issue 继续传播。
