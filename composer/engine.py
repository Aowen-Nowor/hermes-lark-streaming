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
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..cardkit.progline import split_progress_segments, strip_dedup_suffix
from ..cardkit.seq import call_with_seq_recovery
from .classify import (
    _EMOJI_RE,
    _PLATFORM_NOTICE_RES,
    _THINKING_PREFIX,
    CH_FINAL,
    CH_INTERIM,
    CH_PROGRESS,
    CH_STREAM,
    ClassifyResult,
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
    # v2.0.24 面板懒渲染：当前卡片 JSON 上是否存在 agent loop 面板。
    # 真值源 = 每次 build_card 产物实测（_card_has_panel），不靠推断——
    # 建卡时无状态行则无面板（用户反馈：纯 answer 回合空面板体验差），
    # 首个状态行到达时 _patch_status 全量更新补插。finalize 空面板删除
    # 分支据此跳过对从未有过面板的卡发起 delete_elements。
    panel_on_card: bool = False
    # ── v2.0.26 clarify 暂停/恢复（同卡续流）─────────────────────────
    # hermes 边界语义（run_turn_runner.py:1404）：clarify 交互前强制关闭当前
    # 流（finalize 帧），回答后重开新流。边界 finalize 与真回合终态到达本
    # 引擎时参数完全一致（text, finalize=True）→ finalize 帧先进暂停窗口，
    # 窗口内 send_clarify 到达即确认为边界（卡保持流式态等回调），到期无
    # send_clarify → _pause_expire 真落定（普通回合语义）。
    pause_pending: bool = False       # finalize 帧已到、等待 send_clarify 确认的窗口
    paused: bool = False              # clarify 等待中（回调后 resume 续写同一张卡）
    resume_base: str = ""             # 暂停时正文快照（reopen 后新段快照的拼接基线）
    turn_id: str = ""                 # hermes 回合标识（transport 每帧携带；防跨回合串卡）


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
        clarify_suspend_sec: float | None = None,
    ) -> None:
        self._get_client = get_client          # () -> FeishuClient | None
        self._build_card = build_card          # build_streaming_card_v2(**kwargs)
        self._build_final_card = build_final_card
        self._config = config
        # v2.0.26：clarify 暂停窗口（秒）。显式参数 > Config.clarify_suspend_sec
        # > 0（关闭——测试/无 config 环境走 v2.0.25 现状语义，存量断言零影响）。
        # 生产 adapter 传插件 Config（默认 1.2s）→ 边界 finalize → send_clarify
        # 的实测间隔 <200ms（2026-10-27 13:20 用户回合日志），1.2s 裕度充分。
        if clarify_suspend_sec is not None:
            self._clarify_suspend_sec = max(0.0, float(clarify_suspend_sec))
        else:
            try:
                self._clarify_suspend_sec = max(
                    0.0, float(config.clarify_suspend_sec))  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001 — 无 config/属性缺失 → 关闭（现状语义）
                self._clarify_suspend_sec = 0.0
        # v2.0.26：per-chat 暂停窗口落定定时器（loop.call_later，与看门狗同款
        # 架构——句柄不在 all_tasks() 中，到期才建 task）。
        self._pause_timers: dict[str, asyncio.TimerHandle] = {}
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
            elif st.paused or st.pause_pending:
                # v2.0.26：clarify 暂停/窗口期豁免——hermes clarify 硬超时
                # （clarify_gateway.resolve_clarify_timeout 默认 3600s）远大于
                # TTL；回调/超时后必有续流帧或真 finalize 落定。此处回收会把
                # 暂停卡状态蒸发，回调后续流退化为建新卡（暂停语义失效）。
                continue
            elif idle > ttl:
                stale.append(cid)
        for cid in stale:
            st = self._states.pop(cid, None)
            if st and st.card_message_id:
                self._by_message.pop(st.card_message_id, None)
            self._cancel_pause(cid)

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
                    return await self._on_stream(chat_id, content, reply_to=reply_to)
                if cls.channel == CH_PROGRESS:
                    return await self._on_progress(chat_id, cls, reply_to=reply_to)
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
        reply_to: str | None = None,
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

        v2.0.26 clarify 暂停/恢复（同卡续流）：hermes 在 clarify 交互前强制
        finalize 当前流（run_turn_runner.py:1404，WeCom 气泡模型语义；实测
        2026-10-27 13:20 回合日志），边界 finalize 与真回合终态到达时参数
        完全一致（text, finalize=True）——唯一可靠区分是边界后必紧跟
        ``send_clarify``（同一事件循环 <200ms：agent 线程 boundary future
        resolve 后立即 schedule）。因此 finalize 帧先进「暂停窗口」
        （``clarify_suspend_sec``，默认 1.2s，0=关闭走 v2.0.25 语义）：
        - 窗口内 ``suspend_for_clarify`` 到达 → 确认为边界：卡保持流式态 +
          面板暂停行（clarify_wait），等待用户点击 clarify 交互卡回调；
        - 回调后 hermes Eager re-seed/lazy re-seed 重开流（seed 空帧 → delta
          「新段内累积快照」）→ paused 分支解除暂停、``_merge_resumed_answer``
          段拼接，**同一张卡**续流至真回合终态落定（用户验收：一张卡贯穿）；
        - 窗口到期无 send_clarify → ``_pause_expire`` 真落定（普通回合仅
          footer/关流式延迟窗口时长）。
        暂停/窗口期豁免看门狗 seal 与 GC idle 回收（hermes clarify 硬超时
        3600s 保证终态必达）；turn_id 变化的帧对暂停卡先真落定再建新卡
        （用户不等澄清直接发新消息的防串）。

        返回 True＝帧已消费；False＝本帧未消费，调用方回落。
        """
        self._loop = asyncio.get_running_loop()
        async with self._chat_lock(chat_id):
            self._gc()
            try:
                # v2.0.26：turn_id 防串——新回合帧到达而旧卡未落定（含 clarify
                # 暂停卡：用户不等回答直接发新消息）→ 旧卡立即真落定，新回合
                # 语义交由下方主流程（建新卡）。turn_id 缺失（mock/直调）跳过。
                st0 = self._states.get(chat_id)
                if (st0 is not None and not st0.finalized and turn_id
                        and st0.turn_id and turn_id != st0.turn_id):
                    self._cancel_pause(chat_id)
                    st0.pause_pending = False
                    st0.paused = False
                    await self._finalize(st0, st0.answer_text)
                    _logger.info(
                        "composer: turn boundary seal (chat=%s old=%s new=%s)",
                        chat_id, st0.turn_id[:8], turn_id[:8],
                    )
                # v2.0.26：finalize 帧先行分流——clarify 边界 finalize 与真回合
                # 终态参数一致（text, finalize=True），唯一可靠区分 = 边界后必紧跟
                # send_clarify（同一事件循环 <200ms，见 handle_stream_frame 类
                # docstring v2.0.26 段）。暂停窗口内不 split/不落正文面板/
                # 不关流式：文本暂存 answer_text（suspend 时占位判定清空 /
                # expire 时 _finalize 终值 patch），两分支由
                # suspend_for_clarify / _pause_expire 承接。
                if finalize and self._clarify_suspend_sec > 0:
                    st = self.active_state(chat_id)
                    if st is not None:
                        if turn_id and not st.turn_id:
                            st.turn_id = turn_id
                        if text:
                            st.answer_text = text
                        st.pause_pending = True
                        st.last_activity = time.monotonic()
                        self._arm_pause_expire(chat_id)
                        return True
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
                        await self._create_card(chat_id, answer, reply_to=reply_to)
                        st = self._states.get(chat_id)
                        if st is None:  # pragma: no cover — create 失败已抛异常
                            return False
                    if turn_id and not st.turn_id:
                        st.turn_id = turn_id
                    await self._finalize(st, answer)
                    return True
                st = self.active_state(chat_id)
                if st is None:
                    # 种子帧/首内容帧：建卡（暂存行并入首卡面板；answer 空则
                    # 只建骨架不写 answer 元素——_create_card 内已处理）。
                    await self._create_card(chat_id, answer, reply_to=reply_to)
                    st = self._states.get(chat_id)
                    if st is not None and turn_id:
                        st.turn_id = turn_id
                    return True
                if turn_id and not st.turn_id:
                    st.turn_id = turn_id
                # v2.0.26：clarify 回调后重开帧到达——同一张卡恢复续流。
                # hermes 边界处理时 _reset_segment_state 清空 accumulated，
                # reopen 后 delta 是「新段内累积快照」（不含暂停前内容）→
                # 与 resume_base 按段拼接（同一回合多次 clarify 递进累积）。
                if st.paused:
                    st.paused = False
                    _drop_clarify_wait_row(st)
                    if (answer or "").strip():
                        st.answer_text = _merge_resumed_answer(st.resume_base, answer)
                        st.resume_base = st.answer_text
                        await self._stream_answer(st)
                    else:
                        # 种子帧（空快照）：仅解除暂停 + 面板移除暂停行
                        # （全量更新；后续 delta 局部更新正文）。
                        await self._patch_status(st)
                    self._arm_watchdog(chat_id)  # 暂停期让位，恢复后重新武装
                    return True
                if st.resume_base:
                    # v2.0.26：clarify resume 后的新段快照——hermes reopen 后
                    # delta 是「新段内累积」（_reset_segment_state 清空 accumulated，
                    # 不含暂停前内容），与 resume_base 段拼接；递进更新 resume_base
                    # （同回合多次 clarify 递进累积；真 final 帧为 authoritative
                    # 全文，走暂停窗口路径直接覆盖，无拼接问题）。
                    st.answer_text = _merge_resumed_answer(st.resume_base, answer)
                    st.resume_base = st.answer_text
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

    async def _on_stream(self, chat_id: str, content: str, *, reply_to: str | None = None) -> SendResult | None:
        st = self.active_state(chat_id)
        if st is None:
            return await self._create_card(chat_id, content, reply_to=reply_to)
        # 已有活跃卡片：hermes 分段发送 → 追加段落
        st.answer_text = (st.answer_text + "\n\n" + content).strip()
        await self._stream_answer(st)
        return SendResult(success=True, message_id=st.card_message_id)

    async def _create_card(self, chat_id: str, content: str, *, reply_to: str | None = None) -> SendResult | None:
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
        st.panel_on_card = _card_has_panel(card)
        card_id = await client.cardkit_create(card)
        # v2.0.22：卡片回复引用触发消息（用户验收：私聊/群聊都不知回复哪条）。
        # V1 全程 reply 发卡（_reply_anchor_for_event）；V2 曾丢弃 reply_to 直发。
        # hermes 官方默认锚点 = 触发消息 message_id（base.py:180），cron/后台
        # 投递锚点是合成 task_id（非 om_）→ reply API 必败（99992354，V1
        # issue #18-3 铁证）→ 非 om_ 直发。reply 失败（消息被删/非法）降级
        # 直发——内容永不丢红线。流式更新按 card_id 走，与消息发送方式无关。
        anchor = (reply_to or "").strip()
        message_id: str | None = None
        if anchor.startswith("om_"):
            try:
                message_id = await client.reply_card_by_id(anchor, card_id)
            except Exception:
                _logger.warning(
                    "composer: reply card failed (anchor=%s) — fallback to direct send",
                    anchor[:20], exc_info=True,
                )
                message_id = None
        if not message_id:
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
        # v2.0.26：clarify 暂停族字段随回合重置（prev 复用连续性——上一回合
        # 残留的暂停态不得渗透进新回合）。
        st.pause_pending = False
        st.paused = False
        st.resume_base = ""
        st.turn_id = ""
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

    async def _on_progress(self, chat_id: str, cls: ClassifyResult, *, reply_to: str | None = None) -> SendResult | None:
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
                await self._create_card(chat_id, "", reply_to=reply_to)
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
        # v2.0.24：首个状态行到达时全量更新补插面板（懒渲染，s172 实证）；
        # 此处 status_lines 恒非空（调用点均已过 merge 非空门），面板必在。
        st.panel_on_card = _card_has_panel(card)
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

    # ── clarify 暂停/恢复（v2.0.26，同卡续流）────────────────────────────
    async def suspend_for_clarify(self, chat_id: str, question: str = "") -> bool:
        """clarify 交互卡发出前调用：活跃流式卡进入「等待澄清」暂停态.

        v2.0.26 主流程（用户验收：clarify 期间流式卡应等待回调、回答后同一张
        卡续流，而非「先完成 → 回调后开新卡」）：
        - finalize 帧暂停窗口内到达（pause_pending=True）→ 确认该 finalize 是
          clarify 边界（hermes run_turn_runner.py:1404 官方边界语义）→ 取消
          落定定时器，卡保持流式态；
        - 窗口外到达（edit 灰度流 / 无边界 finalize 通道）→ 活跃卡同样暂停
          （hermes 在 edit 模式不关流，回调后续流走 handle_send 追加——
          active_state 未落定 → 自然同卡）。
        面板追加暂停行（i18n clarify_wait）并全量更新（v2.0.24 懒渲染补插
        机制承接纯 answer 卡首面板）。无活跃卡返回 False（adapter 照常发
        clarify 卡——回调后建新卡的 v2.0.25 行为自然承接，内容永不丢）。
        """
        self._loop = asyncio.get_running_loop()
        async with self._chat_lock(chat_id):
            st = self.active_state(chat_id)
            if st is None:
                _logger.info(
                    "composer: clarify suspend skipped (no active card) chat=%s", chat_id)
                return False
            self._cancel_pause(chat_id)
            if st.pause_pending:
                st.pause_pending = False
                # hermes clarify 占位（stream_consumer._finalize_boundary_stream：
                # accumulated 为空时 finalize_text = 「💬 ...」placeholder，WeCom
                # 气泡模型的过渡文案）——不落飞书卡正文（面板暂停行已承载
                # 等待语义）。
                if _is_clarify_placeholder(st.answer_text):
                    st.answer_text = ""
            st.paused = True
            st.resume_base = st.answer_text
            row = _clarify_wait_row()
            if row not in st.status_lines:
                st.status_lines.append(row)
                st.line_times.append(time.monotonic())
            st.last_activity = time.monotonic()
            await self._patch_status(st)
            _logger.info(
                "composer: clarify suspend chat=%s card=%s question=%r",
                chat_id, st.card_id, (question or "")[:40],
            )
            return True

    def _arm_pause_expire(self, chat_id: str) -> None:
        """暂停窗口落定定时器（重复武装无害——先取消旧句柄再挂新）."""
        self._cancel_pause(chat_id)
        loop = self._loop
        if loop is None or loop.is_closed() or self._clarify_suspend_sec <= 0:
            return
        try:
            self._pause_timers[chat_id] = loop.call_later(
                self._clarify_suspend_sec, self._pause_due, chat_id)
        except RuntimeError:  # loop 已停转（跨线程窗口）
            pass
        except Exception:
            _logger.debug("composer: pause timer arm failed chat=%s", chat_id, exc_info=True)

    def _cancel_pause(self, chat_id: str) -> None:
        timer = self._pause_timers.pop(chat_id, None)
        if timer is not None:
            with contextlib.suppress(Exception):
                timer.cancel()

    def _pause_due(self, chat_id: str) -> None:
        """窗口到期回调（loop 线程）：窗口内无 send_clarify → 普通回合真落定."""
        try:
            self._pause_timers.pop(chat_id, None)
            st = self._states.get(chat_id)
            if st is None or not st.pause_pending:
                return
            asyncio.ensure_future(self._pause_expire(chat_id))
        except Exception:
            _logger.debug("composer: pause due failed chat=%s", chat_id, exc_info=True)

    async def _pause_expire(self, chat_id: str) -> None:
        """窗口到期真落定：与帧路径同一把 chat 锁（suspend/帧互斥）。"""
        try:
            async with self._chat_lock(chat_id):
                st = self._states.get(chat_id)
                if st is None or not st.pause_pending or st.finalized:
                    return
                st.pause_pending = False
                await self._finalize(st, st.answer_text)
        except Exception:
            _logger.debug("composer: pause expire failed chat=%s", chat_id, exc_info=True)

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
            if st.paused or st.pause_pending:
                # v2.0.26：clarify 暂停/窗口期看门狗让位——等待可长达 hermes
                # clarify 硬超时（默认 3600s），静默是预期而非孤儿；不 seal、
                # 保持监视（resume 后 _arm_watchdog 重新武装由帧路径承接）。
                self._arm_watchdog(chat_id)
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
        self._cancel_pause(chat_id)  # v2.0.26：暂停窗口定时器随状态一并清理

    def clear(self) -> None:
        for cid in list(self._pause_timers):
            self._cancel_pause(cid)
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

# hermes 官方流式光标（gateway/config.py:483 DEFAULT_STREAMING_CURSOR = " ▉"）。
# 仅 interim tick 的 display 文本末尾追加（stream_consumer.py:_push_update:829-833）；
# 非 interim 推送帧与 finalize 帧不带光标。纯显示装饰，重组前剥除——官方 ACK 侧
# 同款语义（stream_consumer.py:301-302 ``acked.endswith(self.cfg.cursor)`` 剥除）。
_NATIVE_CURSOR = " ▉"


def _strip_native_cursor(text: str) -> str:
    """剥掉官方流式光标尾巴（display 装饰；剥除语义与官方 ACK 侧一致）。"""
    if text.endswith(_NATIVE_CURSOR):
        return text[: -len(_NATIVE_CURSOR)]
    return text


def _is_named_tool_line(line: str) -> bool:
    """单行是否为官方工具行：emoji 开头**且**能解析出工具名.

    head 模板 ``{token} {ascii-name}`` 不含 emoji 前置条件，"hello world" 这类
    两个英文单词的普通文本会命中 tool="world"——必须先卡 emoji（官方 progress
    模板全部 emoji 起始）；通用 emoji 行（✅ 确认语 / 🌤️ 正文小节等）解析不出
    工具名，同样不算工具行。
    """
    if not _EMOJI_RE.match(line):
        return False
    kind, tool, _clean = parse_progress_line(line)
    return kind == "tool" and bool(tool)


def _is_progress_frame_text(text: str) -> bool:
    """整帧行语法扫描（v2.0.25 结构化校验）：官方 native 进度帧的行语法.

    合法行（官方 overlay/progress 内容只有这几种，stream_consumer.py:246-254 +
    run_turn_runner _progress_* 模板）：

    1. 💬 思考块（reasoning 中继 / interim 播报）及其后续非 emoji 续行；
    2. 围栏命令块（``` 开闭之间任意内容；连续调用省略 header 的裸围栏也算）；
    3. 通知轨固定前缀行（⚠️ Subagent failed/timed out，hermes 硬编码）；
    4. 具名工具行（emoji + 可解析工具名：pending / preview / head / 动词行），
       以及**紧随其后**的缩进命令体行——无围栏多行工具行
       （``💻 Terminal\\n<缩进命令>``，2026-10-12 14:37 实录帧）的延续；
    5. 空行中性（思考块段落分隔，v2.0.8 语义）。

    v2.0.25 事故根治（用户 2026-10-27 反馈）：旧「emoji 开头且 ≤300 即进度」
    启发式把 ✅ 开头的 answer 累积快照整帧误判为进度 → 逐行拆进面板（cron
    创建回合 finalize status_lines=12：任务表格+内容构成全部泄漏进折叠面板；
    正文因下一快照自愈反而正确）。本扫描器下，answer 的正文行（表格行、
    非 emoji 小节、非缩进段落）任一不合法即整帧出局。
    误杀方向安全：真进度行落入正文一闪而过（官方 overlay 下一帧即清空，
    stream_consumer.py:290-291），内容永不丢。
    """
    in_fence = False
    in_thinking = False
    prev_tool = False  # 仅对紧随的下一行有效（命令体只挂靠直接上一工具行）
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            continue  # 空行：块间分隔，不参与判定
        if in_fence:
            if stripped.startswith("```"):
                in_fence = False
            continue
        if stripped.startswith("```"):
            in_fence = True
            in_thinking = False
            prev_tool = False
            continue
        if stripped.startswith(_THINKING_PREFIX):
            in_thinking = True
            prev_tool = False
            continue
        if any(rx.match(stripped) for rx in _PLATFORM_NOTICE_RES):
            in_thinking = False
            prev_tool = False
            continue
        if _is_named_tool_line(stripped):
            in_thinking = False
            prev_tool = True
            continue
        # 非 emoji 行：思考续行 / 缩进命令体（raw 保留原始缩进）→ 合法；
        # 其余（表格行、正文段落……）→ 整帧不是进度帧
        if in_thinking:
            continue
        if prev_tool and raw[:1] in (" ", "\t"):
            continue
        return False
    return True


def _split_native_composite(text: str) -> tuple[str, list[str]]:
    """拆 hermes native 复合帧：``{answer}`` + 分隔符 + ``{工具进度行}``.

    官方复合格式（stream_consumer.py:_compose_frame_content:251-255）：
    interim tick（工具运行中）帧体为 ``answer + "\n\n---\n" + progress``；
    progress 是 ``"\n".join(_tool_progress_lines)``（项可为多行围栏块）。

    官方帧形态全集（stream_consumer.py:824-847 + stream_consumer_transport.py:373-406）：
    种子帧 ``""``；非 interim 推送帧 = 纯 answer 累积快照（无光标无进度）；
    interim tick 有进度行 → 复合帧、无进度行 → ``answer + 光标``、accumulated
    为空 → progress-only 帧（后三种都带光标尾巴）；finalize 帧 = 纯 answer 全量
    （无光标）。native 帧通道不携带任何 interim/composite 标记
    （stream_consumer_transport.py:66-70 仅 text/finalize/chat/reply/turn），
    通道归属只能靠帧体结构判定。

    拆分策略（v2.0.25 结构化校验，保守优先）：
    - 先剥光标尾巴（display 装饰，不参与语义判定）；
    - 无分隔符：整帧过 ``_is_progress_frame_text`` 行语法扫描，全部行合法才整帧
      视为进度行，否则整体是正文。旧「emoji 开头 ≤300 即进度」启发式已废弃
      （✅ 开头的 answer 快照整帧误拆进面板，2026-10-27 事故）；
    - 有分隔符：只拆**最后一个**分隔符（正文自身可含 markdown 分隔线），
      拆出部分同样过行语法扫描才拆，否则整体视为正文。误拆由下一快照帧覆盖自愈
      （官方 :290-291 真实 delta 到达即清空 overlay）；误杀（进度行落入正文）
      同样由 overlay 语义自愈，内容永不丢。

    永不抛异常；返回 ``(answer_text, progress_lines)``。
    """
    try:
        text = _strip_native_cursor((text or "").strip())
        if not text:
            return "", []
        idx = text.rfind(_NATIVE_COMPOSITE_SEP)
        if idx < 0:
            if _is_progress_frame_text(text):
                return "", split_progress_segments(text) or [text]
            return text, []
        head = text[:idx].strip()
        tail = text[idx + len(_NATIVE_COMPOSITE_SEP):].strip()
        if not head or not tail:
            return text, []
        if _is_progress_frame_text(tail):
            return head, split_progress_segments(tail) or [tail]
        return text, []
    except Exception:  # noqa: BLE001 — 拆分纯装饰，失败整体按正文
        return (text or "", [])


# ── clarify 暂停/恢复 helpers（v2.0.26，同卡续流）────────────────────────────
def _is_clarify_placeholder(text: str) -> bool:
    """hermes clarify 边界占位文案判定（stream_consumer._finalize_boundary_stream：
    accumulated 为空时 finalize_text = ``t("gateway.clarify.native_stream_placeholder")``
    —— zh「💬 等待你的选择...」/ en「💬 Waiting for your choice...」，WeCom 气泡
    模型的过渡文案，不应落飞书卡正文）。判定规则刻意用形态而非文案枚举：
    💬 前缀 + 单行 + ≤40 字符（占位是一句短语；真实 answer 的思考内容走面板、
    正文不会是单个 💬 短行）——跨语言/hermes 文案改动均稳健。
    """
    t = (text or "").strip()
    return bool(t) and t.startswith("💬") and "\n" not in t and len(t) <= 40


def _clarify_wait_row() -> str:
    """面板暂停行文本（单语 en，与面板既有 hermes 进度行语言一致；见 i18n 词条）。"""
    from ..cardkit.i18n import _T

    return _T["clarify_wait"][0]


def _drop_clarify_wait_row(st: CardTurnState) -> None:
    """resume 时从内存面板移除暂停行（status_lines/line_times 必须同步收缩）.

    API 层面无需专发更新：行从 status_lines 移除后，后续 _patch_status（进度行
    到达）/ finalize _reseal_panel 重建 / finalize 空面板删除（panel_on_card=True
    且行空）必然把它从卡上带走——回合必经 finalize，行不会残留。
    """
    try:
        row = _clarify_wait_row()
        idx = st.status_lines.index(row)
    except ValueError:
        return
    st.status_lines.pop(idx)
    if idx < len(st.line_times):
        st.line_times.pop(idx)


def _merge_resumed_answer(resume_base: str, snapshot: str) -> str:
    """clarify 回调后的新段快照与暂停前正文按段拼接.

    hermes 边界处理时 ``_reset_segment_state()`` 清空 accumulated → reopen 后
    delta 是「新段内累积快照」（不含暂停前内容）→ 段间用空行拼接；同一回合
    多次 clarify 时 resume_base 随 suspend 递进累积（见 handle_stream_frame）。
    """
    base = (resume_base or "").strip()
    snap = (snapshot or "").strip()
    if not base:
        return snap
    if not snap:
        return base
    return f"{base}\n\n{snap}"


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


def _card_has_panel(card: dict[str, Any]) -> bool:
    """实测卡片 JSON 是否含 unified panel 元素（v2.0.24 懒渲染真值源）.

    以建/更卡产物为准而非调用参数推断——工厂参数（include_unified_panel）
    或未来渲染变更不影响本判定。永不抛异常。
    """
    try:
        body = card.get("body") or {}
        elements = body.get("elements") or card.get("elements") or []
        from ..cardkit.elements import UNIFIED_PANEL_ELEMENT_ID

        return any(
            isinstance(el, dict) and el.get("element_id") == UNIFIED_PANEL_ELEMENT_ID
            for el in elements
        )
    except Exception:  # noqa: BLE001 — 追踪是装饰性状态，绝不影响主流程
        return False


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
