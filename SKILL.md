---
name: hermes-lark-streaming-install
description: 指导 AI agent 在 Linux 服务器上完成 hermes-lark-streaming 飞书流式卡片插件（hermes-agent 平台插件）的安装、配置、启动、健康验证与升级回滚。当用户要求"安装这个插件 / 部署到服务器 / 换个机器装一份 / 升级插件版本 / 回滚插件"时使用本 skill。
---

# hermes-lark-streaming 安装指南（agent 专用）

本插件是 hermes-agent 的飞书平台增强插件（CardKit v2.0 流式卡片）。**面向 agent 的完整安装手册**——人类视角的介绍见 `README.md`；本文假设你（agent）拿到一台服务器的 SSH 访问权，需要从零把插件跑起来并验证健康。

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
# 方式 A：git clone（推荐，便于升级/回滚）
git clone <REPO_URL> ~/.hermes/plugins/hermes-lark-streaming
# 方式 B：已有代码包 → 解压/复制到上述路径
```

### 1.2 依赖安装

插件依赖 `lark-oapi >= 1.4.24`（必须带完整 async cardkit 方法集：`acreate/acontent/abatch_update/asettings`；1.4.0 无 async 方法插件无法初始化）。装进 **hermes 运行所用的同一个 venv**：

```bash
# 找到 hermes 的 venv（常见位置：hermes-agent 仓库内 venv/ 或专用环境目录）
<VENV_PYTHON> -m pip install "lark-oapi>=1.4.24" pyyaml
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

可选配置（全部有缺省值，不配也能跑）——完整键位表见 `README.md` 配置节 / `docs/API.md`：

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
<VENV_PYTHON> scripts/e2e_v2.py --scenario full   # 9 断言，全 PASS 退出码 0
```

`full` 场景覆盖：建卡前暂存 → 首帧建卡 → 面板行 → 流式更新 → 定稿页脚。私聊场景把 `FEISHU_E2E_CHAT_ID` 换成 `FEISHU_E2E_OPEN_ID`（若脚本支持）或直接在私聊里 @ 机器人发一句话，观察卡片是否：引用你的提问消息（v2.0.22+）、流式打字、面板出现工具/思考行、页脚落定。

## 5. 升级与回滚

**升级**（已有安装，拉新版本）：
```bash
cd ~/.hermes/plugins/hermes-lark-streaming
git pull                                    # 或按版本 checkout tag
<VENV_PYTHON> -m py_compile $(git diff --name-only HEAD@{1} HEAD | grep '\.py$')  # 语法门禁
find . -name __pycache__ -type d -prune -exec rm -rf {} +   # 清缓存（热替换旧字节码必做）
pkill -f 'python -m gateway.run'; sleep 3; rm -f ~/.hermes/gateway.lock
# 再走第 2 节启动 + 第 3 节验证
```

**回滚**：`git checkout <旧tag>` 后同上重启。插件零 monkey-patch，平台名 `feishu-streaming` 改回 `feishu` 即整体停用（无运行时残留）。

## 6. 已知环境坑速查

| 症状 | 根因 | 处置 |
|---|---|---|
| 启动即退、无日志 | `~/.hermes/gateway.lock` 残留 | 删锁重启 |
| SSH 断开后进程消失 | 未用 `setsid` 脱离会话 | 用第 2 节原样命令 |
| 插件加载了但 API 全 404/attribute error | lark-oapi < 1.4.24（无 async cardkit） | 升级 lark-oapi |
| 卡片建出来了但旧观感/字号不对 | `card_style` 配置残留（v2.0.20+ 完全 opt-in） | 删掉该配置段并重启，或按 README 配置 |
| 密码 SSH 登不上 | 服务器 sshd 加固为仅公钥 | 用密钥认证（paramiko `pkey=` / `ssh -i`） |
| 改了代码行为不变 | `__pycache__` 旧字节码 | 清缓存重启（第 5 节命令） |
| 回合卡片引用不了消息 | 锚点非 `om_` 前缀（cron/后台合成 id） | 属预期：非 om_ 直发（reply API 必败 99992354） |
