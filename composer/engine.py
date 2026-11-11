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

# OPT-1b/2/5：卡片字节预算守卫、进度行解析 memo、流式 markdown 卫生化。
# cards 不反向依赖 composer，顶层导入无环（与 progline/seq 同层叶子）。
from ..cardkit.cards import (
    _enforce_card_byte_budget,
    _parse_line_cached,
    md_hygiene_cached,
    md_hygiene_line_cached,
)
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

# v2.0.21（审计 R1）：孤儿卡看门狗静默阈值。原值 120s（429 冷却 60s 的 2 倍裕度）
# 在生产误封活跃回合（11-08 16:05 铁证：回合存活、网关上下文压缩 300s+ 静默
# ——16:00:33 压缩启动 → 16:05:01 看门狗封卡 → 16:05:33 日志「compression
# still streaming after 300s」；11-07 12:19 同族）。
# v2.0.32（审计 P1-1，用户确认阈值方案）：提到 300s——5×429 冷却 60s 裕度、
# 与网关压缩首个检查点（300s）同量级；长静默的工具执行/压缩期不再被误封。
# 可经配置 ``watchdog_idle_sec`` 调整（60~1800s 钉制，见 Config.watchdog_idle_sec）；
# 引擎侧以 _watchdog_idle_sec() 动态解析（Config 缺失/异常回落本常量）。
_WATCHDOG_IDLE_SEC = 300.0

# v2.0.32（审计 P1-3 方案B，用户确认）：主动续期阈值。官方 CardKit 流式会话
# 寿命 ~10min（生产实证 11-07 12:01 建卡 → 12:11:08 撞 200850，600s 整；
# 15:08 建卡 → 15:18:35 同族）。会话龄 ≥540s 时下一帧更新前先 settings
# streaming_mode:true 续期（client.cardkit_reopen_streaming），防患于未然——
# 主流程预防，非兜底；撞上 200850 仍由 call_with_seq_recovery 的重开自愈
# 兑底闭环（双层，同一主链）。长回合 >10min 不再中断打字机。
_STREAM_RENEW_AGE_SEC = 540.0

# OPT-1a（性能五项审计 P1）：面板更新尾沿合并窗口（秒）。
# 生产取证：499s 回合 35 次工具完成 ≈70 次全量快照上传 ≈1.3MB 上行，其中
# 思考增长/interim tick/耗时回填均为「行数不变的原地改写」——0.2s 窗口内
# 合并为一次 cardkit_update（每 chat 单挂起 timer）。新增行（工具步骤出现）
# 不合并、保持同步上传：结构性进展即时可见（v2.0.24 面板即时补插语义同族），
# 也与存量 e2e 契约「每新增行一次 update」（test_e2e_full 两次状态补丁两次
# update）兼容——这是有意的保守切分，见 _queue_panel_flush。
_PANEL_FLUSH_DELAY_SEC = 0.2

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
    # v2.0.32（审计 P1-3 方案B）：当前流式会话起点（monotonic）。建卡时起算，
    # 主动续期/200850 自愈重开成功时刷新（seq.py._mark_session_renewed）；
    # 0 = 未知（不参与续期判定）。
    stream_session_at: float = 0.0
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
    # ── OPT-1a（性能审计）：面板待发旗标——absorb/interim/耗时回填等路径置位，
    # 0.2s 尾沿合并单发（_panel_flush_timers 每 chat 至多一个挂起 timer）；
    # _flush_panel_now 冲刷后清除。终态（finalized/degraded）下由 seal/finalize
    # 路径接管面板，冲刷自动跳过——最后状态先落卡再封卡的红线不变。
    panel_dirty: bool = False
    # ── OPT-4（性能审计）：最近一次成功 cardkit_stream_element 的 answer 全文
    # （create 种子与 _stream_answer 成功后记录）。finalize 比对：字节一致 →
    # 跳过 answer 终值 patch（流式路径已把同内容落卡，省一次全量上传）；
    # reasoning 前插会改写 answer_text → 与基线不等 → patch 照发（自然失效）。
    # 从未流过（None/空）→ 恒不跳过。
    last_streamed_answer: str = ""
    # ── OPT-5（性能审计）：流式 markdown 卫生化缓存。answer 用单槽精确串缓存
    # （键=原文串 == 比对——native 帧每帧把 answer_text 重绑为内容相等的新
    # str 对象，is 键在 interim tick 上逐帧 miss；见 cards.md_hygiene_cached）；
    # status_lines 用逐行精确串缓存（行合并后不可变，命中率近恒 1）。变换只
    # 作用于发往卡片的视图；st.answer_text/status_lines 恒存原文——合并去重、
    # 耗时表键（strip_dedup_suffix(行)）、答案复读过滤均不受影响。
    answer_md_cache: dict[str, Any] = field(default_factory=dict)
    md_lines_cache: dict[str, str] = field(default_factory=dict)


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
        model_resolver: Callable[[str], str | None] | None = None,
    ) -> None:
        self._get_client = get_client          # () -> FeishuClient | None
        self._build_card = build_card          # build_streaming_card_v2(**kwargs)
        self._build_final_card = build_final_card
        self._config = config
        # v2.0.31：页脚 model 字段的会话级解析器（chat_id → 实际模型名）。
        # adapter 注入——/model 会话切换在 config.yaml 单一事实源之外
        # （_resolve_gateway_model 只读 config），不注入则页脚退回 config 值。
        self._model_resolver = model_resolver
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
        # v2.0.33（审计 FIX-6）：GC 孤儿卡收口的 fire-and-forget 任务强引用集。
        # ensure_future 裸任务可能被事件循环中途垃圾回收（asyncio 文档明确
        # 要求保留引用），done_callback 自动清席。
        self._gc_seal_tasks: set[asyncio.Task[None]] = set()
        self._states: dict[str, CardTurnState] = {}
        self._by_message: dict[str, str] = {}  # card_message_id -> chat_id
        # v2.0.11 P0：per-chat 帧处理串行化锁（见模块头并发模型说明）。
        # 锁随 chat 惰性创建；只在 clear() 全量清空，forget() 不清——在途持有者
        # 与后来者必须共用同一把锁才有互斥意义（chat 级锁对象内存可忽略）。
        # OPT-3 修订：forget()/_gc 弹出状态后，若锁未持有且无等待者则一并驱逐
        # （见 _evict_chat_lock_if_idle）——持有中绝不驱逐，互斥语义不变。
        self._chat_locks: dict[str, asyncio.Lock] = {}
        # OPT-1a：per-chat 面板尾沿合并定时器（单挂起，见 _queue_panel_flush）
        # 与冲刷任务强引用集（FIX-6 同款——裸任务防事件循环中途 GC）。
        self._panel_flush_timers: dict[str, asyncio.TimerHandle] = {}
        self._panel_flush_tasks: set[asyncio.Task[None]] = set()
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

    def _watchdog_idle_sec(self) -> float:
        """v2.0.32（审计 P1-1）：看门狗静默阈值动态解析（可配置，钉制 60~1800s）.

        Config 缺失/异常回落模块常量 _WATCHDOG_IDLE_SEC（300s）；配置值域外
        钉制同 card_duration_sec/print_step 先例（上游配置异常不炸主流程）。
        """
        try:
            val = float(getattr(self._config, "watchdog_idle_sec", _WATCHDOG_IDLE_SEC))
        except Exception:  # noqa: BLE001
            return _WATCHDOG_IDLE_SEC
        return min(1800.0, max(60.0, val))

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

        v2.0.33（审计 FIX-6）：未定稿孤儿回收前先优雅收口远端卡——空闲超
        TTL 的未定稿状态其远端卡仍处流式态（spinner 永挂家族），而看门狗
        阈值（默认 300s，可配）与 card_duration_sec 相互独立，TTL 先到时
        GC 是唯一收口时机（card_ttl_sec < watchdog_idle_sec 配置下看门狗
        永远轮不到）。收口顺序：先置 degraded+finalized（与 _watchdog_seal
        同款：后续帧不再消费本状态）再异步 seal（fire-and-forget，复用
        finalize_card 四步）；seal 失败仅留日志、不回滚回收——进程内卫生
        不被远端 API 阻塞，官方 ~10min 流式会话自愈为最终防线。已定稿
        60s 冷却回收不变（远端卡已在定稿时关流）；paused/pause_pending
        继续豁免（clarify 等待可达 hermes 硬超时 3600s）。
        """
        ttl = self._ttl_sec()
        now = time.monotonic()
        stale: list[str] = []
        orphan_seals: list[CardTurnState] = []
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
                if st.card_id:
                    # 未定稿孤儿（有远端卡）：置终态旗标停止消费 + 收口排队。
                    st.degraded = True
                    st.finalized = True
                    orphan_seals.append(st)
        for cid in stale:
            st = self._states.pop(cid, None)
            if st and st.card_message_id:
                self._by_message.pop(st.card_message_id, None)
            self._cancel_pause(cid)
            # OPT-1a（面板合并护栏）：合并 timer 随状态驱逐一并取消——已定稿
            # 60s 冷却回收与未定稿孤儿 seal 排队（_schedule_orphan_seal 内含
            # force 冲刷）两条路径都不该再留挂起 timer，否则到期只会空转一次
            # 冲刷任务并对已弹出的 chat 重建锁对象（dangling timer 红线）。
            self._cancel_panel_flush(cid)
            # OPT-3：状态已弹出且锁空闲无等待者 → 一并驱逐锁对象。
            # 帧路径的 _gc 在 chat 锁内运行（locked() 恒真）→ 天然不驱逐
            # （在途持有者与后来者必须共用同一把锁，见 _chat_locks 注释）；
            # forget()/无锁直调路径才真正回收。
            self._evict_chat_lock_if_idle(cid)
        # 弹出后再调度 seal（任务体操作已脱离注册表的状态对象，与注册表零竞态）。
        for st in orphan_seals:
            self._schedule_orphan_seal(st)

    def _schedule_orphan_seal(self, st: CardTurnState) -> None:
        """GC 孤儿卡收口的 fire-and-forget 调度（seal 失败不回滚回收决定）.

        _gc 全部调用点均在帧处理入口的 chat 锁内（async 上下文），正常路径
        必有 running loop；无 running loop（同步直调，仅测试环境）时跳过
        seal——本端状态已回收，远端卡由官方 ~10min 流式会话自愈兑底。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _logger.debug(
                "composer: gc orphan seal skipped (no running loop) card=%s",
                str(st.card_id)[:12],
            )
            return
        task = loop.create_task(self._seal_live_card(st))
        self._gc_seal_tasks.add(task)  # 强引用防任务被中途 GC
        task.add_done_callback(self._gc_seal_tasks.discard)

    def _chat_lock(self, chat_id: str) -> asyncio.Lock:
        lock = self._chat_locks.get(chat_id)
        if lock is None:
            lock = self._chat_locks[chat_id] = asyncio.Lock()
        return lock

    def _evict_chat_lock_if_idle(self, chat_id: str) -> None:
        """OPT-3：驱逐空闲 per-chat 锁（前置条件：该 chat 状态已从 _states 弹出）.

        判定保守钉死方向：locked() 为真绝不驱逐（在途持有者与后来者必须共用
        同一把锁才有互斥意义）；``_waiters`` 属性不可得（跨版本实现细节，
        getattr 落空）时视为占用不驱逐。漏驱逐的代价只是一把小锁对象随
        chat 数缓增，误驱逐的代价是互斥失效建重复卡——不可逆方向不冒险。
        """
        lock = self._chat_locks.get(chat_id)
        if lock is None or lock.locked():
            return
        if not hasattr(lock, "_waiters"):
            return  # 属性不可得（跨版本实现变化）→ 视为占用，不冒险驱逐
        # 3.11+ 实现：_waiters 为 None（无等待者）或 futures 容器（有等待者）。
        waiters = getattr(lock, "_waiters", None)
        if waiters:
            return
        self._chat_locks.pop(chat_id, None)

    # ── OPT-1a：面板更新尾沿合并（0.2s 窗口单飞）─────────────────────────
    async def _queue_panel_flush(self, st: CardTurnState, *, urgent: bool = False) -> None:
        """置脏并调度面板冲刷：urgent（新增行）/首个面板插入即时发，其余尾沿合并.

        生产画像（审计取证：499s 回合 35 次工具完成 ≈70 次全量快照 ≈1.3MB 上行）
        中，思考增长、interim tick、pending→preview 取代、耗时回填均为「行数不变
        的原地改写」→ 0.2s 窗口内合并为一次 cardkit_update；新增行（工具步骤/
        通报/暂停行出现）urgent 同步上传——结构性进展即时可见，且存量 e2e 以
        「每新增行一次 update」为契约（test_e2e_full 两次状态补丁两次 update）。
        全部调用点都在 chat 锁内，即时分支与旧 _patch_status 直发等价。
        """
        st.panel_dirty = True
        if urgent or not st.panel_on_card:
            # v2.0.24 懒渲染语义：首个状态行补插面板必须即时（test_v224 契约）。
            await self._flush_panel_now(st.chat_id, st)
            return
        if st.chat_id in self._panel_flush_timers:
            return  # 已有挂起冲刷：它取最新状态单发，无需重复调度
        loop = self._loop
        if loop is None or loop.is_closed():
            return  # 无 loop（同步直调环境）：保持置脏，交后续帧/finalize 冲刷
        try:
            self._panel_flush_timers[st.chat_id] = loop.call_later(
                _PANEL_FLUSH_DELAY_SEC, self._panel_flush_due, st.chat_id)
        except RuntimeError:  # loop 已停转（跨线程窗口）
            self._panel_flush_timers.pop(st.chat_id, None)

    def _cancel_panel_flush(self, chat_id: str) -> None:
        """取消挂起的面板合并 timer（forget/clear/即时冲刷共用）."""
        timer = self._panel_flush_timers.pop(chat_id, None)
        if timer is not None:
            with contextlib.suppress(Exception):
                timer.cancel()

    def _panel_flush_due(self, chat_id: str) -> None:
        """合并窗口到期回调（loop 线程）：持 chat 锁冲刷一次最新面板状态."""
        self._panel_flush_timers.pop(chat_id, None)
        try:
            task = asyncio.ensure_future(self._coalesced_flush(chat_id))
        except RuntimeError:  # 无可用 loop（极端停机窗口）
            return
        self._panel_flush_tasks.add(task)  # 强引用防任务被中途 GC（FIX-6 同款）
        task.add_done_callback(self._panel_flush_tasks.discard)

    async def _coalesced_flush(self, chat_id: str) -> None:
        """timer 冲刷体：与帧路径同一把 chat 锁；失败与帧路径同语义（degrade+seal）.

        旧同步路径里 patch 失败即帧入口降级（handle_send/handle_edit except 块
        → _degrade + _seal_degraded_card）；合并冲刷的失败一一对应映射，绝不静默
        吞掉导致面板永挂/降级红线失明。锁内复查 dirty/终态旗标：挂起窗口内
        回合已封（seal/finalize 先到）则跳过——最后状态先落卡再封卡的红线由
        seal 侧的强制冲刷承接，timer 不与 seal 双发。
        """
        try:
            # 状态已被 forget/_gc 驱逐 → 无主冲刷直接放弃（查 _states 而非先取
            # 锁：_chat_lock 会惰性重建锁对象，对已驱逐 chat 是纯泄漏）。
            if chat_id not in self._states:
                return
            async with self._chat_lock(chat_id):
                await self._flush_panel_now(chat_id)
        except Exception as exc:
            self._degrade(chat_id, "panel_flush", exc)
            _logger.debug("composer: panel flush failed chat=%s", chat_id[:12], exc_info=True)
            with contextlib.suppress(Exception):
                await self._seal_degraded_card(chat_id)

    async def _flush_panel_now(
        self, chat_id: str, st: CardTurnState | None = None, *, force: bool = False,
    ) -> None:
        """立即冲刷待发面板状态（取消挂起 timer；调用方须持 chat 锁或状态已脱管）.

        force=True 供收口路径（_seal_live_card）使用：状态已被置终旗标
        （degraded/finalized，降级/GC 孤儿/停机收口均先置旗标再 seal）时仍
        冲刷——seal/finalize 发 API 前最后面板状态必须先落卡（流式正确性红线）。
        非 force 路径遇终态只清旗标不发包：终态卡片由 seal 侧接管，双发竞态零容忍。
        """
        self._cancel_panel_flush(chat_id)
        if st is None:
            st = self._states.get(chat_id)
        if st is None or not st.panel_dirty or not st.card_id:
            return
        st.panel_dirty = False
        if not force and (st.finalized or st.degraded):
            return
        await self._patch_status(st)

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
        # 【锚点归一】网关流式消费者不走 send(reply_to=) 而是打 metadata.reply_to_message_id；
        # 本引擎 _create_card 只看 reply_to 槽位。入口统一回退，建卡时才能以引用发表。
        if not (reply_to or "").strip() and isinstance(metadata, dict):
            reply_to = str(metadata.get("reply_to_message_id") or "").strip() or None
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
                    return await self._on_interim(chat_id, content, cls, reply_to=reply_to)
                if cls.channel == CH_FINAL:
                    return await self._on_final_send(chat_id, content)
                return None  # CH_PLAIN → 原生
            except Exception as exc:  # noqa: BLE001 — 降级红线：任何失败走原生
                self._degrade(chat_id, "send", exc)
                # v2.0.32（P0-2）：降级瞬间把远端卡优雅收口（footer+关流式），
                # 消灭「降级后远端卡永久流式态」跑马灯。
                await self._seal_degraded_card(chat_id)
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
                # OPT-1a：耗时回填是行数不变的原地改写 → 尾沿合并（0.2s 窗口）。
                await self._queue_panel_flush(st)
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
                if not finalize and st.finalized:
                    # v2.0.33（审计 FIX-3）：定稿后的状态在 60s GC 冷却窗口内仍可
                    # 经 state_by_message 反查到。迟到的非定稿 edit（网关重试/
                    # 换通道竞态）会对已关流式的卡发 cardkit_stream_element →
                    # 300309（CARDKIT_STREAMING_CLOSED，client.py:107）无处处理
                    # → 整通道误降级（后续帧全部退原生文本）。定稿 edit 本身
                    # 已携带全量终值，非定稿迟到帧按构造是重复内容：短路返回
                    # 成功（与 _on_stream 追加段同款 SendResult 形态），不触
                    # 卡片 API、不降级。
                    return SendResult(success=True, message_id=st.card_message_id)
                if finalize:
                    return await self._finalize(st, content)
                return await self._patch_answer(st, content)
            except Exception as exc:  # noqa: BLE001 — 降级红线：任何失败走原生
                self._degrade(chat_id, "edit", exc)
                # v2.0.32（P0-2）：同 send 降级路径——远端卡优雅收口。
                await self._seal_degraded_card(chat_id)
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
                        # OPT-1a：行数不变的原地改写 → 尾沿合并（timer 兑现，
                        # 或紧随的 delta/finalize 强制冲刷）。
                        await self._queue_panel_flush(st)
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
                # v2.0.32（P0-2）：同 send/edit 降级路径——远端卡优雅收口。
                await self._seal_degraded_card(chat_id)
                return False

    async def _absorb_status_lines(self, st: CardTurnState, lines: list[str]) -> None:
        """进度行合并进活跃面板（幂等/原地取代/思考累积，v2.0.5 合并器）.

        v2.0.13 从 ``_on_progress`` 有卡分支抽出共用：native 复合帧的工具行
        与网关进度气泡行走同一套合并/耗时认领/行时戳逻辑。

        OPT-2：answer 归一化（" ".join(split())）每次调用只算一次——旧实现对
        每条 incoming 行重复全量扫描 answer 缓冲（大 answer × accumulate 快照
        多行 = O(n×m)）；归一化结果传入 _is_answer_duplicate，行为逐字等价。
        OPT-1a：新增行（urgent）同步上传；原地改写进 0.2s 尾沿合并。
        """
        # OPT-2：每次 absorb 归一化一次 answer（旧：每行一次）。
        answer_norm = " ".join((st.answer_text or "").split())
        fresh = [
            ln for ln in lines
            if not _is_answer_duplicate(ln, st.answer_text, answer_norm=answer_norm)
        ]
        if not fresh:
            return
        old_lines = st.status_lines
        merged = _merge_progress_lines(old_lines, fresh)
        if merged is None:
            return
        self._migrate_tool_durations(st, old_lines, merged)
        new_times = _reconcile_line_times(old_lines, st.line_times, merged)
        self._attach_pending_durations(st, old_lines, merged)
        st.status_lines = merged
        st.line_times = new_times
        # OPT-1a：新增行 = 用户可见的结构性进展 → 同步上传不延迟；原地改写
        # （思考增长/pending→preview 取代）→ 0.2s 尾沿合并单发。
        await self._queue_panel_flush(st, urgent=len(merged) > len(old_lines))

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
        raw_pending = list(prev.pending_status) if prev is not None else []
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
            # OPT-5：首卡面板行同样过流式 markdown 卫生化（与 _patch_status
            # 同一视图/同一缓存——否则建卡帧与更新帧样式来回翻转）。
            status_lines=[
                md_hygiene_line_cached(ln, st.md_lines_cache) for ln in pending
            ] or None,
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
        # v2.0.33（审计 FIX-4a）：复用状态对象升级为活跃卡时，旧 card_message_id
        # 的 _by_message 反查键必须先驱逐——否则旧卡消息被删除时
        # state_by_message(旧id) 仍解析到本状态对象（当前活跃卡），adapter.
        # delete_message 据此 forget(chat_id) 会把进行中的回合状态连根拔掉
        # （正文断流 + 面板蒸发）。新键注册保留下方原逻辑。
        old_message_id = st.card_message_id
        st.card_message_id = str(message_id)
        if old_message_id and old_message_id != st.card_message_id:
            self._by_message.pop(old_message_id, None)
        st.answer_text = ""
        st.seq = 0
        st.created_at = time.monotonic()
        st.last_activity = time.monotonic()
        # v2.0.32（P1-3 方案B）：新卡即新流式会话，续期计时从建卡起算。
        st.stream_session_at = time.monotonic()
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
        # OPT-1a/4/5：新回合清位——挂起面板脏旗标（旧 turn 的合并 timer 到期后
        # 查 dirty=False 自然 no-op）、answer 流式基线、markdown 卫生化缓存
        # （旧行/旧文缓存引用随回合释放，不跨回合钉内存）。
        st.panel_dirty = False
        self._cancel_panel_flush(chat_id)
        st.last_streamed_answer = ""
        st.answer_md_cache.clear()
        st.md_lines_cache.clear()
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
            # OPT-5：种子正文过流式 markdown 卫生化（与 _patch_status 全量快照
            # 同一视图——防全量更新与流式元素内容样式来回翻转）。
            seed_view = md_hygiene_cached(st.answer_text, st.answer_md_cache)
            await call_with_seq_recovery(
                st,
                lambda seq: client.cardkit_stream_element(
                    card_id, _answer_element_id(), seed_view, sequence=seq),
                what="create:answer-seed",
                reopen=self._reopen_closure(st),
            )
            # OPT-4：种子上传成功即记录基线（finalize 等值跳过比对用）。
            st.last_streamed_answer = st.answer_text
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

    # ── 200850 流式会话治理（v2.0.32，审计 P0-1/P1-3 方案B）─────────────
    def _reopen_closure(self, st: CardTurnState) -> Callable[[int], Any] | None:
        """构造 settings streaming_mode:true 重开闭包（200850 自愈/主动续期共用）.

        client 缺失返回 None（seq.py 保持旧语义原样上抛）。永不抛异常。
        """
        client = self._get_client()
        if client is None or not st.card_id:
            return None

        def _reopen(seq: int) -> Any:
            return client.cardkit_reopen_streaming(st.card_id, sequence=seq)

        return _reopen

    async def _renew_stream_if_due(self, st: CardTurnState) -> None:
        """主动续期（P1-3 方案B，用户确认）：会话龄 ≥540s 先重开再更新.

        官方 ~10min 流式会话寿命（生产实证 11-07 两起 200850），长回合在
        第 10 分钟撞墙 → 整回合降级纯文本。主流程预防：下一帧更新前发现
        会话将到期，先 settings 重开（seq+1 消耗，跳号允许 s98），把寿命
        归零。续期失败仅 INFO（预防性步骤；真撞 200850 时
        call_with_seq_recovery 的重开自愈仍闭环，双层同链）。置
        stream_session_at=now 防止每帧重复尝试（退避语义）。
        """
        if not st.card_id or st.stream_session_at <= 0:
            return
        if time.monotonic() - st.stream_session_at < _STREAM_RENEW_AGE_SEC:
            return
        reopen = self._reopen_closure(st)
        if reopen is None:
            return
        try:
            st.seq += 1
            await reopen(st.seq)
            st.stream_session_at = time.monotonic()
            _logger.info(
                "composer: streaming session renewed card=%s age>=%.0fs",
                str(st.card_id)[:12], _STREAM_RENEW_AGE_SEC,
            )
        except Exception:  # noqa: BLE001 — 预防性续期失败不阻断本帧更新
            st.stream_session_at = time.monotonic()
            _logger.info(
                "composer: streaming renew skipped card=%s",
                str(st.card_id)[:12], exc_info=True,
            )

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
        # v2.0.32（P1-3 方案B）：会话将到期先主动续期（主流程预防）。
        await self._renew_stream_if_due(st)
        # OPT-5：发往卡片的正文视图过流式 markdown 卫生化（oneshot 卡同款待遇，
        # special.py 先例；不做 chunk/split——流式语义是全量快照，只做变换）。
        # 状态缓冲 st.answer_text 恒存原文：OPT-4 基线、网关拼接、复读过滤均按原文。
        content = md_hygiene_cached(st.answer_text, st.answer_md_cache)
        st.seq += 1
        st.last_activity = time.monotonic()
        await call_with_seq_recovery(
            st,
            lambda seq: client.cardkit_stream_element(
                st.card_id, _answer_element_id(), content, sequence=seq),
            what="stream:answer",
            reopen=self._reopen_closure(st),
        )
        # OPT-4：上传成功即记录基线（finalize 比对 state.answer_text 等值跳过）。
        st.last_streamed_answer = st.answer_text

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

    async def _on_interim(
        self, chat_id: str, content: str, cls: ClassifyResult, *, reply_to: str | None = None,
    ) -> SendResult | None:
        st = self.active_state(chat_id)
        text = (content or "").strip()
        if st is None and text and cls.meta.get("conversational_commentary"):
            # v2.0.34-hotfix（用户症状①，logs11.11 22:33 轮三方互证）：turn 首段
            # 前导文本经 hermes commentary 通道独立投递（gateway
            # _send_commentary → adapter.send(reply_to=触发锚, _interim_send)），
            # 在 delta 流式停止的回合里它先于首个工具进度行到达 → 无活跃卡 →
            # 原样直发 = 用户看到的独立"第一条文本消息"。
            # 修复：early-create 回合卡（与 _on_progress 占位建卡同款语义），
            # commentary 本体按 V1 语义以 💬 思考行并入下方面板分支；正文留空
            # 待 final send 全量覆盖收口同一张卡（_finalize 覆盖语义不变）。
            # 判定依据 adapter 层打的 conversational_commentary 标（原始
            # reply_to 形参——官方语义级：commentary 带锚、后台通知不带），
            # 💾/⏳/⚠️ 生命周期通知（无锚）永不建卡——跑马灯红线不动。
            try:
                await self._create_card(chat_id, "", reply_to=reply_to)
            except Exception:  # noqa: BLE001 — 建卡失败维持原生直发（内容永不丢）
                _logger.debug(
                    "composer: commentary early-create failed — native fallback chat=%s",
                    chat_id, exc_info=True,
                )
                return None
            st = self.active_state(chat_id)
        if st is None or not text:
            return None  # 无卡片：原生（含 💾/⏳ 后台通知——永不建卡，跑马灯根因修复）
        # v2.0.8：非 emoji 播报（模型可见间歇文本 commentary，如「SpaceX买」碎片）
        # 按 V1 语义归入思考轮次（灰色 notation 行 + 合并/幂等/增长取代）；
        # emoji 通报（⏳/💾 等）保持原样独立行。
        if not _EMOJI_RE.match(text):
            text = f"{_THINKING_PREFIX} {text}"
        lines = split_progress_segments(text) or ([text] if text else [])
        old_len = len(st.status_lines)
        merged = _merge_progress_lines(st.status_lines, lines)
        if merged is None:
            return SendResult(success=True, message_id=st.card_message_id)
        new_times = _reconcile_line_times(st.status_lines, st.line_times, merged)
        st.status_lines = merged
        st.line_times = new_times
        # OPT-1a：新增行即时上传；思考增长类原地改写 → 尾沿合并（0.2s 窗口）。
        await self._queue_panel_flush(st, urgent=len(merged) > old_len)
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
        # OPT-5：发往卡片的视图过流式 markdown 卫生化（oneshot 卡 special.py
        # 同款待遇）。状态缓冲恒存原文——面板标题/耗时表键/合并语义都按原文。
        answer_view = md_hygiene_cached(st.answer_text or "", st.answer_md_cache)
        lines_view = [
            md_hygiene_line_cached(ln, st.md_lines_cache) for ln in st.status_lines
        ]
        card = self._build_card(
            status_lines=lines_view,
            answer_text=answer_view,   # 防全量更新把已流 answer 重置为空
            panel_title=_panel_title(st.status_lines, elapsed_ms=_turn_elapsed_ms(st)),
            status_durations=dict(st.tool_durations),
            status_times=list(st.line_times),
        )
        # OPT-1b：30KB 悬崖守卫——上传前实测序列化体积，超预算从最旧面板行裁起
        # （复用 _enforce_card_element_limit 的裁剪走查与 trim hint 提示机制，
        # UX-1a 起为诚实双语措辞「⚡ N earlier steps omitted / 前 N 项已省略」；
        # answer 永不裁）。生产实证：34 行状态 19.3KB/update，~12KB
        # 答案即撞官方 30KB 上限（200860）→ 全通道中途降级；裁剪只作用于本次
        # 上传视图，st.status_lines 恒存全量（不丢过程记录）。
        card = _enforce_card_byte_budget(card)
        # v2.0.24：首个状态行到达时全量更新补插面板（懒渲染，s172 实证）；
        # 此处 status_lines 恒非空（调用点均已过 merge 非空门），面板必在。
        st.panel_on_card = _card_has_panel(card)
        # v2.0.32（P1-3 方案B）：全量更新同样打在流式会话上，到期先续期。
        await self._renew_stream_if_due(st)
        st.seq += 1
        st.last_activity = time.monotonic()
        await call_with_seq_recovery(
            st,
            lambda seq: client.cardkit_update(st.card_id, card, sequence=seq),
            what="update:status",
            reopen=self._reopen_closure(st),
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
        # OPT-1a：定稿入口强制冲刷挂起面板状态（取消合并 timer，同步上传）——
        # 最后面板状态必须先于 finalize 四步 API 落卡（流式正确性红线）。
        # 置 finalized 旗标之前冲刷：_flush_panel_now 对终态非 force 不发包。
        await self._flush_panel_now(st.chat_id, st)
        st.finalized = True
        if content:
            st.answer_text = content
        # 网关最终预影搬运（show_reasoning=true 时最终 response 最前是推理块）：
        # 与内置飞书一致的“定稿一次性补”语义——推理收进折叠面板，不留答案正文。
        # 剥出的推理文本以 💬 思考行并入面板（渲染层按轮次分组 + markdown 渲染）。
        try:
            clean_answer, reasoning_text = _extract_reasoning_block(st.answer_text or "")
            if reasoning_text:
                st.answer_text = clean_answer
                row = f"{_THINKING_PREFIX} {reasoning_text}"
                # 避免与面板里已有的同文思考行重复（与网关进度气泡的中继去重同语义）
                if row not in st.status_lines:
                    st.status_lines.append(row)
                    st.line_times.append(time.monotonic())
        except Exception:  # noqa: BLE001 — 推理搬运纯装饰，失败维持原答案
            _logger.debug("composer: reasoning block relocate skipped chat=%s", st.chat_id, exc_info=True)
        st.last_activity = time.monotonic()
        await finalize_card(
            client, st, answer_element_id=_answer_element_id(),
            footer_fields=self._footer_fields(),
            footer_show_label=self._footer_show_label(),
            panel_title=_panel_title(st.status_lines, elapsed_ms=_turn_elapsed_ms(st)),
            panel_expanded=self._panel_expanded(),
            model_name=self._resolve_footer_model(st.chat_id),
            reopen=self._reopen_closure(st),
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
            # OPT-1a：clarify 暂停行同走置脏+尾沿合并（与 absorb/interim 同一
            # 护栏）；本行是首块面板时 _queue_panel_flush 的懒渲染分支保持即时
            # 补插（v2.0.24 语义，test_v226「暂停行补插面板」契约不回退）。
            await self._queue_panel_flush(st)
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
        except Exception as exc:
            # v2.0.33（审计 FIX-2）：_finalize 先置 finalized=True 再跑远端三步
            # 定稿——定稿中段抛异常（如 answer 终值 patch 撞不可自愈错误）时
            # 状态已带 finalized 旗标，看门狗对其豁免，远端卡将永挂流式态
            # （跑马灯家族）。原 blanket debug 吞异常后无人收口。与 send/
            # edit/stream_frame 帧路径同语义：_degrade 退役卡片通道 +
            # _seal_degraded_card 优雅关流式（幂等：无卡/client 缺失为 no-op；
            # state 已 finalized 不重复消费）。
            self._degrade(chat_id, "pause_expire", exc)
            await self._seal_degraded_card(chat_id)
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

    async def _seal_degraded_card(self, chat_id: str) -> None:
        """v2.0.32（审计 P0-2）：降级瞬间优雅收口远端卡（跑马灯永挂根治）.

        事故实证（11-07 12:11:08 / 15:18:35）：200850 撞墙 → _degrade 只置本端
        旗标，远端卡 streaming_mode 仍开、spinner 永挂，用户手动删卡才消失。
        本方法与看门狗 seal 同款语义：复用 finalize_card（answer 终值 + 页脚 +
        关流式 + 摘要），给用户一个体面的终态卡；后续帧走原生文本，内容永不丢。
        与降级同锁（调用点均在 chat 锁内），远端 API 失败仅 debug——本端已
        停止消费该卡，官方 ~10min 流式会话自愈为最终防线，主链不依赖。
        """
        st = self._states.get(chat_id)
        if st is None or not st.card_id:
            return  # 占位/无卡降级（如 client 缺失）无远端卡可收口
        if await self._seal_live_card(st):
            _logger.info(
                "composer: degraded card sealed chat=%s card=%s",
                chat_id[:12], str(st.card_id)[:12],
            )

    async def _seal_live_card(self, st: CardTurnState) -> bool:
        """对单张远端活跃卡执行 finalize_card 优雅收口（降级/GC/停机共用核心）.

        v2.0.33 自 _seal_degraded_card 抽出：finalize_card 四步（answer 终值
        patch + 页脚 + 面板收口 + 关流式）对「已脱离消费链路的活跃卡」语义
        一致——降级收口、GC 孤儿收口、网关停机收口（seal_all_on_shutdown）
        三条主链共用。调用方负责先置 degraded+finalized 停止消费；远端 API
        失败仅 debug 不上抛（官方 ~10min 流式会话自愈为最终防线），返回是否
        真正落盘（供调用方记 info 日志/计数）。
        """
        client = self._get_client()
        if client is None:
            return False
        try:
            from .finalize import finalize_card

            # OPT-1a：收口前强制冲刷挂起面板状态。降级/GC 孤儿/停机收口的调用方
            # 均已置终旗标或已把状态脱离注册表（force=True 保障仍发包）；并发
            # timer 冲刷查 _states 落空/见终旗标自然跳过，无双发竞态。冲刷失败
            # 不阻断收口（下方 finalize_card 同款 best-effort）。
            with contextlib.suppress(Exception):
                await self._flush_panel_now(st.chat_id, st, force=True)

            await finalize_card(
                client, st, answer_element_id=_answer_element_id(),
                footer_fields=self._footer_fields(),
                footer_show_label=self._footer_show_label(),
                panel_title=_panel_title(st.status_lines, elapsed_ms=_turn_elapsed_ms(st)),
                panel_expanded=self._panel_expanded(),
                model_name=self._resolve_footer_model(st.chat_id),
                reopen=self._reopen_closure(st),
            )
        except Exception:
            _logger.debug(
                "composer: card seal skipped card=%s",
                str(st.card_id)[:12], exc_info=True,
            )
            return False
        return True

    async def seal_all_on_shutdown(self) -> int:
        """网关停机收口：对全部未落定的远端活跃卡执行优雅定稿（v2.0.33 FIX-1）.

        证据（agent.log/gateway.log 2026-11-07 13:50:38-39）：网关停机序列里
        通知卡建卡（composer: card created）与「Sent shutdown notification」
        同秒，随后 adapter 断连、进程退出、零 finalize → 远端卡 streaming
        永挂（用户需手动删卡）。内置基类 disconnect 只停自家 WS，感知不到
        本插件的 composer 状态表；本方法由 adapter.disconnect 在基类断连后
        调用，逐卡 best-effort 收口。

        语义：非 finalized 且非 degraded 且有远端卡的状态全部收口——含
        paused/pause_pending（暂停卡同样是远端活卡，跑马灯同族）。每卡复用
        _seal_live_card（finalize 四步），单卡失败 warning 后继续（个体失败
        不阻断其余卡）；总预算 ~5s（deadline 检查，超时即停——收口不应拖住
        停机序列，残余卡交官方流式会话自愈）。返回成功收口的卡数，并记
        'composer: shutdown seal closed N live card(s)' 供停机取证。
        """
        sealed = 0
        deadline = time.monotonic() + 5.0
        for chat_id, st in list(self._states.items()):
            if st.finalized or st.degraded or not st.card_id:
                continue
            st.degraded = True
            st.finalized = True  # 停止消费（与 _watchdog_seal 同款先置旗标）
            self._cancel_pause(chat_id)  # 暂停卡收口后暂停窗口定时器无意义
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _logger.warning(
                    "composer: shutdown seal budget exhausted chat=%s card=%s",
                    chat_id[:12], str(st.card_id)[:12],
                )
                break
            try:
                ok = await asyncio.wait_for(self._seal_live_card(st), timeout=remaining)
                if ok:
                    sealed += 1
            except Exception:
                # wait_for 超时取消（预算耗尽）或 seal 意外上抛：个体失败
                # 不阻断其余卡（本端旗标已置，进程即将退出无后续消费）。
                _logger.warning(
                    "composer: shutdown seal failed chat=%s card=%s",
                    chat_id[:12], str(st.card_id)[:12], exc_info=True,
                )
        _logger.info("composer: shutdown seal closed %d live card(s)", sealed)
        return sealed

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
            # v2.0.33（审计 FIX-7）：重武装周期走配置解析（_watchdog_idle_sec，
            # 60~1800s 钉制）而非硬编码常量——原实现 arm 与 due 两处只看配置，
            # 重武装却钉死 300s，自定义 watchdog_idle_sec < 300 的部署里 due
            # 判定按配置阈值放行后重武装却要再等 300s（孤儿卡收口延迟最高
            # 5 倍于配置值）；> 300 配置则提前到期、due 内静默复查挡住（无害
            # 但空转）。统一以 _watchdog_idle_sec() 为唯一事实源。
            loop.call_later(self._watchdog_idle_sec(), self._watchdog_due, chat_id)
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
            if time.monotonic() - st.last_activity >= self._watchdog_idle_sec():
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
        60s < 阈值；到期回调静默不达标时重新武装。v2.0.32（P1-1）：阈值
        120s → 300s 可配置（11-08 16:05 压缩期误封实证——回合存活、网关
        上下文压缩 300s+ 静默被误判孤儿；300s 与压缩首个检查点同量级）。

        触发（静默 ≥ 阈值且未终态）：复用 finalize_card 优雅落定（footer+
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
                idle_sec = self._watchdog_idle_sec()
                if time.monotonic() - st.last_activity < idle_sec:
                    return  # 双保险：ensure_future 窗口内回合恢复活跃
                # OPT-1a：收口前强制冲刷挂起面板状态（此刻未置终旗标，非 force
                # 即可发；先落卡再 seal API——流式正确性红线）。
                await self._flush_panel_now(chat_id, st)
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
                        reopen=self._reopen_closure(st),
                    )
                except Exception:
                    _logger.debug(
                        "composer: watchdog seal (api) skipped card=%s",
                        str(st.card_id)[:12], exc_info=True,
                    )
                _logger.warning(
                    "composer: watchdog sealed idle card chat=%s card=%s idle>=%.0fs "
                    "(no finalize frame — likely out-of-turn notice)",
                    chat_id[:12], str(st.card_id)[:12], idle_sec,
                )
        except Exception:
            _logger.debug("composer: watchdog failed chat=%s", chat_id, exc_info=True)

    # ── 清理 ────────────────────────────────────────────────────────────
    def forget(self, chat_id: str) -> None:
        st = self._states.pop(chat_id, None)
        if st and st.card_message_id:
            self._by_message.pop(st.card_message_id, None)
        self._cancel_pause(chat_id)  # v2.0.26：暂停窗口定时器随状态一并清理
        # OPT-1a：面板合并 timer 随状态清理（避免无主 timer 到期后空转）。
        self._cancel_panel_flush(chat_id)
        # OPT-3：状态已弹出且锁空闲无等待者 → 驱逐锁对象（锁集合回收口；
        # adapter.delete_message 的 forget 不持 chat 锁，此处真正生效）。
        self._evict_chat_lock_if_idle(chat_id)

    def clear(self) -> None:
        for cid in list(self._pause_timers):
            self._cancel_pause(cid)
        for cid in list(self._panel_flush_timers):
            self._cancel_panel_flush(cid)  # OPT-1a：合并 timer 全量清空
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

    def _resolve_footer_model(self, chat_id: str) -> str | None:
        """v2.0.31：页脚 model 字段取会话实际模型——/model 切换在 config 之外.

        注入的 model_resolver（adapter 会话存根）优先；未注入/返回空 →
        None → finalize 回退官方 _resolve_gateway_model（config 单一事实源）。
        """
        try:
            resolver = getattr(self, "_model_resolver", None)
            if resolver is not None:
                return resolver(chat_id) or None
        except Exception:  # noqa: BLE001 — 页脚装饰字段，失败回退 config 值
            _logger.debug("composer: session model resolve failed chat=%s", chat_id, exc_info=True)
        return None

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


# ── 网关推理预影搬运（show_reasoning=true 定稿一次性补）─────────────────────
def _extract_reasoning_block(text: str) -> tuple[str, str | None]:
    """从最终 answer 中剥出网关推理预影，返回 ``(clean_answer, reasoning_text)``。

    网关在回路结束时把最后一次推理拼在正文最前（`run_turn_persistence._hmwa_prepend_reasoning`），
    格式由 `locales/gateway.reasoning.block` 的当前语言模板决定：
    - en: ``💭 **Reasoning:**\n```\n{reasoning}\n```\n\n{response}``
    - zh: ``💭 **推理：**\n```\n{reasoning}\n```\n\n{response}``
    另外两种显示风格（reasoning_style=blockquote/subtext）只是把那段推理
    包在引用/次级文本里再拼正文，与 code 块保持一致的 peel 语义。

    本解析只在“块出现在回答最前方且围栏闭合”时触发：
    - 抑制正文自身含 markdown 分隔块而误伤（正文里只有内容，不会有
      ``💭 **推理：**`` 块头在第 0 行）；
    - 中英双语模板同款识别；
    - 多语言未来模板改文案时，下版随 Hermes 更新跟上即可（保守失败方向
      是不动正文，推理留在 answer，内容永不丢）。
    永不抛异常；未命中时原样返回。
    """
    try:
        t = (text or "").lstrip()
        if not t.startswith("💭"):
            return text, None
        lines = t.splitlines()
        if not lines:
            return text, None
        head = lines[0].strip()
        if "推理" not in head and "Reasoning" not in head:
            return text, None
        # 代码块风格：第二行应为 ```
        rest = "\n".join(lines[1:]).strip()
        if not rest.startswith("```"):
            return text, None
        end = rest.find("\n```")
        if end < 0:
            return text, None
        reasoning = rest[3:end].strip()
        if not reasoning:
            return text, None
        clean = rest[end + 4:].strip()
        return clean, reasoning
    except Exception:  # noqa: BLE001 — 推理搬运纯装饰，失败维持原文
        return text, None


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
        # 兜底清洗（流式工具双贴根因）：分隔符存在即官方复合帧信号；
        # 严格语法对新工具/中文动词连结符变异的尾行会整帧回滚为正文，
        # 答案区被工具行污染、面板又被独立进度气泡线签一份。
        # 这层只在尾段已有“emoji 工具样式行”时触发拆分，
        # 纯正文的 markdown 分隔线不会命中（无 emoji 首行）。
        tail_lines = [ln for ln in tail.splitlines() if ln.strip()]
        if any(_is_named_tool_line(ln.strip()) for ln in tail_lines) or (
            tail_lines and sum(1 for ln in tail_lines if _EMOJI_RE.match(ln.strip())) >= 1
            # 至少一半行是 emoji 开头，视为工具进度尾巴而非正文段落
            and sum(1 for ln in tail_lines if _EMOJI_RE.match(ln.strip())) * 2 >= len(tail_lines)
        ):
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


def _is_answer_duplicate(
    line: str, answer_text: str, *, answer_norm: str | None = None,
) -> bool:
    """💬 思考行是否为已流答案的复读（v2.0.8）.

    网关 ``_relay_thinking``（agent/turn_response_intake.py）把每次模型响应的
    **可见正文**前 500 字都中继成 ``💬 {text}`` 进度行——最终答案也不例外。
    答案已完整出现在卡片 answer 区域；V1 从不把 final answer 展示进面板
    （final 走 stream 通道），本过滤器对齐该语义。

    仅做前缀匹配（归一化空白后取前 60 字符、≥40 才判）：隐藏推理（reasoning
    内容与答案正文不同源）绝不误伤；碎片（<40 字符）保留。永不抛异常。

    OPT-2：``answer_norm`` 允许调用方传入预归一化 answer（_absorb_status_lines
    每次调用只算一次 " ".join(split())）；缺省就地计算，行为逐字等价。
    """
    try:
        text = (line or "").strip().removeprefix(_THINKING_PREFIX).strip()
        body = " ".join(text.split())[:60]
        if len(body) < 40:
            return False
        if answer_norm is not None:
            answer = answer_norm
        else:
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


def _panel_title(status_lines: list[str], *, elapsed_ms: float = 0.0) -> tuple[str, str]:
    """V1(1.8.1) 同款面板动态标题（UX-2a：双语 ``(en, zh)`` 文案对）.

    "agent loop" ·（N 轮）·（M 个工具）·（X.Xs）——各段有数据才拼接：
    轮次数 = 💬 思考行分组数；工具数 = 工具行条数（pending 已被取代不重复计）；
    elapsed = 首帧→当前实测（v2.0.3 曾以「帧无耗时」为由省略，v2.0.5 起由
    composer 本地实测，非网关伪造）。永不抛异常。

    UX-2a（审计，2026-11-09）：此前恒拼 zh 段（f"{rounds} 轮"/f"{tools} 个工具"
    硬编码），en 客户端用户看中文标题；i18n 表 rounds/tools_count 两键定义后
    零调用。现按 _T 元组序产出 ``(en, zh)`` 对，落卡由
    cards._apply_panel_title 写 content=en + i18n_content 双语（面板占位标题
    elements._build_unified_panel_placeholder 同款机制）。
    """
    try:
        rounds = tools = 0
        prev_thinking = False
        for ln in status_lines or []:
            try:
                # OPT-2：经模块级 memo（cards._parse_line_cached）——_patch_status
                # 每次全量渲染对同一批行只解析 1 次（旧：分组判定/渲染循环/
                # 本标题各解析一次 = 3 次）。
                kind, _tool, _c = _parse_line_cached(ln)
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
        # 基段与轮次/工具段走 i18n 表（此前 rounds/tools_count 为死键）
        from ..cardkit.i18n import _T

        en_base, zh_base = _T["agent_process"]
        en_parts = [en_base]
        zh_parts = [zh_base]
        if rounds:
            en_parts.append(_T["rounds"][0].format(rounds))
            zh_parts.append(_T["rounds"][1].format(rounds))
        if tools:
            en_parts.append(_T["tools_count"][0].format(tools))
            zh_parts.append(_T["tools_count"][1].format(tools))
        if elapsed_ms >= 100:  # <0.1s 不显示（避免 "0.0s" 噪音）
            from ..cardkit.elements import _format_elapsed

            elapsed_label = _format_elapsed(elapsed_ms)
            en_parts.append(elapsed_label)
            zh_parts.append(elapsed_label)
        return " · ".join(en_parts), " · ".join(zh_parts)
    except Exception:  # noqa: BLE001 — 标题永不影响主流程
        from ..cardkit.i18n import _T

        return _T["agent_process"]
