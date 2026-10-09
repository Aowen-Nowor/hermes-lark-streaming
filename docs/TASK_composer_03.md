# TASK_composer_03 — FrameComposer 帧分类器与卡片状态机
- 依赖: TASK_adapter_02 ｜ 预估: ≤2h
## 步骤
1. classify.py：ClassifyResult + classify_send/classify_edit（expect_edits/_interim_send/notify/finalize/progress 模板识别），防御性解析（永不抛异常）
2. engine.py：CardTurnState（card_id/message_id/answer 缓冲/状态行/节流窗口/降级旗标）+ FrameComposer（per (chat_id,turn) 状态表、TTL 清理）
3. finalize.py：定稿流程（折叠思考区、footer 收尾、转静态、延迟清理）
## 验收标准
- 四通道帧样本（流式/进度/interim/定稿）分类正确率 100%
- 卡片 API 失败时置降级旗标，本 turn 后续帧全走原生
