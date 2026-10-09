# TASK_integration_04 — 卡片驱动接线与特化卡片
- 依赖: TASK_composer_03 ｜ 预估: ≤2h
## 步骤
1. cardkit_create / cardkit_stream_element / cardkit_update 接入 composer（FeishuClient 复用）
2. send_update_prompt / _send_exec_approval_prompt 卡片化重写
3. send_clarify（澄清卡片）+ handle_card_action_event（按钮回调：澄清选择/提交/确认）
4. cron/后台投递卡片化（build_cron_card / build_gateway_card，可配置开关）
5. /aowen 指令与功能卡片接入入站事件层
## 验收标准
- 澄清卡片选择→提交→确认闭环可走通（卡片 API mock 层面）
- cron 卡片渲染正确（内容截断/emoji/标题）
