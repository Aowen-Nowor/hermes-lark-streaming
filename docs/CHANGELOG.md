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
