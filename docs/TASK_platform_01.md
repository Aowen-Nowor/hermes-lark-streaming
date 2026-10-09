# TASK_platform_01 — 平台插件形态改造
- 依赖: 无 ｜ 预估: ≤2h
## 步骤
1. 重写 plugin.yaml：kind: platform，name: feishu-streaming-platform（平台名自动解析为 feishu-streaming），声明 requires_env（复用 FEISHU_APP_ID/SECRET）与 optional_env
2. 重写根 __init__.py：导出 register
3. 新建 adapter.py 骨架：多候选路径导入内置 FeishuAdapter 与其辅助函数（feishu_deps_present / check_feishu_requirements / interactive_setup / _apply_yaml_config / _standalone_send / _is_connected）
4. register(ctx)：ctx.register_platform(name="feishu-streaming", adapter_factory=FeishuStreamingAdapter, 其余 fn 全部复用内置)
5. 保留 plugin/ 配置注入与 lark-oapi 版本门槛，并入 register 流程
## 验收标准
- hermes 插件发现机制能列出 feishu-streaming 平台（manifest 解析 + 平台名推导正确）
- 缺少 lark-oapi 时 check_fn 返回 False 且提示安装
