# Changelog

## v2.0.7 (2026-10-11)

**思考行修复：DeepSeek reasoning 隐藏流 → 面板「第 N 轮」思考记录（零 monkey-patch）。**

用户验收（2026-10-11）：「折叠面板里为何没有思考、推理的记录，只有工具的记录」。
根因（日志+源码+端点实测三重定位）：hermes 网关 `thinking_progress` 只中继模型在
工具调用之间的**可见间歇文本**（`_relay_thinking` 读 assistant content）；而
DeepSeek-v4-flash 的思考走 `reasoning_content` 隐藏流（端点流式实测 62 reasoning
块/次回答）——网关主代理路径不接 `reasoning_callback`（仅 api_server 接），
`_fire_reasoning_delta` 静默丢弃，面板永远收不到 💬 行。

| 变更 | 文件 | 说明 |
|---|---|---|
| **新增** reasoning 流桥 | `reasoning_feed.py` | 注册官方 `on_stream_delta`/`on_stream_end` 观察者钩子；`kind="reasoning"` delta 按会话（`gateway.session_context` ContextVar 定位 chat）累积、节流（1.5s/400 字符）、间隔 >8s 断轮重置，回灌 composer 的 CH_PROGRESS/thinking 通道 → 面板按既有合并语义（增长原地替换）渲染「第 N 轮」思考行；600 字符冻结上限保前缀增长幂等 |
| **新增** 线程安全回灌 | `composer/engine.py` | `submit_progress_from_thread()`：捕获事件循环引用（构造期/帧入口），`call_soon_threadsafe` → handle_send 消费；已定稿/已降级丢弃 |
| **新增** 适配器接线 | `adapter.py` | 实例化时 `bind_composer()`；`register(ctx)` 注册观察者钩子（需 config `plugins.stream_reasoning_deltas: true` 才有投递；未配置/失败仅降级为无思考行） |

**部署要求**：config.yaml 增加 `plugins: {stream_reasoning_deltas: true}`。

套件 674/674（新增 11 用例）；ruff 新代码零新增告警。

## v2.0.6 (2026-10-10)

**真实帧格式对齐：E2E ≠ 正式对话根因修复（用户验收反馈驱动）。**

用户验收（2026-10-10）：「E2E 群聊消息已接近理想效果，但私聊正式对话面板里是
`💻 terminal` + 行号代码块」。根因：v2.0.5 的 E2E 用合成帧（`🖥️ terminal："…"`）
恰好命中解析正则，而 hermes v0.21.x 真实 progress 模板（`agent/display.py` +
`gateway/run_turn_runner.py` + locales）完全不同——正式对话的行从未被结构化解析，
面板只能原样渲染 emoji 原文。

| 变更 | 文件 | 说明 |
|---|---|---|
| **新增** 真实格式解析 | `cardkit/progline.py` | ① terminal 围栏代码块（`💻 terminal` + ` ``` ` 块）→ (tool, 首行命令)；② 中文/英文动词行（`🔍 搜索网页 · q` / `💻 Running uname`）→ 动词反查工具名（hermes locales display.verb 双语动词表）；③ 裸围栏块（连续 terminal 调用省略 header）；④ 网关去重后缀 `(×N)` 剥除；⑤ `split_progress_segments`：accumulate 快照按逻辑段拆分，围栏块与 header 保持一体（直拆 splitlines 会把块拆成破碎 ` ``` ` 行——正是正式对话面板破碎的直接原因） |
| **修复** 面板渲染 | `cardkit/cards.py` | 真实格式 → V1 风格元素：官方图标 + 彩色粗体工具标题 + 缩进灰字命令；emoji / ` ``` ` 围栏永不进面板元素；包裹引号在 `preview_text` 内剥除（不再误伤命令尾部引号） |
| **修复** 合并语义 | `composer/engine.py` | `(×N)` 幂等；pending → 代码块原地取代；同工具再次调用追加新步骤；建卡前暂存行过同一套分段 |
| **修复** 分类通道 | `composer/classify.py` | 裸围栏块判入 progress 通道（≤600 字符），真实会话裸 send 升格路径可用 |
| **新增** 回归防线 | `tests/e2e/test_v206_real_formats.py` | 固化真实格式的解析/分段/合并/渲染/引擎端到端语义（22 用例），防再次回退 |

套件 663/663；ruff 新代码零新增告警。

## v2.0.1 (2026-10-10)

**生产部署夜修复：进度帧上下文升格 + 进度气泡快照合并 + 网关咬合加固。**

云服务器实测（hermes 2026.9.24 快照 + v0.21.6）暴露三个生产缺陷，本版全部修复：

| 变更 | 文件 | 说明 |
|---|---|---|
| **新增** 进度帧上下文升格 | `adapter.py` | edit-transport 平台（feishu）的工具进度气泡首条是**裸 send**（hermes 只给 native-stream / 预览路径打 `expect_edits`，`_send_progress_text` 不带任何标记）→ 原分类落 CH_PLAIN 产生独立纯文本气泡。现在：活跃卡片期 + 进度启发式（💬 前缀 / emoji 短行）→ 升格 CH_PROGRESS 吸收进状态面板 |
| **修复** 进度快照合并 | `composer/engine.py` | 网关 accumulate 模式下气泡文本是「当前全部进度行」的全量快照（每 tick 重发累积全文）——原实现整块 append 会产生膨胀 blob。现在按行拆分 + 集合去重幂等合并（accumulate / separate 两种模式都正确） |
| **修复** 进度帧返回契约 | `composer/engine.py` | `_on_progress` 不再回传卡片 message_id——防止网关 `_send_progress_text` 把卡片 id 记作 `progress_msg_id` 后用 `_edit_progress_message` 把工具行**覆写进答案区**（卡片内容损坏事故预防） |
| **新增** `display_tier="high"` | `adapter.py` | 显式声明显示基线（streaming 跟随全局 + tool_progress "all" + interim 开），防插件平台默认漂移静默关闭流式 |
| **新增** 生命周期日志 | `composer/engine.py` | 建卡 / 定稿 INFO 日志（chat/card/行数），生产日志可观测 |

**兼容性**：hermes v0.21.6（官方最新稳定版）注册协议全兼容（`PlatformEntry.display_tier` 官方字段）；v0.21.6 已移除 PM/uv-sync 准入机制，插件以目录插件形态直接加载（`~/.hermes/plugins/<name>/`）。

## v2.0.0 (2026-02-06)

**架构重构：standalone 补丁插件 → 正规 platform 插件（零 monkey-patch）。**

V2.0.0 是一次彻底重写：插件从「运行时补丁集合」变为「可安装的平台」，与内置 `feishu` 平台并存、随时切换。V1 文档与补丁架构全部退役。

| 变更 | 说明 |
|---|---|
| **新增** `adapter.py` | `FeishuStreamingAdapter` 继承内置 `FeishuAdapter`（能力零损失），重写 `send` / `edit_message` / `delete_message` / `send_clarify` / `_handle_card_action_event` |
| **新增** `composer/` | 帧重组引擎：官方 metadata 帧分类（`expect_edits` / `_interim_send` / `notify` / progress 模板）→ CardKit 生命周期（create → stream_element → close_streaming）；卡片 API 失败自动降级原生文本，永不丢消息 |
| **新增** `clarify.py` | 澄清卡片闭环正式化（选择/输入/重试/确认三态 + fail-closed 授权） |
| **变更** `plugin.yaml` | `kind: standalone` → `kind: platform`，平台名 `feishu-streaming` |
| **变更** 配置节 | `hermes_lark_streaming` → `feishu_streaming`（自动迁移） |
| **删除** `patching/` | 全部运行时 monkey-patch（v2.0.0 验收标准：0 补丁） |
| **删除** `controller/` | token 级流式控制器——事件源（agent 回调钩子）随补丁架构一并退役，由帧重组引擎替代 |
| **保留** `cardkit/` `feishu/` `state/` `flush/` `aowen/` `config/` | 卡片引擎、CardKit 客户端、会话状态、/aowen 指令原样继承 |
| **已知限制** | ① `/aowen` 在 agent 运行中的"中断提示卡片"随补丁架构退役（无官方状态查询接口）；② 终态卡片状态区折叠与 footer 计费字段为 v2.1 规划；③ 非 clarify 的卡片按钮点击被抑制 `/card` 合成命令（继承 V1 行为） |

**升级**：安装本插件后把 gateway 平台名 `feishu` → `feishu-streaming` 即可；回滚改回即恢复原生。

**v2.0.0 E2E 验证补充（refactor/v2.0.0 @ 0334cc9）**：

- 真飞书 E2E 实测打通：云服务器（Ubuntu 26.04 / Python 3.14 / hermes v0.21.6）+ 真实应用凭证，`scripts/e2e_v2.py` 七场景（connect/answer/progress/interim/full/clarify/plain）25+ 项断言全 PASS——含建卡前 progress 暂存并入首卡状态面板、重复工具行去重、打字机流式、两步定稿、澄清卡片直发
- 随验证修复的三处 V2 真实缺陷：① composer `active_state()` 未排除建卡前占位状态（工具先行场景整 turn 降级）② `_create_card()` 丢弃 pending_status（首卡状态面板恒空）③ `clarify.py` 3 处 `..cardkit` 相对导入越界（生产澄清卡片必静默降级纯文本）
- 部署实测再修两处适配器引导缺陷：④ `type()` 重建类借用的方法函数 `__class__` cell 指向旧占位基类 → 生产首次实例化 super() 身份错乱（改为**导入期绑定真实基类**，rebuild 退化为 no-op；`__bases__` 原地赋值在 CPython 3.12+ 直接 TypeError，不可用）⑤ 插件平台凭证需在 config `platforms.feishu-streaming.extra` 显式种入 `app_id`/`app_secret`（内置平台的 `_ENV_STEPS` 环境桥不覆盖插件平台，`_is_connected` 据此判定）
- `tests/e2e/` 完成到 V2 的移植（23 用例，mock/real 双模式）+ 导入期绑定回归测试，全量套件 585/585
- 部署形态（已上线）：hermes gateway 常驻 + feishu-streaming 平台接管 + 真飞书 WS + SenseNova LLM；插件准入需配国内 PyPI 镜像 + 放宽 pm 超时（`uv sync` 全量重建 venv 在国内网络必超时）
