"""CardKit sequence 冲突（300317）主流程自愈 — v2.0.9 P0-1.

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
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

_logger = logging.getLogger("hermes_lark_streaming.cardkit.seq")

#: 飞书 CardKit sequence 冲突错误码（feishu/client.CARDKIT_SEQUENCE_CONFLICT 同值；
#: 此处常量独立定义——composer/cardkit 层不依赖 feishu 实现，按 .code duck-type）。
SEQ_CONFLICT = 300317

#: 自愈重试上限。网络层重试最多让服务端 sequence 领先 1 次；3 是防御性余量。
_MAX_SEQ_RECOVERIES = 3


def is_seq_conflict(exc: BaseException) -> bool:
    """异常是否为 300317（duck-type 读 ``.code``，兼容任何客户端异常类型）."""
    return getattr(exc, "code", None) == SEQ_CONFLICT


async def call_with_seq_recovery(
    st: Any,
    send: Callable[[int], Awaitable[Any]],
    *,
    what: str,
) -> None:
    """执行 ``send(st.seq)``；300317 时本地 seq 对齐服务端（+1）重发同内容.

    ``st`` 需带可变 ``seq`` 属性（composer CardTurnState / finalize state）。
    非冲突异常原样上抛；冲突超过上限同样上抛（交由降级红线兜住消息不丢）。
    """
    for attempt in range(_MAX_SEQ_RECOVERIES):
        try:
            await send(st.seq)
            return
        except Exception as exc:
            if not is_seq_conflict(exc) or attempt == _MAX_SEQ_RECOVERIES - 1:
                raise
            st.seq += 1
            _logger.warning(
                "cardkit: 300317 sequence conflict on %s — self-heal retry #%d seq=%d",
                what, attempt + 1, st.seq,
            )
