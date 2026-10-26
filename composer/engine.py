"""V2 帧重组引擎 — hermes 出站帧 → 飞书 CardKit 流式卡片.

职责：
1. 维护 per-chat 的 ``CardTurnState``（活跃卡片注册表）
2. 消费 ``classify`` 的分类结果，驱动 CardKit 生命周期：
   create → send_card_by_id → stream_element（answer 打字机）→ update（状态区）→ close_streaming
3. 降级保障：任何卡片 API 失败 → per-chat 降级旗标 → 后续帧全部走原生路径，**永不丢消息**

线程/并发模型：v2.0.11 起 handle_send/handle_edit 按 chat 串行化（per-chat
asyncio.Lock）。历史假设“hermes 对单 chat 的出站天然串行”不成立——reasoning
回灌与工具耗时经 ``ensure_future``/``call_soon_threadsafe`` 火忘任务投递，网关
多 lane（进度气泡/思考中继/status 通报/流式消费者）在回合事件密集期亚秒级齐发；
建卡窗口（cardkit_create + send_card_by_id ≈ 0.5-1s 网络挂起）内并发帧各自看到
“无活跃卡”而重复建卡（2026-10-12 14:30 “几点了”一轮 3 卡；10-11 23:13 同族
孤儿卡）。锁内 active_state 复查天然提供双重检查语义；跨 chat 状态互不共享。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..cardkit.progline import split_progress_segments, strip_dedup_suffix
from ..cardkit.seq import call_with_seq_recovery
from .classify import (
    _EMOJI_RE,
    _THINKING_PREFIX,
    CH_FINAL,
    CH_INTERIM,
    CH_PROGRESS,
    CH_STREAM,
    ClassifyResult,
    _is_progress_text,
    parse_progress_line,
)

_logger = logging.getLogger("hermes_lark_streaming.composer")

# v2.0.21（审计 R1）：孤儿卡看门狗静默阈值。120s = 429 冷却 60s 的 2 倍裕度；
# 正常回合工具执行期网关进度气泡/native interim tick 持续重发快照刷新
# last_activity，不会误伤（详见 _watchdog docstring）。
_WATCHDOG_IDLE_SEC = 120.0

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
    # v2.0.8：与 status_lines 对齐的到达时间戳（monotonic）——
    # 思考轮次标题「第 N 轮 · X.Xs」的耗时数据源（V1 对齐）。
    line_times: list[float] = field(default_factory=list)
    # v2.0.8：单工具耗时（V1 对齐「Terminal (2.3s)」）。key=状态行原文
    # （merge 后已剥 (×N)），value=格式化标签；post_tool_call 官方钩子数据。
    tool_durations: dict[str, str] = field(default_factory=dict)
    # 钩子先于文本帧到达时的排队区（tool_name → 待认领标签，cap 4）
    pending_tool_durations: dict[str, list[str]] = field(default_factory=dict)
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
        # v2.0.11 P0：per-chat 帧处理串行化锁（见模块头并发模型说明）。
        # 锁随 chat 惰性创建；只在 clear() 全量清空，forget() 不清——在途持有者
        # 与后来者必须共用同一把锁才有互斥意义（chat 级锁对象内存可忽略）。
        self._chat_locks: dict[str, asyncio.Lock] = {}
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
        """v2.0.9 P0-2 重写：旧实现的运算符优先级使条件实际为
        ``(finalized and idle>60) or (created_at>ttl)`` —— 活跃回合建卡满
        TTL 即被弹出（长回合断卡：answer edit 落到内置适配器打卡片消息必败、
        进度帧新建第二张卡、旧卡跑马灯永挂；gateway19 生产事故同族）。

        新语义（空闲回收，主流程修复）：
        - 已定稿：60s 冷却后回收（保留 ``_by_message`` 反查窗口）
        - 未定稿（活跃回合/占位）：仅 ``last_activity`` 空闲超 TTL 才回收
          —— 活跃回合每帧都刷新 last_activity，长回合永不误杀；空闲超 TTL
          的状态只能是已中断的孤儿回合（回收同 TTL 语义，非新增兕底）。
        """
        ttl = self._ttl_sec()
        now = time.monotonic()
        stale: list[str] = []
        for cid, st in self._states.items():
            idle = now - st.last_activity
            if st.finalized:
                if idle > 60:
                    stale.append(cid)
            elif idle > ttl:
                stale.append(cid)
        for cid in stale:
            st = self._states.pop(cid, None)
            if st and st.card_message_id:
                self._by_message.pop(st.card_message_id, None)

    def _chat_lock(self, chat_id: str) -> asyncio.Lock:
        lock = self._chat_locks.get(chat_id)
        if lock is None:
            lock = self._chat_locks[chat_id] = asyncio.Lock()
        return lock

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
        # v2.0.11 P0：同 chat 帧串行化——建卡/定稿的挂起窗口内，后续帧在锁上
        # 排队而非并发进建卡分支；锁内 active_state 复查即双重检查。
        async with self._chat_lock(chat_id):
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
    def submit_progress_from_thread(
        self, chat_id: str, raw_line: str, *, final: bool = False,
    ) -> bool:
        """线程安全地把一行进度文本（如 reasoning_feed 的 💬 行）投递进面板.

        仅活跃卡片期消费：未建卡（思考先于 answer 首帧）由 handle_send 的
        暂存分支处理；已定稿/已降级则丢弃（终态卡片不再变化）。返回是否受理。

        v2.0.9 P1-3：``final=True`` 标记回合流结束的一次性尾 flush
        （reasoning_feed.on_stream_end）。入队守卫与执行时复查双保险——
        agent 层 on_stream_end 先于网关 finalize edit，入队时回合未终态、
        执行时可能已终态（竞态窗）；不复查会在已封卡回合后建带跑马灯
        幽灵卡。final flush 仅在活跃卡片存在时进面板，其余一律丢弃
        （回合无卡时思考无处归属，暂存会错误并入下一回合面板）。
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return False
        st = self._states.get(chat_id)
        if st is not None and (st.finalized or st.degraded):
            return False
        if final and st is None:
            return False
        try:
            loop.call_soon_threadsafe(self._spawn_progress, chat_id, raw_line, final)
            return True
        except RuntimeError:
            return False

    def _spawn_progress(self, chat_id: str, raw_line: str, final: bool = False) -> None:
        asyncio.ensure_future(self._feed_progress(chat_id, raw_line, final=final))

    # ── 工具耗时回灌（v2.0.8，post_tool_call 官方钩子线程 → 事件循环）─────
    def submit_tool_duration_from_thread(self, chat_id: str, tool_name: str, duration_ms: float) -> bool:
        """线程安全地把单工具耗时（官方钩子 duration_ms）挂到面板工具行.

        V1 的「Terminal (2.3s)」由工具事件实测；V2 零补丁架构下数据源是
        hermes 官方 ``post_tool_call`` 钩子（payload 自带 duration_ms）。
        已定稿/已降级丢弃；行未到达时入 pending 队列由 _on_progress 认领。
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return False
        if not tool_name or duration_ms <= 0:
            return False
        st = self._states.get(chat_id)
        if st is not None and (st.finalized or st.degraded):
            return False
        if st is None:
            # v2.0.9：无活跃状态（含占位）不受理——耗时行只能挂在已存在的
            # 卡片行上；行未到达时由 pending 队列承接（active_state 建卡后认领）。
            return False
        try:
            loop.call_soon_threadsafe(
                self._spawn_tool_duration, chat_id, str(tool_name), float(duration_ms))
            return True
        except RuntimeError:
            return False

    def _spawn_tool_duration(self, chat_id: str, tool_name: str, duration_ms: float) -> None:
        asyncio.ensure_future(self._feed_tool_duration(chat_id, tool_name, duration_ms))

    async def _feed_tool_duration(self, chat_id: str, tool_name: str, duration_ms: float) -> None:
        try:
            st = self._states.get(chat_id)
            if st is None or st.finalized or st.degraded:
                return
            from ..cardkit.elements import _format_duration_label

            label = _format_duration_label(duration_ms)
            # v2.0.9 P2-8：只认「尾数第一个同工具行」（调用序配对的最近近似）。
            # 旧实现向前反查任意未挂行：更早调用的钩子丢失时，本钩子的耗时
            # 会误挂到旧行。现在：最近同工具行已挂 → 本钩子属更新的调用
            # （其行未到达）→ pending 队列；更早未挂行保持缺失（钩子丢失，
            # 诚实不伪造）。残余窗口：丢钩子的下一调用行未插入且更旧行存在
            # （写入 CHANGELOG 已知限制）。
            for i in range(len(st.status_lines) - 1, -1, -1):
                line = st.status_lines[i]
                kind, tool, _c = parse_progress_line(line)
                if kind != "tool" or tool != tool_name:
                    continue
                key = strip_dedup_suffix(line)
                if key in st.tool_durations:
                    break  # 最近同工具行已挂 → 属更新调用，行未到 → pending
                st.tool_durations[key] = label
                await self._patch_status(st)
                return
            queue = st.pending_tool_durations.setdefault(tool_name, [])
            if len(queue) < 4:
                queue.append(label)
        except Exception:  # 耗时是装饰性数据，绝不影响主流程
            _logger.debug("composer: tool duration feed failed chat=%s", chat_id, exc_info=True)

    def _attach_pending_durations(self, st: CardTurnState, old: list[str], new: list[str]) -> None:
        """新追加的工具行认领 pending 队列里的耗时（钩子先到场景）."""
        if not st.pending_tool_durations:
            return
        old_set = set(old)
        for text in new:
            if text in old_set:
                continue
            kind, tool, _c = parse_progress_line(text)
            if kind != "tool" or not tool:
                continue
            queue = st.pending_tool_durations.get(tool)
            if queue:
                st.tool_durations[strip_dedup_suffix(text)] = queue.pop(0)
                if not queue:
                    st.pending_tool_durations.pop(tool, None)

    def _migrate_tool_durations(
        self, st: CardTurnState, old: list[str], merged: list[str],
    ) -> None:
        """pending→preview 原地取代时迁移耗时标签（v2.0.9 P2-8）.

        ``tool_durations`` 的 key 是行文本：pending 行（``...``）挂上耗时后，
        同一调用的 preview 行原地取代它 → key 失联，用户看到该工具无耗时。
        同位置、同工具、旧行 pending → 新行非 pending 时迁移标签（key 对齐
        渲染层的 ``strip_dedup_suffix(行)`` 查找）。迁移失败静默保留旧 key
        （孤儿 key 渲染层查不到，无害）。
        """
        if not st.tool_durations:
            return
        for i in range(min(len(old), len(merged))):
            if old[i] == merged[i]:
                continue
            old_key = strip_dedup_suffix(old[i])
            label = st.tool_durations.get(old_key)
            if not label or not old_key.endswith("..."):
                continue  # 只处理 pending 行的取代
            new_key = strip_dedup_suffix(merged[i])
            if new_key in st.tool_durations:
                continue
            _k_old, tool_old, _c = parse_progress_line(old_key)
            _k_new, tool_new, _c2 = parse_progress_line(new_key)
            if tool_old and tool_old == tool_new:
                st.tool_durations[new_key] = st.tool_durations.pop(old_key)

    async def _feed_progress(self, chat_id: str, raw_line: str, *, final: bool = False) -> None:
        cls = ClassifyResult(CH_PROGRESS, kind="thinking", raw_content=raw_line)
        if final:
            # v2.0.9 P1-3：执行时复查（入队→执行之间回合可能已终态）。
            # 终态/无卡一律丢弃：不占位、不建卡（幽灵卡防线），面板已封不补发。
            st = self._states.get(chat_id)
            if st is None or st.finalized or st.degraded:
                _logger.debug(
                    "composer: final reasoning flush dropped (turn closed) chat=%s", chat_id)
                return
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
        # v2.0.11 P0：edit 与 send 共用 per-chat 锁——定稿与下一回合建卡复用
        # 同一状态对象，无锁并发时 finalize_card 的挂起段可能与 _create_card 的
        # 状态重置交错（footer 落错卡）。
        async with self._chat_lock(chat_id):
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

    # ── native streaming 帧通道（v2.0.13，Task 18 P1-B）──────────────────────
    async def handle_stream_frame(
        self,
        chat_id: str,
        text: str,
        *,
        finalize: bool = False,
        turn_id: str | None = None,
    ) -> bool:
        """hermes native streaming 帧（adapter.send_stream_frame 委托入口）.

        官方契约（gateway/stream_consumer_transport.py）：
        - 首帧 ``text=""``（种子帧）＝ typing 指示；随后每帧是**全量快照**
          （cumulative，``_native_push`` push 累积文本）——与 CH_STREAM 的分段
          追加语义不同，直接赋值 ``answer_text``；
        - ``finalize=True`` 仅回合终态：native 路径吞掉 segment-break finalize
          （transport:389 ``finalize = finalize and is_turn_final``）且 native
          ＝ cumulative transport（``_end_segment`` 直接 return）→ 工具边界
          不再封卡，单卡全回合（多卡片同族缺陷的通道级根治）；
        - interim tick 帧为复合形态 ``"{answer}\n\n---\n{工具进度行}"``
          （stream_consumer.py::_compose_frame_content，官方在真实 delta 到达
          时清空 overlay :290-291）——工具行拆出并入面板（保持本平台状态面板
          特色，不进正文），正文快照照常；
        - 失败（含任何异常）→ 返回 False → 官方 consumer 禁用 native 并回落
          edit/send 现有链路（降级红线：帧永不丢）。

        返回 True＝帧已消费；False＝本帧未消费，调用方回落。
        """
        self._loop = asyncio.get_running_loop()
        async with self._chat_lock(chat_id):
            self._gc()
            try:
                answer, progress = _split_native_composite(text)
                if progress:
                    st_now = self.active_state(chat_id)
                    if st_now is not None:
                        await self._absorb_status_lines(st_now, progress)
                    else:
                        # 卡未建（进度行早于任何答案帧）：暂存，建卡时并入首卡
                        # 面板（与 _on_progress 占位同语义，但不提前建卡——种子/
                        # 答案帧随即到达，建卡由答案帧驱动）。
                        placeholder = self._states.setdefault(
                            chat_id, CardTurnState(chat_id=chat_id))
                        if placeholder.turn_started_at is None:
                            placeholder.turn_started_at = time.monotonic()
                        placeholder.pending_status.extend(progress)
                        placeholder.last_activity = time.monotonic()
                if finalize:
                    st = self.active_state(chat_id)
                    if st is None:
                        # 终帧无活跃卡（上轮已封/状态被清理）：建卡再封——
                        # 快照即终值，内容永不丢红线。
                        await self._create_card(chat_id, answer)
                        st = self._states.get(chat_id)
                        if st is None:  # pragma: no cover — create 失败已抛异常
                            return False
                    await self._finalize(st, answer)
                    return True
                st = self.active_state(chat_id)
                if st is None:
                    # 种子帧/首内容帧：建卡（暂存行并入首卡面板；answer 空则
                    # 只建骨架不写 answer 元素——_create_card 内已处理）。
                    await self._create_card(chat_id, answer)
                else:
                    st.answer_text = answer
                    await self._stream_answer(st)
                return True
            except Exception as exc:  # noqa: BLE001 — 降级红线：native 帧失败回落
                self._degrade(chat_id, "stream_frame", exc)
                return False

    async def _absorb_status_lines(self, st: CardTurnState, lines: list[str]) -> None:
        """进度行合并进活跃面板（幂等/原地取代/思考累积，v2.0.5 合并器）.

        v2.0.13 从 ``_on_progress`` 有卡分支抽出共用：native 复合帧的工具行
        与网关进度气泡行走同一套合并/耗时认领/行时戳逻辑。
        """
        fresh = [ln for ln in lines if not _is_answer_duplicate(ln, st.answer_text)]
        if not fresh:
            return
        merged = _merge_progress_lines(st.status_lines, fresh)
        if merged is None:
            return
        self._migrate_tool_durations(st, st.status_lines, merged)
        new_times = _reconcile_line_times(st.status_lines, st.line_times, merged)
        self._attach_pending_durations(st, st.status_lines, merged)
        st.status_lines = merged[-12:]
        st.line_times = new_times[-12:]
        await self._patch_status(st)

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
            status_durations=dict(st.tool_durations),
            status_times=[time.monotonic()] * len(pending),
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
        st.line_times = [time.monotonic()] * len(pending)
        # v2.0.9 P2-8 伴生：耗时表必须随回合重置——残留 key 是上一回合的
        # 行文本，同命令重跑时新行会错误继承旧耗时（跨回合数据污染）。
        st.tool_durations.clear()
        st.pending_tool_durations.clear()
        st.pending_status.clear()
        self._states[chat_id] = st
        self._by_message[st.card_message_id] = chat_id
        st.answer_text = content or ""
        st.seq += 1
        if st.answer_text:
            await call_with_seq_recovery(
                st,
                lambda seq: client.cardkit_stream_element(
                    card_id, _answer_element_id(), st.answer_text, sequence=seq),
                what="create:answer-seed",
            )
        _logger.info(
            "composer: card created chat=%s card=%s msg=%s status_lines=%d",
            chat_id, card_id, st.card_message_id, len(pending),
        )
        # v2.0.21（审计 R1）：孤儿卡看门狗（跑马灯永挂根治，见 _watchdog）。
        self._spawn_watchdog(chat_id)
        return SendResult(success=True, message_id=st.card_message_id)

    async def _patch_answer(self, st: CardTurnState, content: str) -> SendResult:
        st.answer_text = content if content is not None else st.answer_text
        await self._stream_answer(st)
        return SendResult(success=True, message_id=st.card_message_id)

    async def _stream_answer(self, st: CardTurnState) -> None:
        # v2.0.17：空 content 守卫——飞书 stream_element 的 content 必填非空
        # （s152 真飞书探针 2026-10-14：content="" → 99992402 field validation
        # failed；未闭合 markdown/围栏/分隔线等其余 9 种形态全部合法）。native
        # 复合帧「answer 空 + 进度行非空」是工具边界后的固有低频形态，空串
        # 直发 degrade 并连锁拆卡（2026-10-14 14:37 生产三卡事故，v2.0.11 家族
        # 同症状新根因）。cumulative 快照语义下空 = 无新内容，跳过 API 无损。
        if not (st.answer_text or "").strip():
            return
        client = self._get_client()
        if client is None:
            raise RuntimeError("card client unavailable")
        st.seq += 1
        st.last_activity = time.monotonic()
        await call_with_seq_recovery(
            st,
            lambda seq: client.cardkit_stream_element(
                st.card_id, _answer_element_id(), st.answer_text, sequence=seq),
            what="stream:answer",
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
            # v2.0.9 P0-2 伴生：占位状态同样按 last_activity 空闲回收——
            # 建卡失败期的持续帧必须续命，否则 TTL 后暂存被 GC 误清。
            placeholder.last_activity = time.monotonic()
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
        before = len(st.status_lines)
        await self._absorb_status_lines(st, lines)
        if len(st.status_lines) == before:
            # 无新增/无变化：不触发 cardkit_update（API 经济性；V2.0.0 同语义）
            return SendResult(success=True)
        # 同上：不回传 message_id，网关进度气泡循环会继续 _send_progress_text
        # （全量快照）而非 edit —— 面板更新由本引擎的 cardkit_update 驱动。
        return SendResult(success=True)

    async def _on_interim(self, chat_id: str, content: str, cls: ClassifyResult) -> SendResult | None:
        st = self.active_state(chat_id)
        text = (content or "").strip()
        if st is None or not text:
            return None  # 无卡片：原生（含 💾/⏳ 后台通知——永不建卡，跑马灯根因修复）
        # v2.0.8：非 emoji 播报（模型可见间歇文本 commentary，如「SpaceX买」碎片）
        # 按 V1 语义归入思考轮次（灰色 notation 行 + 合并/幂等/增长取代）；
        # emoji 通报（⏳/💾 等）保持原样独立行。
        if not _EMOJI_RE.match(text):
            text = f"{_THINKING_PREFIX} {text}"
        lines = split_progress_segments(text) or ([text] if text else [])
        merged = _merge_progress_lines(st.status_lines, lines)
        if merged is None:
            return SendResult(success=True, message_id=st.card_message_id)
        new_times = _reconcile_line_times(st.status_lines, st.line_times, merged)
        st.status_lines = merged[-12:]
        st.line_times = new_times[-12:]
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
            status_durations=dict(st.tool_durations),
            status_times=list(st.line_times),
        )
        st.seq += 1
        st.last_activity = time.monotonic()
        await call_with_seq_recovery(
            st,
            lambda seq: client.cardkit_update(st.card_id, card, sequence=seq),
            what="update:status",
        )

    async def _finalize(self, st: CardTurnState, content: str) -> SendResult:
        from .finalize import finalize_card

        client = self._get_client()
        if client is None:
            raise RuntimeError("card client unavailable")
        # v2.0.9 P1-4：幂等守卫先行——网关重试/重复 finalize 事件会重复插
        # 页脚（三步定稿非事务性）。已定稿直接短路返回成功。
        if st.finalized:
            return SendResult(success=True, message_id=st.card_message_id)
        st.finalized = True
        if content:
            st.answer_text = content
        st.last_activity = time.monotonic()
        await finalize_card(
            client, st, answer_element_id=_answer_element_id(),
            footer_fields=self._footer_fields(),
            footer_show_label=self._footer_show_label(),
            panel_title=_panel_title(st.status_lines, elapsed_ms=_turn_elapsed_ms(st)),
            panel_expanded=self._panel_expanded(),
        )
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

    # ── 孤儿卡看门狗（v2.0.21，审计 R1）───────────────────────────────────
    def _spawn_watchdog(self, chat_id: str) -> None:
        """建卡后武装看门狗定时器（见 _watchdog_due；重复武装无害——复查挡）.

        实现说明：用 ``loop.call_later`` 而非 ``ensure_future(task+sleep)``
        ——task 会被 ``asyncio.gather(*all_tasks())`` 类调用方（如测试
        _settle / 生产中的任务等待）捕获，120s 长睡 task 既拖慢等待者又
        可能永不到期（time.monotonic 被接管的环境）；timer 句柄不在
        all_tasks() 中，到期才建 task，任意时刻每 chat 至多一个 pending
        timer，loop 关闭时自动丢弃。
        """
        self._arm_watchdog(chat_id)

    def _arm_watchdog(self, chat_id: str) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_later(_WATCHDOG_IDLE_SEC, self._watchdog_due, chat_id)
        except RuntimeError:  # loop 已停转（跨线程窗口）
            pass
        except Exception:
            _logger.debug("composer: watchdog arm failed chat=%s", chat_id, exc_info=True)

    def _watchdog_due(self, chat_id: str) -> None:
        """定时器到期回调（loop 线程）：静默达标 → 落定；仍活跃 → 重新武装."""
        try:
            st = self._states.get(chat_id)
            if st is None or st.finalized or st.degraded:
                return
            if time.monotonic() - st.last_activity >= _WATCHDOG_IDLE_SEC:
                asyncio.ensure_future(self._watchdog_seal(chat_id))
            else:
                # 回合仍活跃（tick/快照帧持续刷新 last_activity）——再等一个周期
                self._arm_watchdog(chat_id)
        except Exception:
            _logger.debug("composer: watchdog due failed chat=%s", chat_id, exc_info=True)

    async def _watchdog_seal(self, chat_id: str) -> None:
        """静默孤儿卡优雅落定（跑马灯永挂根治，v2.0.21 审计 R1）.

        背景（2026-10-16 全生命周期审计取证）：send_message 工具族主动投递
        emoji 开头 ≤300 字短通知（hermes tools/send_message_tool.py:549-556
        metadata 无 notify 标记）→ _upgrade_plain_to_progress 升格
        CH_PROGRESS → _on_progress 无活跃卡 early-create 占位卡（v2.0.4 s70
        回流）——该卡永远等不到 answer/finalize 帧（回合外投递无流式回合），
        spinner 永挂直到用户删卡。

        不误伤正常回合：工具执行期网关进度气泡（accumulate 模式 edit_interval
        周期快照）/native interim tick 持续重发刷新 last_activity；429 冷却
        60s < 阈值；到期回调静默不达标时重新武装。

        触发（静默 ≥120s 且未终态）：复用 finalize_card 优雅落定（footer+
        关流式；空 answer 合法终态），状态置 degraded+finalized——后续帧若
        真到来：native 路径 active_state None → 建新卡承载（内容永不丢），
        edit 灰度路径 super().edit_message 官方原生 PATCH 兑底。装饰性
        API 失败仅 debug（卡片留在流式态，不重试不上抛）。
        """
        try:
            async with self._chat_lock(chat_id):
                st = self._states.get(chat_id)
                if st is None or st.finalized or st.degraded:
                    return
                if time.monotonic() - st.last_activity < _WATCHDOG_IDLE_SEC:
                    return  # 双保险：ensure_future 窗口内回合恢复活跃
                st.degraded = True
                st.finalized = True
                client = self._get_client()
                if client is None:
                    return
                try:
                    from .finalize import finalize_card

                    await finalize_card(
                        client, st, answer_element_id=_answer_element_id(),
                        footer_fields=self._footer_fields(),
                        footer_show_label=self._footer_show_label(),
                        panel_title=_panel_title(st.status_lines, elapsed_ms=_turn_elapsed_ms(st)),
                        panel_expanded=self._panel_expanded(),
                    )
                except Exception:
                    _logger.debug(
                        "composer: watchdog seal (api) skipped card=%s",
                        str(st.card_id)[:12], exc_info=True,
                    )
                _logger.warning(
                    "composer: watchdog sealed idle card chat=%s card=%s idle>=%.0fs "
                    "(no finalize frame — likely out-of-turn notice)",
                    chat_id[:12], str(st.card_id)[:12], _WATCHDOG_IDLE_SEC,
                )
        except Exception:
            _logger.debug("composer: watchdog failed chat=%s", chat_id, exc_info=True)

    # ── 清理 ────────────────────────────────────────────────────────────
    def forget(self, chat_id: str) -> None:
        st = self._states.pop(chat_id, None)
        if st and st.card_message_id:
            self._by_message.pop(st.card_message_id, None)

    def clear(self) -> None:
        self._states.clear()
        self._by_message.clear()
        self._chat_locks.clear()

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

    def _panel_expanded(self) -> bool:
        """面板展开态（v2.0.16 面板收口重建用；与流式骨架同源同默认）。"""
        try:
            return bool(self._config.streaming_panel_expanded)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            return True


def _answer_element_id() -> str:
    from ..cardkit.elements import ANSWER_ELEMENT_ID

    return ANSWER_ELEMENT_ID


# hermes native 复合帧分隔符（stream_consumer.py:_compose_frame_content:253
# ``"\n\n---\n".join((accumulated, progress))``；任一段为空时 join 结果不含分隔符）。
_NATIVE_COMPOSITE_SEP = "\n\n---\n"


def _split_native_composite(text: str) -> tuple[str, list[str]]:
    """拆 hermes native 复合帧：``{answer}`` + 分隔符 + ``{工具进度行}``.

    官方复合格式（stream_consumer.py:_compose_frame_content:251-255）：
    interim tick（工具运行中）帧体为 ``answer + "\n\n---\n" + progress``；
    progress 是 ``"\n".join(_tool_progress_lines)``（项可为多行围栏块）。

    拆分策略（保守优先，误拆自愈）：
    - 无分隔符：整帧是纯正文；但 ``answer`` 为空的 interim 帧只有 progress
      （join 的空段过滤）——整帧命中进度启发式时视为进度行（emoji 开头/💬/
      围栏块 ≤600，极早期 delta 的误判由下一快照帧自愈）。
    - 有分隔符：只拆**最后一个**分隔符（正文自身可含 markdown 分隔线），
      拆出部分经 ``split_progress_segments``（围栏块保持一体）后**逐段全部**
      命中进度启发式才拆，否则整体视为正文。误拆由下一快照帧覆盖自愈
      （官方 :290-291 真实 delta 到达即清空 overlay）。

    永不抛异常；返回 ``(answer_text, progress_lines)``。
    """
    try:
        text = (text or "").strip()
        if not text:
            return "", []
        idx = text.rfind(_NATIVE_COMPOSITE_SEP)
        if idx < 0:
            if _is_progress_text(text):
                return "", split_progress_segments(text) or [text]
            return text, []
        head = text[:idx].strip()
        tail = text[idx + len(_NATIVE_COMPOSITE_SEP):].strip()
        if not head or not tail:
            return text, []
        segments = split_progress_segments(tail) or ([tail] if tail else [])
        if segments and all(_is_progress_text(seg) for seg in segments):
            return head, segments
        return text, []
    except Exception:  # noqa: BLE001 — 拆分纯装饰，失败整体按正文
        return (text or "", [])


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


def _is_answer_duplicate(line: str, answer_text: str) -> bool:
    """💬 思考行是否为已流答案的复读（v2.0.8）.

    网关 ``_relay_thinking``（agent/turn_response_intake.py）把每次模型响应的
    **可见正文**前 500 字都中继成 ``💬 {text}`` 进度行——最终答案也不例外。
    答案已完整出现在卡片 answer 区域；V1 从不把 final answer 展示进面板
    （final 走 stream 通道），本过滤器对齐该语义。

    仅做前缀匹配（归一化空白后取前 60 字符、≥40 才判）：隐藏推理（reasoning
    内容与答案正文不同源）绝不误伤；碎片（<40 字符）保留。永不抛异常。
    """
    try:
        text = (line or "").strip().removeprefix(_THINKING_PREFIX).strip()
        body = " ".join(text.split())[:60]
        if len(body) < 40:
            return False
        answer = " ".join((answer_text or "").split())
        if not answer:
            return False
        return answer[:len(body)] == body
    except Exception:  # noqa: BLE001 — 过滤纯装饰，失败保留原行
        return False


def _reconcile_line_times(
    old: list[str], old_times: list[float], new: list[str],
) -> list[float]:
    """合并后的新行列表 → 对齐的到达时间戳（v2.0.8，思考轮次耗时数据源）.

    规则（对齐 _merge_progress_lines 的三种变更形态）：
    - 同位置同文本 → 继承原时间（未变化）
    - 思考行增长原地取代 → 继承被取代行的起点（轮次耗时从轮次首行起算，
      不随每次 flush 重置）
    - 其余（新增/移动）→ now

    永不抛异常；输出长度恒等于 ``len(new)``。
    """
    try:
        now = time.monotonic()
        if not new:
            return []
        times: list[float] = []
        for i, text in enumerate(new):
            if i < len(old) and i < len(old_times) and old[i] == text:
                times.append(old_times[i])
                continue
            inherited: float | None = None
            # 思考增长取代：向前找同轮前缀行继承起点（与合并器同款回走语义）
            if text.startswith(_THINKING_PREFIX):
                body = text[len(_THINKING_PREFIX):].strip()
                for j in range(min(len(old), len(old_times)) - 1, -1, -1):
                    prev = old[j]
                    if not prev.startswith(_THINKING_PREFIX):
                        break  # 思考轮连续段结束（合并器同款边界）
                    old_body = prev[len(_THINKING_PREFIX):].strip()
                    if body.startswith(old_body) or old_body.startswith(body):
                        inherited = old_times[j]
                        break
            if inherited is None:
                for j in range(min(len(old), len(old_times))):
                    if old[j] == text:
                        inherited = old_times[j]
                        break
            times.append(inherited if inherited is not None else now)
        return times
    except Exception:  # noqa: BLE001 — 耗时纯装饰，失败退化为全 now
        now = time.monotonic()
        return [now] * len(new)


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
