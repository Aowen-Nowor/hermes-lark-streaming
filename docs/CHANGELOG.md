# 更新日志

## v2.0.29（2026-10-27）

| 类型 | 变更 |
|------|------|
| 说明 | V2 线唯一发版条目，合并 v2.0.0 → v2.0.28 全部迭代；V1（1.8.4）→ V2 升级步骤见 README「从 V1 升级」，agent 手册见 SKILL.md 第 6 节 |
| 重构 | standalone 钩子插件（运行时 monkey-patch GatewayRunner / FeishuAdapter）→ 正规 platform 插件：`FeishuStreamingAdapter` 继承内置 `FeishuAdapter`（WebSocket/webhook、媒体、threads、群门控、cron 投递能力零损失），官方 `register_platform` 协议注册；删除 `patching/`、`controller/`（钩子与 token 级控制器）；平台名 `feishu-streaming` 与原生 `feishu` 二选一，改回即整体回滚，无运行时残留 |
| 重构 | 新增 composer 帧重组引擎：官方 metadata 四通道分类（answer 流 / progress 面板 / interim / final）→ CardKit v2.0 卡片生命周期，per-chat 帧串行化；clarify 交互卡片闭环（select/input/button/retry 四分支 + 操作者授权 fail-closed） |
| 新增 | native streaming 通道：适配器声明官方 `SUPPORTS_NATIVE_STREAMING`，答案流走 `send_stream_frame` 原生帧——工具边界不封卡、单卡全回合；任何帧失败自动回落 edit/send；`native_streaming: false` 一键回 edit-based |
| 新增 | agent loop 状态面板：工具行/思考行实时渲染（真实 progress 模板结构化解析、思考按轮分组、单工具耗时、官方图标 token、思考正文 markdown 渲染）；纯 answer 回合面板懒渲染（无空壳）；DeepSeek reasoning 隐藏流经官方 `on_stream_delta` 观察者钩子回灌面板（`stream_reasoning_deltas: true`） |
| 新增 | 卡片 reply 引用触发消息（私聊/群聊；锚点非 `om_` 或 reply 失败自动降级直发）；cron 定时投递卡片化（任务名标题 + job_id 溯源页脚）；回合外投递与 ≥200 字长文一次性静态卡（`oneshot_cards` 可关）；子代理失败/超时通知并入面板或 early-create 回合卡；页脚 `reasoning` 推理等级；`card_style.text_size` 自定义字号（完全 opt-in，未配置零注入） |
| 新增 | clarify 暂停/续流：澄清等待期卡片保持「进行中」+ 面板暂停行，选择后**同一张卡**续流至定稿，一次澄清回合一张卡（`clarify_suspend_sec`，默认 1.2s，0 = 关闭） |
| 修复 | 稳定性主线（真飞书实测沉淀）：300317 sequence 冲突对齐重发自愈、长回合断卡（GC 回收条件）、burst 帧多孤儿卡（帧串行化）、一问多卡（stream_element 空串守卫）、emoji 开头 answer 快照误拆进面板（行语法扫描根治）、status 通报幽灵卡、孤儿卡看门狗（回合外投递永不挂卡）、clarify fallback 假确认、双 finalize 幂等、200 元素上限裁剪、流式光标剥除、思考行吸附与答案复读过滤、围栏内文本不被误改写、长文切点围栏闭合校验 |
| 兼容 | hermes-agent ≥ v0.22（`SUPPORTS_NATIVE_STREAMING`；启动 fail-fast 探测，失配即报点位）；plugin.yaml 声明 `python_dependencies`（PEP 508）；`requires-python >=3.11,<3.15`（官方运行时 3.14）；lark-oapi ≥ 1.6.4（≥1.7.x 回调类路径迁移已多候选兼容） |
| 清理 | V1 遗留死代码/死配置/死测试分批清除（钩子层约 2700 行 + 后续累计约千行）；删除 `state/`、`flush/` 遗留包 |
| 文档 | 文档体系重组：README（人类向：项目说明 + 部署/升级）、SKILL.md（agent 向：安装/验证/升级手册）、API.md（配置键位 + 出站帧契约）、CHANGELOG.md 同归 docs/；CI：Gitee → GitHub 全分支 + tag 每小时强制镜像、PR/Issue 双向飞书通知、hermes 上游发版飞书监控 |
| 已知 | 页脚 cost/tokens/cache 字段在 V2 数据契约下不可达（配置自动省略）；卡片转发复制为飞书平台默认能力（`enable_forward`），插件不额外干预 |
| 验证 | 真飞书 E2E 11 场景（`scripts/e2e_v2.py`，hermes v0.21.6/v0.22 + Python 3.14）；全量 pytest 771 passed |
