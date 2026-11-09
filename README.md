<div align="center">

# hermes-lark-streaming

**飞书 CardKit v2.0 流式卡片平台插件 · hermes-agent**

[![version](https://img.shields.io/badge/version-2.0.32-brightgreen)](#更新日志)
[![platform](https://img.shields.io/badge/platform-feishu--streaming-teal)](#快速开始)
[![hermes](https://img.shields.io/badge/hermes--agent-%E2%89%A50.22-grey)](https://github.com/NousResearch/hermes-agent)
[![python](https://img.shields.io/badge/python-3.14-informational)](#快速开始)
[![tests](https://img.shields.io/badge/tests-771%20passed-brightgreen)](#测试与-e2e)
[![license](https://img.shields.io/badge/license-MIT-yellow)](#license)

*回答打字机流式呈现 · 工具与思考实时进面板 · 澄清问题变成可点击卡片 · 全程零 monkey-patch*

<img src="assets/screenshots/img1.png" alt="流式卡片实景：agent loop 状态面板 + 流式回答 + 定稿页脚" width="560"/>

</div>

---

## 这是什么

hermes 内置飞书平台的**增强版**：适配器完整继承内置 `FeishuAdapter`（WebSocket/webhook、媒体、threads、群门控、cron 投递……一行未改），只在出站通道上叠加一层 CardKit v2.0 流式卡片引擎。

- **零 monkey-patch**：纯官方 `register_platform` 协议注册；上游重构启动即 fail-fast 报失配点
- **随时回滚**：平台名 `feishu-streaming` ↔ `feishu` 一改即切回原生，无运行时残留
- **永不丢消息**：卡片 API 任何失败 → 当前回合立即降级原生文本

## 特性

| 特性 | 说明 |
|------|------|
| 流式卡片（核心） | 回答以打字机效果写入 CardKit 卡片；两步优雅定稿，页脚呈现耗时/模型/上下文统计 |
| 流式编辑回退 | 与内置飞书同款 edit-based 流：网关最终拼好的全量 `response`（含推理预影）才能进卡片收口，不再被原生快照吞掉 |
| agent loop 状态面板 | 工具进度与思考实时渲染：动态标题、思考按轮分组、推理定稿搬进面板（`show_reasoning=true` 时网关预影收进 `💬` 行）、官方图标；纯 answer 回合无空面板 |
| 回复引用 | 卡片以飞书原生 reply 形态引用触发提问（私聊/群聊）；cron/后台投递仍直发，reply 失败自动降级 |
| clarify 暂停/续流 | 澄清等待期流式卡保持「进行中」+ 面板暂停行，选择后**同一张卡**续流至定稿——一次澄清回合一张卡 |
| 交互卡片 | 澄清请求渲染为按钮/输入卡片，三态反馈，操作者授权校验 fail-closed |
| 回合外投递 | cron 定时任务响应卡片化（任务名标题 + job_id 溯源页脚）；回合外定稿与主动投递同走静态卡（短回复也收） |
| 平台通知轨 | 子代理失败/超时通知并入面板或 early-create 回合卡，不再孤挂原生文本 |
| 稳定性 | 300317 sequence 自愈、per-chat 帧串行化、孤儿卡看门狗、空闲 GC、继承面 fail-fast 探测 |

## 快速开始

> 🤖 **AI agent 部署？** 读 [`docs/SKILL.md`](docs/SKILL.md)（完整安装/启动/健康验证/升级回滚手册）。

```bash
# 1. 克隆到 hermes 插件目录
git clone <REPO_URL> ~/.hermes/plugins/hermes-lark-streaming

# 2. 依赖装进 hermes 运行的同一个解释器环境（判别方法见 docs/SKILL.md）
pip install 'lark-oapi>=1.6.4' PyYAML
```

在 `config.yaml` 中启用平台（与内置 `feishu` 二选一，防双回复）：

```yaml
platforms:
  feishu:
    enabled: false            # 关内置
  feishu-streaming:
    enabled: true
    extra:
      app_id: cli_xxx
      app_secret: xxx

# 建议同时开启（状态面板数据源）：
display:
  platforms: {feishu-streaming: {thinking_progress: true}}
  tool_progress: all
streaming: {enabled: true}
```

重启网关，日志出现 `✓ feishu-streaming connected` 即生效。**回滚**：平台名改回 `feishu`。

### 桌面版安装（Windows / desktop-managed）

桌面应用（MSIX 安装包）与 CLI 安装有三处关键差异，逐条对应实测踩坑（2026-11-06，Windows 11 + 桌面版 2026.7411）：

**① 运行环境不在安装目录，且只读**
- 真实运行环境：`%LOCALAPPDATA%\hermes\installs\<install-id>\environments\<hash>\venv\`（可写）。
- 安装负载 `%LOCALAPPDATA%\WindowsApps\...agent-payload\`（D:\WindowsApps\...）下的 venv **只读且不可重建**——不要试图在它里面 `uv sync`/`pip install`，会 `os error 5 拒绝访问`。
- 依赖装进**所有** environments 下的 venv（桌面版可能存在多个环境）：
  ```bash
  UV=<桌面版自带 uv，路径如 agent-payload/tools/uv-*/uv.exe>
  for v in "$LOCALAPPDATA/hermes/installs"/*/environments/*/venv; do
    "$UV" pip install --python "$v/Scripts/python.exe" 'lark-oapi>=1.6.4' 'PyYAML>=6.0'
  done
  ```

**② `hermes pm install`（venv 与 uv.lock 失步时必须跑）**
- 症状：CLI 启动即报 `install out of sync (venv: out of sync with uv.lock)`，插件 enable 报 `pm: worker exited without a result`。
- 修复：在**网关进程之外**的终端跑 `hermes pm install`（会重新解析依赖并重建 venv，需几分钟）。首次运行会对 ffmpeg/node/npm 弹交互确认。
- ⚠️ 网关是独立常驻 PID：**重启桌面应用窗口 ≠ 重启网关**。改完 config 必须确认网关进程真正退出再启动（或在 Hermes 之外的终端 `hermes gateway restart`），否则会一直看到旧报错。

**③ 凭据必须写进 `platforms.feishu-streaming.extra`，只放 `.env` 无效**
- 网关侧平台校验（继承内置 feishu 的 `_is_connected`）只查平台配置的 `extra.app_id` 是否非空；`.env` 里的 `FEISHU_APP_ID` 只会桥接到内置 `feishu` 平台名，不会桥接到 `feishu-streaming`。
- 校验失败的表现：`Platform 'Feishu / Lark (Streaming Cards)' config validation failed` → `No adapter available for enabled feishu-streaming`。
- 正确结构见上方「快速开始」的 yaml；内置 `feishu` 同步设 `enabled: false` 防双回复。

**验证**：`grep "feishu-streaming" <网关日志> | tail` 看到 `✓ feishu-streaming connected`；桌面「消息平台」页显示 已连接 且无红色报错。首次在飞书发消息会触发 pairing，执行 `hermes pairing approve feishu-streaming <code>`（平台名是 `feishu-streaming`，配对按平台隔离）。

## 从 V1（1.x）升级

V1（standalone 钩子形态，会向 config.yaml **注入配置**）与 V2（platform 形态，零注入）实现形态不同，升级 = **先卸载干净 V1，再全新安装 V2**（不是原地换代码）。agent 可执行版本见 [`docs/SKILL.md`](docs/SKILL.md) 第 6 节。

**① 前提**：hermes-agent **≥ v0.22**（V1 常见 v0.21，先升本体）。

**② 卸载 V1**（三步，顺序不能乱——cleanup 清除 V1 注入的配置，必须在插件代码还在时执行）：

```bash
# 1. 清理 V1 注入到 config.yaml 的配置（插件代码还在时执行）
#    自动检测 Hermes Python 路径：
HERMES_PYTHON=$(python3 ~/.hermes/plugins/hermes-lark-streaming/__main__.py python)
$HERMES_PYTHON ~/.hermes/plugins/hermes-lark-streaming/__main__.py cleanup

# 2. 卸载插件
hermes plugins uninstall hermes-lark-streaming

# 3. 重启网关
hermes gateway restart
```

**③ 全新安装 V2**：按[快速开始](#快速开始)执行。

**④ config.yaml 三处改动**

| # | 位置 | V1 | V2 |
|---|---|---|---|
| ① | `plugins.enabled` | `- hermes-lark-streaming` | `- feishu-streaming-platform` |
| ② | `platforms` | 只开原生 `feishu` | `feishu: enabled: false` + `feishu-streaming: enabled: true`（凭证平移；二选一防双回复） |
| ③ | 配置节 | `hermes_lark_streaming:` | 改名 `feishu_streaming:`（不改也自动兼容） |

**⑤ 验证**：重启 → 发一句话，卡片应引用提问并流式打字。
**回滚**：恢复 config.yaml 备份 → 按原方式重装 V1 → 重启。

## 配置

插件配置节 `feishu_streaming`（完整键位与出站帧契约见 [docs/API.md](docs/API.md)）：

| 键 | 默认 | 说明 |
|---|---|---|
| `print_strategy` / `print_step` | `delay` / `4` | 打字机策略与步长 |
| `card_ttl_sec` | `600` | 卡片会话 TTL（秒，下限 60 钳制） |
| `streaming_panel_expanded` | `true` | 状态面板默认展开 |
| `oneshot_cards` | `true` | 回合外投递一次性卡（cron / 回合外 final / 主动 plain，短回复也收）；false 全部回原生文本 |
| `clarify_suspend_sec` | `1.2` | clarify 暂停窗口；0 = 关闭 |
| `footer.fields` / `footer.show_label` | — | 定稿页脚字段（status/elapsed/model 等）；`display.show_reasoning: true` 时默认自动追加 `reasoning` |
| `card_style.text_size.answer` / `.aux` | 未配置（零注入） | 正文/辅助行字号（`{default, pc, mobile}` 三档，完全 opt-in） |

## 测试与 E2E

```bash
python -m pytest tests/                    # 单元 + e2e mock（771 用例）
python scripts/smoke_v2.py                 # 冒烟（无飞书凭证）
export FEISHU_E2E_APP_ID=cli_xxx FEISHU_E2E_APP_SECRET=xxx FEISHU_E2E_CHAT_ID=oc_xxx
python scripts/e2e_v2.py --scenario all    # 真飞书 E2E（11 场景）
```

`reply` 场景即引用回复验收（群聊必验；`FEISHU_E2E_DM_CHAT_ID` 可选私聊双验证）。版本水印自动跟随 plugin.yaml。

## FAQ

| 问题 | 答案 |
|---|---|
| 与内置 `feishu` 平台什么关系？ | 适配器完整继承内置 `FeishuAdapter`，仅重写出站语义入口，把官方 metadata 标记的帧重组为 CardKit 卡片 |
| 会影响其它平台吗？ | 不会。只注册 `feishu-streaming`，不触碰任何全局对象 |
| hermes 升级后会坏吗？ | 出站协议均为官方接口；启动 fail-fast 探测明确报失配点；上游发新版时 `hermes-release-watch` 推飞书提醒 |
| 卡片渲染失败？ | 当前回合立即降级原生文本（降级红线），后续帧继续原生直到下一回合；日志保留完整原因 |
| 点澄清卡提示「尚未配置卡片回调」？ | [开发者后台](https://open.feishu.cn) → 你的应用 → 事件与回调 → 卡片交互回调 → 选「**使用长连接接收回调**」保存即生效（勿选 webhook，网关无公网回调端点） |

## 更新日志

见 [docs/CHANGELOG.md](docs/CHANGELOG.md)。

## License

MIT
