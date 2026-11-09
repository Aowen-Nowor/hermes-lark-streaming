"""CardKit sequence 冲突（300317）与流式会话超时（200850）主流程自愈.

历史：v2.0.9 P0-1 落地 300317 自愈；v2.0.32（审计 P0-1/P1-3）追加 200850。

生产事故（gateway19.log 23:09:50，用户回合整段降级纯文本）：
``feishu/client._retry_transient`` 对网络读超时的重试用**同一 sequence** 重发；
首次请求若实际已到达服务端（响应在网络中丢失），重发必撞
``300317 sequence compare failed``。该码在 ``_is_transient_error`` 中不是
瞬态错误 → 直接上抛 → composer 整回合降级。

语义定案（真飞书实验 s98，卡 om_x100b63f0d183a880b1f89b86413d0d1）：
- 重放/回退 seq → 必现 300317
- seq+1 → 立即自愈
- 跳号（seq 大幅领先服务端）→ 允许

因此恢复策略是**主流程自愈**而非兜底：捕获 300317 → 本地 seq 对齐服务端
（+1）→ 重发同内容。所有走本 helper 的调用内容均为全量语义（answer 缓冲 /
状态快照 / 页脚插入），重发幂等。循环上限内多次冲突逐次 +1（网络重试最多
造成服务端领先一次，上限 3 是防御性余量）；上限外仍上抛交由降级红线。

200850 流式会话超时（v2.0.32，审计 P0-1/P1-3）：
官方 CardKit 流式会话寿命 ~10min（生产实证：11-07 12:01 建卡 → 12:11:08
600s 整必现；15:08 建卡 → 15:18:35 同族），到期后 stream_element 必返回
``200850 card streaming timeout``。官方恢复动作（开放平台 cardkit 文档，
审计 R5 核对）= settings ``streaming_mode:true`` 同卡重开，sequence 跳号
允许（s98）。恢复策略同为主流程自愈：捕获 200850 → seq+1 调 ``reopen``
重开会话 → seq+1 重发同内容；与 300317 同循环、同上限。调用方经可选
``reopen`` 参数注入重开闭包（engine/finalize 构造；未注入时 200850 原样
上抛，行为与旧版一致）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

_logger = logging.getLogger("hermes_lark_streaming.cardkit.seq")

#: 飞书 CardKit sequence 冲突错误码（feishu/client.CARDKIT_SEQUENCE_CONFLICT 同值；
#: 此处常量独立定义——composer/cardkit 层不依赖 feishu 实现，按 .code duck-type）。
SEQ_CONFLICT = 300317

#: 飞书 CardKit 流式会话超时错误码（feishu/client.CARDKIT_STREAMING_TIMEOUT 同值）。
STREAM_TIMEOUT = 200850

#: 自愈重试上限。网络层重试最多让服务端 sequence 领先 1 次；3 是防御性余量
#: （200850 自愈一次消耗 2 个 seq，同在上限内闭环）。
_MAX_SEQ_RECOVERIES = 3


def is_seq_conflict(exc: BaseException) -> bool:
    """异常是否为 300317（duck-type 读 ``.code``，兼容任何客户端异常类型）."""
    return getattr(exc, "code", None) == SEQ_CONFLICT


def is_stream_timeout(exc: BaseException) -> bool:
    """异常是否为 200850 流式会话超时（duck-type 读 ``.code``）."""
    return getattr(exc, "code", None) == STREAM_TIMEOUT


def _mark_session_renewed(st: Any) -> None:
    """重开后刷新状态上的会话起点（主动续期计时基线；无该字段则跳过）."""
    try:
        st.stream_session_at = time.monotonic()
    except Exception:  # 装饰性计时基线，失败不影响自愈主链
        _logger.debug("cardkit: stream_session_at mark skipped", exc_info=True)


async def call_with_seq_recovery(
    st: Any,
    send: Callable[[int], Awaitable[Any]],
    *,
    what: str,
    reopen: Callable[[int], Awaitable[Any]] | None = None,
) -> None:
    """执行 ``send(st.seq)``；300317 时本地 seq 对齐服务端（+1）重发同内容.

    200850（流式会话超时）且调用方注入 ``reopen`` 时：seq+1 → ``reopen(seq)``
    重开会话 → seq+1 重发同内容（官方恢复动作，s98 实证 seq 跳号允许）；
    重开成功即刷新 ``st.stream_session_at``（主动续期计时基线）。

    ``st`` 需带可变 ``seq`` 属性（composer CardTurnState / finalize state）。
    非自愈范围异常原样上抛；超过上限同样上抛（交由降级红线兜住消息不丢）。
    """
    for attempt in range(_MAX_SEQ_RECOVERIES):
        try:
            await send(st.seq)
            return
        except Exception as exc:
            if attempt == _MAX_SEQ_RECOVERIES - 1:
                raise
            if reopen is not None and is_stream_timeout(exc):
                st.seq += 1
                _logger.warning(
                    "cardkit: 200850 streaming timeout on %s — reopen streaming "
                    "(seq=%d) and resend",
                    what, st.seq,
                )
                await reopen(st.seq)
                _mark_session_renewed(st)
                st.seq += 1
                continue
            if not is_seq_conflict(exc):
                raise
            st.seq += 1
            _logger.warning(
                "cardkit: 300317 sequence conflict on %s — self-heal retry #%d seq=%d",
                what, attempt + 1, st.seq,
            )
