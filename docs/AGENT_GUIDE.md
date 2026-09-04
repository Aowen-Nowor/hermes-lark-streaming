# hermes-lark-streaming 安装指南

> 高信息密度参考文档，专为 Agent 自动解析设计
> 最后更新: 2026-09-04 (v1.8.0)

## 快速概览

| 项目 | 值 |
|------|-------|
| 名称 | hermes-lark-streaming (飞书敖式卡片) |
| 许可证 | MIT |
| Python | >=3.11 |
| 依赖 | lark-oapi>=1.6.4, PyYAML>=6.0 |
| 插件类型 | standalone |
| 验证基线 | hermes-agent v0.21.0 (tag v2026.8.31)；生产建议 >= v0.19.0 |
| Gitee | https://gitee.com/Aowen-Nowor/hermes-lark-streaming |
| GitHub | https://github.com/Aowen-Nowor/hermes-lark-streaming |

## 手动安装

### 方式一：Hermes CLI（推荐）

```bash
# Gitee (SSH)
hermes plugins install git@gitee.com:Aowen-Nowor/hermes-lark-streaming.git

# Gitee (HTTPS)
hermes plugins install https://gitee.com/Aowen-Nowor/hermes-lark-streaming

# GitHub (SSH)
hermes plugins install git@github.com:Aowen-Nowor/hermes-lark-streaming.git

# GitHub (HTTPS)
hermes plugins install https://github.com/Aowen-Nowor/hermes-lark-streaming
```

提示时输入 `Y` 启用插件，然后重启网关：

```bash
hermes gateway restart
```

### 方式二：本地目录

```bash
git clone https://gitee.com/Aowen-Nowor/hermes-lark-streaming.git
cd hermes-lark-streaming
hermes plugins add .
hermes gateway restart
```

### 方式三：pip 安装（⚠️ 不可用——PyPI 上是第三方衍生包）

> **PyPI 上的 `hermes-lark-streaming`（2.x，"泡波卡"/aiduPOP）不是本插件官方发布**，
> 与作者无关。它是第三方账号（monkey2jack）上传的衍生包：fork 了本仓库代码
> （多个核心文件字节级相同）并叠加其自有主题层，包元数据却署名 Aowen-Nowor。
> `pip install hermes-lark-streaming` 装到的是那个衍生版，**不是本插件**。
>
> 官方分发渠道仅限：**方式一**（Hermes CLI 从上述 Git 仓库安装）和
> **方式二**（本仓库本地安装）。
>
> 已误装的话：`pip uninstall hermes-lark-streaming`，再按方式一/方式二重装。

## 卸载

```bash
# 1. 清理注入的配置（插件代码还在时执行）
HERMES_PYTHON=$(python3 ~/.hermes/plugins/hermes-lark-streaming/__main__.py python)
$HERMES_PYTHON ~/.hermes/plugins/hermes-lark-streaming/__main__.py cleanup

# 2. 卸载插件
hermes plugins uninstall hermes-lark-streaming

# 3. 重启网关
hermes gateway restart
```

## 更新

```bash
hermes plugins update hermes-lark-streaming
hermes gateway restart
```

或手动更新：

```bash
cd ~/.hermes/plugins/hermes-lark-streaming
git pull origin DEV
hermes plugins reload hermes-lark-streaming
hermes gateway restart
```

## 必需凭据配置

### 环境变量方式（优先级最高）

```bash
export FEISHU_APP_ID="your_app_id"
export FEISHU_APP_SECRET="your_app_secret"
```

### 文件方式（优先级中等）

在 `~/.hermes/.env` 文件中检查：

```ini
FEISHU_APP_ID=your_app_id
FEISHU_APP_SECRET=your_app_secret
```

### 配置文件方式（优先级最低）

在 `~/.hermes/config.yaml` 中检查：

```yaml
feishu:
  app_id: "your_app_id"
  app_secret: "your_app_secret"
```

**优先级顺序**: 环境变量 > `~/.hermes/.env` > `~/.hermes/config.yaml`

## 可选配置项 (config.yaml)

### hermes_lark_streaming 节

| 配置键 | 默认值 | 范围 | 说明 |
|--------|---------|------|------|
| `max_tool_steps` | `20` | 1–100 | 统一面板中工具步骤最大数量（超限折叠） |
| `max_reasoning_rounds` | `20` | 1–100 | 统一面板中推理轮次最大数量（超限折叠） |
| `card_ttl_sec` | `600` | >0 | 会话 TTL（秒），超时卡片失效 |
| `flush_interval_ms` | `200` | 70–2000 | 插件发送间隔（毫秒） |
| `print_strategy` | `delay` | `fast`/`delay` | 打字机效果策略 |
| `print_step` | `4` | 1–10 | 打字机每次渲染字符数（需飞书7.23+） |
| `panel_expanded` | `false` | bool | 完成态卡片面板是否展开 |
| `streaming_panel_expanded` | `false` | bool | 流式态卡片面板是否展开 |
| `footer.show_label` | `false` | bool | 是否显示页脚字段标签 |
| `footer.fields` | `[[status, elapsed, model, cost, compression_exhausted]]` | array | 页脚字段配置 |

### display 节（Hermes 全局配置，非 hermes_lark_streaming 节）

| 配置键 | 默认值 | 说明 |
|--------|---------|------|
| `display.show_reasoning` | `false` | 是否展示推理/思考面板（全局，影响所有平台） |
| `display.platforms.feishu.show_reasoning` | — | 飞书平台专属推理显示开关（优先于全局） |

示例配置：

```yaml
hermes_lark_streaming:
  max_tool_steps: 20
  max_reasoning_rounds: 20
  card_ttl_sec: 600
  flush_interval_ms: 200
  print_strategy: delay
  print_step: 4
  footer:
    show_label: false
    fields:
      - [status, elapsed, model, cost, compression_exhausted]

# display 节是 Hermes 全局配置，不在 hermes_lark_streaming 下
display:
  show_reasoning: true
  # 或按平台配置：
  # platforms:
  #   feishu:
  #     show_reasoning: true
```

### 插件命令（/aowen 前缀）

所有 `/aowen` 开头的命令由插件处理，不经过 Hermes AI：

| 命令 | 说明 |
|------|------|
| `/aowen help` | 显示所有命令列表 |
| `/aowen status` | 查看插件状态 + 当前配置（折叠面板展示） |
| `/aowen monitor` | 查看监控面板（卡片创建数、API 调用数、错误码分布等） |
| `/aowen monitor reset` | 重置监控统计计数器 |
| `/aowen config reload` | 修改 config.yaml 后重新加载配置立即生效 |
| `/aowen` | 同 `/aowen help` |

> **中断场景**：当 AI 正在回复中（agent 运行中）发送 `/aowen` 命令时，插件会回复一张橙色提示卡"AI 正在回复中"（借鉴 Hermes 原生 `/model` 命令的 "Agent is running — wait or /stop first" UX），提示用户等待完成或 `/stop` 后再使用。命令本身不会被发给 AI。

## 提供的钩子（Hooks）

- `pre_gateway_dispatch` - 消息分发前拦截（/aowen 命令）
- `on_feishu_normalize` - 飞书消息标准化
- `on_message_started` - 消息开始
- `on_message_completed` - 消息完成
- `on_message_aborted` - 消息中止
- `on_message_interrupted` - 消息中断
- `on_answer_delta` - 回答增量更新
- `on_thinking_delta` - 思考增量更新
- `on_reasoning_delta` - 推理增量更新
- `on_tool_updated` - 工具调用更新
- `on_background_review_message` - 后台审查消息

> v1.7.0：`on_cron_deliver` 已删除（死代码，从未被调用——cron 卡片由 `_wrap_cron_deliver` 直接驱动；RELAY 部署由 `RelayAdapter.send_for_platform` 包装器驱动）。

## 故障排查

| 现象 | 原因 | 解决方案 |
|------|------|----------|
| 卡片不显示 | 缺少凭据 | 设置 `FEISHU_APP_ID` 和 `FEISHU_APP_SECRET` |
| 飞书消息完全无响应（网关在跑） | lark-oapi < 1.6.4：hermes ≥ 0.19 传 `extra_ua_tags`，旧 SDK 不认 → WS 连不上（2026-07-21 生产 22 分钟中断根因；插件启动时会打 ERROR 指纹） | `pip install -U 'lark-oapi>=1.6.4'` |
| 错误码 300305 | 元素超限（硬限制 200） | 减小 `max_tool_steps` / `max_reasoning_rounds` |
| 错误码 300315 | Schema 校验失败 | 检查飞书 Card 2.0 规范，确认卡片属性合法 |
| 内容被截断 | 静态卡片表格超限 | 静态卡片（cron/gateway）表格行数 >5 时自动降级 |
| 流式卡住 | TTL 过期 | 增加 `card_ttl_sec` 值 |
| 封口失败 | 元素总数超标 | 安全网已自动裁剪早期面板，检查日志确认 |
| 文本兜底 | 极端超限场景 | 核心内容保留，完整内容降级为纯文本 |
| 长时间收不到消息且 `/aowen monitor` 最近入站不动 | WS 静默断连（SDK 重连预算耗尽后线程退出，适配器仍报已连接，v1.8.0 前无可观测信号） | 重启网关 `hermes gateway restart`；用 `/aowen monitor` 渠道健康区观察最近入站时间 |

## 验证安装

```bash
# 查看插件列表
hermes plugins list

# 检查日志
grep hermes_lark_streaming ~/.hermes/logs/agent.log

# 查看版本号
python -c "from hermes_lark_streaming import __version__; print(__version__)"

# 或从 plugin.yaml 读取
grep 'version:' ~/.hermes/plugins/hermes-lark-streaming/plugin.yaml
```

## 常见问题

**Q: 与原版 Cheerwhy/hermes-lark-streaming 兼容吗？**  
A: 不兼容。如已安装原版，请先卸载再安装本插件。

**Q: 如何获取飞书 App ID 和 Secret？**  
A: 在飞书开放平台创建应用后，在「凭证与基础信息」页面获取。

**Q: 卡片元素限制是多少？**  
A: 单卡片最多 200 个 Tag 对象，插件内置安全网自动裁剪超限内容。

**Q: 支持哪些 Hermes Agent 版本？**  
A: 需要支持插件系统的 Hermes Agent（v0.17+）。本插件按 hermes-agent v0.21.0（tag v2026.8.31）源码逐点验证兼容；生产建议 >= v0.19.0。RELAY 前置部署需 v0.20.5+。旧版 hermes（< 0.19）不受 lark-oapi 门槛影响，但建议同步升级。

**Q: 卸载时忘记运行 cleanup 怎么办？**  
A: 可忽略，或手动清理 `~/.hermes/config.yaml` 中的相关配置。

## 相关链接

- **官方文档**: https://larkcommunity.feishu.cn/wiki/DKkpwgMcJiglIhk88N4cqJEan5f
- **问题反馈**: https://gitee.com/Aowen-Nowor/hermes-lark-streaming/issues
- **交流群**: [点击加入](https://applink.feishu.cn/client/message/link/open?token=AmoQJk5dwczIahKlW78ADLU%3D)
