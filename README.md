# hermes-feishu-streaming-platform

**飞书 CardKit v2.0 流式卡片平台插件** — hermes-agent 的 `feishu` 平台增强版。

继承 hermes 内置飞书平台的全部能力，在其出站通道上叠加实时流式卡片：回答以打字机效果写入卡片、工具进度与思考过程实时渲染进状态面板、澄清问题变为可点击的交互卡片。**全程零 monkey-patch** —— 不修改、不包装、不拦截 hermes 的任何内部实现。

- 版本：v2.0.10
- 平台名：`feishu-streaming`
- 协议：hermes platform 插件（`plugin.yaml` `kind: platform`）
- 兼容：hermes-agent ≥ 0.21 · Python ≥ 3.11 · lark-oapi ≥ 1.6.4

---

## 特性

### 流式卡片（核心）
- **实时打字机**：回答内容经 hermes 原生流式通道驱动，CardKit 实体级流式更新，飞书客户端原生渲染打字机动画
- **状态面板**：工具调用进度（`⚙️ 正在执行 <tool>...`）与思考气泡（`💬 ...`）实时进入卡片状态区，自动限深
- **优雅定稿**：回答完成后卡片关闭流式模式，摘要折叠进消息预览
- **自动降级**：CardKit API 任何失败（限流/权限/网络）→ 当前回合立即降级为原生文本消息，**永不丢消息**

### 交互卡片
- **澄清卡片**：agent 的澄清请求渲染为按钮/输入卡片，点击即回复，三态反馈（选择 → 已提交 → 已确认），支持重试与操作者授权校验（fail-closed）
- **/aowen 指令**：`/aowen help`、`/aowen status`（状态卡：版本/客户端/活跃卡片/架构状态）、`/aowen monitor`（指标）

### 内置平台能力（全量继承，一行未改）
WebSocket / webhook 双模式 · 消息归一化 · 群 @ 门控 · DM 配对 · threads · 图片/语音/视频/文件收发 · TTS · 消息编辑/删除/回应 · 评论 · 会议邀请 · cron 投递 · 独立发送通道

---

## 安装

```bash
# 1. 将本仓库克隆/复制到 hermes 插件目录
cp -r hermes-lark-streaming ~/.hermes/plugins/feishu-streaming

# 2. 确认依赖（lark-oapi >= 1.6.4）
pip install 'lark-oapi>=1.6.4'
```

## 启用

在 `config.yaml` 的 gateway 平台配置中，把平台名从 `feishu` 改为 `feishu-streaming`：

```yaml
gateway:
  platforms:
    - name: feishu-streaming
      type: feishu-streaming
      # 原有 FEISHU_APP_ID / FEISHU_APP_SECRET 等配置原样保留
```

建议同时开启思考与工具进度显示面（卡片状态面板的数据源）：

```yaml
display:
  thinking_progress: true
  tool_progress: all
streaming:
  enabled: true
```

重启网关生效。**回滚**：把平台名改回 `feishu` 即恢复原生行为，本插件可随时卸载、无任何运行时残留。

## 配置

插件配置节：`feishu_streaming`（自动注入；V1 的 `hermes_lark_streaming` 节自动迁移）：

```yaml
feishu_streaming:
  print_strategy: "delay"     # 打字机策略
  print_step: 4               # 打字机步长
  card_ttl_sec: 600           # 卡片会话 TTL（秒）
  max_tool_steps: 20          # 状态区工具行上限
  footer:
    show_label: false
```

完整配置说明见 [API.md](API.md)，架构与设计决策见 [docs/DESIGN.md](docs/DESIGN.md)。

## FAQ

**Q: 与内置 `feishu` 平台是什么关系？**
A: 本插件的适配器继承内置 `FeishuAdapter`，内置能力一个不少；仅重写 `send` / `edit_message` 等出站语义入口，把 hermes 官方 metadata 标记的帧重组为 CardKit 卡片。

**Q: 会不会影响其它平台（QQ/TG 等）？**
A: 不会。本插件只注册 `feishu-streaming` 平台，不触碰任何全局对象。

**Q: hermes 升级后会坏吗？**
A: 出站帧协议（metadata 标记）与适配器接口都是 hermes 官方协议；适配器启动时对继承面做 fail-fast 探测，若上游重构会在启动日志中明确报出失配点，而不是静默出错。

## License

MIT
