"""V2 定稿流程 — 卡片收尾：answer 终值落盘 + V1 风格页脚 + 关流式 + 摘要.

为什么不走 ``cardkit_update`` 全量重建：全量替换会把 answer 元素重置为
模板占位文本，已经流出去了的 answer 会被覆盖回去。因此定稿分三步：

1. ``cardkit_stream_element``：把 answer 终值再 patch 一次（保证元素内容是定稿全文）
2. ``cardkit_batch_update``：insert_before spinner 插入 V1 风格页脚
   （hr + 「已完成 · 耗时 · 模型」notation 灰字），再尽力删除 loading spinner
   ——装饰性步骤，任何失败只 warning，绝不上抛（否则上层降级走原生
   重发会造成重复消息；此时答案已经成功送达）。
3. ``cardkit_close_streaming``：关闭流式模式 + 更新会话摘要（折叠进消息预览）

页脚数据：``duration`` = 卡片创建→定稿耗时（monotonic）；``model`` = 官方
``gateway.run._resolve_gateway_model``（读 config.yaml 单一事实源，失败留空
→ 页脚自动跳过 model 字段）。
"""

from __future__ import annotations

import logging
import time
from typing import Any

_logger = logging.getLogger("hermes_lark_streaming.composer")

# 默认页脚字段（V1 对齐；拿不到数据的字段由渲染层自动跳过）。
_DEFAULT_FOOTER_FIELDS: list[list[str]] = [["status", "elapsed", "model"]]


def _summary_of(state: Any) -> str:
    text = (getattr(state, "answer_text", "") or "").strip()
    if not text:
        return "已完成"
    head = text.splitlines()[0].strip()
    return head[:120] or "已完成"


def _resolve_model_name() -> str:
    """网关配置的模型名（hermes 官方解析器；无 hermes 环境返回空 → 页脚跳过）。"""
    try:
        from gateway.run import _resolve_gateway_model

        return str(_resolve_gateway_model() or "").strip()
    except Exception:  # noqa: BLE001 — 页脚 model 字段尽力而为
        return ""


async def _seal_footer(
    client: Any,
    state: Any,
    *,
    footer_fields: list[list[str]] | None,
    show_label: bool,
    model_name: str,
) -> None:
    """插入 V1 风格页脚 + 删除 loading spinner（两步独立 batch_update）。"""
    from ..cardkit.elements import _LOADING_ELEMENT_ID, _build_footer_elements

    duration_sec = max(0.0, time.monotonic() - getattr(state, "created_at", time.monotonic()))
    footer_data = {"duration": duration_sec, "model": model_name or ""}
    elements = _build_footer_elements(
        footer_data,
        fields=footer_fields or _DEFAULT_FOOTER_FIELDS,
        show_label=show_label,
    )
    if not elements:
        return
    state.seq += 1
    await client.cardkit_batch_update(
        state.card_id,
        [{
            "action": "add_elements",
            "params": {
                "type": "insert_before",
                "target_element_id": _LOADING_ELEMENT_ID,
                "elements": elements,
            },
        }],
        sequence=state.seq,
    )
    # 删 spinner：尽力而为（streaming close 后飞书可能已隐藏 loading；
    # not found / 并发编辑失败都不影响页脚已经落卡）。
    try:
        state.seq += 1
        await client.cardkit_batch_update(
            state.card_id,
            [{"action": "delete_elements", "params": {"element_ids": [_LOADING_ELEMENT_ID]}}],
            sequence=state.seq,
        )
    except Exception:
        _logger.debug(
            "composer: loading spinner delete skipped card=%s", str(state.card_id)[:12],
            exc_info=True,
        )


async def finalize_card(
    client: Any,
    state: Any,
    *,
    answer_element_id: str,
    footer_fields: list[list[str]] | None = None,
    footer_show_label: bool = False,
) -> None:
    """把活跃卡片转为终态（三步，见模块 docstring）。"""
    client.cardkit_stream_element  # noqa: B018 — 提前触发 attribute 错误便于诊断
    state.seq += 1
    await client.cardkit_stream_element(
        state.card_id, answer_element_id, state.answer_text or "", sequence=state.seq,
    )
    try:
        await _seal_footer(
            client, state,
            footer_fields=footer_fields,
            show_label=footer_show_label,
            model_name=_resolve_model_name(),
        )
    except Exception:
        _logger.warning(
            "composer: footer seal failed (non-fatal) card=%s", str(state.card_id)[:12],
            exc_info=True,
        )
    state.seq += 1
    await client.cardkit_close_streaming(
        state.card_id, sequence=state.seq, summary=_summary_of(state),
    )
    _logger.debug(
        "composer: card finalized card=%s elapsed=%.1fs status_lines=%d",
        state.card_id[:12], time.monotonic() - state.created_at, len(state.status_lines),
    )
