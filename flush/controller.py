"""通用节流调度器 — FlushController.

与 openclaw-lark 对齐：
- 异常后正确设置 last_update_time
- wait_for_flush 支持
- card_message_ready gate
- 线程安全：schedule_update 可从 worker 线程调用
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

_logger = logging.getLogger("hermes_lark_streaming")


CARDKIT_MS = 0.080  # CardKit 流式 API 的刷新间隔（80ms：≥官方默认 print_frequency_ms 70ms，留余量给 API 往返）
PATCH_MS = 1.500  # IM patch 降级通道的刷新间隔
LONG_GAP_MS = 1.000  # 超过此间隔 → 认为是长时间空闲
BATCH_AFTER_GAP_MS = 0.100  # 长时间空闲后等待这个时间再 flush


class FlushController:
    """带互斥锁 + 延迟刷新的通用节流调度器.

    不包含飞书业务逻辑，只负责决定何时执行回调.

    **线程安全**: ``schedule_update`` 可从任意线程（worker thread 或
    event loop thread）安全调用。内部通过 ``call_soon_threadsafe``
    确保节流逻辑和回调调度始终在事件循环线程上执行，避免
    ``call_soon``/``call_later`` 跨线程调用无法唤醒事件循环的问题。
    """

    def __init__(self, throttle_ms: float = CARDKIT_MS) -> None:
        self._throttle_ms = throttle_ms
        self._flush_in_progress = False
        self._needs_reflush = False
        self._pending_timer: asyncio.TimerHandle | None = None
        self._last_update_time = 0.0
        self._completed = False
        self._card_message_ready = False
        self._flush_resolvers: list[asyncio.Future[None]] = []
        # v1.3.4 fix (P2): 持有 flush Task 强引用，防止 GC 在任务完成前回收
        # （Python 文档："Save a reference to the result of this function"）
        self._pending_flush_tasks: set[asyncio.Task[None]] = set()
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                self._loop = asyncio.get_event_loop()
            except RuntimeError:
                self._loop = None  # Will be lazily resolved via _get_loop()

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        """Lazily resolve the event loop if not set during __init__.

        In Python 3.10+ (especially 3.11.15), both get_running_loop()
        and get_event_loop() may raise RuntimeError when no event
        loop exists — e.g. during unit tests or synchronous construction.
        This method defers loop resolution until the loop is actually needed.
        """
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        return self._loop

    @property
    def throttle_ms(self) -> float:
        return self._throttle_ms

    @throttle_ms.setter
    def throttle_ms(self, value: float) -> None:
        self._throttle_ms = value

    @property
    def last_update_time(self) -> float:
        return self._last_update_time

    def schedule_update(self, do_flush: Callable[[], Awaitable[None]]) -> None:
        """请求一次节流后的卡片刷新.

        do_flush: async callable，执行实际 API 调用.

        **线程安全**: 可从 worker thread 或 event loop thread 调用。
        使用 ``call_soon_threadsafe`` 确保事件循环被唤醒。
        """
        if self._completed or not self._card_message_ready:
            return
        # ── 线程安全：通过 call_soon_threadsafe 调度到事件循环线程 ──
        # 直接使用 call_soon / call_later 在 worker thread 中调用时，
        # 回调虽然加入了 _ready 队列，但不会触发 _write_to_self() 唤醒
        # 事件循环，导致回调永远不会被及时处理——直到有其他事件
        # （如定时器、I/O）自然唤醒循环。
        # 这正是 "跑马灯无文字，等很久才出文字" 的根因。
        try:
            self._get_loop().call_soon_threadsafe(self._schedule_update_on_loop, do_flush)
        except RuntimeError:
            pass  # Event loop already closed

    def _schedule_update_on_loop(self, do_flush: Callable[[], Awaitable[None]]) -> None:
        """在事件循环线程上执行节流逻辑.

        由 ``schedule_update`` 通过 ``call_soon_threadsafe`` 调度，
        保证 ``call_later`` / ``call_soon`` 均在事件循环线程上执行。
        """
        if self._completed or not self._card_message_ready:
            return
        now = time.monotonic()
        elapsed = now - self._last_update_time

        if elapsed >= self._throttle_ms:
            # 超出节流窗口
            if elapsed > LONG_GAP_MS:
                # 长时间空闲 → 延迟一小批让内容更完整
                if self._pending_timer is None:
                    self._schedule(delay=BATCH_AFTER_GAP_MS, do_flush=do_flush)
            else:
                # 立即 flush
                self._do_flush_task(do_flush)
        else:
            # 仍在节流窗口内 → 延迟到窗口边界
            if self._pending_timer is None:
                delay = self._throttle_ms - elapsed
                self._schedule(delay=delay, do_flush=do_flush)

    async def flush_now(self, do_flush: Callable[[], Awaitable[None]]) -> None:
        """立即执行一次 flush，等待完成."""
        if self._completed or not self._card_message_ready:
            return
        self._cancel_timer()
        await self._do_flush(do_flush)

    async def wait_for_flush(self) -> None:
        """等待进行中的 flush 完成."""
        if not self._flush_in_progress:
            return
        future: asyncio.Future[None] = self._get_loop().create_future()
        self._flush_resolvers.append(future)
        await future

    def mark_completed(self) -> None:
        """标记完成，不再接受新更新."""
        self._completed = True
        self._cancel_timer()
        for r in self._flush_resolvers:
            if not r.done():
                r.set_result(None)
        self._flush_resolvers.clear()

    def set_throttle(self, ms: float) -> None:
        self._throttle_ms = ms

    def set_card_message_ready(self, ready: bool) -> None:
        """设置卡片消息已就绪，初始化时间戳."""
        self._card_message_ready = ready
        if ready:
            self._last_update_time = time.monotonic()

    def _schedule(self, delay: float, do_flush: Callable[[], Awaitable[None]]) -> None:
        self._cancel_timer()
        self._pending_timer = self._get_loop().call_later(
            delay,
            self._do_flush_task,
            do_flush,
        )

    def _do_flush_task(self, do_flush: Callable[[], Awaitable[None]]) -> None:
        self._pending_timer = None
        # v1.3.4 fix (P2): 持有 Task 强引用防止 GC 回收
        self._get_loop().call_soon(self._create_flush_task, do_flush)

    def _create_flush_task(self, do_flush: Callable[[], Awaitable[None]]) -> None:
        task = self._get_loop().create_task(self._do_flush(do_flush))
        self._pending_flush_tasks.add(task)
        task.add_done_callback(self._pending_flush_tasks.discard)

    async def _do_flush(self, do_flush: Callable[[], Awaitable[None]]) -> None:
        if self._completed or self._flush_in_progress:
            self._needs_reflush = True
            return

        self._flush_in_progress = True
        self._needs_reflush = False
        try:
            await do_flush()
        except Exception:
            _logger.debug("flush error suppressed", exc_info=True)
        finally:
            self._flush_in_progress = False
            self._last_update_time = time.monotonic()
            # 唤醒等待者
            resolvers = self._flush_resolvers
            self._flush_resolvers = []
            for r in resolvers:
                if not r.done():
                    r.set_result(None)

        # 如果 flush 期间又有新数据 → 立即重刷
        if self._needs_reflush and not self._completed:
            self._needs_reflush = False
            # v1.3.4 fix (P2): 持有 Task 强引用防止 GC 回收
            self._get_loop().call_soon(self._create_flush_task, do_flush)

    def _cancel_timer(self) -> None:
        if self._pending_timer is not None:
            self._pending_timer.cancel()
            self._pending_timer = None
