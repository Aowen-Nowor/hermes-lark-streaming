# hermes-lark-streaming Changelog

---

## v0.8.6 — 2026-05-26

### 修复

| 问题 | 说明 |
|------|------|
| 安装后无卡片效果 | 插件 Config 读不到顶层 `streaming` 配置，`enabled` 始终为 `False` |
| 卸载残留配置 | 卸载后 `streaming` 配置段和 `plugins.enabled` 中的条目未清理 |

### 改动

1. **`config.py`** — `_streaming_sec()` 只查顶层 `streaming`，不再查无关的 `display` 层
2. **`plugin.py`** — 新增 `unregister()`，卸载时：
   - 删除顶层 `streaming` 配置段
   - 从 `plugins.enabled` 列表中移除 `hermes-lark-streaming`
3. **`plugin.py`** — `register()` 新增 `_ensure_streaming_config()`，首次加载时自动注入 `streaming` 配置段（含默认值）
4. **README 文档** — 更新中英文配置说明，标注自动注入/清理行为

# hermes-lark-streaming Changelog

---

## v0.8.5 — 2026-05-26

包含以下 5 个修复：

| 修复版本 | 日期 | 问题 |
|---------|------|------|
| fix1 | 2026-05-25 | 插件根目录缺少 `__init__.py` 导致加载失败 |
| fix2 | 2026-05-25 | 回调重复包装导致内容重复 |
| fix3 | 2026-05-25 | `setattr` 缩进错误导致语法异常 |
| fix4 | 2026-05-26 | `contextvars` 不跨线程导致后续消息无流式更新 |
| fix5 | 2026-05-26 | `_set_thread_local_ctx()` 未定义 + 备份目录干扰 |

### 问题
fix4 修复后，第二/第三条消息（及后续）仍然出现跑马灯+Done 的问题。
第一条偶尔正常，但重启后所有消息均无流式更新。

### 日志模式
```
linear card created → on_completed  （无任何 stream/flush）
```

日志中未出现 `HLS_CALLED` 标记，说明 `_maybe_wrap_callbacks` 从未被调用。

### 原因
**双重根因：**

**1. `_set_thread_local_ctx()` 函数未定义**

fix4 的 changelog 声称新增了 `_set_thread_local_ctx()` 函数，但实际代码中**只有调用没有定义**。这行在 `_wrap_run_agent` 的 wrapper 中位于 `ctx["event_message_id"] = event_message_id` 之后，虽然因 `ctx` 可能为 None 而侥幸未崩溃，但 context 从未正确复制到 thread-local 存储。

**2. 备份目录干扰插件加载**

`/home/long/.hermes/plugins/hermes-lark-streaming.bak.20260525_224429/` 目录也有 `__init__.py`，导致 Hermes 的插件发现机制同时扫描了 `hermes-lark-streaming` 和 `hermes-lark-streaming.bak...`，两者都注册为 `hermes_plugins.hermes_lark_streaming` 命名空间。备份目录的旧版本（无 `monkey_patch.py`、无 `plugin.py`）**覆盖**了正在工作的新版，导致所有 runtime patch 从未生效。

### 修复
1. 修复 `_set_thread_local_ctx` 未定义问题：
   - 定义 `_thread_local_ctx = threading.local()` 作为 thread-local 存储
   - 将调用改为 `_thread_local_ctx.data = dict(ctx)`
   - `_get_event_message_id()` 增加 thread-local fallback（`_msg_ctx` 为空时回退到 `_get_thread_local_ctx()`）
2. 添加双重保险：在 `apply_patches()` 中直接 patch `AIAgent.run_conversation` 方法（5秒延迟执行），确保即使模块级 patch 未生效也能拦截 callback
3. 删除备份目录 `hermes-lark-streaming.bak.20260525_224429`，消除命名空间冲突

### 涉及文件
- `hermes_lark_streaming/monkey_patch.py` — 修复 `_set_thread_local_ctx` 未定义、增加 `_get_thread_local_ctx()`、`_schedule_direct_patch()` 和 `_apply_direct_agent_patch()`
- `hermes-lark-streaming.bak.20260525_224429` — 删除（命名空间冲突）

---

## v0.8.0-fix4 — 2026-05-26

### 问题
第一条消息卡片流式正常，第二条（及后续）消息卡片只显示跑马灯和 Done.，工具调用和完整回复走了文本回复。

### 日志模式
```
# 第一条 ✅
linear card created → linear flush → linear stream → on_completed

# 第二条 ❌
linear card created → on_completed  （中间无任何 stream/flush）
```

### 原因
双重重叠的 bug：

**1. `_wrap_run_conversation` 补丁失效**
V3 修复时误将 `apply_patches()` 中的 `from run_agent import AIAgent` 和 `AIAgent.run_conversation = _wrap_run_conversation(...)` 两行删除了（edit 替换时多覆盖了）。导致 callback 包装器从未被安装到 `AIAgent.run_conversation` 上。

**2. `contextvars.ContextVar` 不跨线程**
Hermes 的 `_run_agent` 通过线程池（`ThreadPoolExecutor`）执行 `run_conversation`。而 `_maybe_wrap_callbacks` 通过 `contextvars.ContextVar` 读取 `event_message_id`，`contextvars` **不跨线程传播**。线程池里 `_msg_ctx.get()` 永远返回 `None`，导致 `_get_event_message_id()` 返回空 → `_maybe_wrap_callbacks` 在第一个 guard（检查 eid 是否为空）就直接 return 了，永远不会去包装 callback。

第一条消息之所以偶尔正常，是因为某些路径下 callback 在 async 上下文里被触发（contextvars 可读），但第二条纯线程执行时必然失败。

### 修复
1. 恢复 `apply_patches()` 中对 `conversation_loop.run_conversation` 的 patch
2. 引入 `threading.local()` 作为 `contextvars` 的 fallback 存储
3. 在 `_wrap_run_agent` 中将 msg context 复制到 thread-local
4. `_get_event_message_id()` 先查 contextvars，查不到则 fallback 到 thread-local

### 涉及文件
- `hermes_lark_streaming/monkey_patch.py` — 新增 `threading` import、`_thread_local`、`_set_thread_local_ctx()`、`_get_event_message_id()` 的 thread-local fallback、`_wrap_run_agent` 中 thread-local 写入

---

## v0.8.0-fix3 — 2026-05-25

### 问题
fix2 打包的 `monkey_patch.py` 中 `setattr(agent, _wrap_attr, True)` 错位缩进，导致 Python 语法错误（`IndentationError: expected an indented block after 'except' statement`）。

插件启动时 `register()` 调用 `apply_patches()` 抛出异常，整条流式卡片链路断裂：
- 无卡片效果（fallback 到纯文本回复）
- fix2 的防重复包装 guard 也一起失效

### 原因
fix2 在 `_maybe_wrap_callbacks` 中新增了 guard 逻辑和 `setattr`，但 `setattr(agent, _wrap_attr, True)` 这行被插到了 `except Exception:` 和 `pass` 之间，破坏 Python 缩进结构：

```python
# ❌ fix2 中的错误位置
except Exception:

    setattr(agent, _wrap_attr, True)  # ← 错位到 except 内部
                pass                  # ← 缩进错乱
```

### 修复
将 `setattr(agent, _wrap_attr, True)` 正确移动到 `_tool_wrapper` 函数定义和赋值之后（`agent.tool_progress_callback = _tool_wrapper` 之后），保持正确缩进：

```python
# ✅ fix3 中的正确位置
except Exception:
    pass
return _orig(event_type, tool_name, preview, *args, **kwargs)

agent.tool_progress_callback = _tool_wrapper

# Mark as wrapped
setattr(agent, _wrap_attr, True)
```

### 涉及文件
- `hermes_lark_streaming/monkey_patch.py` — 修复 `_maybe_wrap_callbacks()` 中 `setattr` 缩进位置

---

## v0.8.0-fix2 — 2026-05-25

### 问题
卡片内相同文本重复出现两次。比如回复 "好问题，先查一下 AstrBot..."，卡片里这段文字出现了两遍。

### 原因
Hermes 在同一个会话中会**多次调用 `run_conversation`**（每次工具调用结束后继续流式输出）。每次调用 `run_conversation` 时，插件的 `_maybe_wrap_callbacks` 函数都会把 `stream_delta_callback`（及其他 callback）重新包装一层。

第一次包装：`_orig = 原始 callback` → `_answer_wrapper` 调 `on_answer_delta` 消费文本 → `return _orig(text)` 传给原始 callback。

第二次包装：`_orig = 第一次的 _answer_wrapper` → 新的 `_answer_wrapper` 调 `on_answer_delta` 再次消费文本 → `return _orig(text)` 又传给第一次的 wrapper → 又调一次 `on_answer_delta`。

结果：每段文本被 `on_answer_delta` 处理两次 → 卡片里追加了两次。

### 修复
在 `_maybe_wrap_callbacks` 开头加了防重复包装守卫：

```python
_wrap_attr = "_hls_wrapped"
if getattr(agent, _wrap_attr, False):
    return  # 已包装过，跳过
```

第一次包装 callback 后在 agent 实例上设置 `agent._hls_wrapped = True`，后续 `run_conversation` 调用时检测到标记直接跳过，不再重复包装。

### 涉及文件
- `hermes_lark_streaming/monkey_patch.py` — `_maybe_wrap_callbacks()` 函数开头新增守卫逻辑

---

## v0.8.0-fix1 — 2026-05-25

### 问题
通过 `hermes plugins install` 安装后，插件加载失败，无卡片效果，Hermes 仍然回复纯文本。

日志报错：
```
WARNING hermes_cli.plugins: Failed to load plugin 'hermes-lark-streaming':
  No __init__.py in /home/long/.hermes/plugins/hermes-lark-streaming
```

### 原因
Gitee 仓库的目录结构为：
```
hermes-lark-streaming/
├── hermes_lark_streaming/   ← 子包（含 __init__.py 和 register 函数）
├── plugin.yaml
├── pyproject.toml
└── ...（没有根目录 __init__.py）
```

Hermes 的 plugin loader `_load_directory_module()` 要求插件**根目录**必须有 `__init__.py`，且其中暴露 `register(ctx)` 函数。仓库只提供了 `hermes_lark_streaming/` 子包，缺少根目录的 `__init__.py`。

### 修复
在插件根目录新增 `/home/long/.hermes/plugins/hermes-lark-streaming/__init__.py`，内容为导入桥：

```python
"""hermes-lark-streaming — Feishu/Lark CardKit v2.0 streaming cards for Hermes Agent."""

from .hermes_lark_streaming import register

__all__ = ["register"]
```

这样 Hermes 加载根目录 `__init__.py` 时能找到 `register` 函数，进而调用 `apply_patches()` 应用 runtime monkey patch。

### 涉及文件
- `__init__.py`（根目录）— 新增文件，导入子包的 register 函数