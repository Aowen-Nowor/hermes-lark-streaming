"""V2 帧重组引擎 — hermes 出站帧 → 飞书 CardKit 流式卡片.

职责：
1. 维护 per-chat 的 ``CardTurnState``（活跃卡片注册表）
2. 消费 ``classify`` 的分类结果，驱动 CardKit 生命周期：
   create → send_card_by_id → stream_element（answer 打字机）→ update（状态区）→ close_streaming
3. 降级保障：任何卡片 API 失败 → per-chat 降级旗标 → 后续帧全部走原生路径，**永不丢消息**

线程/并发模型：全部方法在适配器的事件循环内串行调用（hermes 对单 chat 的出站
天然串行）；跨 chat 状态互不共享。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .classify import (
    CH_FINAL,
    CH_INTERIM,
    CH_PROGRESS,
    CH_STREAM,
    ClassifyResult,
)

_logger = logging.getLogger("hermes_lark_streaming.composer")

try:  # hermes 运行时存在；导入失败时 composer 退化为纯直通（tests / 无 hermes 环境）
    from gateway.platforms.base import SendResult  # type: ignore
except Exception:  # noqa: BLE001 — hermes 缺失时兜底（tests/独立使用）  # pragma: no cover

    class SendResult:  # type: ignore[no-redef]
        def __init__(self, success: bool, message_id: str | None = None, error: str | None = None,
                     raw_response: Any = None, retryable: bool = False) -> None:
            self.success = success
            self.message_id = message_id
            self.error = error
            self.raw_response = raw_response
            self.retryable = retryable


@dataclass
class CardTurnState:
    """一次 turn 的活跃卡片状态."""

    chat_id: str
    card_id: str = ""                  # CardKit 实体 id
    card_message_id: str = ""          # 卡片作为飞书消息的 id
    answer_text: str = ""              # answer 全量缓冲（hermes 帧是全量语义）
    status_lines: list[str] = field(default_factory=list)   # 工具/思考/interim 状态行
    seq: int = 0                       # CardKit sequence 递增
    created_at: float = field(default_factory=time.monotonic)
    last_activity: float = field(default_factory=time.monotonic)
    finalized: bool = False
    degraded: bool = False             # 卡片 API 失败 → 本 turn 走原生
    pending_status: list[str] = field(default_factory=list)  # 卡片建立前的 progress 暂存


class FrameComposer:
    """把四通道出站帧重组为一张 CardKit 流式卡片.

    返回约定：``handle_send / handle_edit`` 返回 ``None`` 表示"本帧走原生
    ``super()`` 路径"；返回 ``SendResult`` 表示已由卡片通道消费。
    """

    def __init__(
        self,
        *,
        get_client: Callable[[], Any | None],
        build_card: Callable[..., dict[str, Any]],
        build_final_card: Callable[..., dict[str, Any]],
        config: Any | None = None,
    ) -> None:
        self._get_client = get_client          # () -> FeishuClient | None
        self._build_card = build_card          # build_streaming_card_v2(**kwargs)
        self._build_final_card = build_final_card
        self._config = config
        self._states: dict[str, CardTurnState] = {}
        self._by_message: dict[str, str] = {}  # card_message_id -> chat_id

    # ── 状态表管理 ──────────────────────────────────────────────────────
    def _ttl_sec(self) -> int:
        try:
            return int(getattr(self._config, "card_duration_sec", 600) or 600)
        except Exception:  # noqa: BLE001 — 配置异常按默认 TTL
            return 600

    def _gc(self) -> None:
        ttl = self._ttl_sec()
        now = time.monotonic()
        stale = [cid for cid, st in self._states.items()
                 if st.finalized and now - st.last_activity > 60 or now - st.created_at > ttl]
        for cid in stale:
            st = self._states.pop(cid, None)
            if st and st.card_message_id:
                self._by_message.pop(st.card_message_id, None)

    def active_state(self, chat_id: str) -> CardTurnState | None:
        st = self._states.get(chat_id)
        # card_id 为空 = 建卡前的 progress 暂存占位（非活跃卡片）
        if st and st.card_id and not st.finalized and not st.degraded:
            return st
        return None

    def state_by_message(self, message_id: str) -> CardTurnState | None:
        chat_id = self._by_message.get(str(message_id or ""))
        if not chat_id:
            return None
        return self._states.get(chat_id)

    # ── send 帧处理 ─────────────────────────────────────────────────────
    async def handle_send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None,
        metadata: dict[str, Any] | None,
        cls: ClassifyResult,
    ) -> SendResult | None:
        self._gc()
        try:
            if cls.channel == CH_STREAM:
                return await self._on_stream(chat_id, content)
            if cls.channel == CH_PROGRESS:
                return await self._on_progress(chat_id, cls)
            if cls.channel == CH_INTERIM:
                return await self._on_interim(chat_id, content, cls)
            if cls.channel == CH_FINAL:
                return await self._on_final_send(chat_id, content)
            return None  # CH_PLAIN → 原生
        except Exception as exc:  # noqa: BLE001 — 降级红线：任何失败走原生
            self._degrade(chat_id, "send", exc)
            return None

    async def handle_edit(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        finalize: bool,
        cls: ClassifyResult,
    ) -> SendResult | None:
        self._gc()
        try:
            st = self.state_by_message(message_id)
            if st is None or st.degraded:
                return None
            if finalize:
                return await self._finalize(st, content)
            return await self._patch_answer(st, content)
        except Exception as exc:  # noqa: BLE001 — 降级红线：任何失败走原生
            self._degrade(chat_id, "edit", exc)
            return None

    # ── 通道实现 ────────────────────────────────────────────────────────
    async def _on_stream(self, chat_id: str, content: str) -> SendResult | None:
        st = self.active_state(chat_id)
        if st is None:
            return await self._create_card(chat_id, content)
        # 已有活跃卡片：hermes 分段发送 → 追加段落
        st.answer_text = (st.answer_text + "\n\n" + content).strip()
        await self._stream_answer(st)
        return SendResult(success=True, message_id=st.card_message_id)

    async def _create_card(self, chat_id: str, content: str) -> SendResult | None:
        client = self._get_client()
        if client is None:
            return None
        # 并入建卡前暂存的 progress 行（首卡即带状态面板）
        prev = self._states.get(chat_id)
        pending = list(prev.pending_status)[-12:] if prev is not None else []
        card = self._build_card(status_lines=pending or None)
        card_id = await client.cardkit_create(card)
        message_id = await client.send_card_by_id_to_chat(chat_id, card_id)
        # 复用占位/上一 turn 状态对象（保留注册表连续性），升级为活跃卡片。
        # 注意必须重置终态旗标：同 chat 连续 turn 时 prev 可能是上一 turn 的
        # finalized 状态，不重置会导致第 2 turn active_state 恒 None（重复建卡）。
        st = prev if prev is not None else CardTurnState(chat_id=chat_id)
        st.chat_id = chat_id
        st.card_id = card_id
        st.card_message_id = str(message_id)
        st.answer_text = ""
        st.seq = 0
        st.created_at = time.monotonic()
        st.last_activity = time.monotonic()
        st.finalized = False
        st.degraded = False
        st.status_lines = pending
        st.pending_status.clear()
        self._states[chat_id] = st
        self._by_message[st.card_message_id] = chat_id
        st.answer_text = content or ""
        st.seq += 1
        if st.answer_text:
            await client.cardkit_stream_element(
                card_id, _answer_element_id(), st.answer_text, sequence=st.seq,
            )
        return SendResult(success=True, message_id=st.card_message_id)

    async def _patch_answer(self, st: CardTurnState, content: str) -> SendResult:
        st.answer_text = content if content is not None else st.answer_text
        await self._stream_answer(st)
        return SendResult(success=True, message_id=st.card_message_id)

    async def _stream_answer(self, st: CardTurnState) -> None:
        client = self._get_client()
        if client is None:
            raise RuntimeError("card client unavailable")
        st.seq += 1
        st.last_activity = time.monotonic()
        await client.cardkit_stream_element(
            st.card_id, _answer_element_id(), st.answer_text, sequence=st.seq,
        )

    async def _on_progress(self, chat_id: str, cls: ClassifyResult) -> SendResult | None:
        st = self.active_state(chat_id)
        line = cls.raw_content.strip()
        if st is None:
            # 卡片未建立：暂存（turn 建卡时并入；TTL 兜底清理）
            self._states.setdefault(chat_id, CardTurnState(chat_id=chat_id)).pending_status.append(line)
            return SendResult(success=True)  # 已消费（不产生独立气泡）
        if cls.kind == "thinking":
            new_line = line
        else:
            new_line = line
        if st.status_lines and st.status_lines[-1] == new_line:
            return SendResult(success=True, message_id=st.card_message_id)
        st.status_lines.append(new_line)
        st.status_lines = st.status_lines[-12:]
        await self._patch_status(st)
        return SendResult(success=True, message_id=st.card_message_id)

    async def _on_interim(self, chat_id: str, content: str, cls: ClassifyResult) -> SendResult | None:
        st = self.active_state(chat_id)
        if st is None:
            return None  # 无卡片：原生小气泡
        st.status_lines.append(content.strip())
        st.status_lines = st.status_lines[-12:]
        await self._patch_status(st)
        return SendResult(success=True, message_id=st.card_message_id)

    async def _on_final_send(self, chat_id: str, content: str) -> SendResult | None:
        st = self.active_state(chat_id)
        if st is None:
            return None
        return await self._finalize(st, content)

    async def _patch_status(self, st: CardTurnState) -> None:
        client = self._get_client()
        if client is None:
            raise RuntimeError("card client unavailable")
        card = self._build_card(status_lines=st.status_lines)
        st.seq += 1
        st.last_activity = time.monotonic()
        await client.cardkit_update(st.card_id, card, sequence=st.seq)

    async def _finalize(self, st: CardTurnState, content: str) -> SendResult:
        from .finalize import finalize_card

        client = self._get_client()
        if client is None:
            raise RuntimeError("card client unavailable")
        if content:
            st.answer_text = content
        st.last_activity = time.monotonic()
        await finalize_card(client, st, answer_element_id=_answer_element_id())
        st.finalized = True
        return SendResult(success=True, message_id=st.card_message_id)

    # ── 降级 ────────────────────────────────────────────────────────────
    def _degrade(self, chat_id: str, where: str, exc: Exception) -> None:
        st = self._states.get(chat_id)
        if st is not None:
            st.degraded = True
            st.finalized = True  # 本 turn 不再消费
        _logger.warning(
            "composer: card channel degraded for chat=%s at %s — falling back to native text: %s",
            chat_id, where, exc,
        )

    # ── 清理 ────────────────────────────────────────────────────────────
    def forget(self, chat_id: str) -> None:
        st = self._states.pop(chat_id, None)
        if st and st.card_message_id:
            self._by_message.pop(st.card_message_id, None)

    def clear(self) -> None:
        self._states.clear()
        self._by_message.clear()


def _answer_element_id() -> str:
    from ..cardkit.elements import ANSWER_ELEMENT_ID

    return ANSWER_ELEMENT_ID
