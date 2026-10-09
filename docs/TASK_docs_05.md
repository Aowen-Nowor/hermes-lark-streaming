# TASK_docs_05 — 全新 V2 文档体系与测试适配
- 依赖: TASK_integration_04 ｜ 预估: ≤2h
## 步骤
1. 删除 V1 全部文档（README/README.zh-CN/docs/SKILL.md/AGENT_GUIDE.md/ISSUES_TEMPLATE.md/CHANGELOG.md）
2. 重写 README.md（V2.0.0 全新视角：定位/安装/配置/能力/FAQ）
3. 重写 API.md（配置接口：gateway 配置/插件配置/env 清单）
4. 重写 TEST_*.md（验收文档：冒烟用例/结果/结论）
5. tests/ 适配：删除 patching/controller 架构测试，保留 cardkit/text/phase/linear/tooluse/config，test_version 适配 V2 版本点
6. CHANGELOG.md 全新（仅 V2.0.0 条目）
## 验收标准
- 仓库内无任何 V1 文档残留、无 patching/controller 代码引用
- 冒烟脚本通过（导入/注册协议/帧分类/卡片构建）
