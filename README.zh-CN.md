<h1 align="center">hermes-lark-streaming</h1>

<p align="center">
  <img src="https://img.shields.io/badge/项目-Vibe%20Coding-ff69b4" alt="Vibe Coding">
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/License-MIT-4caf50.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/python-3.11+-3776AB.svg" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/version-1.0.4-ff9800.svg" alt="Version">
</p>

<p align="center">
<a href="https://applink.feishu.cn/client/message/link/open?token=AmoQJk5dwczIahKlW78ADLU%3D"><img src="https://img.shields.io/badge/官方唯一交流群-中国-red" alt="官方交流群"></a>
<a href="https://larkcommunity.feishu.cn/wiki/DKkpwgMcJiglIhk88N4cqJEan5f?from=from_copylink"><img src="https://img.shields.io/badge/docs-知识库-3370FF?logo=feishu&logoColor=white" alt="知识库文档"></a>
</p>

<p align="center">
<a href="README.md">English</a> | 中文版
</p>

为 Hermes Agent 提供飞书/Lark CardKit v2.0 流式消息卡片插件 — 实时 AI 响应展示，支持打字机效果、统一可折叠面板、按时间线交错显示推理与工具调用等。

> 基于 [Cheerwhy/hermes-lark-streaming](https://github.com/Cheerwhy/hermes-lark-streaming) v0.7.0 版本 fork 后进行改造和优化
>
> ⚠️ **与上游插件不兼容** — 如已安装原版 `Cheerwhy/hermes-lark-streaming`，请先卸载后再安装本插件。

---

## 效果预览

<img src="assets/img1.png" width="20%" style="max-height: 250px; object-fit: contain; margin: 5px;" />
<img src="assets/img2.png" width="20%" style="max-height: 250px; object-fit: contain; margin: 5px;" />
<img src="assets/img3.png" width="20%" style="max-height: 250px; object-fit: contain; margin: 5px;" />
<img src="assets/img4.png" width="20%" style="max-height: 250px; object-fit: contain; margin: 5px;" />

---

## 快速开始

### 前置要求

- [Hermes Agent](https://github.com/NousResearch/hermes-agent)（已运行，已配置飞书平台）
- Hermes CLI 支持插件系统（可用 `hermes plugins` 命令）

### 安装
> 插件会自动读取 `HERMES_HOME` 环境变量定位安装路径（默认 `~/.hermes`），非默认路径下无需额外操作。

**Gitee**
> 以下两种方式任选其一即可：
```bash
# Gitee (SSH)
hermes plugins install git@gitee.com:Aowen-Nowor/hermes-lark-streaming.git
# Gitee (HTTPS)
hermes plugins install https://gitee.com/Aowen-Nowor/hermes-lark-streaming
```
**GitHub**
> 以下两种方式任选其一即可：
```bash
# GitHub (SSH)
hermes plugins install git@github.com:Aowen-Nowor/hermes-lark-streaming.git
# GitHub (HTTPS)
hermes plugins install https://github.com/Aowen-Nowor/hermes-lark-streaming
```

提示时输入 `Y` 启用插件，然后重启网关：

```bash
hermes gateway restart
```

### 更新

```bash
hermes plugins update hermes-lark-streaming
hermes gateway restart
```

### 卸载

```bash
# 1. 先清理注入的配置（插件代码还在时执行）
# 自动检测 Hermes Python 路径：
HERMES_PYTHON=$(python3 ~/.hermes/plugins/hermes-lark-streaming/__main__.py python)
$HERMES_PYTHON ~/.hermes/plugins/hermes-lark-streaming/__main__.py cleanup

# 2. 卸载插件
hermes plugins uninstall hermes-lark-streaming

# 3. 重启网关
hermes gateway restart
```

### 验证安装

```bash
hermes plugins list
grep hermes_lark_streaming ~/.hermes/logs/agent.log
# 自动检测 Hermes Python 路径：
HERMES_PYTHON=$(python3 ~/.hermes/plugins/hermes-lark-streaming/__main__.py python)
$HERMES_PYTHON ~/.hermes/plugins/hermes-lark-streaming/__main__.py status
$HERMES_PYTHON ~/.hermes/plugins/hermes-lark-streaming/__main__.py verify
```

> **排障提示**：安装后若无卡片效果，请检查：(1) `hermes plugins list` 显示插件已启用；(2) `~/.hermes/plugins/` 下无 `*.bak` 目录干扰；(3) 飞书凭据已配置（见[飞书凭据](#飞书凭据)）。

---

## 配置说明

所有配置项位于 `~/.hermes/config.yaml` 的 `hermes_lark_streaming:` 节下。插件首次加载时自动注入默认配置；卸载前请先运行 `cleanup` 命令清除。

```yaml
hermes_lark_streaming:
  enabled: true                    # 启用流式卡片
  linear: true                     # 线性模式：单卡片原地更新（统一面板架构）
  panel_expanded: false            # 完成态卡片中面板是否保持展开
  streaming_panel_expanded: false  # 流式态卡片中面板是否保持展开
  print_strategy: delay            # "fast"（即时）或 "delay"（更丝滑打字机，默认）
  flush_interval_ms: 100           # 卡片刷新间隔（毫秒，70~2000，默认 100）
  card_ttl_sec: 600               # 卡片存活检测超时（秒）
  inject_time: false               # 时间感知模式（详见下方说明）

  footer:
    show_label: false              # 是否显示字段标签
    fields:
      - [status, elapsed, model, cost, compression_exhausted]
      # 可用字段说明：
      #   status      — 回复状态（已完成 / 出错 / 已停止）
      #   elapsed     — AI 回复耗时
      #   model       — 使用的模型名称
      #   cost        — 预估费用及可信度（$0.023 估算 / $0.023 实报 / 免费）
      #   compression_exhausted — 上下文已满（⚠ 上下文已满）
      # 以下字段默认不显示 — 在 fields 列表中添加即可启用：
      #   cache       — 缓存命中率（缓存命中/总输入 命中率%）
      #   tokens      — Token 用量（↑ 输入 ↓ 输出 💭 推理）
      #   context     — 上下文窗口用量（已用/总量 百分比）
      #   api_calls   — 本轮对话的 API 调用次数
      #   history_offset — 对话历史偏移量；值越大对话越长，值突然变小说明发生了上下文压缩
      # 每个内层列表为页脚的一行，字段仅在有值时显示
```

### 时间感知模式（`inject_time`）

开启 `inject_time: true` 后，插件在每条用户消息前添加 `<time>HH:MM:SS</time>` 时间前缀，让 AI 无需调用 `date` 工具即可感知当前时间。使用 XML 标签是因为 LLM 普遍将其理解为结构化元数据，不会在回复中模仿。Prefix Cache 安全（每条约 6 tokens）。详见 [SKILL.md](docs/SKILL.md)。

### 推理面板显示

```yaml
display:
  show_reasoning: true  # 在统一面板中显示推理内容
```

### 飞书凭据

| 优先级 | 来源 | 示例 |
|--------|------|------|
| 1 | 环境变量 | `FEISHU_APP_ID`、`FEISHU_APP_SECRET` |
| 2 | 文件 | `~/.hermes/.env` |
| 3 | 配置文件 | `hermes_lark_streaming.feishu.app_id` |

```bash
# ~/.hermes/.env 示例
FEISHU_APP_ID=cli_xxxxxx
FEISHU_APP_SECRET=xxxxxx
FEISHU_BASE_URL=https://open.feishu.cn/open-apis
```

---

## 开发者指南与更新日志

> 📖 **[SKILL.md](docs/SKILL.md)** — LLM 快速上手指南。项目架构、关键设计决策、常见陷阱，高效代码修改指南。

> 完整版本历史请查看 [CHANGELOG.md](docs/CHANGELOG.md)

> ⚠️ **重要提醒：** 如从 v1.0.1 及以下版本升级，请按照卸载流程卸载老版本，重新安装新版本，禁止通过更新方式升级！

---

## 如何提交 ISSUES
> 请查看模板 [ISSUES_TEMPLATE.md](docs/ISSUES_TEMPLATE.md)
---

## 致谢

[![joshcheng820222](https://avatars.githubusercontent.com/u/26886147?v=4&s=48)](https://github.com/joshcheng820222)
