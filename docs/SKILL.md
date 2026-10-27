---
name: hermes-lark-streaming-install
description: 指导 AI agent 在 Linux 服务器上完成 hermes-lark-streaming 飞书流式卡片插件（hermes-agent 平台插件）的安装、配置、启动、健康验证、从 V1（1.x）升级迁移与回滚。当用户要求"安装这个插件 / 部署到服务器 / 换个机器装一份 / 升级插件版本 / 从 V1 升级到 V2 / 回滚插件"时使用本 skill。
---

# hermes-lark-streaming 安装指南（agent 专用）

本插件是 hermes-agent 的飞书平台增强插件（CardKit v2.0 流式卡片）。**面向 agent 的完整安装手册**——人类视角的介绍见 `../README.md`；本文假设你（agent）拿到一台服务器的 SSH 访问权，需要从零把插件跑起来并验证健康。

## 0. 前置条件核查（缺一项先补一项）

按顺序核查目标服务器，全部满足才继续：

1. **hermes-agent 主程序可用**：`~/.hermes/` 目录存在，且存在可运行的 hermes 环境（官方 v0.22+，插件用 `SUPPORTS_NATIVE_STREAMING` 等接口需要 v0.22+）。确认方式：
   ```bash
   ls ~/.hermes/config.yaml && ls ~/.hermes/plugins/ 2>/dev/null
   ```
2. **Python ≥3.11**（生产实测 3.14）：`python3 --version`
3. **飞书应用凭证**：App ID / App Secret（飞书开放平台自建应用，事件订阅用 WebSocket 长连接模式），且机器人已被拉进目标群/可私聊。
4. **网络**：能访问 `open.feishu.cn` 与 `msg-frontier.feishu.cn`（WebSocket 长连接）。

## 1. 安装插件本体

### 1.1 放置插件目录

插件必须放在 **`~/.hermes/plugins/hermes-lark-streaming/`**（hermes 用户级插件目录，hermes 启动时自动扫描 `source=user`）：

```bash
mkdir -p ~/.hermes/plugins
# 方式 A：git clone（推荐，便于升级/回滚）——⚠ 必须带 -b 分支参数：
#   默认分支 github_sync 在手动同步 V2 之前仍是 V1（1.8.4 standalone）！
#   不带 -b 会把 V2 用户装成 V1。V2 只在 refactor/v2.0.0 分支与 v2.0.x tag 上。
git clone -b refactor/v2.0.0 <REPO_URL> ~/.hermes/plugins/hermes-lark-streaming
# 方式 B：已有代码包 → 解压/复制到上述路径
```

### 1.2 依赖安装

插件依赖 `lark-oapi >= 1.6.4`（双硬 floor：1.4.0 无 async cardkit 方法集 `acreate/acontent/abatch_update/asettings`，1.6.4 才有 extra_ua_tags；与 plugin.yaml `python_dependencies` 逐字同步）+ `PyYAML >= 6.0`。装进 **hermes 运行所用的同一个 venv**：

```bash
# 找到 hermes 的 venv（常见位置：hermes-agent 仓库内 venv/ 或专用环境目录）
<VENV_PYTHON> -m pip install "lark-oapi>=1.6.4" "PyYAML>=6.0"
```

注意：不要动 hermes 本体的依赖版本；只增量安装插件所需的包。

### 1.3 启用插件（config.yaml）

编辑 `~/.hermes/config.yaml`，平台名用 `feishu-streaming`（增强插件）而不是 `feishu`（原生）：

```yaml
platforms:
  feishu-streaming:
    enabled: true        # 以 hermes 当前版本的平台配置结构为准
    app_id: cli_xxxxxxxx
    app_secret: xxxxxxxx
plugins:
  enabled:
    - feishu-streaming-platform   # 插件注册名（plugin.yaml 的 name）
```

可选配置（全部有缺省值，不配也能跑）——完整键位表见 `../README.md` 配置节 / `API.md`：

```yaml
feishu_streaming:
  native_streaming: true        # native 帧通道（false 回 edit-based）
  oneshot_cards: true           # 回合外投递卡片化
  card_ttl_sec: 1800            # 活跃状态 TTL（下限 60 钳制）
  footer_fields:                # 页脚字段（默认 status/elapsed/model）
    - ["status", "elapsed", "model"]
  card_style:                   # 字号自定义（完全 opt-in；不配 = 零注入）
    text_size:
      answer: {default: normal_v2, pc: normal_v2, mobile: x-large}
display:
  platforms:
    feishu-streaming:
      thinking_progress: true   # 思考流回灌进面板
```

## 2. 启动网关

hermes 网关（`python -m gateway.run`）负责拉起平台插件。**首次安装**在 hermes-agent 仓库根（或 hermes 运行目录）执行：

```bash
rm -f ~/.hermes/gateway.lock          # 残留锁会阻止启动
setsid nohup <VENV_PYTHON> -m gateway.run \
  >> ~/.hermes/logs/gateway$(date +%m%d%H%M).log 2>&1 < /dev/null &
```

三个要点（生产踩坑沉淀，必须遵守）：
- **必须 `setsid nohup ... < /dev/null &` + `disown`**：直接 `nohup cmd &` 在 SSH 通道关闭时可能被 SIGHUP 带走；
- **日志写进 `~/.hermes/logs/`**，带时间戳文件名，方便区分每次重启；
- **重启前 `pkill -f 'python -m gateway.run'` 后 `sleep 3`**，并删 `~/.hermes/gateway.lock`（lock 残留 = 启动即退）。

## 3. 健康验证（启动后 30 秒内五项）

```bash
LOG=$(ls -t ~/.hermes/logs/gateway*.log | head -1)
# ① 插件加载路径必须是 ~/.hermes/plugins/hermes-lark-streaming（主目录）
grep "Loading plugin 'feishu-streaming-platform'" ~/.hermes/logs/agent.log | tail -1
# ② 加载失败必须为 0
grep -c "Failed to load" "$LOG"
# ③ 飞书 WebSocket 长连接已建立
grep -aoE "connected to wss[^ ]*" "$LOG" | head -1
# ④ Traceback 计数（启动期上游 429 限流 traceback 属存量良性，可忽略）
grep -c "Traceback" "$LOG"
# ⑤ 网关错误码 99992=0
```

①~③ 任一不满足 = 安装失败，回读日志定位；不要带病继续。

## 4. 真飞书 E2E 验证（可选但强烈建议）

仓库自带真飞书 E2E 脚本（直驱 FrameComposer + 真 FeishuClient）：

```bash
cd ~/.hermes/plugins/hermes-lark-streaming
export FEISHU_E2E_APP_ID=cli_xxx FEISHU_E2E_APP_SECRET=xxx FEISHU_E2E_CHAT_ID=oc_xxx
<VENV_PYTHON> scripts/e2e_v2.py --scenario full   # 10 断言，全 PASS 退出码 0
```

`full` 场景覆盖：建卡前暂存 → 首帧建卡 → 面板行（含 v2.0.22 连续 terminal 裸围栏块独立步骤行）→ 流式更新 → 定稿页脚。

全场景 11 个：connect / answer / progress / interim / full / burst / native / clarify / plain / oneshot / **reply**（v2.0.22 引用验收：群聊必验 + 私聊经 `FEISHU_E2E_DM_CHAT_ID` 双验证 + 伪锚点降级；与 connect 连跑时复用其消息作锚点零额外噪音：`--scenario connect,reply`）。版本水印自动跟随 plugin.yaml，无硬编码。

私聊场景验收：把 `FEISHU_E2E_DM_CHAT_ID` 设为机器人与用户的 P2P 会话 id（`oc_` 开头，可从网关日志 `Inbound dm message ... chat_id=` 取得），reply 场景会在私聊里重复引用验证；或在私聊里 @ 机器人发一句话，观察卡片是否：引用你的提问消息（v2.0.22+）、流式打字、面板出现工具/思考行、页脚落定。

## 5. 升级与回滚

**升级**（已有安装，拉新版本）：
```bash
cd ~/.hermes/plugins/hermes-lark-streaming
git fetch --tags && git checkout v2.0.28       # 按版本 checkout tag（勿依赖默认分支，见第 6 节）
<VENV_PYTHON> -m py_compile $(git diff --name-only HEAD@{1} HEAD | grep '\.py$')  # 语法门禁
find . -name __pycache__ -type d -prune -exec rm -rf {} +   # 清缓存（热替换旧字节码必做）
pkill -f 'python -m gateway.run'; sleep 3; rm -f ~/.hermes/gateway.lock
# 再走第 2 节启动 + 第 3 节验证
```

**回滚**：`git checkout <旧tag>` 后同上重启。插件零 monkey-patch，平台名 `feishu-streaming` 改回 `feishu` 即整体停用（无运行时残留）。

## 6. 从 V1（1.x）升级到 V2（一次性迁移）

V1（≤1.8.x，standalone 钩子形态，运行时拦截 GatewayRunner/FeishuAdapter）与 V2（≥2.0.0，platform 形态，零 monkey-patch）**共用同一个插件目录** `~/.hermes/plugins/hermes-lark-streaming/`。升级 = 换代码 + 三处配置 + 重启，全程约 5 分钟。

### 6.0 先分清安装方式（V1 两种安装路径处置不同）

```bash
ls ~/.hermes/plugins/hermes-lark-streaming/patching/ 2>/dev/null && echo V1代码在位
grep -E "^version" ~/.hermes/plugins/hermes-lark-streaming/plugin.yaml
```
- **CLI 安装**（V1 README 的 `hermes plugins install <git url>`）：目录可能带 `.git`（可直接 checkout），也可能被 PM 复制（无 .git → 走方式 B 重新 clone 覆盖）
- **手动放置**：直接 git 操作即可
- ⚠ **手动同步完成前不要用 `hermes plugins update hermes-lark-streaming` 升 V2**：它拉仓库**默认分支 github_sync（同步 V2 之前仍为 V1 1.8.4）**，拿不到 V2。V2 只在 `refactor/v2.0.0` 分支与 `v2.0.x` tag 上。

### 6.1 前置核查（V1 特有差异）

| 项 | V1 现状 | V2 要求 | 动作 |
|---|---|---|---|
| hermes-agent | 常见 v0.21.x（V1 CI 验证到 v0.21.6） | **≥ v0.22**（`SUPPORTS_NATIVE_STREAMING` 官方接口） | 不满足先升级 hermes 本体，否则启动即 fail-fast 报失配 |
| lark-oapi | ≥1.6.4（V1 1.8.0 起同 floor） | 同左（+ PyYAML≥6.0） | 已满足则零动作；更老 V1 则 `pip install -U 'lark-oapi>=1.6.4'` |
| Python | ≥3.11 | 同左（官方运行时 3.14） | 零动作 |

### 6.2 停网关 + 换代码

```bash
pkill -f 'python -m gateway.run'; sleep 3; rm -f ~/.hermes/gateway.lock
cd ~/.hermes/plugins/hermes-lark-streaming
git fetch --tags && git checkout v2.0.28    # 或 git pull origin refactor/v2.0.0
```

> V1 的 `controller/ patching/ state/ flush/` 子包在 V2 已删除，git checkout 会自动清掉工作区副本；**解压/复制部署的用户必须手动删除这些旧目录**（连同 V1 的 `docs/AGENT_GUIDE.md`），不要让两代代码同目录共存。

### 6.3 config.yaml 三处改动

1. **插件注册名**（V2 manifest name 已变）——`plugins.enabled`：
   `- hermes-lark-streaming` → `- feishu-streaming-platform`
   （兼容事实：目录名即 path key，旧条目其实也能命中加载；规范做法是改名，消除歧义）
2. **平台切换**（V1 借道原生 `feishu` 平台+钩子；V2 是独立平台）——**必须显式切换，二选一防双回复**：
   ```yaml
   platforms:
     feishu:              # 原生平台（V1 用户必然开着）
       enabled: false
     feishu-streaming:    # V2 平台，凭证从原 feishu 段平移
       enabled: true
       extra:
         app_id: cli_xxx
         app_secret: xxx
   ```
3. **配置节名**：`hermes_lark_streaming` → `feishu_streaming`（**不改也兼容**——V2 reader 惰性回读旧节名；V1 的 9 个死键 `linear / panel_expanded / flush_interval_ms / answer_flush_interval_ms / feishu_base_url / show_reasoning / gateway_cards / max_tool_steps / max_reasoning_rounds` 留着零副作用，建议顺手删）

### 6.4 依赖 + 清缓存 + 重启 + 验证

```bash
<VENV_PYTHON> -m pip install "lark-oapi>=1.6.4" PyYAML    # 已满足则跳过
find ~/.hermes/plugins/hermes-lark-streaming -name __pycache__ -type d -prune -exec rm -rf {} +
# 第 2 节命令启动网关 → 第 3 节五项健康验证
# 真飞书验收：--scenario full,reply（引用+裸块归属）
```

### 6.5 V1 → V2 行为差异速查（agent 应主动向用户说明）

| 维度 | V1 | V2 |
|---|---|---|
| 实现形态 | 钩子 + 运行时拦截（patching/ 六 wrapper） | 官方 register_platform 协议，零 monkey-patch |
| 平台占用 | 借道原生 feishu 平台 | 独立平台名 feishu-streaming（原生可关可留） |
| 配置注入 | 往 config.yaml 注入配置节 + plugins.enabled 条目 | 零注入（v2.0.20 起全部 opt-in） |
| 引用回复 | 无（卡片直发） | 卡片 reply 引用触发消息（v2.0.22，私聊/群聊；cron/后台无 om_ 锚点自动直发） |
| hermes 版本面 | v0.21.x 可用 | 需 v0.22+（启动 fail-fast 探测会明确报失配点） |
| 回滚 | — | `git checkout v1.8.4` + 平台段改回 feishu + plugins.enabled 改回旧名 → 重启即回 V1 |

### 6.6 迁移失败速查

| 症状 | 根因 | 处置 |
|---|---|---|
| 启动日志 `Skipping 'feishu-streaming-platform' (not in plugins.enabled)` | 6.3-① 未改 | 改 plugins.enabled 条目 |
| 双平台同时回复 / 卡片+原生文本双发 | 6.3-② 原生 feishu 未关 | 二选一，关 feishu 开 feishu-streaming |
| 启动 fail-fast 报继承面失配 | hermes < v0.22 | 先升级 hermes 本体 |
| `ModuleNotFoundError` 而 pip list 可见 | 依赖装错环境（PM site-packages 漂移） | 见 README 依赖节判别方法 |

## 7. 已知环境坑速查

| 症状 | 根因 | 处置 |
|---|---|---|
| 启动即退、无日志 | `~/.hermes/gateway.lock` 残留 | 删锁重启 |
| SSH 断开后进程消失 | 未用 `setsid` 脱离会话 | 用第 2 节原样命令 |
| 插件加载了但 API 全 404/attribute error | lark-oapi < 1.4.24（无 async cardkit） | 升级 lark-oapi |
| **装完发现是 V1（kind: standalone / 有 patching/ 目录）** | **clone 未带 `-b refactor/v2.0.0`，默认分支 github_sync 同步前仍是 V1** | 删目录按第 1.1 节带分支重装；V1 用户升级见第 6 节 |
| 卡片建出来了但旧观感/字号不对 | `card_style` 配置残留（v2.0.20+ 完全 opt-in） | 删掉该配置段并重启，或按 README 配置 |
| 密码 SSH 登不上 | 服务器 sshd 加固为仅公钥 | 用密钥认证（paramiko `pkey=` / `ssh -i`） |
| 改了代码行为不变 | `__pycache__` 旧字节码 | 清缓存重启（第 5 节命令） |
| 回合卡片引用不了消息 | 锚点非 `om_` 前缀（cron/后台合成 id） | 属预期：非 om_ 直发（reply API 必败 99992354） |
| 点澄清卡按钮提示「该应用尚未配置卡片回调」 | 飞书开放平台未订阅卡片回调（非插件缺陷，插件与内核回调链路已就绪） | 开发者后台 → 事件与回调 → 卡片交互回调 → 选「使用长连接接收回调」保存即生效；勿选 webhook（无公网回调端点） |
| clarify 等待期卡片直接「已完成」/回调后另开新卡 | `clarify_suspend_sec` 被设为 0（v2.0.26 暂停语义关闭） | 删掉该配置或设 ≥1（默认 1.2s）后重启；v2.0.25 及以下无此能力，升级见第 6 节 |
