<div align="center">

# hermes-lark-streaming

**飞书 CardKit v2.0 流式卡片平台插件 · hermes-agent**

[![version](https://img.shields.io/badge/version-2.0.12-brightgreen)](#更新日志)
[![platform](https://img.shields.io/badge/platform-feishu--streaming-teal)](#快速开始)
[![hermes](https://img.shields.io/badge/hermes--agent-%E2%89%A50.21-grey)](https://github.com/NousResearch/hermes-agent)
[![python](https://img.shields.io/badge/python-3.14-informational)](#快速开始)
[![tests](https://img.shields.io/badge/tests-542%20passed-brightgreen)](#测试与-e2e)
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
- 工具进度与思考过程实时渲染进卡片状态面板：`agent loop · N 轮 · M 个工具 · 耗时` 动态标题、思考按轮分组（`第 N 轮 · X.Xs`）、思考正文 markdown 渲染（灰色小字号，`**加粗**`/`*斜体*`/`` `代码` `` 生效）、单工具耗时（`Terminal (2.3 s)`）、官方图标体系
- 两步优雅定稿：终值落盘 + `close_streaming`，页脚呈现耗时 / 模型 / 上下文统计

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

```bash
# 1. 复制到 hermes 插件目录
cp -r hermes-lark-streaming ~/.hermes/plugins/

# 2. 依赖（lark-oapi >= 1.6.4）
pip install 'lark-oapi>=1.6.4'
```

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

## 配置

插件配置节 `feishu_streaming`（V1 的 `hermes_lark_streaming` 节自动迁移）：

| 键 | 默认 | 说明 |
|---|---|---|
| `print_strategy` | `delay` | 打字机策略（`delay` / `fast`） |
| `print_step` | `4` | 打字机步长 |
| `card_ttl_sec` | `600` | 卡片会话 TTL（秒） |
| `max_tool_steps` | `20` | 状态区工具行上限 |
| `max_reasoning_rounds` | — | 思考轮次上限 |
| `streaming_panel_expanded` | `true` | 状态面板默认展开 |
| `footer.fields` / `footer.show_label` | — | 定稿页脚字段（elapsed/model/cost/status 等） |
| `gateway_cards` | `false` | 网关级卡片投递 |

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
├── tests/                    # 542 用例（单元 + e2e mock 全链路）
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

# 真飞书 E2E（8 场景：connect / answer / progress / interim / full / burst / clarify / plain）
export FEISHU_E2E_APP_ID=cli_xxx FEISHU_E2E_APP_SECRET=xxx FEISHU_E2E_CHAT_ID=oc_xxx
python scripts/e2e_v2.py --scenario all
```

`burst` 场景即 v2.0.11 修复的验收：并发帧 gather 投递，断言恒为单卡片。

## FAQ

**Q: 与内置 `feishu` 平台是什么关系？**
A: 适配器继承内置 `FeishuAdapter`，内置能力一个不少；仅重写 `send` / `edit_message` 等出站语义入口，把 hermes 官方 metadata 标记的帧重组为 CardKit 卡片。

**Q: 会不会影响其它平台（QQ/TG 等）？**
A: 不会。只注册 `feishu-streaming` 平台，不触碰任何全局对象。

**Q: hermes 升级后会坏吗？**
A: 出站帧协议（metadata 标记）与适配器接口都是官方协议；启动时对继承面 fail-fast 探测，上游重构会在启动日志明确报出失配点，而不是静默出错。

**Q: 卡片渲染失败会怎样？**
A: 当前回合立即降级为原生文本消息（降级红线），后续帧继续原生直到下一回合。日志保留完整原因（`composer: card channel degraded …`）。

## 更新日志

见 [CHANGELOG.md](CHANGELOG.md) —— 每版本一张「类型 | 问题 | 原因 | 修复」表格，附审计方法与已知限制。

## License

MIT
