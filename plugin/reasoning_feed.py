"""V2.0.7 — reasoning 隐藏流 → 状态面板思考行（「第 N 轮」）桥.

根因（用户验收 2026-10-11）：折叠面板只有工具行、没有思考行。hermes 网关的
``thinking_progress`` 只中继模型在工具调用之间的**可见间歇文本**
（``agent/turn_response_intake.py::_relay_thinking`` 读 assistant content）；
而 DeepSeek 等推理模型的思考走 ``reasoning_content`` 隐藏流 —— 网关主代理
路径不接 ``reasoning_callback``（仅 api_server 接），思考流被静默丢弃。

官方扩展通道（零 monkey-patch）：插件注册 ``on_stream_delta`` 观察者钩子 +
config ``plugins.stream_reasoning_deltas: true`` 后，网关把 ``kind="reasoning"``
的 delta 投递到本回调（``agent/plugin_stream_hooks.py``：独立有界队列 + 守护
线程，**不阻塞 token 路径**；回调运行在入队 turn 的 contextvars 里，可读
``gateway.session_context`` 会话变量定位 chat）。

合并语义对齐（关键）：composer 的 thinking 合并要求**新行是旧行的前缀延伸**
才原地替换。因此缓冲保留整段思考全文，每次 flush 发送**累积文本**（增长），
仅三种情形清空缓冲开新行：① delta 间隔 >8s（断轮，工具执行中）；②
``on_stream_end``（本次响应流结束）；③ 触达 600 字符冻结上限后文本不再变化
（重发同文本被合并器幂等跳过，不产生 cardkit_update）。

永不抛异常：观察者回调失败由 hermes 捕获告警，本模块再自兜一层。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

_logger = logging.getLogger("hermes_lark_streaming.reasoning_feed")

__all__ = ["bind_composer", "register_hooks", "reset_for_tests"]

# ── 节流参数 ────────────────────────────────────────────────────────────────
_FLUSH_INTERVAL_SEC = 1.5   # 两次面板刷新的最小间隔（cardkit_update 经济性）
_FLUSH_CHARS = 400          # 距上次刷新新增字符达到该值立即刷新
_MAX_LINE_CHARS = 2000      # 思考行冻结上限（v2.0.8：600→2000，V1
                            # _REASONING_DISPLAY_LIMIT 对齐——600 会把推理
                            # 正文/新闻草稿腰斩，用户验收「后面格式不对」伴生）
_GAP_RESET_SEC = 8.0        # delta 间隔超过该值（工具执行中）→ 清缓冲开新行

# 会话变量名（gateway/session_context.py _SESSION_VARS）
_CHAT_ID_VAR = "HERMES_SESSION_CHAT_ID"

# 绑定的 composer（adapter 实例化时注册；最多保留 4 个，覆盖 gateway 重载）
_BOUND: list[Any] = []
_MAX_BOUND = 4
_LOCK = threading.Lock()

# per-chat 思考缓冲：text=整段累积（冻结上限内）、sent=已投递长度、
# last_delta/last_flush=节流时钟、lock=串行化
_BUFS: dict[str, dict[str, Any]] = {}


def bind_composer(composer: Any) -> None:
    """adapter 实例化时把 composer 交给 feed（reasoning → 面板的落点）."""
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
    """register(ctx) 期注册观察者钩子（官方 hook API，unknown-name 会告警但无害）."""
    ctx.register_hook("on_stream_delta", on_stream_delta)
    ctx.register_hook("on_stream_end", on_stream_end)
    _logger.info("feishu-streaming: reasoning stream feed registered (on_stream_delta/on_stream_end)")


def _current_chat_id() -> str:
    """读当前 turn 的会话 chat id（ContextVar 优先，env 兜底）."""
    try:
        from gateway.session_context import get_session_env

        return (get_session_env(_CHAT_ID_VAR) or "").strip()
    except Exception:  # noqa: BLE001 — 会话上下文缺失时放弃归属（此处 BLE001 实际触发）
        return ""


def _buf(chat: str) -> dict[str, Any]:
    buf = _BUFS.get(chat)
    if buf is None:
        buf = _BUFS[chat] = {
            "text": "",
            "sent": "",
            "last_delta": 0.0,
            "last_flush": 0.0,
            "lock": threading.Lock(),
        }
    return buf


def on_stream_delta(**payload: Any) -> None:
    """观察者回调：``kind="reasoning"`` 的 delta → 按会话累积 → 节流回灌面板."""
    try:
        if payload.get("kind") != "reasoning":
            return  # 可见答案流（kind=text）由网关帧路径处理，此处只收思考
        delta = payload.get("delta") or ""
        if not isinstance(delta, str) or not delta:
            return
        chat = _current_chat_id()
        if not chat:
            return
        now = time.monotonic()
        buf = _buf(chat)
        with buf["lock"]:
            if buf["text"] and now - buf["last_delta"] > _GAP_RESET_SEC:
                # 长间隔（上轮思考已断，如工具执行中）→ 清缓冲开新行
                buf["text"] = ""
                buf["sent"] = ""
            buf["text"] = (buf["text"] + delta)[:_MAX_LINE_CHARS]
            buf["last_delta"] = now
            if (
                len(buf["text"]) - len(buf["sent"]) >= _FLUSH_CHARS
                or now - buf["last_flush"] >= _FLUSH_INTERVAL_SEC
            ):
                _flush_locked(chat, buf, now)
    except Exception:  # 观察者永不影响主流程
        _logger.debug("reasoning feed: on_stream_delta failed", exc_info=True)


def on_stream_end(**payload: Any) -> None:
    """响应流结束 → 该轮思考缓冲立即完整落盘并清空（下一轮开新行）.

    v2.0.9 P1-3：尾 flush 标记 ``final=True`` —— agent 层 on_stream_end 先于
    网关 finalize edit（竞态窗已实验证实），composer 对 final flush 做
    **执行时**终态复查：终态/无卡一律丢弃，不占位不建卡（幽灵卡防线）。
    """
    try:
        chat = _current_chat_id()
        if not chat:
            return
        now = time.monotonic()
        buf = _BUFS.get(chat)
        if buf is None:
            return
        with buf["lock"]:
            _flush_locked(chat, buf, now, final=True)
            buf["text"] = ""
            buf["sent"] = ""
    except Exception:  # 观察者永不影响主流程
        _logger.debug("reasoning feed: on_stream_end failed", exc_info=True)


def _flush_locked(chat: str, buf: dict[str, Any], now: float, *, final: bool = False) -> None:
    """把**累积全文**（非增量）作为一行 💬 推给所有绑定 composer。持锁调用.

    增长文本 → composer 合并器原地替换上一条思考行；文本冻结（达上限）后
    重发同文本 → 合并器幂等跳过，不产生 cardkit_update。``final=True``
    （on_stream_end 尾 flush）透传给 composer 做执行时终态复查（P1-3）。"""
    text = (buf.get("text") or "").strip()
    if not text or text == buf.get("sent"):
        return
    raw = f"\U0001f4ac {text}"  # 💬 —— engine 按 thinking 通道合并（增长原地替换）
    delivered = False
    for composer in _composers():
        try:
            if composer.submit_progress_from_thread(chat, raw, final=final):
                delivered = True
        except TypeError:
            # 兼容旧 composer 签名（无 final 参数）——降级为普通提交
            try:
                if composer.submit_progress_from_thread(chat, raw):
                    delivered = True
            except Exception:
                _logger.debug("reasoning feed: submit failed chat=%s", chat, exc_info=True)
        except Exception:  # 观察者永不影响主流程
            _logger.debug("reasoning feed: submit failed chat=%s", chat, exc_info=True)
    if delivered:
        buf["sent"] = text
        buf["last_flush"] = now
    _logger.debug("reasoning feed: flush chat=%s len=%d delivered=%s final=%s", chat, len(text), delivered, final)


def reset_for_tests() -> None:
    """测试隔离：清空缓冲与绑定."""
    with _LOCK:
        _BOUND.clear()
        _BUFS.clear()
