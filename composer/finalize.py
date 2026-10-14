"""V2 定稿流程 — 卡片收尾：answer 终值落盘 + V1 风格页脚 + 关流式 + 摘要.

为什么不走 ``cardkit_update`` 全量重建：全量替换会把 answer 元素重置为
模板占位文本，已经流出去了的 answer 会被覆盖回去。因此定稿分四步：

1. ``cardkit_stream_element``：把 answer 终值再 patch 一次（保证元素内容是定稿全文）
2. ``cardkit_batch_update``：insert_before spinner 插入 V1 风格页脚
   （hr + 「已完成 · 耗时 · 模型」notation 灰字），再尽力删除 loading spinner
   ——装饰性步骤，任何失败只 warning，绝不上抛（否则上层降级走原生
   重发会造成重复消息；此时答案已经成功送达）。
3. ``cardkit_batch_update``（v2.0.16）：面板收口重渲染——流式期最后一轮
   思考「进行中橙」→「已完成绿」（仅当流式末渲染存在未封口轮时才发
   API；通道为 s146 真飞书探针定案：官方 batch_update 无 update_elements
   action（code=10002 实测），用同批次 delete 面板 + add 重建面板
   （insert_before answer 元素）。空面板（status_lines=0）走原删除分支。
4. ``cardkit_close_streaming``：关闭流式模式 + 更新会话摘要（折叠进消息预览）

页脚数据：``duration`` = 卡片创建→定稿耗时（monotonic）；``model`` = 官方
``gateway.run._resolve_gateway_model``（读 config.yaml 单一事实源，失败留空
→ 页脚自动跳过 model 字段）。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..cardkit.seq import call_with_seq_recovery

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
    # v2.0.9 P0-1：页脚插入同样走 300317 自愈（网络重试同 seq 重发场景）
    await call_with_seq_recovery(
        state,
        lambda seq: client.cardkit_batch_update(
            state.card_id,
            [{
                "action": "add_elements",
                "params": {
                    "type": "insert_before",
                    "target_element_id": _LOADING_ELEMENT_ID,
                    "elements": elements,
                },
            }],
            sequence=seq,
        ),
        what="finalize:footer-add",
    )
    # 删 spinner：尽力而为（streaming close 后飞书可能已隐藏 loading；
    # not found / 并发编辑失败都不影响页脚已经落卡）。
    try:
        state.seq += 1
        await call_with_seq_recovery(
            state,
            lambda seq: client.cardkit_batch_update(
                state.card_id,
                [{"action": "delete_elements", "params": {"element_ids": [_LOADING_ELEMENT_ID]}}],
                sequence=seq,
            ),
            what="finalize:spinner-del",
        )
    except Exception:
        _logger.debug(
            "composer: loading spinner delete skipped card=%s", str(state.card_id)[:12],
            exc_info=True,
        )


async def _reseal_panel(
    client: Any,
    state: Any,
    *,
    panel_title: str,
    expanded: bool,
) -> bool:
    """面板收口重渲染：流式期「进行中橙」→ 定稿「已完成绿」（v2.0.16）.

    s146 真飞书探针定案（2026-10-13）：官方 batch_update 无 update_elements
    action（V1/V2 变体 code=10002 "action is invalid"），可行通道 = 同批次
    ``delete_elements(面板)`` + ``add_elements(insert_before answer)`` 重建
    面板（marker 语义验证通过）。

    幂等优化：流式末渲染本就无未封口轮（重建结果与重建前逐字节一致）时
    跳过 API（可对照性：同输入渲染确定性）。装饰性步骤：失败仅 warning
    不上抛（答案已送达；极端失败模式 = 面板消失，CHANGELOG 已知限制）。
    返回是否实际重建（测试观测点）。
    """
    from ..cardkit.cards import _apply_panel_title, _render_status_children
    from ..cardkit.elements import (
        ANSWER_ELEMENT_ID,
        UNIFIED_PANEL_ELEMENT_ID,
        _collapsible_panel,
    )
    from ..cardkit.i18n import _T, _i18n

    lines = list(getattr(state, "status_lines", None) or [])
    if not lines:
        return False
    durations = dict(getattr(state, "tool_durations", None) or {})
    times = list(getattr(state, "line_times", None) or [])
    final_children = _render_status_children(
        lines, durations=durations, line_times=times, turn_state="finalized")
    if _render_status_children(
            lines, durations=durations, line_times=times,
            turn_state="streaming") == final_children:
        return False  # 流式末渲染已无「进行中」轮，重建无视觉差异
    en_title, zh_title = _T["agent_process"]
    title_el: dict[str, Any] = {
        "tag": "plain_text",
        "content": en_title,
        "i18n_content": _i18n(en_title, zh_title),
        "text_color": "grey",
        "text_size": "notation",
    }
    panel = _collapsible_panel(expanded=expanded, title_el=title_el, elements=final_children)
    if panel_title:
        _apply_panel_title(panel, panel_title)
    panel["element_id"] = UNIFIED_PANEL_ELEMENT_ID
    state.seq += 1
    await call_with_seq_recovery(
        state,
        lambda seq: client.cardkit_batch_update(
            state.card_id,
            [
                {"action": "delete_elements",
                 "params": {"element_ids": [UNIFIED_PANEL_ELEMENT_ID]}},
                {"action": "add_elements",
                 "params": {"type": "insert_before",
                            "target_element_id": ANSWER_ELEMENT_ID,
                            "elements": [panel]}},
            ],
            sequence=seq,
        ),
        what="finalize:panel-reseal",
    )
    return True


async def finalize_card(
    client: Any,
    state: Any,
    *,
    answer_element_id: str,
    footer_fields: list[list[str]] | None = None,
    footer_show_label: bool = False,
    panel_title: str = "",
    panel_expanded: bool = True,
) -> None:
    """把活跃卡片转为终态（四步，见模块 docstring）。"""
    client.cardkit_stream_element  # noqa: B018 — 提前触发 attribute 错误便于诊断
    state.seq += 1
    # v2.0.9 P0-1：三步定稿全部走 300317 自愈（answer 终值/页脚/关流式）
    # v2.0.17：answer 空时跳过终值 patch——stream_element content="" 被飞书拒
    # （99992402，s152 真飞书探针定案）；纯工具回合 answer 为空是合法终态。
    # answer 元素骨架期无条件存在（空 content 占位），页脚 insert_before
    # 锚点不受影响。
    if (state.answer_text or "").strip():
        await call_with_seq_recovery(
            state,
            lambda seq: client.cardkit_stream_element(
                state.card_id, answer_element_id, state.answer_text, sequence=seq),
            what="finalize:answer",
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
    # v2.0.13（Task 18 P1-A）：空面板壳清除——整轮无工具/思考行（status_lines=0）
    # 时删除 collapsible_panel 元素。纯 answer 回合（Task 18 取证 16:54:57 卡：
    # created status_lines=0 → finalized status_lines=0）卡上遗留 "agent loop"
    # 空面板头（cards.py 无条件渲染面板骨架——流式期作「卡片先现」视觉锚，终态
    # 后无内容即成空壳）。有面板行的卡片承载过程记录，保留。
    # v2.0.16：有面板行时先做收口重渲染（流式「进行中橙」→ 定稿「已完成绿」），
    # 与空面板删除互斥；两者均为装饰性步骤：失败仅 debug/warning，不上抛阻断定稿。
    if not (getattr(state, "status_lines", None) or []):
        try:
            from ..cardkit.elements import UNIFIED_PANEL_ELEMENT_ID

            state.seq += 1
            await call_with_seq_recovery(
                state,
                lambda seq: client.cardkit_batch_update(
                    state.card_id,
                    [{
                        "action": "delete_elements",
                        "params": {"element_ids": [UNIFIED_PANEL_ELEMENT_ID]},
                    }],
                    sequence=seq,
                ),
                what="finalize:panel-del",
            )
        except Exception:
            _logger.debug(
                "composer: empty panel delete skipped card=%s", str(state.card_id)[:12],
                exc_info=True,
            )
    else:
        try:
            await _reseal_panel(
                client, state, panel_title=panel_title, expanded=panel_expanded)
        except Exception:
            _logger.warning(
                "composer: panel reseal skipped (non-fatal) card=%s",
                str(state.card_id)[:12], exc_info=True,
            )
    state.seq += 1
    await call_with_seq_recovery(
        state,
        lambda seq: client.cardkit_close_streaming(
            state.card_id, sequence=seq, summary=_summary_of(state)),
        what="finalize:close",
    )
    _logger.debug(
        "composer: card finalized card=%s elapsed=%.1fs status_lines=%d",
        state.card_id[:12], time.monotonic() - state.created_at, len(state.status_lines),
    )
