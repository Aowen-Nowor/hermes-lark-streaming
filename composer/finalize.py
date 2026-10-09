"""V2 定稿流程 — 卡片收尾：answer 终值落盘 + 关流式 + 摘要.

为什么不走 ``cardkit_update`` 全量重建：全量替换会把 answer 元素重置为
模板占位文本，已经流出去了的 answer 会被覆盖回去。因此定稿只用两步：

1. ``cardkit_stream_element``：把 answer 终值再 patch 一次（保证元素内容是定稿全文）
2. ``cardkit_close_streaming``：关闭流式模式 + 更新会话摘要（折叠进消息预览）

任何一步失败由上层（engine._degrade）兜底：置降级旗标，后续帧走原生文本。
"""

from __future__ import annotations

import logging
import time
from typing import Any

_logger = logging.getLogger("hermes_lark_streaming.composer")


def _summary_of(state: Any) -> str:
    text = (getattr(state, "answer_text", "") or "").strip()
    if not text:
        return "已完成"
    head = text.splitlines()[0].strip()
    return head[:120] or "已完成"


async def finalize_card(client: Any, state: Any, *, answer_element_id: str) -> None:
    """把活跃卡片转为终态（两步，见模块 docstring）。"""
    client.cardkit_stream_element  # noqa: B018 — 提前触发 attribute 错误便于诊断
    state.seq += 1
    await client.cardkit_stream_element(
        state.card_id, answer_element_id, state.answer_text or "", sequence=state.seq,
    )
    state.seq += 1
    await client.cardkit_close_streaming(
        state.card_id, sequence=state.seq, summary=_summary_of(state),
    )
    _logger.debug(
        "composer: card finalized card=%s elapsed=%.1fs status_lines=%d",
        state.card_id[:12], time.monotonic() - state.created_at, len(state.status_lines),
    )
