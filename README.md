<div align="center">

# hermes-lark-streaming

**飞书 CardKit v2.0 流式卡片平台插件 · hermes-agent**

[![version](https://img.shields.io/badge/version-2.0.25-brightgreen)](#更新日志)
[![platform](https://img.shields.io/badge/platform-feishu--streaming-teal)](#快速开始)
[![hermes](https://img.shields.io/badge/hermes--agent-%E2%89%A50.22-grey)](https://github.com/NousResearch/hermes-agent)
[![python](https://img.shields.io/badge/python-3.14-informational)](#快速开始)
[![tests](https://img.shields.io/badge/tests-754%20passed-brightgreen)](#测试与-e2e)
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

**流式卡片（核心）**
- 回答以打字机效果写入 CardKit 卡片（实体级 `stream_element` 流式更新）
- **native streaming 通道（v2.0.13）**：适配器声明官方 `SUPPORTS_NATIVE_STREAMING`，答案流走 `send_stream_frame` 原生帧——工具边界不再封卡，单卡全回合；任何帧失败自动回落 edit/send（帧永不丢）；`feishu_streaming.native_streaming` 可一键回 edit-based
- 工具进度与思考过程实时渲染进卡片状态面板：`agent loop · N 轮 · M 个工具 · 耗时` 动态标题、思考按轮分组（`第 N 轮 · X.Xs`）、思考正文 markdown 渲染（灰色小字号，`**加粗**`/`*斜体*`/`` `代码` `` 生效）、单工具耗时（`Terminal (2.3 s)`）、官方图标体系
- 两步优雅定稿：终值落盘 + `close_streaming`，页脚呈现耗时 / 模型 / 上下文统计；纯回答回合自动移除空面板壳（v2.0.13）

**回合外投递（v2.0.14）**
- **cron 定时任务响应卡片化**：任务名标题行 + markdown 正文 + job_id 溯源页脚（此前为富文本 post）；成功/失败/队列重发三种投递同源覆盖（官方 `metadata["job_id"]` 精确识别，零启发式）
- 回合外定稿（非流式单发回复）与 ≥200 字主动长文（send_message 工具族）同走静态卡通道；`feishu_streaming.oneshot_cards` 一键回原生

**平台通知轨（v2.0.15）**
- 子代理失败/超时通知（`⚠️ Subagent failed/timed out` 官方固定前缀）豁免进度行 300 字符上限：有活跃卡并入面板（warning_outlined 通报行样式），无活跃卡 early-create 回合卡——不再孤挂原生 post 文本

**面板视觉（v2.0.16）**
- 折叠面板轮次三色（V1 对齐）：流式进行中思考轮橙色、已完成绿、定稿收口全绿；警告族通报行（⚠️/🚨）橙色醒目，其余通报行保持灰
- Terminal/shell 工具行 `code_outlined` 代码图标（原齿轮）；cron 卡头部去图标（job_id 溯源页脚保留）

**回复引用与调用步骤（v2.0.22）**
- **卡片回复引用触发消息**（私聊/群聊都生效）：卡片以飞书原生 reply 形态回应你的提问，群聊一眼看出在回应谁；cron/后台投递（无 om_ 锚点）仍直发，reply 失败自动降级直发（内容永不丢）
- **连续 terminal 调用渲染为独立步骤行**：每次工具调用一个带图标步骤（`Terminal (169 ms)`），命令归属一目了然、耗时不再丢失（hermes 连续调用省略 header 的裸块归属 terminal）

**交互卡片**
- 澄清请求渲染为按钮/输入卡片，点击即回复，三态反馈（选择 → 已提交 → 已确认），操作者授权校验 fail-closed
- `/aowen help · status · monitor` 运行状态指令

**稳定性**
- CardKit `300317` sequence 冲突主流程自愈（seq 对齐重发）
- per-chat 帧串行化：建卡窗口内并发帧不再重复建卡（v2.0.11）
- 空闲回收 GC：活跃长回合永不误杀；元素超限自动降级
- 继承面 fail-fast 探测：hermes 上游重构在启动日志明确报出失配点，绝不静默出错

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

> 🤖 **AI agent 部署？** 请读 [`SKILL.md`](SKILL.md)——面向 agent 的完整安装/启动/健康验证/V1→V2 迁移/升级回滚手册（含网关启动三要点、健康五项检查、V1 迁移三处配置、已知环境坑速查表）。人类读者继续往下即可。

> ⚠ **git 安装必须带分支参数**：仓库默认分支 `github_sync` 仍是 V1（1.8.4）。V2 只在 `refactor/v2.0.0` 分支与 `v2.0.x` tag 上：`git clone -b refactor/v2.0.0 <REPO_URL>`；老用户 **勿用 `hermes plugins update` 升 V2**（它拉默认分支，拿不到 V2）。

```bash
# 1. 复制到 hermes 插件目录
cp -r hermes-lark-streaming ~/.hermes/plugins/

# 2. 依赖（lark-oapi >= 1.6.4 + PyYAML）
pip install 'lark-oapi>=1.6.4' PyYAML
```

> **依赖装进 hermes 的激活解释器环境**：hermes v0.21.x 的 PM 运行时会把它选代的
> 依赖环境设为进程的 site-packages（`~/.hermes/installs/<id>/environments/<gen>/venv`，
> 见 `pm/environments.py::activate_dependencies`），插件依赖必须装进**该环境**才能被
> 插件 import（manifest `python_dependencies` 官方契约是仅校验提示、不自动安装）。
> 判别方法：gateway 日志若插件报 `ModuleNotFoundError` 而 `pip list` 可见，即为环境
> 错位——对 `$HOME/.hermes/installs/*/environments/*/venv/bin/python` 执行上述 pip。

> Python 版本与 hermes 官方对齐：**3.14**（`requires-python = ">=3.11,<3.15"`）。

在 `config.yaml` 中启用平台（与内置 `feishu` 二选一）：

```yaml
platforms:
  feishu:
    enabled: false            # 关内置（可选，环境变量凭证不再覆盖显式禁用）
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

V1（1.8.x 及更早，钩子拦截形态）与 V2 **共用同一个插件目录** `~/.hermes/plugins/hermes-lark-streaming/`，升级 = 换代码 + 三处配置 + 重启，约 5 分钟。agent 可执行的完整步骤（含两种安装路径的甄别、迁移失败速查）见 [`SKILL.md`](SKILL.md) 第 6 节。

**① 前提**：hermes-agent **≥ v0.22**（V1 常见 v0.21，先升 hermes 本体）；lark-oapi ≥ 1.6.4（V1 1.8.0 起同 floor，一般零动作）。

**② 换代码**（停网关后）——不要用 `hermes plugins update`（默认分支仍是 V1）：

```bash
pkill -f 'python -m gateway.run'; sleep 3; rm -f ~/.hermes/gateway.lock
cd ~/.hermes/plugins/hermes-lark-streaming
git fetch --tags && git checkout v2.0.25
git status --short   # 确认无残留：V1 的 controller/ patching/ state/ flush/ 应已消失
```

**③ config.yaml 三处改动**

| # | 位置 | V1 | V2 |
|---|---|---|---|
| ① | `plugins.enabled` | `- hermes-lark-streaming` | `- feishu-streaming-platform` |
| ② | `platforms` | 只开着原生 `feishu` | `feishu: enabled: false` + 新增 `feishu-streaming: enabled: true`（凭证平移；**二选一，防双回复**） |
| ③ | 配置节 | `hermes_lark_streaming:` | 改名 `feishu_streaming:`（**不改也自动兼容**；V1 的 9 个死键留着零副作用，可顺手删） |

**④ 重启验证**：清 `__pycache__` → 启动网关 → 日志健康五项（[`SKILL.md`](SKILL.md) 第 3 节）→ 群聊与私聊各发一句话，卡片应**引用你的提问**（v2.0.22+）并流式打字。

**回滚**：`git checkout v1.8.4` + 表中 ②③ 改回 + 重启，即回 V1（V2 无任何运行时残留）。

## 配置

插件配置节 `feishu_streaming`（V1 的 `hermes_lark_streaming` 节自动迁移）：

| 键 | 默认 | 说明 |
|---|---|---|
| `print_strategy` | `delay` | 打字机策略（`delay` / `fast`） |
| `print_step` | `4` | 打字机步长 |
| `card_ttl_sec` | `600` | 卡片会话 TTL（秒） |
| `streaming_panel_expanded` | `true` | 状态面板默认展开 |
| `native_streaming` | `true` | native streaming 通道（v2.0.13）：false 回 edit-based 流（v2.0.12 行为） |
| `oneshot_cards` | `true` | 回合外投递一次性卡（v2.0.14）：cron 投递族 / 回合外 final / ≥200 字长文；false 全部回原生文本（v2.0.13 行为） |
| `footer.fields` / `footer.show_label` | — | 定稿页脚字段（elapsed/model/cost/status 等）；`display.show_reasoning: true` 且 `agent.reasoning_effort` 有效时默认字段自动追加 `reasoning`（推理等级），也可在 fields 中显式添加 |
| `card_style.text_size.answer` | 未配置（零注入） | 正文（answer 元素）字号，v2.0.20 完全 opt-in：配置后按 `{default, pc, mobile}` 三档生效，未写的档位回落 `normal_v2`（v2.0.18 观感）；值域为官方 13 档 token（heading-0 30px ~ x-small 10px），手机观感偏小可设 `mobile: x-large`；未配置时不注入 `config.style`，payload 与 v2.0.18 逐字段一致 |
| `card_style.text_size.aux` | 未配置（零注入） | 辅助行（面板思考/工具行/警告/页脚）字号，同上三档结构，未写档位回落 `notation` |

> v2.0.17 移除 V1 遗留死键：`linear` / `panel_expanded` / `flush_interval_ms` /
> `answer_flush_interval_ms` / `feishu_base_url` / `show_reasoning` / `gateway_cards` /
> `max_tool_steps` / `max_reasoning_rounds` —— V2 引擎从未消费；陈旧键留在
> config.yaml 中无副作用，平台启停由 hermes 平台配置（`platforms.feishu-streaming`）负责。

完整说明见 [docs/API.md](docs/API.md)。

## 仓库结构

```text
hermes-lark-streaming/
├── plugin.yaml               # 插件清单（kind: platform · 版本单一来源）
├── __init__.py               # 包入口（register 导出 + __version__）——根目录唯一运行时 .py 之一
├── __main__.py               # python -m 诊断入口——根目录唯一运行时 .py 之二
├── plugin/                   # hermes 集成层（对 hermes 的适配与钩子）
│   ├── adapter.py            #   FeishuStreamingAdapter（出站语义层重写）
│   ├── clarify.py            #   澄清卡片闭环（三态反馈 + fail-closed 授权）
│   ├── reasoning_feed.py     #   思考隐藏流 → 面板思考行（官方观察者钩子）
│   ├── tool_timing.py        #   post_tool_call 钩子 → 单工具耗时
│   └── __init__.py           #   注册期辅助（lark-oapi 门槛 / 配置注入）
├── cardkit/                  # CardKit v2 卡片层（构建/元素/seq 自愈/官方图标）
├── composer/                 # 帧重组引擎（分类 + per-chat 状态机 + 定稿）
├── feishu/                   # FeishuClient（CardKit REST + 瞬态重试）
├── config/                   # 配置读取（config.yaml → Config）
├── aowen/                    # /aowen 指令
├── tests/                    # 707 用例（单元 + e2e mock 全链路）
├── scripts/                  # e2e_v2 真飞书验收 / smoke_v2 冒烟 / release 工具
├── docs/                     # API.md
├── assets/screenshots/       # 实景截图
├── CHANGELOG.md              # 版本历史（类型|问题|原因|修复 表格）
└── pyproject.toml · LICENSE · README.md
```

> **为什么根目录只有两个 .py？** hermes 目录插件加载器把插件目录本身作为
> Python 包导入（`spec_from_file_location`，`__path__` 指向插件目录），之后
> 所有相对导入走标准 import 机制——加载器唯一契约是根 `__init__.py` 暴露
> `register`，子包布局对加载器不可见。因此运行时模块全部收进 `plugin/`（hermes
> 集成层）与引擎子包，根目录只保留两个入口脚本与文档/配置。测试与脚本通过
> `tests/conftest.py` 的同款引导把仓库根注册为 `hermes_lark_streaming` 包。

**Python 版本**：与 hermes 官方完全对齐——`requires-python = ">=3.11,<3.15"`（官方唯一受支持运行时为 **3.14**，3.11 下限是官方更新器契约，见 hermes-agent `pyproject.toml`）。

## 测试与 E2E

```bash
# 单元 + e2e mock 全链路（542 用例）
python -m pytest tests/

# 冒烟（无飞书凭证）
python scripts/smoke_v2.py

# 真飞书 E2E（11 场景：connect / answer / progress / interim / full / burst / native / clarify / plain / oneshot / reply）
export FEISHU_E2E_APP_ID=cli_xxx FEISHU_E2E_APP_SECRET=xxx FEISHU_E2E_CHAT_ID=oc_xxx
python scripts/e2e_v2.py --scenario all
```

`burst` 场景即 v2.0.11 修复的验收：并发帧 gather 投递，断言恒为单卡片。
`oneshot` 场景即 v2.0.14 修复的验收：cron 投递一次性定稿卡（官方包装头解析 + 标题行 + job_id 溯源页脚）。
`reply` 场景即 v2.0.22 修复的验收：om_ 锚点 → 卡片以 reply 形态引用（群聊必验；`FEISHU_E2E_DM_CHAT_ID` 可选配私聊双验证）+ 伪锚点降级直发；full 场景含 v2.0.22 连续 terminal 裸围栏块独立步骤行归属。版本水印自动跟随 plugin.yaml，无硬编码。

## FAQ

**Q: 与内置 `feishu` 平台是什么关系？**
A: 适配器继承内置 `FeishuAdapter`，内置能力一个不少；仅重写 `send` / `edit_message` 等出站语义入口，把 hermes 官方 metadata 标记的帧重组为 CardKit 卡片。

**Q: 会不会影响其它平台（QQ/TG 等）？**
A: 不会。只注册 `feishu-streaming` 平台，不触碰任何全局对象。

**Q: hermes 升级后会坏吗？**
A: 出站帧协议（metadata 标记）与适配器接口都是官方协议；启动时对继承面 fail-fast 探测，上游重构会在启动日志明确报出失配点，而不是静默出错。

**Q: 卡片渲染失败会怎样？**
A: 当前回合立即降级为原生文本消息（降级红线），后续帧继续原生直到下一回合。日志保留完整原因（`composer: card channel degraded …`）。

**Q: 点澄清卡按钮提示「该应用尚未配置卡片回调」？**
A: 这是飞书开放平台侧的应用配置项，不是插件缺陷（插件与 hermes 内核的回调处理链路已就绪）。到[开发者后台](https://open.feishu.cn) → 你的应用 → 事件与回调 → 卡片交互回调 → 编辑 → 选择「**使用长连接接收回调**」→ 保存即生效（免公网、免重启网关）。注意勿选 webhook 模式（网关不提供公网回调端点）。

## 更新日志

见 [CHANGELOG.md](CHANGELOG.md) —— 每版本一张「类型 | 问题 | 原因 | 修复」表格，附审计方法与已知限制。

## License

MIT
