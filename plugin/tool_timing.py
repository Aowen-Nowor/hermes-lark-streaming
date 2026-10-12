"""V2.0.8 — 官方 ``post_tool_call`` 钩子 → 面板单工具耗时（「Terminal (2.3 s)」）桥.

用户验收（2026-10-11）：「每一条都没有耗时记录，而插件都有的」。V1 由工具
事件实测（state/tooluse.py record_start/record_end）；V2 零补丁架构下的官方
数据源是 hermes ``post_tool_call`` 生命周期钩子——payload 自带
``duration_ms``（model_tools.py::_emit_post_tool_call_hook，工具执行完同步
触发，运行在 turn 的 contextvars 里，可读 ``gateway.session_context`` 定位
chat）。

数据通路：钩子回调（agent 线程）→ ``composer.submit_tool_duration_from_thread``
（call_soon_threadsafe → 事件循环）→ engine 挂到该工具最近的无耗时行 →
重渲染面板。行未到达（钩子先于进度帧 drain）时由 engine 的 pending 队列
在行追加时认领。

永不抛异常：钩子回调失败由 hermes 捕获告警，本模块再自兜一层。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

_logger = logging.getLogger("hermes_lark_streaming.tool_timing")

__all__ = ["bind_composer", "register_hooks", "reset_for_tests"]

# 会话变量名（gateway/session_context.py _SESSION_VARS，与 reasoning_feed 同源）
_CHAT_ID_VAR = "HERMES_SESSION_CHAT_ID"

# 绑定的 composer（adapter 实例化时注册；最多保留 4 个，覆盖 gateway 重载）
_BOUND: list[Any] = []
_MAX_BOUND = 4
_LOCK = threading.Lock()


def bind_composer(composer: Any) -> None:
    """adapter 实例化时把 composer 交给本桥（duration_ms → 面板的落点）."""
    if composer is None:
        return
    with _LOCK:
        if any(bound is composer for bound in _BOUND):
            return
        _BOUND.append(composer)
        del _BOUND[:-_MAX_BOUND]


def _composers() -> list[Any]:
    with _LOCK:
        return list(_BOUND)


def register_hooks(ctx: Any) -> None:
    """register(ctx) 期注册官方工具生命周期钩子（zero monkey-patch）."""
    ctx.register_hook("post_tool_call", on_post_tool_call)
    _logger.info("feishu-streaming: tool timing feed registered (post_tool_call)")


def _current_chat_id() -> str:
    """读当前 turn 的会话 chat id（ContextVar 优先，env 兜底）."""
    try:
        from gateway.session_context import get_session_env

        return (get_session_env(_CHAT_ID_VAR) or "").strip()
    except Exception:  # noqa: BLE001 — 会话上下文缺失时放弃归属（此处理论触发）
        return ""


def on_post_tool_call(**payload: Any) -> None:
    """官方钩子回调：``duration_ms`` > 0 → 投递给所有绑定 composer."""
    try:
        tool_name = str(payload.get("tool_name") or "").strip()
        try:
            duration_ms = float(payload.get("duration_ms") or 0.0)
        except Exception:  # noqa: BLE001 — 载荷异常按无数据丢弃
            duration_ms = 0.0
        if not tool_name or duration_ms <= 0:
            return
        chat = _current_chat_id()
        if not chat:
            return
        for composer in _composers():
            try:
                composer.submit_tool_duration_from_thread(chat, tool_name, duration_ms)
            except Exception:  # 钩子永不影响主流程
                _logger.debug("tool timing: submit failed chat=%s", chat, exc_info=True)
    except Exception:  # 钩子永不影响主流程
        _logger.debug("tool timing: on_post_tool_call failed", exc_info=True)


def reset_for_tests() -> None:
    """测试隔离：清空绑定."""
    with _LOCK:
        _BOUND.clear()
