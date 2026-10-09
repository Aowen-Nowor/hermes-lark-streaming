# Changelog

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
