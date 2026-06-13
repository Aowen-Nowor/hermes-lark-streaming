## v1.0.4 (2026-06-13)

| 类型 | 问题/功能 | 原因 | 修复/说明 |
|------|-----------|------|-----------|
| 🐛 Bug Fix | GitHub Actions 测试失败 (37/780) | test_phase.py: `asyncio.get_event_loop()` 在 Python 3.10+ 无事件循环时抛 RuntimeError；test_monkey_patch.py: guard 日志从 `_logger.debug("HLS_WRAP: guard check")` 变更为 `_logger.warning("HLS_DIAG: ...")` 但测试未同步；test_version.py: `importlib.reload` 在 Python 3.11+ 要求 `__spec__` 非 None | 1) `FlushController.__init__` 惰性获取事件循环（`_loop=None` + `_get_loop()`）；2) 测试设置 `asyncio.set_event_loop()`；3) 测试断言更新为 `_logger.warning` + `HLS_DIAG`；4) `importlib.reload` 替换为 `spec_from_file_location` 重注册 |
| 🐛 Bug Fix | 验证安装/卸载步骤 HERMES_PYTHON 路径错误 | 文档硬编码 `~/.hermes/hermes-agent/venv/bin/python3`，仅适用于 Hermes Desktop；CLI/服务器安装路径为 `/usr/local/lib/hermes-agent/venv/bin/python3` | 新增 `__main__.py python` 命令自动检测路径；文档改用 `$(python3 ... __main__.py python)` 自动检测；补充手动设置说明 |
| ✨ Feature | 新增 `python` CLI 命令 | 用户需要知道 Hermes venv Python 的路径才能运行 status/verify/cleanup | `__main__.py python` 自动搜索常见安装路径并输出，简化文档指令 |
| 🔧 Fix | FlushController 在无事件循环环境初始化失败 | Python 3.10+ 中 `asyncio.get_event_loop()` 在无事件循环时抛 RuntimeError | `__init__` 捕获双重 RuntimeError 设 `_loop=None`，新增 `_get_loop()` 惰性获取 |

## v1.0.3 (2026-06-12)

| 类型 | 问题/功能 | 原因 | 修复/说明 |
|------|-----------|------|-----------|
| 🏗️ Architecture | 卡片生命周期状态机优化 | 参考 openclaw-lark 设计，需显式状态转换、终端原因追踪、epoch 机制 | 新增 `CardPhase`/`TerminalReason`/`CardVisualState` + `PHASE_TRANSITIONS` 转换图 + `transition()`/`should_proceed()`/`is_stale_create()`/`enter_terminal()` 方法；`CREATION_FAILED` 替代旧 `FAILED`；新增 `TERMINATED` 阶段；88 个测试覆盖 |
| 🐛 Bug Fix | 会话列表永久显示"处理中..."（中文用户） | `cardkit_close_streaming` 只更新 `summary.content`，未更新 `summary.i18n_content`；飞书根据用户语言显示 `i18n_content.<locale>` | `cardkit_close_streaming` 同时更新 `content` 和 `i18n_content`（zh_cn + en_us）；新增 `_build_summary()` 辅助函数；4 个回归测试 |
| 🐛 Bug Fix | 重复 `close_streaming` 导致 300317 序列冲突 | `_preservative_seal` 主路径和重试路径都调用 `close_streaming`，第二次调用时 sequence 已过期 | `CardSession` 新增 `_streaming_closed` 守卫标志，确保 `close_streaming` 只调用一次 |
| 🐛 Bug Fix | `UnboundLocalError: 'panel'` 导致恢复路径崩溃 | 300317 重试路径引用 try 块中 `panel` 变量，但 `panel` 在 `close_streaming` 之后才赋值 | 重试路径始终从当前状态重建 `retry_panel`，而非引用 try 块局部变量 |
| 🐛 Bug Fix | 折叠面板思考内容重复（DeepSeek 模型） | `stream_delta_callback` 为 None 时，守卫仅检查其 `_hls_wrapper` 标记，`interim_assistant_callback` 被双重包装 | 守卫同时检查两个回调的 `_hls_wrapper` 标记；去重逻辑从精确匹配升级为长度追踪 |
| 🐛 Bug Fix | 会话列表完成后仍显示"处理中..." | 飞书 settings API 不稳定处理同一请求中的 `summary` + `streaming_mode: false` | 两步更新：先 `close_streaming`（不含 summary），再 `cardkit_update_summary`；流式已关闭时仍更新摘要 |
| 🐛 Bug Fix | 封卡时内容丢失 | `_preservative_seal` 的完整性守卫只清除 dirty 标记未实际 flush | 守卫升级为在 `close_streaming` 前实际 flush 剩余脏数据 |
| 🐛 Bug Fix | 页脚早于回答内容出现 | `COMPLETING` 在 `_TERMINAL` 集合中，晚到回调被丢弃 | 移除 `COMPLETING` 出终端集；drain 步骤确保内容完整输出 |
| 🐛 Bug Fix | 流式参数低于官方默认值 | `print_frequency_ms` 为 10ms（官方默认 70ms） | `print_frequency_ms` 提升至 70ms；`_ANSWER_FAST_STREAM_MS` 提升至 70ms；`flush_interval_ms` 范围改为 70–2000ms |
| ✨ Feature | 打字机效果 | 流式卡片输出按字符渲染，匹配飞书 CardKit v2.0 文档行为 | `print_frequency_ms=70`、`print_step=1`、默认 `flush_interval_ms=100ms`、仅回答快流 70ms |
| 🚀 Performance | 延迟 Markdown 优化 | 流式期间每次 flush 都执行 `optimize_markdown_style` 开销大 | 流式期间发送原始文本，仅在封卡时执行完整 Markdown 优化 |
| 🚀 Performance | 间隔计时器优化 | `LONG_GAP_MS` 和 `BATCH_AFTER_GAP_MS` 过长 | `LONG_GAP_MS` 2.0s → 1.0s，`BATCH_AFTER_GAP_MS` 300ms → 100ms；瞬态重试延迟缩减 |
