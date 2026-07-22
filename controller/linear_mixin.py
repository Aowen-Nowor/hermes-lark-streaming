"""Unified panel linear mode — create, flush, and seal a single-card streaming session."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time as _time
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

from ..cardkit import (
    ANSWER_ELEMENT_ID,
    UNIFIED_PANEL_ELEMENT_ID,
    _LOADING_ELEMENT_ID,
    _LOADING_HINT_ELEMENT_ID,
    _extract_images_from_markdown,
    _streaming_element,
    build_streaming_card_v2,
    build_unified_panel,
    build_preservative_seal_actions,
    _count_tag_objects,
    _enforce_card_element_limit,
)
from ..cardkit.md import _downgrade_tables, escape_markdown_asterisks, optimize_markdown_style
from ..state.linear import UnifiedLinearState
from ..state.text import split_reasoning_text
from ..feishu import (
    CARDKIT_SCHEMA_ERROR,
    CARDKIT_SEQUENCE_CONFLICT,
    CARDKIT_STREAMING_CLOSED,
    FeishuAPIError,
    is_element_not_found_error,
    is_schema_error,
    is_terminal_api_code,
)

from .mixin import (
    _TERMINAL,
    ABORTED,
    COMPLETED,
    COMPLETING,
    CREATION_FAILED,
    CREATING,
    IDLE,
    STREAMING,
    TERMINATED,
)
from ..state.phase import TerminalReason

if TYPE_CHECKING:
    from ..config import Config
    from ..state.session import CardSession
    from ..feishu import FeishuClient

_logger = logging.getLogger("hermes_lark_streaming")

def _build_seal_summary(state: UnifiedLinearState | None) -> str:
    """Build seal summary from state — answer text or fallback to reasoning."""
    if state is None:
        return ""
    summary_text = state.answer_text
    if not summary_text and state.reasoning_rounds:
        summary_text = state.reasoning_rounds[-1].text if state.reasoning_rounds else ""
    if summary_text:
        return summary_text[:120].replace("\n", " ").replace("```", "").strip()
    return ""

async def _fallback_write_answer(
    client: Any,
    card_id: str,
    content: str,
    *,
    sequence: int,
) -> bool:
    """Fallback: write answer content via batch_update + partial_update_element."""
    try:
        fallback_actions = [{
            "action": "partial_update_element",
            "params": {
                "element_id": ANSWER_ELEMENT_ID,
                "partial_element": {
                    "content": content,
                    # 不带 tag/text_align/text_size — 飞书 partial_update 禁止更新 tag
                },
            },
        }]
        await client.cardkit_batch_update(card_id, fallback_actions, sequence=sequence)
        return True
    except FeishuAPIError as e:
        _logger.warning("HLS: fallback write answer failed: %s", e)
        return False

async def _process_media_images(client: Any, text: str) -> str:
    """扫描 answer_text 中的 IMG:/path 引用，上传到飞书获取 img_key，
    替换为 ![alt](img_key) 供 _extract_images_from_markdown 处理。
    如果文件不存在或上传失败，保持原来的 IMG: 路径不变。"""
    if not client or not text:
        return text
    result = text
    for m in _IMG_PATH_RE.finditer(text):
        path = m.group(1)
        if not os.path.isfile(path):
            continue
        try:
            img_key = await client.upload_local_image(path)
            if img_key:
                result = result.replace(m.group(0), f'![media]({img_key})')
        except Exception:
            _logger.debug("HLS: _process_media_images upload failed for %s", path, exc_info=True)
    return result

# NOTE: This is the *server-side flush interval* (how often we send
_ANSWER_FAST_STREAM_MS = 0.150  # answer-only 节流间隔（150ms，v1.2.1 从 70ms 上调）
# MEDIA 路径匹配正则 - 用于 _process_media_images
# 注意：这里用 IMG: 而非 MEDIA:，因为 MEDIA: 会被 Hermes 平台层拦截发送为独立附件
# IMG: 只在插件内生效，不会被平台处理
_IMG_PATH_RE = re.compile(r'IMG:([^\s\n]+)')

class UnifiedControllerMixin:
    """Unified panel linear mode — phased card lifecycle."""

    # ── Instance attributes provided by StreamCardController ──
    _client: FeishuClient | None
    _cfg: Config
    _ensure_init: Callable[..., Coroutine[Any, Any, None]]
    _schedule_card_update: Callable[[CardSession], None]
    _cleanup: Callable[[str], None]
    _flush_deferred_background_reviews: Callable[[CardSession], None]

    async def _do_create_linear_card(self, session: CardSession) -> None:
        """Create the initial placeholder card — loading hint only, no panel."""
        if session.state != IDLE:
            return
        # Snapshot epoch before async creation
        epoch = session.create_epoch
        session.state = CREATING
        session._create_epoch_snap = epoch
        session.linear = True
        # v1.4.0 fix (问题3 根因1 — delegate_task 后卡片降级纯文本):
        if session.unified_state is None:
            session.unified_state = UnifiedLinearState()

        try:
            await self._ensure_init()
            assert self._client is not None

            try:
                reply_to = session.anchor_id or session.message_id
                card = build_streaming_card_v2(
                    include_unified_panel=False,   # Panel added on first token
                    include_answer_element=False,   # Answer element added with panel
                    include_loading_hint=True,      # "正在加载上下文..."
                    streaming_panel_expanded=self._cfg.streaming_panel_expanded,
                    print_strategy=self._cfg.print_strategy,
                    print_step=self._cfg.print_step,
                    card_width=self._cfg.card_width,
                )
                card_id = await self._client.cardkit_create(card)
                card_msg_id = await self._client.reply_card_by_id(reply_to, card_id)

                session.card_id = card_id
                session.card_msg_id = card_msg_id
                session.card_created_at = _time.time()
                session.flush.set_throttle(self._cfg.flush_interval_sec)

                # Track existing elements — only 2 are pre-allocated
                session.existing_elements = {
                    _LOADING_HINT_ELEMENT_ID,
                    _LOADING_ELEMENT_ID,
                }
                session._creation_stages.discard("panel")  # Panel NOT in initial card

            except FeishuAPIError as e:
                _logger.info("linear CardKit create failed: %s", e)
                raise

            session.flush.set_card_message_ready(True)

            # ── Stale-create guard ──
            if session.state == CREATING and not session.is_stale_create(epoch):
                session.state = STREAMING

            if session.linear and session.unified_state and (
                session.unified_state.has_dirty or session._pending_flush
            ):
                session._pending_flush = False
                if not session._first_flush_done:
                    # First content → immediate flush (首字即显)
                    session._first_flush_done = True
                    # v1.3.4 fix (P2): 持有 Task 强引用防止 GC 回收
                    # （与 core.py _fire_and_forget 同模式）
                    self._fire_and_forget(
                        session.flush.flush_now(lambda: self._do_unified_flush(session)),
                        asyncio.get_running_loop(),
                    )
                else:
                    # Subsequent content → throttled flush
                    self._schedule_linear_flush(session)

            # Must be set AFTER card_id/card_msg_id are assigned and
            session._card_ready.set()
            _logger.info(
                "HLS: linear card created msg=%s trace=%s linear=%s card_id=%s",
                (session.message_id or "?")[:12],
                session.card_trace_id,
                session.linear,
                (session.card_id or "")[:12],
            )
        except Exception as e:
            _logger.exception("_do_create_linear_card failed")
            # v1.3.4 fix (P1): 消息被删/撤回时触发 UnavailableGuard，避免后续
            # 对已删除消息的无效 API 调用。原实现 guard 从未被调用（死代码）。
            if isinstance(e, FeishuAPIError) and is_terminal_api_code(e.code):
                try:
                    session.guard.terminate("_do_create_linear_card", err=e)
                except Exception:
                    _logger.debug("guard.terminate failed in create path", exc_info=True)
            session.state = CREATION_FAILED
            session.enter_terminal(
                reason=TerminalReason.CREATION_FAILED,
                source="_do_create_linear_card",
            )
            # Signal readiness even on failure so awaiters don't deadlock
            session._card_ready.set()

    def _schedule_linear_flush(self, session: CardSession) -> None:
        """Schedule a unified panel flush for the given session."""
        if not session.should_proceed("_schedule_linear_flush"):
            return
        # COMPLETING is not terminal, but we should not schedule new flushes
        if session.state == IDLE or session.state == COMPLETING:
            return

        state = session.unified_state
        if state is None or not state.has_dirty:
            return

        if not session.flush._card_message_ready:
            session._pending_flush = True
            return

        # ── First-Token Immediate Flush (首字即显) ──
        if not session._first_flush_done:
            session._first_flush_done = True
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = session._loop
            if loop is not None and not loop.is_closed():
                # v1.3.4 fix (P2): 持有 Task 强引用防止 GC 回收
                self._fire_and_forget(
                    session.flush.flush_now(lambda: self._do_unified_flush(session)),
                    loop,
                )
            return

        _answer_only = state.answer_dirty and not state.panel_dirty and not state.tool_steps_dirty
        if _answer_only:
            session.flush.set_throttle(_ANSWER_FAST_STREAM_MS)
        else:
            session.flush.set_throttle(self._cfg.flush_interval_sec)

        session.flush.schedule_update(lambda: self._do_unified_flush(session))

    async def _do_unified_flush(self, session: CardSession) -> None:
        """Unified panel flush — max 2 API calls per flush cycle."""
        if session.is_terminal_phase or session.state == COMPLETING:
            return
        if not session.card_id:
            return
        state = session.unified_state
        if state is None:
            return
        assert self._client is not None

        actions: list[dict[str, Any]] = []

        # Bug fix (v1.0.5): Split Phase 2 into two sub-paths:
        # The answer element must be created in BOTH paths so the answer text can be
        if "answer" not in session._creation_stages and (state.panel_visible or state.answer_dirty or state.answer_text):
            new_elements: list[dict[str, Any]] = []

            # ── Path A: Has reasoning or tools → add unified panel ──
            if state.panel_visible:
                all_tool_steps = session.tool_use.build_display_steps()
                panel = build_unified_panel(
                    reasoning_rounds=state.reasoning_rounds,
                    current_reasoning_text=state.current_reasoning_text,
                    tool_steps=all_tool_steps,
                    tool_elapsed_ms=session.tool_use.elapsed_ms,
                    show_reasoning=self._cfg.show_reasoning,
                    expanded=self._cfg.streaming_panel_expanded,
                    panel_events=state.panel_events,
                    max_tool_steps=self._cfg.max_tool_steps,
                    max_reasoning_rounds=self._cfg.max_reasoning_rounds,
                )
                new_elements.append(panel)

            # ── Path A & B: Always add answer streaming element ──
            new_elements.append(_streaming_element(element_id=ANSWER_ELEMENT_ID))

            # Add new elements before loading hint
            actions.append({
                "action": "add_elements",
                "params": {
                    "type": "insert_before",
                    "target_element_id": _LOADING_HINT_ELEMENT_ID,
                    "elements": new_elements,
                },
            })
            # Delete loading hint
            if _LOADING_HINT_ELEMENT_ID in session.existing_elements:
                actions.append({
                    "action": "delete_elements",
                    "params": {"element_ids": [_LOADING_HINT_ELEMENT_ID]},
                })
            # Note: panel_dirty and tool_steps_dirty are cleared AFTER

            # ── Execute Phase 2 batch_update ──
            if actions:
                _has_panel = state.panel_visible
                session.sequence += 1
                try:
                    await self._client.cardkit_batch_update(
                        session.card_id, actions, sequence=session.sequence,
                    )
                    # Update tracking after success
                    session._creation_stages.add("answer")
                    session._creation_stages.add("hint_removed")
                    session.existing_elements.add(ANSWER_ELEMENT_ID)
                    if _has_panel:
                        session._creation_stages.add("panel")
                        session.existing_elements.add(UNIFIED_PANEL_ELEMENT_ID)
                    session.existing_elements.discard(_LOADING_HINT_ELEMENT_ID)
                    # Clear dirty flags only after API success
                    state.panel_dirty = False
                    state.tool_steps_dirty = False
                except FeishuAPIError as e:
                    if e.code == CARDKIT_STREAMING_CLOSED:
                        if session._streaming_closed_logged:
                            pass
                        else:
                            _logger.info(
                                "unified flush: streaming closed, will be handled by TTL or seal: card=%s",
                                session.card_id[:12],
                            )
                            session._streaming_closed_logged = True
                        session._streaming_closed = True
                        return
                    if is_schema_error(e):
                        _logger.error(
                            "unified flush phase 2 SCHEMA ERROR (permanent): %s — detail: %s card=%s",
                            e, e.extract_schema_detail(), session.card_id[:12],
                        )
                        state.panel_dirty = False
                        state.answer_dirty = False
                        state.tool_steps_dirty = False
                        return
                    elif is_element_not_found_error(e):
                        _logger.warning(
                            "unified flush phase 2 element not found (non-fatal): %s card=%s",
                            e, session.card_id[:12],
                        )
                        session.existing_elements.discard(_LOADING_HINT_ELEMENT_ID)
                        state.panel_dirty = False
                        state.answer_dirty = False
                        state.tool_steps_dirty = False
                        return
                    else:
                        # v1.3.3 fix (P0): transient API error (rate limit, auth
                        _logger.warning(
                            "unified flush phase 2 batch_update failed: %s — "
                            "resetting _first_flush_done for retry, card=%s",
                            e, session.card_id[:12] if session.card_id else "?",
                        )
                        session._first_flush_done = False
                        return
                except asyncio.CancelledError:
                    # v1.3.4 fix (P1): CancelledError 是 BaseException 子类，
                    session._first_flush_done = False
                    raise
                except Exception as e:
                    # v1.3.3 fix (P0): catch non-FeishuAPIError exceptions (network
                    # forever" bug.
                    _logger.warning(
                        "unified flush phase 2 non-API exception: %s — "
                        "resetting _first_flush_done for retry, card=%s",
                        e, session.card_id[:12] if session.card_id else "?",
                        exc_info=True,
                    )
                    session._first_flush_done = False
                    return

            # Note: skip markdown optimization during streaming for performance;
            if state.answer_dirty:
                content = escape_markdown_asterisks(state.answer_text or " ")
                session.sequence += 1
                try:
                    await self._client.cardkit_stream_element(
                        session.card_id, ANSWER_ELEMENT_ID, content, sequence=session.sequence,
                    )
                    state.answer_dirty = False
                except FeishuAPIError as e:
                    if e.code == CARDKIT_STREAMING_CLOSED:
                        session._streaming_closed = True
                        return
                    _logger.debug("unified stream_element failed: %s", e)

            if not state.panel_dirty and not state.tool_steps_dirty and not state.answer_dirty:
                return  # Phase 2 done, nothing more to do

        # ── Phase 3: Update existing panel + stream answer ──
        if state.panel_dirty:
            if "panel" in session._creation_stages:
                # Panel exists — update its content
                all_tool_steps = session.tool_use.build_display_steps()
                panel = build_unified_panel(
                    reasoning_rounds=state.reasoning_rounds,
                    current_reasoning_text=state.current_reasoning_text,
                    tool_steps=all_tool_steps,
                    tool_elapsed_ms=session.tool_use.elapsed_ms,
                    show_reasoning=self._cfg.show_reasoning,
                    expanded=self._cfg.streaming_panel_expanded,
                    panel_events=state.panel_events,
                    max_tool_steps=self._cfg.max_tool_steps,
                    max_reasoning_rounds=self._cfg.max_reasoning_rounds,
                )
                actions.append({
                    "action": "partial_update_element",
                    "params": {
                        "element_id": UNIFIED_PANEL_ELEMENT_ID,
                        "partial_element": {
                            "header": panel["header"],
                            "elements": panel["elements"],
                        },
                    },
                })
            elif "answer" in session._creation_stages:
                # ── Bug fix (v1.0.5): Late-arriving reasoning/tools ──
                all_tool_steps = session.tool_use.build_display_steps()
                panel = build_unified_panel(
                    reasoning_rounds=state.reasoning_rounds,
                    current_reasoning_text=state.current_reasoning_text,
                    tool_steps=all_tool_steps,
                    tool_elapsed_ms=session.tool_use.elapsed_ms,
                    show_reasoning=self._cfg.show_reasoning,
                    expanded=self._cfg.streaming_panel_expanded,
                    panel_events=state.panel_events,
                    max_tool_steps=self._cfg.max_tool_steps,
                    max_reasoning_rounds=self._cfg.max_reasoning_rounds,
                )
                actions.append({
                    "action": "add_elements",
                    "params": {
                        "type": "insert_before",
                        "target_element_id": ANSWER_ELEMENT_ID,
                        "elements": [panel],
                    },
                })
                # Note: "panel" stage will be added after API success below
            # Note: panel_dirty and tool_steps_dirty are cleared AFTER

        # ── Delete loading hint if still present (safety net) ──
        _hint_delete_in_batch = False
        if "hint_removed" not in session._creation_stages and _LOADING_HINT_ELEMENT_ID in session.existing_elements:
            actions.append({
                "action": "delete_elements",
                "params": {"element_ids": [_LOADING_HINT_ELEMENT_ID]},
            })
            _hint_delete_in_batch = True

        # ── Execute Phase 3 batch_update ──
        if actions:
            session.sequence += 1
            try:
                await self._client.cardkit_batch_update(
                    session.card_id, actions, sequence=session.sequence,
                )
                # Clear dirty flags only after API success
                if state.panel_dirty or state.tool_steps_dirty:
                    state.panel_dirty = False
                    state.tool_steps_dirty = False
                if _hint_delete_in_batch:
                    session._creation_stages.add("hint_removed")
                    session.existing_elements.discard(_LOADING_HINT_ELEMENT_ID)
                # ── Track late-arriving panel creation ──
                if "panel" not in session._creation_stages and state.panel_visible:
                    session._creation_stages.add("panel")
                    session.existing_elements.add(UNIFIED_PANEL_ELEMENT_ID)
            except FeishuAPIError as e:
                if e.code == CARDKIT_STREAMING_CLOSED:
                    if session._streaming_closed_logged:
                        pass
                    else:
                        _logger.info(
                            "unified flush: streaming closed, will be handled by TTL or seal: card=%s",
                            session.card_id[:12],
                        )
                        session._streaming_closed_logged = True
                    session._streaming_closed = True
                    return
                if is_schema_error(e):
                    _logger.error(
                        "unified flush phase 3 SCHEMA ERROR (permanent): %s — "
                        "detail: %s — "
                        "clearing dirty flags to stop retry, card=%s",
                        e, e.extract_schema_detail(), session.card_id[:12],
                    )
                    # Clear dirty to stop retry loop on permanent errors
                    state.panel_dirty = False
                    state.tool_steps_dirty = False
                    return
                if is_element_not_found_error(e):
                    # v1.4.1 fix (P1): Phase 3 batch_update 元素不存在 (300315 +
                    # warning 分支 → hint_removed 仍未同步 + existing_elements
                    # 重试。info 级别 (不是 warning/error) — 不是真正的故障。
                    _logger.info(
                        "unified flush phase 3 element not found (non-fatal): %s — "
                        "syncing hint tracking, card=%s",
                        e, session.card_id[:12],
                    )
                    session._creation_stages.add("hint_removed")
                    session.existing_elements.discard(_LOADING_HINT_ELEMENT_ID)
                    # 保留 panel_dirty / tool_steps_dirty 以便下轮 flush 重试
                    return
                _logger.warning("unified flush batch_update failed: %s", e)
                return

        # Note: skip markdown optimization during streaming for performance;
        if state.answer_dirty and "answer" in session._creation_stages:
            content = escape_markdown_asterisks(state.answer_text or " ")
            session.sequence += 1
            try:
                await self._client.cardkit_stream_element(
                    session.card_id, ANSWER_ELEMENT_ID, content, sequence=session.sequence,
                )
                state.answer_dirty = False
            except FeishuAPIError as e:
                if e.code == CARDKIT_STREAMING_CLOSED:
                    if session._streaming_closed_logged:
                        pass
                    else:
                        _logger.info(
                            "HLS: unified stream — streaming closed, will be handled by TTL or seal: card=%s",
                            session.card_id[:12],
                        )
                        session._streaming_closed_logged = True
                    session._streaming_closed = True
                    return
                if is_element_not_found_error(e):
                    _logger.info(
                        "HLS: unified stream — 300313, will retry on next flush: card=%s",
                        session.card_id[:12],
                    )
                    return
                _logger.debug("HLS: unified stream_element failed: %s", e)

        if state.panel_dirty or state.answer_dirty or state.tool_steps_dirty:
            self._schedule_linear_flush(session)

    def _linear_on_thinking(self, session: CardSession, text: str) -> None:
        """Handle a thinking/reasoning delta in linear mode."""
        state = session.unified_state
        if state is None:
            return
        split = split_reasoning_text(text)
        reasoning = split.get("reasoning_text")
        answer = split.get("answer_text")

        _reasoning_already_tracked = bool(state._current_reasoning)
        if reasoning and self._cfg.show_reasoning and not _reasoning_already_tracked:
            state.on_reasoning_delta(reasoning)
        if answer:
            # each interim call contains the full text so far. We must:
            _existing_len = len(state.answer_text)
            if _existing_len == 0:
                # No answer yet - accept the full text
                state.on_answer_delta(answer)
            elif len(answer) > _existing_len and answer[:_existing_len] == state.answer_text:
                # New text extends the existing answer - append only the new portion
                _new_part = answer[_existing_len:]
                if _new_part:
                    _logger.info(
                        "HLS: _linear_on_thinking appends incremental answer "
                        "existing_len=%d new_total=%d diff=%d msg=%s",
                        _existing_len, len(answer), len(_new_part),
                        (session.message_id or "?")[:12],
                    )
                    state.on_answer_delta(_new_part)
            # else: text is same length or shorter - already captured, skip
        if (reasoning and self._cfg.show_reasoning and not _reasoning_already_tracked) or answer:
            self._schedule_linear_flush(session)

    async def _preservative_seal(
        self,
        session: CardSession,
        *,
        partial: bool = False,
        footer_data: dict | None = None,
        is_error: bool = False,
        is_aborted: bool = False,
        error_message: str = "",
        footer_fields: list[list[str]] | None = None,
        footer_show_label: bool = False,
    ) -> bool:
        """Preservative seal — update panel + add footer + close streaming."""
        assert self._client is not None
        card_id = session.card_id
        assert card_id is not None

        try:
            # Before closing streaming, we MUST flush any remaining dirty
            # "footer appears before content finishes" bug.
            state = session.unified_state
            if state is not None and (state.answer_dirty or state.panel_dirty or state.tool_steps_dirty):
                _logger.warning(
                    "preservative seal: dirty data detected at seal time "
                    "answer_dirty=%s panel_dirty=%s tool_steps_dirty=%s card=%s — "
                    "flushing before close",
                    state.answer_dirty, state.panel_dirty, state.tool_steps_dirty,
                    card_id[:12],
                )
                # ── Flush remaining panel content ──
                if (state.panel_dirty or state.tool_steps_dirty) and "panel" in session._creation_stages:
                    all_tool_steps = session.tool_use.build_display_steps()
                    panel = build_unified_panel(
                        reasoning_rounds=state.reasoning_rounds,
                        current_reasoning_text=state.current_reasoning_text,
                        tool_steps=all_tool_steps,
                        tool_elapsed_ms=session.tool_use.elapsed_ms,
                        show_reasoning=self._cfg.show_reasoning,
                        expanded=self._cfg.streaming_panel_expanded,
                        panel_events=state.panel_events,
                        max_tool_steps=self._cfg.max_tool_steps,
                        max_reasoning_rounds=self._cfg.max_reasoning_rounds,
                    )
                    try:
                        session.sequence += 1
                        await self._client.cardkit_batch_update(
                            session.card_id,
                            [{
                                "action": "partial_update_element",
                                "params": {
                                    "element_id": UNIFIED_PANEL_ELEMENT_ID,
                                    "partial_element": {
                                        "header": panel["header"],
                                        "elements": panel["elements"],
                                    },
                                },
                            }],
                            sequence=session.sequence,
                        )
                        state.panel_dirty = False
                        state.tool_steps_dirty = False
                    except FeishuAPIError as e:
                        if e.code == CARDKIT_STREAMING_CLOSED:
                            _logger.info("seal drain: streaming already closed, skipping panel flush")
                            session._streaming_closed = True
                        else:
                            _logger.warning("seal drain panel failed: %s", e)
                        state.panel_dirty = False
                        state.tool_steps_dirty = False

                # ── Flush remaining answer text ──
                if state.answer_dirty and "answer" in session._creation_stages and not session._streaming_closed:
                    content = escape_markdown_asterisks(state.answer_text or " ")
                    try:
                        session.sequence += 1
                        _logger.info(
                            "HLS: seal drain answer text len=%d card=%s",
                            len(content), card_id[:12],
                        )
                        await self._client.cardkit_stream_element(
                            session.card_id, ANSWER_ELEMENT_ID, content,
                            sequence=session.sequence,
                        )
                        state.answer_dirty = False
                    except FeishuAPIError as e:
                        # v1.1.1: 统一 fallback — 300309 和 300313 都改用 batch_update（不带 tag）
                        if e.code == CARDKIT_STREAMING_CLOSED or is_element_not_found_error(e):
                            if e.code == CARDKIT_STREAMING_CLOSED:
                                session._streaming_closed = True
                            _logger.info(
                                "HLS: seal drain answer — %s, falling back to partial_update_element card=%s",
                                "streaming closed" if e.code == CARDKIT_STREAMING_CLOSED else "300313",
                                card_id[:12],
                            )
                            session.sequence += 1
                            await _fallback_write_answer(
                                self._client, session.card_id, content,
                                sequence=session.sequence,
                            )
                        else:
                            _logger.warning("HLS: seal drain answer failed: %s", e)
                        state.answer_dirty = False

            # ── Step 1: Update unified panel to final state (non-streaming) ──
            seal_actions: list[dict[str, Any]] = []
            panel: dict[str, Any] | None = None

            if state is not None:
                state.finalize()

                # ── Bug fix (v1.0.5): Only update panel if it was created ──
                if "panel" in session._creation_stages:
                    all_tool_steps = session.tool_use.build_display_steps()
                    panel = build_unified_panel(
                        reasoning_rounds=state.reasoning_rounds,
                        current_reasoning_text="",
                        tool_steps=all_tool_steps,
                        tool_elapsed_ms=session.tool_use.elapsed_ms,
                        show_reasoning=self._cfg.show_reasoning,
                        expanded=self._cfg.panel_expanded,
                        panel_events=state.panel_events,
                        max_tool_steps=self._cfg.max_tool_steps,
                        max_reasoning_rounds=self._cfg.max_reasoning_rounds,
                    )
                    seal_actions.append({
                        "action": "partial_update_element",
                        "params": {
                            "element_id": UNIFIED_PANEL_ELEMENT_ID,
                            "partial_element": {
                                "header": panel["header"],
                                "elements": panel["elements"],
                            },
                        },
                    })

            # v1.3.1 fix: Do NOT skip this step even when the answer was already fully
            # guard) is a minor visual issue; content truncation is a P0 data-loss bug.
            if state is not None and state.answer_text and "answer" in session._creation_stages:
                # ── Process MEDIA: paths → upload to Feishu → extract as card img elements ──
                _seal_answer_text = await _process_media_images(self._client, state.answer_text)
                _seal_answer_text, _seal_img_elements = _extract_images_from_markdown(_seal_answer_text, image_size=self._cfg.image_size)
                _seal_answer_text = _seal_answer_text or " "
                optimized_content = escape_markdown_asterisks(_downgrade_tables(optimize_markdown_style(_seal_answer_text))) or " "
                seal_actions.append({
                    "action": "partial_update_element",
                    "params": {
                        "element_id": ANSWER_ELEMENT_ID,
                        "partial_element": {
                            "content": optimized_content,
                        },
                    },
                })
                if _seal_img_elements:
                    seal_actions.append({
                        "action": "add_elements",
                        "params": {
                            "type": "insert_after",
                            "target_element_id": ANSWER_ELEMENT_ID,
                            "elements": _seal_img_elements,
                        },
                    })

            # ── Step 3: Add footer + delete loading elements ──
            seal_actions.extend(
                build_preservative_seal_actions(
                    partial=partial,
                    footer_data=footer_data,
                    is_error=is_error,
                    is_aborted=is_aborted,
                    error_message=error_message,
                    footer_fields=footer_fields,
                    footer_show_label=footer_show_label,
                    existing_elements=session.existing_elements,
                    card_trace_id=session.card_trace_id,
                    session_title=session.footer.get("session_title", "") if self._cfg.show_session_title else "",
                )
            )

            if panel is not None:
                simulated_elements: list[dict] = []
                # Panel
                simulated_elements.append(panel)
                # Answer element (1 markdown with content)
                if state is not None and state.answer_text:
                    simulated_elements.append({"tag": "markdown", "content": state.answer_text})
                else:
                    simulated_elements.append({"tag": "markdown", "content": " "})
                # Elements from add_elements actions (footer, error, partial, bg review)
                for action in seal_actions:
                    if action.get("action") == "add_elements":
                        for elem in action.get("params", {}).get("elements", []):
                            simulated_elements.append(elem)
                # Count total tag objects in simulated card body
                total_count = _count_tag_objects(simulated_elements)
                _FEISHU_ELEMENT_LIMIT = 200
                _ELEMENT_LIMIT_MARGIN = 5
                threshold = _FEISHU_ELEMENT_LIMIT - _ELEMENT_LIMIT_MARGIN
                if total_count > threshold:
                    _logger.warning(
                        "preservative seal: card element count %d exceeds threshold %d, "
                        "trimming panel children card=%s",
                        total_count, threshold, card_id[:12],
                    )
                    # Trim panel children from the front
                    children: list[dict] = panel.get("elements", [])
                    # Check if a collapse hint already exists
                    hint_idx = None
                    for i, child in enumerate(children):
                        if isinstance(child.get("content"), str) and "已折叠" in child["content"]:
                            hint_idx = i
                            break
                    # If no hint exists yet, we'll need to add one (1 element), so account for it
                    if hint_idx is None:
                        total_count += 1
                    trimmed_count = 0
                    while total_count > threshold and len(children) > 1:
                        # Skip the collapse hint (first child if it contains "已折叠")
                        remove_idx = 1 if children[0].get("content", "").endswith("已折叠") else 0
                        removed = children.pop(remove_idx)
                        total_count -= _count_tag_objects([removed])
                        trimmed_count += 1
                    if trimmed_count > 0:
                        # Update or add collapse hint
                        # Re-find hint_idx (may have shifted due to removals)
                        hint_idx = None
                        for i, child in enumerate(children):
                            if isinstance(child.get("content"), str) and "已折叠" in child["content"]:
                                hint_idx = i
                                break
                        if hint_idx is not None:
                            old_hint = children[hint_idx]["content"]
                            # Parse existing trimmed count, then add new count
                            # (same logic as _enforce_card_element_limit in cards.py)
                            existing_count = 0
                            _idx = old_hint.find("项")
                            if _idx > 0:
                                # Walk backwards skipping whitespace, then collect digits
                                _end = _idx
                                while _end > 0 and old_hint[_end - 1] == ' ':
                                    _end -= 1
                                _start = _end
                                while _start > 0 and old_hint[_start - 1].isdigit():
                                    _start -= 1
                                if _start < _end:
                                    existing_count = int(old_hint[_start:_end])
                            total_trimmed = existing_count + trimmed_count
                            children[hint_idx]["content"] = f"⚡ 还有 {total_trimmed} 项已折叠"
                        else:
                            children.insert(0, {
                                "tag": "markdown",
                                "content": f"⚡ 还有 {trimmed_count} 项已折叠",
                                "text_size": "notation",
                            })
                        # Update panel's elements
                        panel["elements"] = children
                        # Rebuild the panel update action in seal_actions
                        for i, action in enumerate(seal_actions):
                            if (action.get("action") == "partial_update_element"
                                    and action.get("params", {}).get("element_id") == UNIFIED_PANEL_ELEMENT_ID):
                                seal_actions[i]["params"]["partial_element"]["elements"] = children
                                break
                    _logger.info(
                        "preservative seal: after trimming, estimated total %d, trimmed %d items card=%s",
                        total_count, trimmed_count, card_id[:12],
                    )

            if seal_actions:
                session.sequence += 1
                await self._client.cardkit_batch_update(
                    card_id, seal_actions, sequence=session.sequence,
                )

            # When closing streaming, we MUST also update the card's summary
            # bug the user reported.
            # CRITICAL: Only call close_streaming ONCE per card lifecycle.
            # updated to config.summary.content.  The summary MUST be
            seal_summary = _build_seal_summary(state)

            if not session._streaming_closed:
                session.sequence += 1
                _logger.info(
                    "HLS: preservative seal closing streaming card=%s trace=%s seq=%d summary=%s",
                    card_id[:12], session.card_trace_id, session.sequence,
                    repr(seal_summary[:40]) if seal_summary else "(empty)",
                )
                # ── Bug fix (v1.0.3): Pass summary IN close_streaming ──
                # must be in THIS request — passing summary="" and then
                await self._client.cardkit_close_streaming(
                    card_id, sequence=session.sequence, summary=seal_summary,
                )
                session._streaming_closed = True
            else:
                _logger.info(
                    "preservative seal: streaming already closed, skipping close_streaming card=%s",
                    card_id[:12],
                )
                if seal_summary:
                    try:
                        session.sequence += 1
                        await self._client.cardkit_update_summary(
                            card_id, seal_summary, sequence=session.sequence,
                        )
                        _logger.info(
                            "preservative seal: summary updated (streaming already closed) "
                            "card=%s seq=%d summary=%s",
                            card_id[:12], session.sequence,
                            repr(seal_summary[:40]),
                        )
                    except FeishuAPIError as e:
                        _logger.warning(
                            "preservative seal: summary update failed (already closed) "
                            "card=%s error=%s",
                            card_id[:12], e,
                        )

            return True

        except FeishuAPIError as e:
            if e.code == CARDKIT_SEQUENCE_CONFLICT:
                _logger.warning(
                    "preservative seal: sequence conflict, retrying... card=%s seq=%d",
                    card_id[:12], session.sequence,
                )
                for retry in range(2):
                    try:
                        retry_actions: list[dict[str, Any]] = []
                        if state is not None:
                            # ── Bug fix (v1.0.5): Only update panel if it was created ──
                            if "panel" in session._creation_stages:
                                all_tool_steps = session.tool_use.build_display_steps()
                                retry_panel = build_unified_panel(
                                    reasoning_rounds=state.reasoning_rounds,
                                    current_reasoning_text="",
                                    tool_steps=all_tool_steps,
                                    tool_elapsed_ms=session.tool_use.elapsed_ms,
                                    show_reasoning=self._cfg.show_reasoning,
                                    expanded=self._cfg.panel_expanded,
                                    panel_events=state.panel_events,
                                    max_tool_steps=self._cfg.max_tool_steps,
                                    max_reasoning_rounds=self._cfg.max_reasoning_rounds,
                                )
                                retry_actions.append({
                                    "action": "partial_update_element",
                                    "params": {
                                        "element_id": UNIFIED_PANEL_ELEMENT_ID,
                                        "partial_element": {
                                            "header": retry_panel["header"],
                                            "elements": retry_panel["elements"],
                                        },
                                    },
                                })
                            # v1.3.1: same fix as main seal path — always send final
                            # (see v1.3.1 fix comment in main seal path above).
                            if state.answer_text and "answer" in session._creation_stages:
                                _retry_answer_text = await _process_media_images(self._client, state.answer_text)
                                _retry_answer_text, _retry_img_elements = _extract_images_from_markdown(_retry_answer_text, image_size=self._cfg.image_size)
                                _retry_answer_text = _retry_answer_text or " "
                                optimized_content = escape_markdown_asterisks(_downgrade_tables(optimize_markdown_style(_retry_answer_text))) or " "
                                retry_actions.append({
                                    "action": "partial_update_element",
                                    "params": {
                                        "element_id": ANSWER_ELEMENT_ID,
                                        "partial_element": {
                                            "content": optimized_content,
                                        },
                                    },
                                })
                                if _retry_img_elements:
                                    retry_actions.append({
                                        "action": "add_elements",
                                        "params": {
                                            "type": "insert_after",
                                            "target_element_id": ANSWER_ELEMENT_ID,
                                            "elements": _retry_img_elements,
                                        },
                                    })
                        retry_actions.extend(
                            build_preservative_seal_actions(
                                partial=partial,
                                footer_data=footer_data,
                                is_error=is_error,
                                is_aborted=is_aborted,
                                error_message=error_message,
                                footer_fields=footer_fields,
                                footer_show_label=footer_show_label,
                                existing_elements=session.existing_elements,
                                card_trace_id=session.card_trace_id,
                                session_title=session.footer.get("session_title", "") if self._cfg.show_session_title else "",
                            )
                        )
                        # batch_update BEFORE close_streaming (same order as try block)
                        if retry_actions:
                            session.sequence += 1
                            await self._client.cardkit_batch_update(
                                card_id, retry_actions, sequence=session.sequence,
                            )

                        # Close streaming AFTER batch_update
                        if not session._streaming_closed:
                            # Recompute seal_summary for retry (state may have changed)
                            retry_summary = _build_seal_summary(state)
                            session.sequence += 1
                            await self._client.cardkit_close_streaming(
                                card_id, sequence=session.sequence, summary=retry_summary,
                            )
                            session._streaming_closed = True

                        _logger.info(
                            "preservative seal: retry %d succeeded card=%s",
                            retry + 1, card_id[:12],
                        )
                        return True
                    except FeishuAPIError as retry_e:
                        if retry_e.code == CARDKIT_SEQUENCE_CONFLICT:
                            continue
                        # v1.3.4 fix (P1): retry 路径 300309 应与主路径一致 return False
                        if retry_e.code == CARDKIT_STREAMING_CLOSED:
                            session._streaming_closed = True
                            return False
                        raise
                # All retries exhausted
                _logger.warning(
                    "preservative seal: retry exhausted after sequence conflicts card=%s",
                    card_id[:12],
                )
                return False
            if e.code == CARDKIT_STREAMING_CLOSED:
                session._streaming_closed = True
            return False
        except Exception:
            return False

    async def _do_linear_complete(self, session: CardSession) -> bool:
        """Complete the card with the unified panel architecture."""
        if session.guard.should_skip("_do_linear_complete"):
            return False

        # ── Step 1: Wait for any in-progress flush to finish ──
        await session.flush.wait_for_flush()

        # without being flushed.  We must drain it ALL here, before
        # the "footer appears before content finishes" bug.
        state = session.unified_state
        _MAX_DRAIN_ROUNDS = 8
        _DRAIN_YIELD_SEC = 0.020  # 20ms yield — enough for worker thread callbacks
        for _drain_round in range(_MAX_DRAIN_ROUNDS):
            if not (
                state is not None
                and session.card_id
                and "answer" in session._creation_stages
                and (state.answer_dirty or state.panel_dirty or state.tool_steps_dirty)
            ):
                break  # No dirty data — drain complete

            _logger.info(
                "linear complete: drain round %d/%d "
                "answer_dirty=%s panel_dirty=%s tool_steps_dirty=%s msg=%s",
                _drain_round + 1, _MAX_DRAIN_ROUNDS,
                state.answer_dirty, state.panel_dirty, state.tool_steps_dirty,
                (session.message_id or "?")[:12],
            )
            assert self._client is not None

            # ── Drain panel content ──
            if state.panel_dirty and "panel" in session._creation_stages:
                all_tool_steps = session.tool_use.build_display_steps()
                panel = build_unified_panel(
                    reasoning_rounds=state.reasoning_rounds,
                    current_reasoning_text=state.current_reasoning_text,
                    tool_steps=all_tool_steps,
                    tool_elapsed_ms=session.tool_use.elapsed_ms,
                    show_reasoning=self._cfg.show_reasoning,
                    expanded=self._cfg.streaming_panel_expanded,
                    panel_events=state.panel_events,
                    max_tool_steps=self._cfg.max_tool_steps,
                    max_reasoning_rounds=self._cfg.max_reasoning_rounds,
                )
                drain_actions: list[dict[str, Any]] = [{
                    "action": "partial_update_element",
                    "params": {
                        "element_id": UNIFIED_PANEL_ELEMENT_ID,
                        "partial_element": {
                            "header": panel["header"],
                            "elements": panel["elements"],
                        },
                    },
                }]
                try:
                    session.sequence += 1
                    await self._client.cardkit_batch_update(
                        session.card_id, drain_actions, sequence=session.sequence,
                    )
                    state.panel_dirty = False
                    state.tool_steps_dirty = False
                except FeishuAPIError as e:
                    if e.code == CARDKIT_STREAMING_CLOSED:
                        # v1.2.0 Y3: drain 阶段也用 _streaming_closed_logged 去重
                        if session._streaming_closed_logged:
                            _logger.debug("drain: streaming already closed (already logged)")
                        else:
                            _logger.info("drain: streaming already closed, skipping")
                            session._streaming_closed_logged = True
                        session._streaming_closed = True
                    elif is_schema_error(e):
                        _logger.error("drain SCHEMA ERROR: %s — detail: %s", e, e.extract_schema_detail())
                        state.panel_dirty = False
                        state.tool_steps_dirty = False
                    else:
                        _logger.warning("drain panel failed: %s", e)

            # ── Drain answer text ──
            if state.answer_dirty and "answer" in session._creation_stages:
                content = escape_markdown_asterisks(state.answer_text or " ")
                try:
                    session.sequence += 1
                    _logger.info(
                        "HLS: drain answer text len=%d msg=%s",
                        len(content), (session.message_id or "?")[:12],
                    )
                    await self._client.cardkit_stream_element(
                        session.card_id, ANSWER_ELEMENT_ID, content,
                        sequence=session.sequence,
                    )
                    state.answer_dirty = False
                except FeishuAPIError as e:
                    # v1.1.1: 统一 fallback — 300309 和 300313 都改用 batch_update（不带 tag）
                    # 之前 300309 直接 skip 答案丢失；300313 的 fallback 带 tag 报 300312
                    if e.code == CARDKIT_STREAMING_CLOSED or is_element_not_found_error(e):
                        if e.code == CARDKIT_STREAMING_CLOSED:
                            session._streaming_closed = True
                        # v1.2.0 Y3: streaming closed 日志去重；300313 仍每次打（非重复事件）
                        if e.code == CARDKIT_STREAMING_CLOSED and session._streaming_closed_logged:
                            pass
                        else:
                            _logger.info(
                                "HLS: drain answer — %s, falling back to partial_update_element msg=%s",
                                "streaming closed" if e.code == CARDKIT_STREAMING_CLOSED else "300313",
                                (session.message_id or "?")[:12],
                            )
                            if e.code == CARDKIT_STREAMING_CLOSED:
                                session._streaming_closed_logged = True
                        session.sequence += 1
                        ok = await _fallback_write_answer(
                            self._client, session.card_id, content,
                            sequence=session.sequence,
                        )
                        if ok:
                            state.answer_dirty = False
                    else:
                        _logger.warning("HLS: drain answer failed: %s", e)

            if _drain_round < _MAX_DRAIN_ROUNDS - 1:
                await asyncio.sleep(_DRAIN_YIELD_SEC)

        # ── Final drain check: log warning if dirty data remains ──
        if state is not None and (state.answer_dirty or state.panel_dirty or state.tool_steps_dirty):
            _logger.warning(
                "linear complete: dirty data remains after %d drain rounds "
                "answer_dirty=%s panel_dirty=%s tool_steps_dirty=%s msg=%s — "
                "will be flushed by preservative seal before close_streaming",
                _MAX_DRAIN_ROUNDS,
                state.answer_dirty, state.panel_dirty, state.tool_steps_dirty,
                (session.message_id or "?")[:12],
            )

        # ── Step 3: Mark flush as completed — no more updates accepted ──
        session.flush.mark_completed()

        try:
            await asyncio.wait_for(session._card_ready.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            _logger.warning("complete: card creation timed out: msg=%s", (session.message_id or "?")[:12])

        if not session.card_id:
            session.state = CREATION_FAILED
            session.enter_terminal(
                reason=TerminalReason.CREATION_FAILED,
                source="_do_linear_complete",
            )
            return False

        # ── Step 4: Finalize state ──
        if state:
            state.finalize()

        # ── Build footer data ──
        footer_data = session.footer
        # v1.3.4 fix (P2): bg_review_messages 存在 state 中但从未传给
        if state and state.bg_review_messages:
            if footer_data is None:
                footer_data = {}
            footer_data = {**footer_data, "bg_review_messages": list(state.bg_review_messages)}
        is_aborted = getattr(session, "_was_aborted", False) or session.state == ABORTED
        error_message = getattr(session, "error_message", "")
        # v1.2.0 B1 fix: is_error 必须兼顾 error_message。
        is_error = (
            session.state in (CREATION_FAILED, TERMINATED)
            or (bool(error_message) and not is_aborted)
        )

        # ── Step 5: Preservative seal (the only completion path) ──
        seal_ok = await self._preservative_seal(
            session,
            footer_data=footer_data,
            is_error=is_error,
            is_aborted=is_aborted,
            error_message=error_message,
            footer_fields=self._cfg.footer_fields,
            footer_show_label=self._cfg.footer_show_label,
        )

        if seal_ok:
            # v1.3.4 fix (P1): 如果会话已被 on_aborted 标记为 ABORTED，
            if session._was_aborted:
                session.state = ABORTED
            else:
                session.state = COMPLETED
            # v1.1.1: 释放重数据（unified_state/text/tool_use），减少内存占用
            # session 留最小元数据等 _prune_stale_sessions 清理
            try:
                self._release_session_data(session)
            except Exception:
                _logger.debug("HLS: release session data failed", exc_info=True)
            # v1.1.0: Record metrics
            try:
                from ..aowen import record_card_completed
                record_card_completed()
            except Exception:
                _logger.debug('metrics: record_card_completed failed', exc_info=True)
        else:
            session.state = CREATION_FAILED
            session.enter_terminal(
                reason=TerminalReason.CREATION_FAILED,
                source="_do_linear_complete_seal_failed",
            )
            # v1.1.1: 失败也释放重数据
            try:
                self._release_session_data(session)
            except Exception:
                _logger.debug("HLS: release session data failed", exc_info=True)
            # v1.1.0: Record metrics
            try:
                from ..aowen import record_card_failed
                record_card_failed()
            except Exception:
                _logger.debug('metrics: record_card_failed failed', exc_info=True)

        return seal_ok

__all__ = [
    "UnifiedControllerMixin",
]
