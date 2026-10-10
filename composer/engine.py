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

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..cardkit.progline import split_progress_segments, strip_dedup_suffix
from .classify import (
    _THINKING_PREFIX,
    CH_FINAL,
    CH_INTERIM,
    CH_PROGRESS,
    CH_STREAM,
    ClassifyResult,
    parse_progress_line,
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
    # v2.0.5：turn 起点（首个进度帧/首帧到达），驱动面板标题「· 总耗时」。
    # 与 created_at 区分：created_at 随建卡重置，turn_started_at 只随 turn 重置。
    turn_started_at: float | None = None
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
        # v2.0.7：reasoning 流回灌的线程安全入口需要事件循环引用 ——
        # 构造期若已在 loop 内则直接捕获，否则首个 async 帧入口补捕获。
        try:
            self._loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

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
        self._loop = asyncio.get_running_loop()  # v2.0.7：供线程安全回灌入口使用
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

    # ── reasoning 流回灌（v2.0.7，插件 stream-hook 守护线程 → 事件循环）───
    def submit_progress_from_thread(self, chat_id: str, raw_line: str) -> bool:
        """线程安全地把一行进度文本（如 reasoning_feed 的 💬 行）投递进面板.

        仅活跃卡片期消费：未建卡（思考先于 answer 首帧）由 handle_send 的
        暂存分支处理；已定稿/已降级则丢弃（终态卡片不再变化）。返回是否受理。
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return False
        st = self._states.get(chat_id)
        if st is not None and (st.finalized or st.degraded):
            return False
        try:
            loop.call_soon_threadsafe(self._spawn_progress, chat_id, raw_line)
            return True
        except RuntimeError:
            return False

    def _spawn_progress(self, chat_id: str, raw_line: str) -> None:
        asyncio.ensure_future(self._feed_progress(chat_id, raw_line))

    async def _feed_progress(self, chat_id: str, raw_line: str) -> None:
        cls = ClassifyResult(CH_PROGRESS, kind="thinking", raw_content=raw_line)
        try:
            await self.handle_send(chat_id, raw_line, None, None, cls)
        except Exception:  # 回灌失败不影响主流程（BLE 未启用，noqa 触发 RUF100）
            _logger.debug("composer: reasoning feed drop chat=%s", chat_id, exc_info=True)

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
        # 并入建卡前暂存的 progress 行（首卡即带状态面板）。
        # v2.0.5：暂存行过同一套合并语义（幂等/pending→preview 取代/思考累积），
        # 否则快照式暂存会把同一次调用的 pending 与 preview 双双带进首卡面板。
        prev = self._states.get(chat_id)
        raw_pending = list(prev.pending_status)[-12:] if prev is not None else []
        # v2.0.6：暂存行先过同一套逻辑分段（每条暂存可能是多段快照，
        # 围栏块保持一体），再统一合并——与活跃卡片路径语义一致。
        staged: list[str] = []
        for item in raw_pending:
            staged.extend(split_progress_segments(item) or ([item.strip()] if item.strip() else []))
        pending = _merge_progress_lines([], staged) or []
        # v2.0.5：先落状态对象再建卡——面板标题需要 turn 计时。复用占位状态时
        # 保留本 turn 的 turn_started_at（首个进度帧即 turn 起点）；上一 turn
        # 残留 / 全新会话则重新起算。
        st = prev if prev is not None else CardTurnState(chat_id=chat_id)
        if prev is None or prev.finalized or prev.turn_started_at is None:
            st.turn_started_at = time.monotonic()
        card = self._build_card(
            status_lines=pending or None,
            panel_title=_panel_title(pending, elapsed_ms=_turn_elapsed_ms(st)),
        )
        card_id = await client.cardkit_create(card)
        message_id = await client.send_card_by_id_to_chat(chat_id, card_id)
        # 复用占位/上一 turn 状态对象（保留注册表连续性），升级为活跃卡片。
        # 注意必须重置终态旗标：同 chat 连续 turn 时 prev 可能是上一 turn 的
        # finalized 状态，不重置会导致第 2 turn active_state 恒 None（重复建卡）。
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
        _logger.info(
            "composer: card created chat=%s card=%s msg=%s status_lines=%d",
            chat_id, card_id, st.card_message_id, len(pending),
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
        raw = (cls.raw_content or "").strip()
        if st is None:
            # 卡片未建立：暂存（turn 建卡时并入；TTL 兜底清理）。
            # v2.0.5：占位状态即 turn 起点（面板标题「· 总耗时」从此起算）。
            placeholder = self._states.setdefault(
                chat_id, CardTurnState(chat_id=chat_id))
            if placeholder.turn_started_at is None:
                placeholder.turn_started_at = time.monotonic()
            placeholder.pending_status.append(raw)
            # v2.0.4（s70 回流）：立即建占位卡（还原 V1 体验：卡片先现，工具/思考
            # 实时进面板，不再裸奔成独立气泡）。pending 已并入首卡面板；答案元素
            # 留空待流式。仍不回传 message_id：防网关把返回 id 当作进度气泡
            # anchor 反复 edit（v2.0.1 事故预防）。
            try:
                await self._create_card(chat_id, "")
            except Exception:  # 建卡失败维持暂存语义（TTL 兜底），不阻断
                _logger.debug(
                    "composer: early card creation failed — staged pending only chat=%s",
                    chat_id, exc_info=True,
                )
            return SendResult(success=True)
        # v2.0.1：网关 accumulate 模式下气泡文本是“当前全部进度行”的全量快照
        # （edit_interval 每 tick 重发累积全文）；separate 模式则是单行增量。
        # v2.0.5 合并语义升级（见 _merge_progress_lines）：
        #   完全同文本 → 幂等跳过；pending 行 → 同工具 preview/head 原地取代；
        #   思考行累积增长 → 原地替换（不膨胀行数、不计新轮次）。
        # v2.0.6：accumulate 快照按逻辑段拆分（围栏代码块与工具头一体），
        # 直拆 splitlines 会把真实 terminal 块拆成破碎的 ``` 行（E2E ≠ 正式对话根因）。
        lines = split_progress_segments(raw) or ([raw] if raw else [])
        merged = _merge_progress_lines(st.status_lines, lines)
        if merged is None:
            # 无新增/无变化：不触发 cardkit_update（API 经济性；V2.0.0 同语义）
            return SendResult(success=True)
        st.status_lines = merged[-12:]
        await self._patch_status(st)
        # 同上：不回传 message_id，网关进度气泡循环会继续 _send_progress_text
        # （全量快照）而非 edit —— 面板更新由本引擎的 cardkit_update 驱动。
        return SendResult(success=True)

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
        card = self._build_card(
            status_lines=st.status_lines,
            answer_text=st.answer_text,   # 防全量更新把已流 answer 重置为空
            panel_title=_panel_title(st.status_lines, elapsed_ms=_turn_elapsed_ms(st)),
        )
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
        await finalize_card(
            client, st, answer_element_id=_answer_element_id(),
            footer_fields=self._footer_fields(),
            footer_show_label=self._footer_show_label(),
        )
        st.finalized = True
        _logger.info(
            "composer: card finalized chat=%s card=%s answer_len=%d status_lines=%d",
            st.chat_id, st.card_id, len(st.answer_text or ""), len(st.status_lines),
        )
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

    # ── 页脚配置桥（插件 Config 惰性属性；异常回退 finalize 默认） ──────
    def _footer_fields(self) -> list[list[str]] | None:
        try:
            return self._config.footer_fields  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            return None

    def _footer_show_label(self) -> bool:
        try:
            return bool(self._config.footer_show_label)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            return False


def _answer_element_id() -> str:
    from ..cardkit.elements import ANSWER_ELEMENT_ID

    return ANSWER_ELEMENT_ID


def _turn_elapsed_ms(st: CardTurnState) -> float:
    """turn 已耗时（ms；无起点记录返回 0 = 标题省略 elapsed 段）."""
    ts = getattr(st, "turn_started_at", None)
    if not ts:
        return 0.0
    try:
        return max(0.0, (time.monotonic() - ts) * 1000.0)
    except Exception:  # noqa: BLE001 — 标题装饰性数据，绝不影响主流程
        return 0.0


def _merge_progress_lines(existing: list[str], incoming: list[str]) -> list[str] | None:
    """v2.0.5 进度行合并：幂等 + 状态推进原地取代.

    - 完全同文本重发（accumulate 快照重复部分，含 ``(×N)`` 去重后缀剥除）→ 跳过
    - 思考行累积增长（同轮 💬 文本变长）→ 原地替换最后一行（不膨胀、不计新轮）
    - pending 行（尾部 ``...``）→ 同工具 preview/head/代码块到达时原地取代
      （V1 语义：同一次调用只有一个步骤行）
    - 其余 → 追加（同工具再次调用 = 新步骤）

    返回合并后的新列表；无任何变化返回 None（调用方跳过 cardkit_update）。
    """
    merged = list(existing or [])
    changed = False
    for ln in incoming or []:
        # v2.0.6：剥网关去重后缀（同文本重发变 "... (×N)"，不剥会当新行重复入面板）
        text = strip_dedup_suffix(ln)
        if not text or text in merged:
            continue
        kind, tool, _clean = parse_progress_line(text)
        if kind == "thinking":
            new_body = text[len(_THINKING_PREFIX):].strip()
            replaced = False
            for i in range(len(merged) - 1, -1, -1):
                prev = merged[i]
                if not prev.startswith(_THINKING_PREFIX):
                    break
                old_body = prev[len(_THINKING_PREFIX):].strip()
                if new_body.startswith(old_body) or old_body.startswith(new_body):
                    merged[i] = text
                    replaced = True
                    break
            if replaced:
                changed = True
                continue
        elif kind == "tool" and tool and not text.endswith("..."):
            replaced = False
            for i in range(len(merged) - 1, -1, -1):
                k2, t2, _c2 = parse_progress_line(merged[i])
                if k2 == "tool" and t2 == tool:
                    if merged[i].endswith("..."):
                        merged[i] = text
                        replaced = True
                    break  # 最近同工具行已执行 → 新一次调用，追加
            if replaced:
                changed = True
                continue
        merged.append(text)
        changed = True
    return merged if changed else None


def _panel_title(status_lines: list[str], *, elapsed_ms: float = 0.0) -> str:
    """V1(1.8.1) 同款面板动态标题.

    "agent loop" ·（N 轮）·（M 个工具）·（X.Xs）——各段有数据才拼接：
    轮次数 = 💬 思考行分组数；工具数 = 工具行条数（pending 已被取代不重复计）；
    elapsed = 首帧→当前实测（v2.0.3 曾以「帧无耗时」为由省略，v2.0.5 起由
    composer 本地实测，非网关伪造）。永不抛异常。
    """
    try:
        rounds = tools = 0
        prev_thinking = False
        for ln in status_lines or []:
            try:
                kind, _tool, _c = parse_progress_line(ln)
            except Exception:  # noqa: BLE001
                kind = "unknown"
            if kind == "tool":
                tools += 1
                prev_thinking = False
            elif kind == "thinking":
                if not prev_thinking:
                    rounds += 1
                prev_thinking = True
            else:
                prev_thinking = False
        parts = ["agent loop"]
        if rounds:
            parts.append(f"{rounds} 轮")
        if tools:
            parts.append(f"{tools} 个工具")
        if elapsed_ms >= 100:  # <0.1s 不显示（避免 "0.0s" 噪音）
            from ..cardkit.elements import _format_elapsed

            parts.append(_format_elapsed(elapsed_ms))
        return " · ".join(parts)
    except Exception:  # noqa: BLE001 — 标题永不影响主流程
        return "agent loop"
