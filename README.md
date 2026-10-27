<div align="center">

# hermes-lark-streaming

**飞书 CardKit v2.0 流式卡片平台插件 · hermes-agent**

[![version](https://img.shields.io/badge/version-2.0.27-brightgreen)](#更新日志)
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

- **零 monkey-patch**：不修改、不包装、不拦截 hermes 任何内部实现，纯官方 `register_platform` 协议注册
- **随时回滚**：平台名 `feishu-streaming` ↔ `feishu` 一改即切回原生，无任何运行时残留
- **永不丢消息**：卡片 API 任何失败（限流/权限/网络）→ 当前回合立即降级原生文本

## 特性

| 特性 | 说明 |
|------|------|
| 流式卡片（核心） | 回答以打字机效果写入 CardKit 卡片（实体级 `stream_element` 流式更新）；两步优雅定稿，页脚呈现耗时/模型/上下文统计 |
| native streaming（v2.0.13） | 答案流走官方 `send_stream_frame` 原生帧——工具边界不封卡，单卡全回合；任何帧失败自动回落 edit/send；`native_streaming: false` 可一键回 edit-based |
| agent loop 状态面板 | 工具进度与思考过程实时渲染：动态标题（N 轮 · M 个工具 · 耗时）、思考按轮分组、单工具耗时（`Terminal (2.3 s)`）、官方图标体系、流式思考轮橙色/定稿全绿、纯 answer 回合无空面板（v2.0.24 懒渲染） |
| 回复引用（v2.0.22） | 卡片以飞书原生 reply 形态引用触发提问（私聊/群聊）；cron/后台投递（无 om_ 锚点）仍直发，reply 失败自动降级直发（内容永不丢） |
| clarify 暂停/续流（v2.0.26） | 澄清等待期间流式卡保持「进行中」+ 面板暂停行，用户选择后**同一张卡**续流至定稿——一次澄清回合一张卡 |
| 交互卡片 | 澄清请求渲染为按钮/输入卡片，点击即回复，三态反馈（选择 → 已提交 → 已确认），操作者授权校验 fail-closed |
| 回合外投递（v2.0.14） | cron 定时任务响应卡片化（任务名标题 + job_id 溯源页脚）；回合外定稿与 ≥200 字长文同走静态卡通道 |
| 平台通知轨（v2.0.15） | 子代理失败/超时通知并入面板或 early-create 回合卡，不再孤挂原生文本 |
| 稳定性 | 300317 sequence 自愈、per-chat 帧串行化、孤儿卡看门狗、空闲回收 GC、继承面 fail-fast 探测、字号完全 opt-in（零注入） |

## 架构

```
hermes 出站帧（send / edit_message）
        │
        ▼
classify_send / classify_edit ── 四通道 + plain 分类（metadata 官方标记 + 文本启发式）
        │
        ▼
FrameComposer（per-chat 串行状态机 · CardTurnState）
        ├── CH_STREAM    → cardkit_create → stream_element 打字机 → 两步定稿
        ├── CH_PROGRESS  → 状态面板（幂等合并 / pending→preview 取代 / 思考轮次）
        ├── CH_INTERIM   → 并入面板或原生文本（永不建卡）
        ├── CH_FINAL     → 定稿页脚
        └── CH_PLAIN     → 原生路径（原样透传内置适配器）
```

辅助回灌通道（同为官方协议）：`on_stream_delta` 观察者钩子 → 思考隐藏流入面板；`post_tool_call` 生命周期钩子 → 单工具耗时。

## 快速开始

> 🤖 **AI agent 部署？** 请读 [`docs/SKILL.md`](docs/SKILL.md)——面向 agent 的完整安装/启动/健康验证/V1→V2 迁移/升级回滚手册（网关启动三要点、健康五项检查、已知环境坑速查表）。

> ⚠ **git 安装必须带分支参数**：仓库默认分支 `github_sync` 仍是 V1（1.8.4）。V2 只在 `refactor/v2.0.0` 分支与 `v2.0.x` tag 上：`git clone -b refactor/v2.0.0 <REPO_URL>`；老用户 **勿用 `hermes plugins update` 升 V2**（它拉默认分支，拿不到 V2）。

```bash
# 1. 复制到 hermes 插件目录
cp -r hermes-lark-streaming ~/.hermes/plugins/

# 2. 依赖（lark-oapi >= 1.6.4 + PyYAML）
pip install 'lark-oapi>=1.6.4' PyYAML
```

> **依赖装进 hermes 的激活解释器环境**：hermes v0.21.x 的 PM 运行时会把它选代的依赖环境设为进程 site-packages（`~/.hermes/installs/<id>/environments/<gen>/venv`），插件依赖必须装进**该环境**。判别方法：插件报 `ModuleNotFoundError` 而 `pip list` 可见，即为环境错位。

> Python 与 hermes 官方对齐：**3.14**（`requires-python = ">=3.11,<3.15"`）。

在 `config.yaml` 中启用平台（与内置 `feishu` 二选一）：

```yaml
platforms:
  feishu:
    enabled: false            # 关内置（可选）
  feishu-streaming:
    enabled: true
    extra:
      app_id: cli_xxx
      app_secret: xxx
```

建议同时开启思考与工具进度（状态面板的数据源）：

```yaml
display:
  platforms:
    feishu-streaming:
      thinking_progress: true
  tool_progress: all
streaming:
  enabled: true
plugins:
  stream_reasoning_deltas: true   # 思考隐藏流 → 面板思考行
```

重启网关，日志出现 `✓ feishu-streaming connected` 即生效。**回滚**：平台名改回 `feishu`。

## 从 V1（1.x）升级

V1（1.8.x 及更早，钩子拦截形态）与 V2 **共用同一个插件目录** `~/.hermes/plugins/hermes-lark-streaming/`，升级 = 换代码 + 三处配置 + 重启，约 5 分钟。agent 可执行的完整步骤见 [`docs/SKILL.md`](docs/SKILL.md) 第 6 节。

**① 前提**：hermes-agent **≥ v0.22**（V1 常见 v0.21，先升 hermes 本体）；lark-oapi ≥ 1.6.4（V1 1.8.0 起同 floor，一般零动作）。

**② 换代码**（停网关后）——不要用 `hermes plugins update`（默认分支仍是 V1）：

```bash
pkill -f 'python -m gateway.run'; sleep 3; rm -f ~/.hermes/gateway.lock
cd ~/.hermes/plugins/hermes-lark-streaming
git fetch --tags && git checkout v2.0.27
git status --short   # 确认无残留：V1 的 controller/ patching/ state/ flush/ 应已消失
```

**③ config.yaml 三处改动**

| # | 位置 | V1 | V2 |
|---|---|---|---|
| ① | `plugins.enabled` | `- hermes-lark-streaming` | `- feishu-streaming-platform` |
| ② | `platforms` | 只开着原生 `feishu` | `feishu: enabled: false` + 新增 `feishu-streaming: enabled: true`（凭证平移；**二选一，防双回复**） |
| ③ | 配置节 | `hermes_lark_streaming:` | 改名 `feishu_streaming:`（**不改也自动兼容**；V1 的 9 个死键留着零副作用，可顺手删） |

**④ 重启验证**：清 `__pycache__` → 启动网关 → 日志健康五项（[`docs/SKILL.md`](docs/SKILL.md) 第 3 节）→ 群聊与私聊各发一句话，卡片应**引用你的提问**（v2.0.22+）并流式打字。

**回滚**：`git checkout v1.8.4` + 表中 ②③ 改回 + 重启，即回 V1（V2 无任何运行时残留）。

## 配置

插件配置节 `feishu_streaming`（V1 的 `hermes_lark_streaming` 节自动迁移）：

| 键 | 默认 | 说明 |
|---|---|---|
| `print_strategy` / `print_step` | `delay` / `4` | 打字机策略与步长 |
| `card_ttl_sec` | `600` | 卡片会话 TTL（秒，下限 60 钳制） |
| `streaming_panel_expanded` | `true` | 状态面板默认展开 |
| `native_streaming` | `true` | native streaming 通道；false 回 edit-based 流 |
| `oneshot_cards` | `true` | 回合外投递一次性卡（cron 投递族 / 回合外 final / ≥200 字长文）；false 全部回原生文本 |
| `clarify_suspend_sec` | `1.2` | clarify 暂停窗口秒数（v2.0.26）：窗口内等回调 → 同卡续流；0 = 关闭（回 v2.0.25 行为：卡先完成、回调后开新卡） |
| `footer.fields` / `footer.show_label` | — | 定稿页脚字段（elapsed/model/cost/status 等）；`display.show_reasoning: true` 时默认自动追加 `reasoning` |
| `card_style.text_size.answer` / `.aux` | 未配置（零注入） | 正文/辅助行字号，v2.0.20 完全 opt-in：`{default, pc, mobile}` 三档，手机观感偏小可设 `mobile: x-large`；未配置时 payload 与 v2.0.18 逐字段一致 |

> v2.0.17 已移除 V1 遗留死键（`linear` / `panel_expanded` / `flush_interval_ms` 等 9 个）——陈旧键留在 config.yaml 中零副作用。

完整键位说明与出站帧契约见 [docs/API.md](docs/API.md)。

## 仓库结构

```text
hermes-lark-streaming/
├── plugin.yaml               # 插件清单（kind: platform · 版本单一来源）
├── __init__.py               # 包入口（register 导出 + __version__）——根目录唯一运行时 .py 之一
├── __main__.py               # python -m 诊断入口——根目录唯一运行时 .py 之二
├── plugin/                   # hermes 集成层（adapter / clarify / reasoning_feed / tool_timing）
├── cardkit/                  # CardKit v2 卡片层（构建/元素/seq 自愈/官方图标/md 渲染）
├── composer/                 # 帧重组引擎（分类 + per-chat 状态机 + 定稿 + 暂停窗口）
├── feishu/                   # FeishuClient（CardKit REST + 瞬态重试）
├── config/                   # 配置读取（config.yaml → Config）
├── aowen/                    # /aowen 指令
├── tests/                    # 771 用例（单元 + e2e mock 全链路）
├── scripts/                  # e2e_v2 真飞书验收 / smoke_v2 冒烟 / feishu_notify 通知 / create_release 发版
├── docs/                     # SKILL.md（agent 安装手册）· API.md（配置与契约）· CHANGELOG.md
├── assets/screenshots/       # 实景截图
└── pyproject.toml · LICENSE · README.md
```

> **为什么根目录只有两个 .py？** hermes 目录插件加载器把插件目录本身作为 Python 包导入，唯一契约是根 `__init__.py` 暴露 `register`，子包布局对加载器不可见。测试与脚本通过 `tests/conftest.py` 的同款引导把仓库根注册为 `hermes_lark_streaming` 包。

## 测试与 E2E

```bash
# 单元 + e2e mock 全链路（771 用例）
python -m pytest tests/

# 冒烟（无飞书凭证）
python scripts/smoke_v2.py

# 真飞书 E2E（11 场景：connect / answer / progress / interim / full / burst / native / clarify / plain / oneshot / reply）
export FEISHU_E2E_APP_ID=cli_xxx FEISHU_E2E_APP_SECRET=xxx FEISHU_E2E_CHAT_ID=oc_xxx
python scripts/e2e_v2.py --scenario all
```

`reply` 场景即 v2.0.22 引用验收（群聊必验；`FEISHU_E2E_DM_CHAT_ID` 可选配私聊双验证 + 伪锚点降级直发）。版本水印自动跟随 plugin.yaml，无硬编码。

## CI 与工作流

Gitee 为主仓库，GitHub 为同步备份（双向均自动通知飞书）。

| 工作流 | 平台 | 触发 | 作用 |
|--------|------|------|------|
| `sync-from-gitee.yml` | GitHub Actions | 每小时 | Gitee → GitHub 全分支+tag 强制镜像（`refactor/v2.0.0` + `github_sync`）；变更时跑全量测试；新版本自动创建 GitHub Release；**Gitee PR/Issue 增量摘要 → 飞书** |
| `feishu-notify.yml` | GitHub Actions | PR/Issue 事件 | GitHub 新 PR/Issue（开/重开/关）实时推送飞书卡片 |
| `hermes-release-watch.yml` | GitHub Actions | 每日 | hermes-agent 上游发新版 → 飞书提醒（人工跑 E2E 验证继承面后再升级生产） |
| `.workflow/release-pipeline.yml` | Gitee Go | push 到 `refactor/v2.0.0` | 自动创建 Gitee Release（tag = plugin.yaml 版本） |

**所需仓库 Secrets**（GitHub 仓库 Settings → Secrets and variables → Actions；Gitee Go 在流水线参数设置）：

| Secret | 用途 |
|--------|------|
| `FEISHU_WEBHOOK` | 飞书群自定义机器人 webhook 地址（所有飞书通知） |
| `FEISHU_SIGN_SECRET` | 飞书机器人签名密钥（未开启签名可留空） |
| `GITEE_USERNAME` / `GITEE_TOKEN` | Gitee 账号与令牌（同步拉取 + API 查询 PR/Issue） |

## FAQ

**Q: 与内置 `feishu` 平台是什么关系？**
A: 适配器继承内置 `FeishuAdapter`，内置能力一个不少；仅重写 `send` / `edit_message` 等出站语义入口，把 hermes 官方 metadata 标记的帧重组为 CardKit 卡片。

**Q: 会不会影响其它平台（QQ/TG 等）？**
A: 不会。只注册 `feishu-streaming` 平台，不触碰任何全局对象。

**Q: hermes 升级后会坏吗？**
A: 出站帧协议（metadata 标记）与适配器接口都是官方协议；启动时对继承面 fail-fast 探测，上游重构会在启动日志明确报出失配点，而不是静默出错。`hermes-release-watch` 工作流会在上游发新版时推送飞书提醒。

**Q: 卡片渲染失败会怎样？**
A: 当前回合立即降级为原生文本消息（降级红线），后续帧继续原生直到下一回合。日志保留完整原因（`composer: card channel degraded …`）。

**Q: 点澄清卡按钮提示「该应用尚未配置卡片回调」？**
A: 这是飞书开放平台侧的应用配置项，不是插件缺陷。到[开发者后台](https://open.feishu.cn) → 你的应用 → 事件与回调 → 卡片交互回调 → 编辑 → 选择「**使用长连接接收回调**」→ 保存即生效（免公网、免重启网关）。注意勿选 webhook 模式（网关不提供公网回调端点）。

## 更新日志

见 [docs/CHANGELOG.md](docs/CHANGELOG.md) —— 每版本「版本+日期」标题 + 全表格精简记录。

## License

MIT
