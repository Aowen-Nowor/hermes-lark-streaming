"""与 openclaw-lark UnavailableGuard 对齐."""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from typing import Any

from .client import MSG_NOT_FOUND

_logger = logging.getLogger("hermes_lark_streaming")

_TERMINAL_MESSAGE_CODES = {
    231003,  # message deleted
    MSG_NOT_FOUND,
    230011,  # message recalled
}

_unavailable_cache: dict[str, dict[str, Any]] = {}
_unavailable_cache_lock = threading.Lock()
_UNENHANCED_CACHE_TTL_SEC = 30 * 60  # 30 分钟 TTL
# v1.3.0 perf: prune only when cache exceeds this threshold, instead of on
_PRUNE_THRESHOLD = 50

def _prune_cache() -> None:
    """清理过期缓存条目."""
    now = time.time()
    expired = [k for k, v in _unavailable_cache.items() if now - v.get("at", 0) > _UNENHANCED_CACHE_TTL_SEC]
    for k in expired:
        _unavailable_cache.pop(k, None)

def mark_unavailable(message_id: str, code: int, operation: str = "") -> None:
    """标记消息为不可用."""
    with _unavailable_cache_lock:
        _unavailable_cache[message_id] = {
            "code": code,
            "operation": operation,
            "at": time.time(),
        }

def is_unavailable(message_id: str | None) -> bool:
    """检查消息是否已知不可用."""
    if not message_id:
        return False
    with _unavailable_cache_lock:
        if len(_unavailable_cache) > _PRUNE_THRESHOLD:
            _prune_cache()
        return message_id in _unavailable_cache

def _get_cached_code(message_id: str | None) -> int | None:
    """线程安全地从缓存读取错误码（修复 code=0 被 or 吞掉的 bug）."""
    if not message_id:
        return None
    with _unavailable_cache_lock:
        entry = _unavailable_cache.get(message_id)
        return entry.get("code") if entry else None

_RE_API_CODE = re.compile(r"code[=:]\s*(\d+)")

def extract_api_code(err: Exception | None) -> int | None:
    """从异常中提取 API 错误码."""
    if err is None:
        return None
    if hasattr(err, "code"):
        code = err.code
        if isinstance(code, int):
            return code
    if hasattr(err, "args") and err.args:
        first = err.args[0]
        if isinstance(first, str):
            # 尝试从字符串中提取 code=数字
            match = _RE_API_CODE.search(first)
            if match:
                return int(match.group(1))
    return None

def is_terminal_api_code(code: int | None) -> bool:
    """判断错误码是否为消息终端码."""
    return code is not None and code in _TERMINAL_MESSAGE_CODES

class UnavailableGuard:
    """检测消息被删除/撤回后终止 pipeline."""

    def __init__(
        self,
        reply_to_message_id: str | None,
        get_card_message_id: Callable[[], str | None],
        on_terminate: Callable[[], None],
    ) -> None:
        self._reply_to_message_id = reply_to_message_id
        self._get_card_message_id = get_card_message_id
        self._on_terminate = on_terminate
        self._terminated = False

    def should_skip(self, source: str) -> bool:
        """检查是否应跳过当前操作."""
        if self._terminated:
            return True
        if not self._reply_to_message_id:
            return False
        if is_unavailable(self._reply_to_message_id):
            return self.terminate(source)
        return False

    def terminate(self, source: str, err: Exception | None = None) -> bool:
        """返回 True 表示已终止（或早已终止）."""
        if self._terminated:
            return True

        code = extract_api_code(err)
        card_msg_id = self._get_card_message_id()

        # 从错误码或缓存中判断
        if code is None and (is_unavailable(self._reply_to_message_id) or is_unavailable(card_msg_id)):
            # Fix: use is-None check instead of `or` — `0 or X` returns X,
            # but code=0 is a valid error code that should not be skipped.
            code = _get_cached_code(self._reply_to_message_id)
            if code is None:
                code = _get_cached_code(card_msg_id)

        if not is_terminal_api_code(code):
            return False

        assert code is not None

        self._terminated = True
        self._on_terminate()

        affected = self._reply_to_message_id or card_msg_id or "unknown"
        _logger.warning(
            "reply pipeline terminated by unavailable message: source=%s code=%s message_id=%s",
            source,
            code,
            affected,
        )

        # 标记缓存
        if self._reply_to_message_id:
            mark_unavailable(self._reply_to_message_id, code, source)
        if card_msg_id:
            mark_unavailable(card_msg_id, code, source)

        return True
