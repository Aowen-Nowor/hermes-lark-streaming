<div align="center">

# hermes-lark-streaming

**飞书 CardKit v2.0 流式卡片平台插件 · hermes-agent**

[![version](https://img.shields.io/badge/version-2.0.29-brightgreen)](#更新日志)
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

## 分支模型

| 分支 / tag | 角色 |
|---|---|
| `github_sync`（默认分支 = 主分支） | **V2 正式版**。维护者将 V2 同步进主分支即视为上线；安装与升级（含 `hermes plugins update`）以此为准 |
| `refactor/v2.0.0` | 维护者开发分支，普通用户无需接触 |
| `v2.0.x` tag | 每个正式版本 |

## 特性

| 特性 | 说明 |
|------|------|
| 流式卡片（核心） | 回答以打字机效果写入 CardKit 卡片；两步优雅定稿，页脚呈现耗时/模型/上下文统计 |
| native streaming（v2.0.13） | 答案流走官方 `send_stream_frame` 原生帧——工具边界不封卡，单卡全回合；帧失败自动回落 edit/send；`native_streaming: false` 回 edit-based |
| agent loop 状态面板 | 工具进度与思考实时渲染：动态标题、思考按轮分组、单工具耗时、官方图标；纯 answer 回合无空面板（v2.0.24 懒渲染） |
| 回复引用（v2.0.22） | 卡片以飞书原生 reply 形态引用触发提问（私聊/群聊）；cron/后台投递仍直发，reply 失败自动降级 |
| clarify 暂停/续流（v2.0.26） | 澄清等待期流式卡保持「进行中」+ 面板暂停行，选择后**同一张卡**续流至定稿——一次澄清回合一张卡 |
| 交互卡片 | 澄清请求渲染为按钮/输入卡片，三态反馈，操作者授权校验 fail-closed |
| 回合外投递（v2.0.14） | cron 定时任务响应卡片化（任务名标题 + job_id 溯源页脚）；回合外定稿与 ≥200 字长文同走静态卡 |
| 平台通知轨（v2.0.15） | 子代理失败/超时通知并入面板或 early-create 回合卡，不再孤挂原生文本 |
| 稳定性 | 300317 sequence 自愈、per-chat 帧串行化、孤儿卡看门狗、空闲 GC、继承面 fail-fast 探测 |

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

> 🤖 **AI agent 部署？** 读 [`docs/SKILL.md`](docs/SKILL.md)（完整安装/启动/健康验证/升级回滚手册）。

```bash
# 1. 克隆主分支（V2 正式版）到 hermes 插件目录
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
plugins: {stream_reasoning_deltas: true}   # 思考隐藏流 → 面板思考行
```

重启网关，日志出现 `✓ feishu-streaming connected` 即生效。**回滚**：平台名改回 `feishu`。

## 从 V1（1.x）升级

共用同一插件目录 `~/.hermes/plugins/hermes-lark-streaming/`；agent 可执行步骤见 [`docs/SKILL.md`](docs/SKILL.md) 第 6 节。

**① 前提**：hermes-agent **≥ v0.22**（V1 常见 v0.21，先升本体）；lark-oapi ≥ 1.6.4。

**② 换代码**（停网关后）：

```bash
pkill -f 'python -m gateway.run'; sleep 3; rm -f ~/.hermes/gateway.lock
cd ~/.hermes/plugins/hermes-lark-streaming
git fetch --tags && git checkout v2.0.29   # 或 hermes plugins update（主分支已同步 V2 后）
```

**③ config.yaml 三处改动**

| # | 位置 | V1 | V2 |
|---|---|---|---|
| ① | `plugins.enabled` | `- hermes-lark-streaming` | `- feishu-streaming-platform` |
| ② | `platforms` | 只开原生 `feishu` | `feishu: enabled: false` + `feishu-streaming: enabled: true`（凭证平移；二选一防双回复） |
| ③ | 配置节 | `hermes_lark_streaming:` | 改名 `feishu_streaming:`（不改也自动兼容） |

**④ 验证**：清 `__pycache__` → 重启 → 五项健康检查（SKILL 第 3 节）→ 发一句话，卡片应引用提问并流式打字。**回滚**：`git checkout v1.8.4` + ②③改回 + 重启。

## 配置

插件配置节 `feishu_streaming`（完整键位与出站帧契约见 [docs/API.md](docs/API.md)）：

| 键 | 默认 | 说明 |
|---|---|---|
| `print_strategy` / `print_step` | `delay` / `4` | 打字机策略与步长 |
| `card_ttl_sec` | `600` | 卡片会话 TTL（秒，下限 60 钳制） |
| `streaming_panel_expanded` | `true` | 状态面板默认展开 |
| `native_streaming` | `true` | native streaming 通道；false 回 edit-based 流 |
| `oneshot_cards` | `true` | 回合外投递一次性卡；false 全部回原生文本 |
| `clarify_suspend_sec` | `1.2` | clarify 暂停窗口（v2.0.26）；0 = 关闭（回 v2.0.25 行为） |
| `footer.fields` / `footer.show_label` | — | 定稿页脚字段（status/elapsed/model 等）；`display.show_reasoning: true` 时默认自动追加 `reasoning` |
| `card_style.text_size.answer` / `.aux` | 未配置（零注入） | 正文/辅助行字号（`{default, pc, mobile}` 三档，完全 opt-in） |

## 仓库结构

```text
hermes-lark-streaming/
├── plugin.yaml                # 插件清单（版本单一来源）
├── __init__.py / __main__.py  # 根目录仅有的两个运行时 .py（官方加载器以插件目录为包，唯一契约是根 __init__.py 暴露 register）
├── plugin/ · cardkit/ · composer/ · feishu/ · config/ · aowen/
│                              # hermes 集成层 / 卡片层 / 帧重组引擎 / 客户端 / 配置 / 指令
├── tests/                     # 771 用例（单元 + e2e mock 全链路）
├── scripts/                   # e2e_v2 / smoke_v2 / feishu_notify / create_release
├── docs/                      # SKILL.md（agent 手册）· API.md（配置与契约）· CHANGELOG.md
└── assets/screenshots/ · pyproject.toml · LICENSE
```

## 测试与 E2E

```bash
python -m pytest tests/                    # 单元 + e2e mock（771 用例）
python scripts/smoke_v2.py                 # 冒烟（无飞书凭证）
export FEISHU_E2E_APP_ID=cli_xxx FEISHU_E2E_APP_SECRET=xxx FEISHU_E2E_CHAT_ID=oc_xxx
python scripts/e2e_v2.py --scenario all    # 真飞书 E2E（11 场景）
```

`reply` 场景即 v2.0.22 引用验收（群聊必验；`FEISHU_E2E_DM_CHAT_ID` 可选私聊双验证）。版本水印自动跟随 plugin.yaml。

## CI 与工作流

Gitee 为主仓库，GitHub 为同步备份（双向均自动通知飞书）。分支模型见上文：维护者将 V2 同步进主分支 `github_sync` 即触发 Gitee 发版流水线（与 V1 流程一致）。

| 工作流 | 平台 | 触发 | 作用 |
|--------|------|------|------|
| `sync-from-gitee.yml` | GitHub Actions | 每小时 | Gitee → GitHub 全分支+tag 强制镜像；变更时跑全量测试；新版本自动建 GitHub Release；**Gitee PR/Issue 增量摘要 → 飞书** |
| `feishu-notify.yml` | GitHub Actions | PR/Issue 事件 | GitHub 新 PR/Issue（开/重开/关）实时推送飞书卡片 |
| `hermes-release-watch.yml` | GitHub Actions | 每日 | hermes-agent 上游发新版 → 飞书提醒（人工跑 E2E 后再升级生产） |
| `.workflow/release-pipeline.yml` | Gitee Go | push 到 `github_sync` | 自动创建 Gitee Release（tag = plugin.yaml 版本） |

**所需 Secrets**（GitHub Settings → Secrets and variables → Actions；Gitee Go 在流水线参数设置）：`FEISHU_WEBHOOK`（飞书群机器人 webhook）、`FEISHU_SIGN_SECRET`（开启签名时）、`GITEE_USERNAME` / `GITEE_TOKEN`（同步拉取 + PR/Issue API）。

## FAQ

| 问题 | 答案 |
|---|---|
| 与内置 `feishu` 平台什么关系？ | 适配器完整继承内置 `FeishuAdapter`，仅重写出站语义入口，把官方 metadata 标记的帧重组为 CardKit 卡片 |
| 会影响其它平台吗？ | 不会。只注册 `feishu-streaming`，不触碰任何全局对象 |
| hermes 升级后会坏吗？ | 出站协议均为官方接口；启动 fail-fast 探测明确报失配点；上游发新版时 `hermes-release-watch` 推飞书提醒 |
| 卡片渲染失败？ | 当前回合立即降级原生文本（降级红线），后续帧继续原生直到下一回合；日志保留完整原因 |
| 点澄清卡提示「尚未配置卡片回调」？ | [开发者后台](https://open.feishu.cn) → 你的应用 → 事件与回调 → 卡片交互回调 → 选「**使用长连接接收回调**」保存即生效（勿选 webhook，网关无公网回调端点） |

## 更新日志

见 [docs/CHANGELOG.md](docs/CHANGELOG.md) —— 每版本「版本+日期」标题 + 全表格记录。

## License

MIT
