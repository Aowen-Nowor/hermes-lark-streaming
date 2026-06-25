"""StreamCardController — 流式卡片主控制器（单例）.

与 openclaw-lark 对齐：
- UnavailableGuard 消息不可用保护
- 修复的 FlushController（wait_for_flush, card_message_ready）
- TextState 回复边界检测 + reasoning 处理
- 工具状态预回答更新
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Coroutine
from concurrent.futures import Future as ConcurrentFuture
from typing import TYPE_CHECKING, Any

from ..config import Config
from .linear_mixin import UnifiedControllerMixin
from .mixin import (
    ABORTED,
    COMPLETED,
    COMPLETING,
    CREATION_FAILED,
    TERMINATED,
    ControllerMixin,
)
from ..feishu import (
    FeishuClient,
    FeishuClientConfig,
)
from ..state.text import TextState, strip_reasoning_tags
from ..state.tooluse import ToolUseTracker

_logger = logging.getLogger("hermes_lark_streaming")


# v1.3.2: module-level constant (was previously re-defined on every on_interrupted call)
_INTERRUPT_MAP_MAX = 200

from ..state.session import CardSession  # noqa: F401 — re-exported for backward compatibility


class StreamCardController(ControllerMixin, UnifiedControllerMixin):
    """流式卡片控制器 — 管理多条消息的卡片生命周期."""

    def __init__(self) -> None:
        self._cfg = Config()
        self._client: FeishuClient | None = None
        self._sessions: dict[str, CardSession] = {}
        # v1.3.0 P1-01: _sessions is shared between the event-loop thread
        # (on_message_started / on_completed / prune) and worker threads
        # (callback wrappers in patching/callbacks.py → hooks → controller)
        # and the Feishu webhook thread (clarify card action → get_controller).
        # RLock allows re-entrancy (on_message_started calls on_interrupted
        # which also accesses _sessions).
        self._sessions_lock = threading.RLock()
        self._interrupt_map: dict[str, str] = {}
        # v1.3.0: _interrupt_map is accessed from event-loop thread (on_interrupted
        # writes, on_completed pops) and worker threads (_cleanup iterates+deletes).
        self._interrupt_map_lock = threading.Lock()
        self._initialized = False
        self._init_lock = asyncio.Lock()
        self._session_ttl = self._cfg.card_duration_sec
        self._loop: asyncio.AbstractEventLoop | None = None
        # v1.3.2 fix: hold strong references to fire-and-forget tasks to prevent
        # GC from collecting them mid-execution (asyncio only holds weak refs).
        self._pending_tasks: set[asyncio.Task] = set()

    # ── v1.3.0 P1-01: thread-safe _sessions access helpers ──
    # All external and internal access to self._sessions should go through
    # these helpers to guarantee the RLock is held.  Direct dict access is
    # discouraged but kept for backward-compat in a few hot-path reads that
    # are inherently single-threaded (event-loop only).
    def _sess_get(self, message_id: str) -> CardSession | None:
        """Thread-safe session lookup by message_id (or anchor_id)."""
        with self._sessions_lock:
            return self._sessions.get(message_id)

    def _sess_put(self, key: str, session: CardSession) -> None:
        """Thread-safe session store."""
        with self._sessions_lock:
            self._sessions[key] = session

    def _sess_pop(self, key: str) -> CardSession | None:
        """Thread-safe session removal (returns the removed session or None)."""
        with self._sessions_lock:
            return self._sessions.pop(key, None)

    def _sess_items_snapshot(self) -> list[tuple[str, CardSession]]:
        """Thread-safe snapshot of all (key, session) pairs.

        Returns a list copy so callers can iterate without holding the lock
        (prevents RuntimeError: dictionary changed size during iteration).
        """
        with self._sessions_lock:
            return list(self._sessions.items())

    def _sess_values_snapshot(self) -> list[CardSession]:
        """Thread-safe snapshot of all sessions (values only)."""
        with self._sessions_lock:
            return list(self._sessions.values())

    def _sess_active_count(self) -> int:
        """Thread-safe count of non-terminal (active) sessions."""
        with self._sessions_lock:
            return sum(1 for s in self._sessions.values() if not s.is_terminal_phase)

    def _sess_clear(self) -> None:
        """Thread-safe clear of all sessions (used by unregister)."""
        with self._sessions_lock:
            self._sessions.clear()

    @property
    def enabled(self) -> bool:
        return self._cfg.enabled and bool(self._cfg.feishu_app_id or self._cfg.env_app_id)

    async def _ensure_init(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            app_id = self._cfg.feishu_app_id or self._cfg.env_app_id
            app_secret = self._cfg.feishu_app_secret or self._cfg.env_app_secret
            if not app_id or not app_secret:
                _logger.error(
                    "FeishuClient init failed: credentials not configured "
                    "(app_id=%s, env_app_id=%s)",
                    bool(app_id),
                    bool(self._cfg.env_app_id),
                )
                raise RuntimeError("feishu credentials not configured")
            self._client = FeishuClient(
                FeishuClientConfig(
                    app_id=app_id,
                    app_secret=app_secret,
                    base_url=self._cfg.feishu_base_url,
                )
            )
            self._initialized = True
            _logger.info(
                "FeishuClient initialized: app_id=%s base_url=%s",
                app_id[:8] + "..." if len(app_id) > 8 else app_id,
                self._cfg.feishu_base_url,
            )

    def _client_ok(self) -> bool:
        return self._initialized and self._client is not None

    def _get_loop(self) -> asyncio.AbstractEventLoop | None:
        """获取事件循环，缓存以便跨线程复用."""
        try:
            loop = asyncio.get_running_loop()
            self._loop = loop
            return loop
        except RuntimeError:
            pass
        if self._loop is not None and not self._loop.is_closed():
            return self._loop
        try:
            loop = asyncio.get_event_loop()
            self._loop = loop
            return loop
        except RuntimeError:
            return None

    def _get_active_session(self, message_id: str) -> CardSession | None:
        """获取非终态的活跃 session，不存在或已终态返回 None."""
        session = self._sess_get(message_id)
        if session is None or session.is_terminal_phase:
            return None
        return session

    def _fire_and_forget(self, coro: Coroutine[Any, Any, Any], loop: asyncio.AbstractEventLoop) -> None:
        """Schedule a coroutine for background execution without awaiting.

        v1.3.2 fix: hold strong reference to the created Task to prevent GC
        from collecting it mid-execution. Also close the coroutine if
        scheduling fails to avoid 'coroutine was never awaited' warnings.
        """
        try:
            task = loop.create_task(coro)
            # Hold strong reference until task completes
            self._pending_tasks.add(task)
            task.add_done_callback(self._pending_tasks.discard)
        except RuntimeError:
            # Loop might be closed — try run_coroutine_threadsafe as fallback
            try:
                fut = asyncio.run_coroutine_threadsafe(coro, loop)
                fut.add_done_callback(self._on_bg_task_done)
            except Exception:
                # v1.3.2 fix: close the coroutine to avoid 'never awaited' warning
                coro.close()
                _logger.debug("fire_and_forget failed", exc_info=True)

    def on_message_started(
        self,
        *,
        message_id: str | None,
        chat_id: str,
        anchor_id: str | None = None,
    ) -> None:
        """消息处理开始 — 创建会话 + 发占位卡片.

        v1.1.0 (Task 2.7): Concurrency limiting — when a new message arrives
        in the same chat_id while an old card is still active (streaming/creating),
        the old card is immediately sealed as "interrupted by new message"
        before the new session is created. This prevents two active cards
        in the same chat competing for API calls.
        """
        if not self.enabled:
            return
        if not message_id:
            _logger.warning("HLS: on_message_started missing message_id chat=%s", chat_id[:12])
            return
        if self._sess_get(message_id) is not None:
            return

        self._prune_stale_sessions()

        # ── v1.1.0 Concurrency limiting (Task 2.7) ──
        # Seal any active (non-terminal) session in the same chat_id
        # before creating the new one. This prevents resource contention
        # and ensures the user only sees one active card at a time.
        # v1.3.0: use thread-safe snapshot to avoid RuntimeError on concurrent modification.
        # v1.3.6 fix: 用 seen set 跟踪已处理的 session 对象，防止同一 session
        # 被 anchor_id key 和 message_id key 重复处理。原实现 on_interrupted
        # 创建新 session 时 _sess_put(anchor_id, new_session) 覆盖了
        # _sessions[anchor_id]，导致循环再次遇到 anchor_id key 时把刚创建的
        # 新 session 当作 old_session abort 掉（真飞书模式 E2E 复现）。
        seen_sessions: set[int] = set()
        for existing_msg_id, existing_session in self._sess_items_snapshot():
            if existing_session.chat_id != chat_id:
                continue
            if existing_session.is_terminal_phase:
                continue
            if existing_msg_id == message_id:
                continue
            if id(existing_session) in seen_sessions:
                continue
            seen_sessions.add(id(existing_session))
            _logger.info(
                "HLS: concurrency limit — sealing old active card "
                "msg=%s trace=%s chat=%s (new msg=%s arriving)",
                existing_msg_id[:12],
                existing_session.card_trace_id,
                chat_id[:12],
                message_id[:12],
            )
            # Fire interrupt to seal the old card
            try:
                self.on_interrupted(
                    old_message_id=existing_msg_id,
                    new_message_id=message_id,
                    chat_id=chat_id,
                    anchor_id=anchor_id,
                )
            except Exception:
                _logger.warning("HLS: concurrency seal failed", exc_info=True)

        loop = self._get_loop()
        if loop is None:
            _logger.warning("HLS: no event loop, skipping msg=%s", (message_id or "?")[:12])
            return

        # v1.3.4 fix (P0): concurrency seal 可能已通过 on_interrupted 创建了
        # 当前 message_id 的 session（并已触发 _do_create_linear_card）。
        # 如果直接再创建会覆盖原 session，导致：
        #   1. 两张卡片被创建（on_interrupted 一张 + 这里一张）
        #   2. on_interrupted 创建的那张卡片成为孤儿（永远停在"正在加载上下文..."）
        # 修复：如果 session 已存在（由 on_interrupted 创建），直接复用，仅补记 metrics。
        # v1.3.5 fix: on_interrupted 中 fire-and-forget 的 _do_create_linear_card
        # 可能因旧 session 的 _wait_and_abort + _complete_session 级联任务链而延迟执行，
        # 导致 _card_ready 永远等不到。在此兜底重试调度，_do_create_linear_card
        # 内部有 state != IDLE 守卫，已运行的调用不会被重复执行。
        existing = self._sess_get(message_id)
        if existing is not None:
            _logger.info(
                "HLS: session already created by concurrency seal, reusing msg=%s trace=%s",
                (message_id or "?")[:12], existing.card_trace_id,
            )
            if not existing._card_ready.is_set():
                self._fire_and_forget(self._do_create_linear_card(existing), loop)
            try:
                from ..aowen import record_card_created, set_active_sessions
                record_card_created()
                set_active_sessions(self._sess_active_count())
            except Exception:
                _logger.debug('metrics: record_card_created failed (reuse path)', exc_info=True)
            return

        session = CardSession(message_id, chat_id, loop)
        self._sess_put(message_id, session)
        if anchor_id and anchor_id != message_id:
            session.anchor_id = anchor_id
            self._sess_put(anchor_id, session)
        _logger.info("HLS: session created msg=%s trace=%s chat=%s anchor=%s", (message_id or "?")[:12], session.card_trace_id, chat_id[:12], (anchor_id or "")[:12])

        # v1.1.0: Record metrics
        try:
            from ..aowen import record_card_created, set_active_sessions
            record_card_created()
            set_active_sessions(self._sess_active_count())
        except Exception:
            _logger.debug('metrics: record_card_created failed', exc_info=True)

        # v1.1.0 (Task 1.1+1.2): The non-linear _do_create_card path was
        # removed — linear is the only creation path now. When CardKit v2
        # creation fails, _do_create_linear_card falls back directly to
        # build_im_fallback_card (NOT to the legacy segmented v1 cards).
        self._fire_and_forget(self._do_create_linear_card(session), loop)

    def on_thinking(self, *, message_id: str, text: str) -> None:
        """思考内容增量."""
        if not self.enabled:
            return
        session = self._get_active_session(message_id)
        if session is None or session.guard.should_skip("on_thinking"):
            return

        self._linear_on_thinking(session, text)

    def on_reasoning(self, *, message_id: str, text: str) -> None:
        """Native model reasoning delta (incremental append)."""
        if not self.enabled:
            return
        if not self._cfg.show_reasoning:
            return
        session = self._get_active_session(message_id)
        if session is None or session.guard.should_skip("on_reasoning"):
            return

        # Epoch guard: if session entered terminal phase between lookup and
        # here (concurrent message race), skip to prevent stale writes.
        epoch = session.create_epoch
        if session.is_stale_create(epoch):
            _logger.debug("on_reasoning: stale epoch, skipping msg=%s", (message_id or "?")[:12])
            return

        # v1.1.0 (Task 1.1+1.2): linear is the only path — session.linear
        # and session.unified_state are always set after _do_create_linear_card.
        _logger.debug(
            "HLS: on_reasoning msg=%s text=%r current_reasoning_len=%d",
            (message_id or "?")[:12],
            text[:50] if text else "",
            len(session.unified_state._current_reasoning) if session.unified_state else 0,
        )
        # v1.1.1: 真飞书模式下卡片创建可能降级（unified_state=None），加保护
        if session.unified_state is None:
            _logger.warning("HLS: on_thinking but unified_state is None, skipping msg=%s", (message_id or "?")[:12])
            return
        session.unified_state.on_reasoning_delta(text)
        # v1.1.0 (Task 1.3): The _native_reasoning_active flag was
        # removed.  _linear_on_thinking now uses
        # ``len(state._current_reasoning) > 0`` as the dedup guard,
        # which is updated automatically by on_reasoning_delta above.
        _logger.debug(
            "HLS: on_reasoning delta applied msg=%s current_reasoning_len=%d",
            (message_id or "?")[:12],
            len(session.unified_state._current_reasoning),
        )
        self._schedule_linear_flush(session)

    def on_tool_update(
        self,
        *,
        message_id: str,
        tool_name: str,
        status: str,
        detail: str = "",
    ) -> None:
        """工具调用事件."""
        if not self.enabled:
            return
        session = self._get_active_session(message_id)
        if session is None or session.guard.should_skip("on_tool_update"):
            return

        # Epoch guard: prevent stale writes from previous message's callbacks
        epoch = session.create_epoch
        if session.is_stale_create(epoch):
            _logger.debug("on_tool_update: stale epoch, skipping msg=%s", (message_id or "?")[:12])
            return

        if status in ("running", "started", "tool.started"):
            session.tool_use.record_start(tool_name, detail)
        else:
            is_error = status in ("error", "failed")
            session.tool_use.record_end(
                tool_name,
                error=detail if is_error else "",
                output="" if is_error else detail,
            )

        # v1.1.0 (Task 1.1+1.2): linear is the only path — session.linear
        # and session.unified_state are always set after _do_create_linear_card.
        # v1.1.1: 真飞书模式下卡片创建可能降级（unified_state=None），加保护
        if session.unified_state is None:
            _logger.warning("HLS: on_tool_update but unified_state is None, skipping msg=%s", (message_id or "?")[:12])
            return
        is_new_tool = status in ("running", "started", "tool.started")
        session.unified_state.on_tool_event(is_new_tool=is_new_tool)
        self._schedule_linear_flush(session)

    def on_answer(self, *, message_id: str, text: str) -> None:
        """答案文本增量（流式）."""
        if not self.enabled:
            return
        session = self._get_active_session(message_id)
        if session is None or session.guard.should_skip("on_answer"):
            return

        # Epoch guard: prevent stale writes from previous message's callbacks
        epoch = session.create_epoch
        if session.is_stale_create(epoch):
            _logger.debug("on_answer: stale epoch, skipping msg=%s", (message_id or "?")[:12])
            return

        # ── TTFB: 首字到达时间 ──
        if session._first_answer_time == 0.0:
            session._first_answer_time = time.monotonic()
            _logger.debug(
                "perf: first_answer msg=%s ttfb=%.0fms",
                (message_id or "?")[:12],
                (session._first_answer_time - session.created_at) * 1000,
            )

        # v1.1.0 (Task 1.1+1.2): linear is the only path — session.linear
        # and session.unified_state are always set after _do_create_linear_card.
        # v1.1.1: 真飞书模式下卡片创建可能降级（unified_state=None），加保护
        answer_text = strip_reasoning_tags(text)
        if answer_text:
            if session.unified_state is None:
                _logger.warning("HLS: on_answer but unified_state is None, skipping msg=%s", (message_id or "?")[:12])
                return
            session.unified_state.on_answer_delta(answer_text)
            self._schedule_linear_flush(session)

    def on_aborted(self, *, message_id: str) -> None:
        """用户 /stop 导致消息被中断.

        COMPLETING 短路：如果 session 已在 COMPLETING 状态（on_completed
        已触发，正在 drain 收尾），跳过 abort 逻辑，让 _do_linear_complete
        自然走完。仅标记 _was_aborted 让封卡时显示"已停止"状态。
        """
        if not self.enabled:
            return
        session = self._get_active_session(message_id)
        if session is None:
            return

        # ── Hotfix: skip abort if session is in COMPLETING state ──
        # Same race condition as on_interrupted: if the session is already
        # in COMPLETING (on_completed has fired, drain is in progress),
        # let _do_linear_complete finish naturally. Setting ABORTED here
        # would cancel the flush mid-drain, dropping the last answer chunk,
        # and cause a double-complete race.
        if session.state == COMPLETING:
            _logger.info(
                "on_aborted: skip abort for msg=%s (session in COMPLETING, "
                "let _do_linear_complete finish naturally)",
                (message_id or "?")[:12],
            )
            # Mark _was_aborted so the seal shows "stopped" state
            session._was_aborted = True
            return

        session._was_aborted = True
        session.state = ABORTED
        session.flush.mark_completed()
        _logger.info("on_aborted: msg=%s state=ABORTED", (message_id or "?")[:12])

        # v1.1.0: Record metrics
        try:
            from ..aowen import record_card_aborted
            record_card_aborted()
        except Exception:
            _logger.debug('metrics: record_card_aborted failed', exc_info=True)

        self._complete_session(session)

    def on_interrupted(
        self,
        *,
        old_message_id: str,
        new_message_id: str,
        chat_id: str,
        anchor_id: str | None = None,
    ) -> None:
        """用户发送新消息导致前一条消息被中断 — abort A + create B.

        竞态保护：如果旧 session 正在 _do_linear_flush/_do_linear_split
        中（flush_in_progress=True），先异步等待当前 flush 完成（带超时），
        再标记 ABORTED 并封卡，避免并发操作 session.card_id 导致
        旧卡被封两次或新卡变成孤儿。

        COMPLETING 短路：如果旧 session 已在 COMPLETING 状态（on_completed
        已触发，正在 drain 收尾），跳过 abort 逻辑，让 _do_linear_complete
        自然走完。新 session 创建和 _interrupt_map 更新照常执行。
        """
        if not self.enabled:
            return

        old_session = self._get_active_session(old_message_id)
        if old_session is not None:
            # ── Hotfix: skip abort if session is in COMPLETING state ──
            # COMPLETING 是 on_completed 触发的"正在收尾"中间态，再过几百毫秒
            # 就会自然到 COMPLETED。在这个窗口里收到新消息的 on_interrupted
            # 不应该把卡片覆盖成 ABORTED — 那会触发 fallback 路径重发短文本
            # "已停止"提示，破坏用户体验。只跳过 abort，继续创建新 session
            # 和更新 _interrupt_map（这些必须在任何情况下都执行）。
            if old_session.state == COMPLETING:
                _logger.info(
                    "on_interrupted: skip abort for msg=%s (session in COMPLETING, "
                    "let _do_linear_complete finish naturally)",
                    old_message_id[:12],
                )
            else:
                old_session._was_aborted = True
                old_session.error_message = "Interrupted by new message"

                # ── 竞态保护：等待当前 flush 完成 ──
                # 如果 session 正在 _do_linear_split 中（已封旧卡、正在创建新卡），
                # 需要等 split 完成后再标记 ABORTED，否则并发操作 session.card_id
                # 可能导致：旧卡被封两次 / 新卡变成孤儿 / sequence conflict。
                if old_session.flush._flush_in_progress:
                    loop = self._get_loop()
                    if loop is not None:
                        async def _wait_and_abort():
                            try:
                                await asyncio.wait_for(
                                    old_session.flush.wait_for_flush(),
                                    timeout=3.0,
                                )
                            except (asyncio.TimeoutError, Exception):
                                _logger.debug(
                                    "on_interrupted: flush wait timed out, proceeding with abort: msg=%s",
                                    old_message_id[:12],
                                )
                            # v1.3.2 fix (B3-01): re-check COMPLETING after the
                            # await — the session may have transitioned to COMPLETING
                            # during the wait. If so, skip the abort and let
                            # _do_linear_complete finish naturally (same logic as
                            # the synchronous path above).
                            if old_session.state == COMPLETING:
                                _logger.info(
                                    "on_interrupted: skip abort for msg=%s (session transitioned to COMPLETING during flush wait)",
                                    old_message_id[:12],
                                )
                                return
                            old_session.state = ABORTED
                            old_session.flush.mark_completed()
                            _logger.info(
                                "on_interrupted: abort old msg=%s (after flush wait)",
                                old_message_id[:12],
                            )
                            self._complete_session(old_session)
                        self._fire_and_forget(_wait_and_abort(), loop)
                    else:
                        # No loop — immediate abort (best effort)
                        old_session.state = ABORTED
                        old_session.flush.mark_completed()
                        _logger.info(
                            "on_interrupted: abort old msg=%s (no loop, immediate)",
                            old_message_id[:12],
                        )
                        self._complete_session(old_session)
                else:
                    # No flush in progress — immediate abort
                    old_session.state = ABORTED
                    old_session.flush.mark_completed()
                    _logger.info(
                        "on_interrupted: abort old msg=%s",
                        old_message_id[:12],
                    )
                    self._complete_session(old_session)

        if self._sess_get(new_message_id) is None:
            loop = self._get_loop()
            if loop is not None:
                reply_anchor_id = anchor_id if anchor_id and anchor_id != new_message_id else None
                session = CardSession(new_message_id, chat_id, loop)
                session.anchor_id = reply_anchor_id
                self._sess_put(new_message_id, session)
                if reply_anchor_id:
                    self._sess_put(reply_anchor_id, session)
                _logger.info(
                    "on_interrupted: create new msg=%s chat=%s anchor=%s",
                    new_message_id[:12],
                    chat_id[:12],
                    (reply_anchor_id or new_message_id)[:12],
                )
                # v1.1.0 (Task 1.1+1.2): linear is the only creation path now.
                self._fire_and_forget(self._do_create_linear_card(session), loop)

        # v1.3.0: protect _interrupt_map with its own lock (separate from
        # _sessions_lock to avoid holding both locks simultaneously → deadlock risk)
        with self._interrupt_map_lock:
            self._interrupt_map[old_message_id] = new_message_id
            for key, val in list(self._interrupt_map.items()):
                if val == old_message_id:
                    self._interrupt_map[key] = new_message_id
            # Prevent unbounded growth: keep only the most recent entries
            if len(self._interrupt_map) > _INTERRUPT_MAP_MAX:
                # Remove oldest entries (first inserted)
                excess = len(self._interrupt_map) - _INTERRUPT_MAP_MAX
                for old_key in list(self._interrupt_map.keys())[:excess]:
                    self._interrupt_map.pop(old_key, None)

    def on_completed(
        self,
        *,
        message_id: str | None,
        answer: str = "",
        duration: float = 0.0,
        model: str = "",
        tokens: dict | None = None,
        context: dict | None = None,
        api_calls: int = 0,
        history_offset: int = 0,
        compression_exhausted: bool = False,
        aborted: bool = False,
        error_message: str = "",
        reasoning_tokens: int = 0,
        estimated_cost_usd: float = 0.0,
        cost_status: str = "unknown",
    ) -> bool:
        """消息处理完成 — 构建终端卡片.

        状态机守卫：hermes 可能双调 on_completed（_process_message_background
        的 finally + pop_post_delivery_callback），竞态窗口内两次调用会触发
        300317 sequence 冲突。通过 COMPLETING 状态在 await 之前同步转移，
        防止双调竞态；300317 错误在 complete 方法中视为幂等成功。
        """
        if not self.enabled:
            return False

        # ── message_id 空值守卫 ──
        # 部分飞书事件（如系统消息、reaction 等）可能不携带 message_id，
        # 导致 message_id=None，后续 message_id[:12] 会触发 TypeError。
        if not message_id:
            _logger.warning("on_completed: missing message_id, skipping")
            return False

        # ── 状态机幂等守卫 ──
        # 先做直接查找（绕过 _TERMINAL 过滤），检查是否已在完成中/已完成。
        # COMPLETING: 完成流程已启动，另一条路径的 on_completed 正在执行
        # COMPLETED: 完成流程已结束
        direct_session = self._sess_get(message_id)
        if direct_session is not None and direct_session.state in (COMPLETING, COMPLETED):
            _logger.info(
                "on_completed: idempotent, msg=%s state=%s",
                (message_id or "?")[:12],
                direct_session.state,
            )
            return True

        session = self._get_active_session(message_id)
        if session is None:
            with self._interrupt_map_lock:
                redirected_id = self._interrupt_map.pop(message_id, None)
            if redirected_id is not None:
                # 也检查重定向的 session 是否已在完成中
                redir_session = self._sess_get(redirected_id)
                if redir_session is not None and redir_session.state in (COMPLETING, COMPLETED):
                    _logger.info(
                        "on_completed: idempotent (redirected), msg=%s -> %s state=%s",
                        (message_id or "?")[:12],
                        redirected_id[:12],
                        redir_session.state,
                    )
                    return True
                session = self._get_active_session(redirected_id)
                _logger.info(
                    "on_completed: redirect msg=%s -> msg=%s",
                    (message_id or "?")[:12],
                    redirected_id[:12],
                )
            if session is None:
                return False
            message_id = redirected_id or message_id

        # 卡片创建失败 → 交回 gateway 正常回复
        if session.state in (CREATION_FAILED, TERMINATED):
            _logger.info("on_completed: msg=%s state=%s, yielding to gateway", (message_id or "?")[:12], session.state)
            self._cleanup(message_id)
            return False

        # v1.3.0 P1-06: normal-path completion log downgraded to DEBUG (fires
        # once per session on every successful completion — log noise reduction).
        # The yield-to-gateway log above stays INFO (edge case, useful for debugging).
        _logger.debug(
            "on_completed: msg=%s has_card=%s state=%s use_cardkit=%s",
            (message_id or "?")[:12],
            bool(session.card_msg_id),
            session.state,
            session.use_cardkit,
        )

        if answer:
            session.text.on_deliver(answer)
            # ── Linear mode answer completeness check ──
            # The `answer` parameter from on_completed contains the full
            # response text. We compare it with unified_state.answer_text
            # (which was built incrementally from streaming callbacks) and
            # ensure the card shows the COMPLETE answer:
            #   1. If no answer was streamed -> use the full on_completed answer
            #   2. If the on_completed answer is LONGER than what was streamed
            #      -> append the missing portion (streaming may have missed content
            #      due to callback timing, missing stream_delta_callback, etc.)
            #   3. If the streamed answer is already complete -> no action needed
            if (
                session.linear
                and session.unified_state is not None
            ):
                from ..state.text import strip_reasoning_tags
                clean_answer = strip_reasoning_tags(answer)
                if clean_answer:
                    _existing = session.unified_state.answer_text
                    _existing_len = len(_existing)
                    _clean_len = len(clean_answer)
                    if _existing_len == 0:
                        # No answer was streamed — use the full on_completed answer
                        session.unified_state.on_answer_delta(clean_answer)
                        _logger.info(
                            "on_completed: linear answer fallback, len=%d msg=%s",
                            _clean_len, (message_id or "?")[:12],
                        )
                    elif _clean_len > _existing_len and clean_answer[:_existing_len] == _existing:
                        # on_completed answer extends the streamed answer — append diff
                        _diff = clean_answer[_existing_len:]
                        if _diff:
                            session.unified_state.on_answer_delta(_diff)
                            _logger.info(
                                "on_completed: linear answer extended, existing=%d added=%d msg=%s",
                                _existing_len, len(_diff), (message_id or "?")[:12],
                            )
                    elif _clean_len > _existing_len and clean_answer[:_existing_len] != _existing:
                        # on_completed answer is longer but doesn't start with streamed text
                        # This can happen when the model rewrites or when streaming captured
                        # only a prefix. Replace with the more complete version.
                        _logger.warning(
                            "on_completed: linear answer MISMATCH existing_len=%d clean_len=%d msg=%s "
                            "existing_head=%r clean_head=%r — replacing with on_completed answer",
                            _existing_len, _clean_len, (message_id or "?")[:12],
                            _existing[:60], clean_answer[:60],
                        )
                        session.unified_state.answer_text = clean_answer
                        session.unified_state.answer_dirty = True

        # ── 保存错误/中断消息 ──
        # 用于在卡片正文中展示（而非仅页脚）
        if error_message:
            session.error_message = error_message

        # ── 中断标记 ──
        # 当 monkey_patch 检测到 interrupted/partial 时传入 aborted=True，
        # 保存到 _was_aborted 以便完成方法在 COMPLETING 状态下仍能获取该标记。
        if aborted:
            session._was_aborted = True

        session.footer = {
            "duration": duration,
            "model": model,
            **({"input_tokens": tokens.get("input_tokens")} if tokens else {}),
            **({"output_tokens": tokens.get("output_tokens")} if tokens else {}),
            **({"cache_read_tokens": tokens.get("cache_read_tokens")} if tokens and tokens.get("cache_read_tokens") else {}),
            **({"cache_write_tokens": tokens.get("cache_write_tokens")} if tokens and tokens.get("cache_write_tokens") else {}),
            **({"context_used": context.get("used_tokens")} if context else {}),
            **({"context_max": context.get("max_tokens")} if context else {}),
            **({"api_calls": api_calls} if api_calls else {}),
            **({"history_offset": history_offset} if history_offset else {}),
            **({"compression_exhausted": compression_exhausted} if compression_exhausted else {}),
            **({"reasoning_tokens": reasoning_tokens} if reasoning_tokens else {}),
            **({"estimated_cost_usd": estimated_cost_usd} if estimated_cost_usd else {}),
            **({"cost_status": cost_status} if cost_status and cost_status != "unknown" else {}),
        }

        # ── 状态转移: → COMPLETING ──
        # 在 _complete_session 的 await 之前同步设置，防止 hermes 双调竞态。
        # COMPLETING 不在 _TERMINAL 中：on_answer/on_thinking 等回调在
        # COMPLETING 期间仍可更新 unified_state（确保迟到的内容不被丢弃），
        # 但 _schedule_linear_flush 会拒绝调度新 flush（drain 负责排空）。
        session.state = COMPLETING

        self._complete_session(session)
        return True

    async def on_cron_deliver_async(
        self,
        *,
        chat_id: str,
        content: str,
        loop: asyncio.AbstractEventLoop,
    ) -> bool:
        """Cron 推送 — 包装为静态卡片发送，成功返回 True.

        异步版本：直接 await 协程，避免 run_coroutine_threadsafe 在事件循环线程中死锁。
        """
        if not self.enabled or not content or not chat_id:
            return False
        try:
            await self._do_cron_deliver(chat_id, content)
            _logger.info("cron card delivered: chat=%s len=%d", chat_id[:12], len(content))
            return True
        except Exception:
            _logger.warning("cron card delivery failed", exc_info=True)
            return False

    def on_cron_deliver(
        self,
        *,
        chat_id: str,
        content: str,
        loop: asyncio.AbstractEventLoop,
    ) -> bool:
        """Cron 推送（同步兼容接口）— 从非事件循环线程调用时使用.

        如果在事件循环线程内调用此方法会导致死锁（最多阻塞 30 秒后超时），
        请改用 on_cron_deliver_async。
        """
        if not self.enabled or not content or not chat_id:
            return False
        future = asyncio.run_coroutine_threadsafe(
            self._do_cron_deliver(chat_id, content), loop
        )
        try:
            future.result(timeout=30)
            _logger.info("cron card delivered: chat=%s len=%d", chat_id[:12], len(content))
            return True
        except Exception:
            _logger.warning("cron card delivery failed", exc_info=True)
            return False

    def defer_background_review(
        self,
        *,
        message_id: str,
        text: str,
        sender: Callable[[str], Any],
    ) -> bool:
        """将后台审查消息推入卡片面板（如果在线性模式），否则暂存等卡片收尾后发送."""
        if not self.enabled or not text or not callable(sender):
            return False
        session = self._get_active_session(message_id)
        if session is None:
            return False

        # Try to push into linear state for real-time card display
        if session.linear and session.unified_state:
            session.unified_state.on_background_review(text)
            self._schedule_linear_flush(session)
            return True  # Consumed by card, suppress plain text

        # Non-linear mode: defer as before
        with session.deferred_background_review_lock:
            if session.deferred_background_review_closed:
                return False
            session.deferred_background_reviews.append((text, sender))
        return True

    def _flush_deferred_background_reviews(self, session: CardSession) -> None:
        lock = getattr(session, "deferred_background_review_lock", None)
        reviews = getattr(session, "deferred_background_reviews", None)
        if lock is None or reviews is None:
            return
        with lock:
            session.deferred_background_review_closed = True
            pending = list(reviews)
            reviews.clear()
        for text, sender in pending:
            try:
                sender(text)
            except Exception:
                _logger.debug("background review sender failed", exc_info=True)

    def _cleanup(self, message_id: str) -> None:
        session = self._sess_pop(message_id)
        if session is None:
            return
        anchor = getattr(session, "anchor_id", None)
        if anchor:
            # v1.3.0: atomically check-and-delete the anchor key if it still
            # points to the same session object (prevents deleting a new
            # session that reused the anchor key).
            with self._sessions_lock:
                if self._sessions.get(anchor) is session:
                    del self._sessions[anchor]
        with self._interrupt_map_lock:
            stale_keys = [k for k, v in self._interrupt_map.items() if v == message_id]
            for k in stale_keys:
                del self._interrupt_map[k]
        session.flush.mark_completed()

    def _release_session_data(self, session: CardSession) -> None:
        """完成后释放重数据，仅保留最小元数据供 TTL 追踪.

        在 complete 流程完成后调用，释放 segments、text、tool_use
        等占用的内存。session 仍保留 message_id、
        state、created_at 等元数据直到 _cleanup 清除。
        """
        session.unified_state = None
        if session.text is not None:
            session.text = TextState()  # type: ignore[assignment]
        session.tool_use = ToolUseTracker()  # type: ignore[assignment]
        session.footer = {}

    def _complete_session(self, session: CardSession) -> None:
        """根据 session 线性/非线性选择完成路径.

        v1.1.0 (Task 1.1+1.2): The non-linear ``_do_complete`` path was
        removed. Linear is now the only completion path.

        Note: We intentionally do NOT call session.flush.mark_completed() here.
        That call cancels any pending flush timer, which would drop the
        last chunk of answer text that hasn't been flushed yet.  Instead,
        the completion method (_do_linear_complete) handles
        mark_completed() itself after draining remaining dirty data.
        """
        if session.linear and session.unified_state:
            self._fire_and_forget(self._do_linear_complete_with_fallback(session), session._loop)
        else:
            # Safety net: a non-linear session should never reach here
            # after Task 1.1+1.2, but if it does, route to the linear
            # path so the card still completes (rather than deadlocking).
            _logger.warning(
                "_complete_session: non-linear session dispatched to linear "
                "completer (non-linear path removed in v1.1.0), msg=%s",
                (session.message_id or "?")[:12],
            )
            self._fire_and_forget(self._do_linear_complete_with_fallback(session), session._loop)

    async def _do_linear_complete_with_fallback(self, session: CardSession) -> None:
        """线性模式完成，卡片不可用时回退为文本回复.

        v1.3.1 fix: Save answer_text and error_message BEFORE calling
        _do_linear_complete, because _do_linear_complete calls
        _release_session_data on failure which clears session.text.
        Without this, _send_text_fallback would see an empty display_text.
        """
        # Snapshot fallback text before _do_linear_complete potentially releases it
        _fallback_text = ""
        if session.error_message:
            _fallback_text = session.error_message
        elif session.unified_state and session.unified_state.answer_text:
            _fallback_text = session.unified_state.answer_text
        elif session.text and session.text.display_text:
            _fallback_text = session.text.display_text

        try:
            result = await self._do_linear_complete(session)
            if not result:
                await self._send_text_fallback(session, fallback_text=_fallback_text)
        except Exception:
            _logger.warning(
                "linear complete with fallback failed: msg=%s",
                (session.message_id or "?")[:12],
                exc_info=True,
            )
            await self._send_text_fallback(session, fallback_text=_fallback_text)

    async def _send_text_fallback(self, session: CardSession, *, fallback_text: str = "") -> None:
        """卡片不可用时，通过飞书 API 发送文本回复作为兜底.

        当卡片创建失败或完成流程异常时，网关文本回复已被 card_sent=True 抑制。
        此方法确保用户至少能看到回复内容，避免"什么都看不到"的情况。

        v1.3.1 fix: Added fallback_text parameter. When _do_linear_complete
        fails and calls _release_session_data, session.text is cleared.
        The caller (_do_linear_complete_with_fallback) snapshots the text
        BEFORE the release and passes it here.
        """
        if not self._client:
            return
        try:
            # 优先使用调用方传入的 fallback_text（在 _release_session_data 前快照的）
            # 其次从 session 读取（用于 _do_linear_complete_with_fallback 以外的调用路径）
            text = fallback_text or session.error_message or (session.text.display_text if session.text else "") or ""
            if not text.strip():
                return
            # 限制长度避免过长
            if len(text) > 4000:
                text = text[:4000] + "..."
            from ..cardkit.md import optimize_markdown_style
            content = optimize_markdown_style(text) or text
            reply_id = session.anchor_id or session.message_id
            await self._client.reply_text(reply_id, content)
            _logger.info(
                "text fallback sent: msg=%s len=%d",
                (session.message_id or "?")[:12],
                len(content),
            )
        except Exception:
            _logger.debug(
                "text fallback failed: msg=%s",
                (session.message_id or "?")[:12],
                exc_info=True,
            )

    def _prune_stale_sessions(self) -> None:
        """v1.1.1: 只清理已终态的过期 session，保护活跃 session.

        之前不检查 state，STREAMING 状态的 session 超过 TTL 也会被清理，
        导致 AI 回调找不到 session、卡片永远卡在"流式中"。

        现在：
        - 已终态（COMPLETED/CREATION_FAILED/ABORTED/TERMINATED）+ 超 TTL → 清理
        - 活跃（STREAMING/COMPLETING/CREATING）+ 超 TTL → 只打日志，不清理
        """
        now = time.time()
        # v1.3.0 P1-05: show longer msg_id in prune logs for easier log correlation.
        # v1.3.0 P1-01: use thread-safe snapshot to avoid RuntimeError.
        for mid, s in self._sess_items_snapshot():
            if mid is None or now - s.created_at <= self._session_ttl:
                continue
            if s.is_terminal_phase:
                _logger.warning("pruning stale terminal session: msg=%s", (mid or "?")[:20])
                self._cleanup(mid)
            else:
                # 活跃 session 超 TTL 只打日志，不清理（避免 AI 回调丢失）
                _logger.warning(
                    "HLS: active session over TTL but not terminal, skip cleanup: msg=%s",
                    (mid or "?")[:20],
                )

    @staticmethod
    def _on_bg_task_done(fut: ConcurrentFuture) -> None:
        try:
            fut.result()
        except Exception:
            _logger.warning("background task failed", exc_info=True)


_controller: StreamCardController | None = None


def get_controller() -> StreamCardController:
    global _controller
    if _controller is None:
        _controller = StreamCardController()
    return _controller
