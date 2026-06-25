<h1 align="center">hermes-lark-streaming</h1>

<p align="center">
  <img src="https://img.shields.io/badge/项目-Vibe%20Coding-ff69b4" alt="Vibe Coding">
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/License-MIT-4caf50.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/python-3.11+-3776AB.svg" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/version-1.3.6-ff9800.svg" alt="Version">
</p>

<p align="center">
<a href="mailto:zhengyu.pu@petalmail.com"><img src="https://img.shields.io/badge/邮箱-zhengyu.pu%40petalmail.com-9C27B0?logo=gmail&logoColor=white" alt="邮箱"></a>
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

<table align="center">
  <tr>
    <td><img src="assets/screenshots/img1.png" width="200px" /></td>
    <td><img src="assets/screenshots/img2.png" width="200px" /></td>
    <td><img src="assets/screenshots/img3.png" width="200px" /></td>
    <td><img src="assets/screenshots/img4.png" width="200px" /></td>
  </tr>
</table>

---

## 快速开始

### 前置要求

- [Hermes Agent](https://github.com/NousResearch/hermes-agent)（已运行，已配置飞书平台）
- Hermes CLI 支持插件系统（可用 `hermes plugins` 命令）

### 安装

> **💡 智能安装提示**：将以下提示词复制给 Hermes Agent，它会自动完成安装：
> 
> ```
> 帮我安装飞书敖式卡片：
> - Gitee：https://gitee.com/Aowen-Nowor/hermes-lark-streaming/raw/github_sync/docs/AGENT_GUIDE.md
> - GitHub：https://raw.githubusercontent.com/Aowen-Nowor/hermes-lark-streaming/github_sync/docs/AGENT_GUIDE.md
> ```

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
$HERMES_PYTHON ~/.hermes/plugins/hermes-lark-streaming/__main__.py doctor
```

> **排障提示**：安装后若无卡片效果，请检查：(1) `hermes plugins list` 显示插件已启用；(2) `~/.hermes/plugins/` 下无 `*.bak` 目录干扰；(3) 飞书凭据已配置（见[飞书凭据](#飞书凭据)）。`doctor` 命令可一键诊断插件版本、Python 环境、配置项、飞书凭据、补丁应用状态、日志路径。

---

## 配置说明

所有配置项位于 `~/.hermes/config.yaml` 的 `hermes_lark_streaming:` 节下。插件首次加载时自动注入默认配置；卸载前请先运行 `cleanup` 命令清除。

```yaml
hermes_lark_streaming:
  panel_expanded: false            # 完成态卡片中面板是否保持展开
  streaming_panel_expanded: false  # 流式态卡片中面板是否保持展开
  print_strategy: delay            # "fast"（即时）或 "delay"（更丝滑打字机，默认）
  print_step: 4                    # 打字机每次渲染字符数（默认4，范围1~10，需飞书7.23+）
  flush_interval_ms: 200           # 插件发送间隔（毫秒，70~2000，默认200）
  card_ttl_sec: 600               # 卡片存活检测超时（秒）
  max_tool_steps: 20               # 统一面板最多显示的工具步骤数（默认20，范围1~100）
  max_reasoning_rounds: 20         # 统一面板最多显示的推理轮次数（默认20，范围1~100）
  header:
    enabled: false                  # 卡片头部（蓝色处理中 → 绿色已完成 / 红色出错-已停止）。默认关闭。详见下方说明

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

### 卡片头部（`header.enabled`）

开启 `header.enabled: true` 后，agent 回复卡片顶部显示状态头部：

- **流式中**：蓝色头部，“处理中...”
- **已完成**：绿色头部，“已完成”
- **出错**：红色头部，“出错”
- **已停止**（被中断）：红色头部，“已停止”

默认 `false`（关闭）——卡片没有头部，与 v1.1.x 行为一致。

> **注意**：受飞书 CardKit API 限制，settings/batch_update 接口在流式或增量封卡过程中**无法更新卡片级头部**，只有全量重建（`cardkit_update`）能改变头部颜色。因此**开启 `header.enabled` 后，封卡路径会改走全量重建**（而非默认的增量封卡），以保证头部颜色正确切换（蓝 → 绿/红）。关闭时使用默认增量封卡（性能更优）。

> **作用范围**：`header.enabled` 仅影响 agent 流式卡片和完成态卡片。Cron 推送卡片、网关内部消息卡片不受影响。`/aowen` 命令卡片始终有自己的 banner 风格头部（v1.1.0 设计语言的一部分），不受此配置控制。


### 推理面板显示

```yaml
display:
  show_reasoning: true  # 在统一面板中显示推理内容
```

### 统一面板超限压缩

飞书卡片2.0 **硬性限制200个元素/组件**，超出会报错 `300305 (element exceeds the limit)`，导致卡片封口失败并触发文本兜底（内容重复）。

> **元素计数规则**：每个带 `tag` 属性的 JSON 对象都算1个元素，包括嵌套在内层的 `standard_icon`、`plain_text`、`lark_md` 等。

#### 统一面板各项元素消耗

| 组成部分 | 元素数 | 说明 |
|---------|--------|------|
| 面板容器 | 1 | `collapsible_panel` |
| 面板标题 | 2 | `plain_text` + `standard_icon` |
| 每个推理轮次（最大） | 4 | 标题行 `div`+`standard_icon`+`lark_md` + 推理文本 `markdown` |
| 每个工具步骤（最大） | 7 | 标题行 `div`+`standard_icon`+`lark_md` + 详情行 `div`+`plain_text` + 结果行 `div`+`lark_md` |
| 折叠提示（触发时） | 1 | 1个 `markdown` 元素 |
| 回答文本 | 1~3 | `markdown`，长文本会被拆分 |
| 页脚 | 2 | `hr` + `markdown` |
| 卡片头（启用时） | ~3 | `plain_text` + `standard_icon` |
| 错误面板（有时） | ~4 | `collapsible_panel` + 内部元素 |

**计算示例**：20 轮推理 + 20 步工具 = 20×4 + 20×7 + 固定开销 ≈ 223（超过 200）

因此默认值设为 `max_tool_steps=20` + `max_reasoning_rounds=20`，配合折叠机制确保大多数场景不超限。即使配置值较高或极端情况下元素仍超限，代码内置了**卡片级元素安全网**——封卡时已知全部元素（面板+answer+footer+error），递归计算实际 tag objects 总数，超过195（200-5缓冲）时自动从面板children最老项目开始裁剪，确保卡片元素永远不会超过200。answer、footer、error panel 永不裁剪。

#### 配置项

```yaml
hermes_lark_streaming:
  max_tool_steps: 20           # 统一面板最多显示的工具步骤数（默认20，范围1~100）
  max_reasoning_rounds: 20     # 统一面板最多显示的推理轮次数（默认20，范围1~100）
```

超出限制时，早期项目会被折叠为一行提示，例如：`⚡ 还有 10 轮早期推理、5 步早期操作已折叠`

面板标题始终显示**实际总数**（如"3轮 · 44个工具"），折叠提示仅影响面板内展示的内容。

### /aowen 命令

在飞书中发送 `/aowen` 系列命令，插件直接回复卡片（不经过 Hermes AI）：

| 命令 | 说明 |
|------|------|
| `/aowen help` | 显示所有命令列表 |
| `/aowen status` | 查看插件状态 + 当前配置（折叠面板展示） |
| `/aowen monitor` | 查看监控面板（卡片创建数、API 调用数、错误码分布等） |
| `/aowen monitor reset` | 重置监控统计计数器 |
| `/aowen config reload` | 修改 `~/.hermes/config.yaml` 后，在飞书中发送此命令立即生效，或重启网关生效 |
| `/aowen` | 同 `/aowen help` |

> `/aowen` 是插件的命令前缀，所有 `/aowen` 开头的命令都由插件处理，不经过 Hermes。

### 飞书凭据

插件复用 Hermes 已配置的飞书凭据，无需单独配置。Hermes 安装时已在 `~/.hermes/.env` 中配置：

```bash
# ~/.hermes/.env（Hermes 安装时已配置，插件直接复用）
FEISHU_APP_ID=cli_xxxxxx
FEISHU_APP_SECRET=xxxxxx
FEISHU_DOMAIN=feishu          # feishu=国内版, lark=国际版
```

> 插件自动读取 Hermes 的飞书凭据和域名配置。如果 Hermes 飞书渠道能正常工作，插件也能正常工作。

---

## 开发者指南与更新日志

> 📖 **[SKILL.md](docs/SKILL.md)** — LLM 快速上手指南。项目架构、关键设计决策、高效代码修改指南。

> 完整版本历史请查看 [CHANGELOG.md](docs/CHANGELOG.md)

> ⚠️ **重要提醒：** 如从 v1.0.1 及以下版本升级，请按照卸载流程卸载老版本，重新安装新版本，禁止通过更新方式升级！

---

## 如何提交 ISSUES
> 请查看模板 [ISSUES_TEMPLATE.md](docs/ISSUES_TEMPLATE.md)
---

## 致谢

<a href="https://github.com/joshcheng820222"><img src="https://avatars.githubusercontent.com/u/26886147?v=4&s=66" alt="joshcheng820222" width="66" height="66"></a> <a href="https://github.com/xuu1998"><img src="https://avatars.githubusercontent.com/u/40609659?v=4&s=66" alt="xuu1998" width="66" height="66"></a> <a href="https://gitee.com/joshchengjoshcheng"><img src="assets/avatars/joshchengjoshcheng.png" alt="joshchengjoshcheng" width="66" height="66"></a> <a href="https://github.com/hmhmdcy"><img src="https://avatars.githubusercontent.com/u/163143682?v=4" alt="hmhmdcy" width="66" height="66"></a>
