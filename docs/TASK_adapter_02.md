# TASK_adapter_02 — FeishuStreamingAdapter 继承骨架与出站路由
- 依赖: TASK_platform_01 ｜ 预估: ≤2h
## 步骤
1. class FeishuStreamingAdapter(FeishuAdapter)：__init__ 组装 FrameComposer（注入 FeishuClient 配置）
2. 重写 send(chat_id, content, reply_to, metadata)：帧分类 → 卡片路由 or super().send()
3. 重写 edit_message(chat_id, message_id, content, *, finalize)：卡片实体 patch / 定稿 or super().edit_message()
4. 发送前剥离 _interim_send 内部标记（官方预期行为）
5. 重写点签名探测：启动时 hasattr/inspect 校验继承面，失配 fail-fast 报错
## 验收标准
- plain 帧（无任何标记）行为与内置 feishu 完全一致（走 super()）
- 任何异常不阻断消息投递（降级 super()）
