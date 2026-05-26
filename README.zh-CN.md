<h1 align="center">hermes-lark-streaming</h1>

<p align="center">
  <img src="https://img.shields.io/badge/项目-Vibe%20Coding-ff69b4" alt="Vibe Coding">
  <a href="https://larkcommunity.feishu.cn/wiki/DKkpwgMcJiglIhk88N4cqJEan5f?from=from_copylink"><img src="https://img.shields.io/badge/docs-知识库-3370FF?logo=feishu&logoColor=white" alt="知识库文档"></a>
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/License-MIT-4caf50.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/python-3.11+-3776AB.svg" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/version-0.8.5-ff9800.svg" alt="Version">
</p>

<p align="center">
<a href="https://applink.feishu.cn/client/message/link/open?token=AmoQJk5dwczIahKlW78ADLU%3D"><img src="https://img.shields.io/badge/官方唯一交流群-中国-red" alt="官方交流群"></a>
</p>

<p align="center">
<a href="README.md">English</a> | 中文版
</p>

为 Hermes Agent 提供飞书/Lark CardKit v2.0 流式消息卡片插件 — 实时 AI 响应展示，支持打字机效果、工具面板、推理过程等。

> 本仓库由 [Cheerwhy/hermes-lark-streaming](https://github.com/Cheerwhy/hermes-lark-streaming) Fork 而来

---

## 功能特性

- **流式卡片** — AI 响应实时在交互式卡片中展示，带打字机效果
- **线性模式** — 在单张卡片中动态渲染思考、工具调用和答案内容，自动处理飞书 200 元素限制并支持拆卡
- **思考过程** — 展示模型推理/思考内容
- **工具调用** — 实时工具调用状态和进度，带标准图标和结果/错误块
- **CardKit v2.0** — 优先使用飞书 CardKit 流式 API，自动降级到 IM PATCH
- **终端卡片** — 完成后显示完整结果，包括 token 使用量、耗时、上下文信息
- **消息保护** — 检测消息被删除/撤回后自动终止更新，避免无效 API 调用
- **图片解析** — 自动识别 markdown 图片引用，下载并上传替换为飞书 img_key
- **中断处理** — 处理 /stop 命令和消息中断，显示中断状态卡片并自动开启新会话
- **Cron 推送** — 定时任务结果以飞书卡片形式推送，保留 Markdown 渲染
- **多语言** — 内置中英文双语卡片文本（状态、工具面板、思考标签等），根据飞书客户端语言自动切换
- **插件生命周期** — 通过 `hermes plugins install/uninstall` 安装/卸载，无需修改源文件
- **运行时补丁** — 使用 monkey patching 而非 AST 注入，不修改磁盘上的源文件

![功能预览](assets/img1.png)

---

## 快速开始

### 前置要求

- [Hermes Agent](https://github.com/NousResearch/hermes-agent)（已运行，已配置飞书平台）
- Python >= 3.11
- Hermes CLI 支持插件系统（可用 `hermes plugins` 命令）

### 安装

```bash
hermes plugins install https://gitee.com/Aowen-Nowor/hermes-lark-streaming
```

提示时输入 `Y` 启用插件，然后重启网关：

```bash
hermes gateway restart
```

### 卸载

```bash
# 1. 先清理注入的配置（插件代码还在，可以跑）
python -m hermes_lark_streaming cleanup

# 2. 卸载插件
hermes plugins uninstall hermes-lark-streaming

# 3. 重启网关
hermes gateway restart
```

### 验证安装

```bash
# 检查插件状态
python -m hermes_lark_streaming status

# 验证环境兼容性
python -m hermes_lark_streaming verify

# 查看网关日志
grep hermes-lark-streaming ~/.hermes/logs/gateway.log
```

---

## 配置说明

所有配置项位于 `~/.hermes/config.yaml` 的 `streaming:` 节下。

> **注意**：Hermes 原生也有 `display.streaming: false` 配置项，该配置控制 **CLI/TUI 终端**输出（响应是否在终端流式显示），与本插件的流式卡片**无关**。本插件只读取 `streaming:` 节。

### 插件启用配置

在 `~/.hermes/config.yaml` 中启用本插件：

```yaml
plugins:
  enabled:
    - hermes-lark-streaming
  disabled: []
```

如果 `plugins` 节不存在，需要手动添加。安装后也可以通过以下命令启用：

```bash
hermes plugins enable hermes-lark-streaming
```

禁用插件：

```bash
hermes plugins disable hermes-lark-streaming
hermes gateway restart
```

### 可用配置项

```yaml
streaming:
  enabled: true              # 启用流式卡片
  linear: true               # 线性模式：单卡片原地更新，支持自动拆卡
  panel_expanded: false      # 完成态卡片中面板（工具、推理）是否保持展开
  card_ttl_sec: 600         # 卡片存活检测超时（秒）

  footer:
    fields:
      - status
      - elapsed
      - model
      - tokens
      - context
    show_label: true         # 是否显示字段标签（true/false）
```

### 飞书凭据

插件按以下优先级读取凭据：

| 优先级 | 来源 | 示例 |
|--------|------|------|
| 1 | 环境变量 | `FEISHU_APP_ID`、`FEISHU_APP_SECRET` |
| 2 | 文件 | `~/.hermes/.env` |
| 3 | 配置文件 | `streaming.feishu.app_id` |

```bash
# ~/.hermes/.env 示例
FEISHU_APP_ID=cli_xxxxxx
FEISHU_APP_SECRET=xxxxxx
FEISHU_BASE_URL=https://open.feishu.cn/open-apis
```

### 推理面板显示

推理面板的可见性由 `display.show_reasoning` 或 `display.platforms.feishu.show_reasoning` 控制：

```yaml
display:
  show_reasoning: true  # 在飞书卡片中显示推理面板
```

---

## 常见问题

### 插件加载失败

**问题**：安装后无卡片效果，Hermes 仍回复纯文本

**解决**：
1. 检查插件是否正确安装：`hermes plugins list`
2. 查看网关日志：`grep hermes-lark-streaming ~/.hermes/logs/gateway.log`
3. 验证飞书凭据：`python -m hermes_lark_streaming status`
4. 检查是否存在备份目录干扰：`ls -la ~/.hermes/plugins/ | grep bak`，如有则删除

### 卡片内容重复

**问题**：回复文本在卡片中出现两次

**原因**：Hermes 在同一会话中多次调用 `run_conversation`，导致回调被重复包装

**解决**：插件已内置防重复包装守卫（v0.8.0-fix2），通过 `_hls_wrapper` 属性标记已包装的回调

### 卡片元素超限

**问题**：长对话导致卡片更新失败

**解决**：启用线性模式（`streaming.linear: true`），插件会自动拆卡处理

### 第二条及后续消息无流式更新

**问题**：第一条消息正常，后续消息只显示跑马灯和 Done

**原因**：`contextvars.ContextVar` 不跨线程传播，线程池中无法读取消息上下文

**解决**：插件已通过 `threading.local()` 作为 fallback（v0.8.0-fix4/fix5），确保跨线程场景下上下文正确传递

---

## 更新日志

### v0.8.6 (2026-05-26)

| # | 问题 | 原因 | 修复 |
|---|------|------|------|
| 1 | 安装后无卡片效果 | 插件 Config 读不到顶层 `streaming` 配置，`enabled` 始终为 `False` | `register()` 自动注入顶层 `streaming` 配置段 |
| 2 | 配置文件格式错误 | `footer.fields` 被序列化为二维数组格式 | `_prepare_config()` 展平为一维列表后写入 |
| 3 | 卸载后配置残留 | Hermes 的 `plugins uninstall` 只删目录不调 `unregister` | 新增 `python -m hermes_lark_streaming cleanup` 命令，先清配置再卸载 |

### v0.8.5 (2026-05-26)

| # | 问题 | 原因 | 修复 |
|---|------|------|------|
| fix1 | 插件加载失败 | 仓库缺少根目录 `__init__.py` | 新增根目录 `__init__.py` 桥接导入 |
| fix2 | 卡片内容重复 | 回调被多次包装，每段文本被处理两次 | 防重复包装守卫 `_hls_wrapped` 标记 |
| fix3 | 语法异常 | `setattr` 错位缩进到 `except` 内部 | 修复缩进位置 |
| fix4 | 后续消息无流式更新 | `contextvars` 不跨线程，`_set_thread_local_ctx()` 未定义 | 引入 `threading.local()` fallback |
| fix5 | 重启后所有消息无流式更新 | 备份目录干扰命名空间 + `_set_thread_local_ctx()` 未定义 | 删除备份目录 + 定义 `_thread_local_ctx` + 双重保险直接 patch |

## 致谢