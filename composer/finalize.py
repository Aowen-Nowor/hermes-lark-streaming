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
from collections.abc import Callable
from typing import Any

from ..cardkit.seq import call_with_seq_recovery

_logger = logging.getLogger("hermes_lark_streaming.composer")

# 默认页脚字段（V1 对齐；拿不到数据的字段由渲染层自动跳过）。
# v2.0.21（审计 R10）：与 config/reader.py _default_footer_fields 对齐为同一
# 事实源 [status/elapsed/model]——本常量仅在 Config 不可用时兜底（engine.
# _footer_fields 异常路径），生产路径恒走 Config 默认（reasoning 条件追加）。
_DEFAULT_FOOTER_FIELDS: list[list[str]] = [["status", "elapsed", "model"]]


def _md_stream_view(text: str, state: Any) -> str:
    """OPT-5：终值的流式 markdown 卫生化视图（有缓存字段则用，无则直算）.

    与 engine._stream_answer/_patch_status 同一变换、同一缓存（oneshot 卡
    special.py 同款待遇）——定稿/收口不把已卫生化的流式内容回退成原文。
    """
    from ..cardkit.cards import md_hygiene_cached

    return md_hygiene_cached(text, getattr(state, "answer_md_cache", None))


def _empty_answer_summary() -> str:
    """UX-2c（审计，2026-11-09）：空 answer 摘要兑底——不再单语 zh.

    旧兑底恒返「已完成」，en 客户端会话列表预览恒中文。走 i18n 表
    status_completed（"Completed"/"已完成"）拼双语中性文案：
    ``cardkit_close_streaming(summary: str)`` 的 settings payload 把同一串
    写进 content 与 i18n_content 两语种（feishu/client.py:597-605），字符串
    接口无法携带双语对——双语中性串是客户端 API 约束下唯一诚实的解；
    非空 answer 的摘要取首行原文，本身语种中立，不受影响。
    """
    from ..cardkit.i18n import _T

    en_done, zh_done = _T["status_completed"]
    return f"{zh_done} · {en_done}"


def _summary_of(state: Any) -> str:
    text = (getattr(state, "answer_text", "") or "").strip()
    if not text:
        return _empty_answer_summary()
    head = text.splitlines()[0].strip()
    return head[:120] or _empty_answer_summary()


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
    # v2.0.19：footer reasoning 等级（display.show_reasoning: true 且
    # reasoning_effort 有效时供值；渲染层无值自动跳过该字段）。
    from ..config import Config

    reasoning_level = Config().footer_reasoning_level
    if reasoning_level:
        footer_data["reasoning_effort"] = reasoning_level
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
    panel_title: str | tuple[str, str],
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
        _aux_text_size,
        _collapsible_panel,
    )
    from ..cardkit.i18n import _T, _i18n

    lines = list(getattr(state, "status_lines", None) or [])
    if not lines:
        return False
    # OPT-5：面板行与流式期同一卫生化视图（同缓存）——收口重建不回退样式。
    from ..cardkit.cards import md_hygiene_line_cached

    line_cache = getattr(state, "md_lines_cache", None)
    view = [md_hygiene_line_cached(ln, line_cache) for ln in lines]
    durations = dict(getattr(state, "tool_durations", None) or {})
    times = list(getattr(state, "line_times", None) or [])
    final_children = _render_status_children(
        view, durations=durations, line_times=times, turn_state="finalized")
    if _render_status_children(
            view, durations=durations, line_times=times,
            turn_state="streaming") == final_children:
        return False  # 流式末渲染已无「进行中」轮，重建无视觉差异
    en_title, zh_title = _T["agent_process"]
    title_el: dict[str, Any] = {
        "tag": "plain_text",
        "content": en_title,
        "i18n_content": _i18n(en_title, zh_title),
        "text_color": "grey",
        # v2.0.20：card_style 已配置 → token；未配置 → v2.0.18 字面值 notation
        # （与流式期面板标题一致，收口重建不改观感）。
        "text_size": _aux_text_size(),
    }
    panel = _collapsible_panel(expanded=expanded, title_el=title_el, elements=final_children)
    if panel_title:
        _apply_panel_title(panel, panel_title)
    panel["element_id"] = UNIFIED_PANEL_ELEMENT_ID
    # UX-1b（审计，2026-11-09）：重建面板先过 200 元素上限安全网——与建卡
    # build_streaming_card_v2 出厂前同一机制（cards._enforce_card_element_limit）。
    # 此前直拼 state.status_lines 全量重建且无裁剪：流式期曾被元素/字节
    # 预算裁过的回合，定稿收口会把裁掉的行原样拼回 → add 超限 → 飞书对
    # delete+add 原子 batch 整体拒收（300305/11310）→ 面板留在流式渲染态，
    # 与定稿态不一致。enforcement 对包裹卡（panel 独占 body）计数：阈值 195
    # 与建卡口径一致，余量 5 吸收面板外的 answer/页脚/spinner 兄弟元素；
    # 裁剪/提示均就地改写本 panel dict（_enforce 返回包裹卡，panel 同引用）。
    from ..cardkit.cards import _enforce_card_element_limit

    _enforce_card_element_limit({"body": {"elements": [panel]}})
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
    panel_title: str | tuple[str, str] = "",
    panel_expanded: bool = True,
    model_name: str | None = None,
    reopen: Callable[[int], Any] | None = None,
) -> None:
    """把活跃卡片转为终态（四步，见模块 docstring）。

    v2.0.32：``reopen`` 为 settings streaming_mode:true 重开闭包（engine 注入），
    answer 终值 patch 撞 200850（长回合流式会话超时）时由 call_with_seq_recovery
    主流程自愈后重发——定稿是最后一刀，撞墙即弃会前功尽弃。
    """
    client.cardkit_stream_element  # noqa: B018 — 提前触发 attribute 错误便于诊断
    state.seq += 1
    # v2.0.9 P0-1：三步定稿全部走 300317 自愈（answer 终值/页脚/关流式）
    # v2.0.17：answer 空时跳过终值 patch——stream_element content="" 被飞书拒
    # （99992402，s152 真飞书探针定案）；纯工具回合 answer 为空是合法终态。
    # answer 元素骨架期无条件存在（空 content 占位），页脚 insert_before
    # 锚点不受影响。
    # v2.0.32：answer 终值 patch 接入 200850 重开自愈（reopen 闭包）。
    # OPT-4：终值与最近一次成功流式上传字节一致 → 跳过 patch（流式路径已把
    # 同内容落卡，省一次全量上传）。last_streamed_answer 缺失（直调/旧测试
    # state）或 answer 从未流过（空基线）→ 照旧 patch；reasoning 前插改写
    # answer_text → 与基线不等 → patch 照发（基线自然失效）。
    answer_text = state.answer_text or ""
    last_streamed = getattr(state, "last_streamed_answer", None) or ""
    if answer_text.strip() and answer_text != last_streamed:
        # OPT-5：终值与流式期同一卫生化视图（同缓存），定稿不回退样式。
        # 不 chunk/split（终值是单元素全量语义，同 _stream_answer）。
        answer_view = _md_stream_view(answer_text, state)
        await call_with_seq_recovery(
            state,
            lambda seq: client.cardkit_stream_element(
                state.card_id, answer_element_id, answer_view, sequence=seq),
            what="finalize:answer",
            reopen=reopen,
        )
        # OPT-4：patch 成功即刷新基线（seal 复用本函数时同样受益）。
        state.last_streamed_answer = answer_text
    try:
        await _seal_footer(
            client, state,
            footer_fields=footer_fields,
            show_label=footer_show_label,
            model_name=model_name if model_name is not None else _resolve_model_name(),
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
    # v2.0.24（用户反馈）：面板懒渲染（cards.py 仅 status_lines 非空时渲染）——
    # 纯 answer 回合建卡即无面板，本分支从「主清理路径」退居防御性保险：
    # panel_on_card=True 且行空（理论不可达的旗标/内容错位）才发起删除，
    # 正常路径不再对从未有过面板的卡发 delete_elements（省 API + 免日志噪声）。
    # v2.0.16：有面板行时先做收口重渲染（流式「进行中橙」→ 定稿「已完成绿」），
    # 与空面板删除互斥；两者均为装饰性步骤：失败仅 debug/warning，不上抛阻断定稿。
    if not (getattr(state, "status_lines", None) or []) and getattr(
        state, "panel_on_card", False
    ):
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
    # v2.0.21（审计 R3）：关流式装饰性化——此前失败上抛 → _degrade → 卡片
    # 通道退役 + 官方回落路径要么被幂等守卫短路（跑马灯永挂：流式模式未关，
    # spinner 持续转），要么原生重发全文（重复消息）。此刻答案已流完送达、
    # 页脚已落卡，关流式失败的最坏后果 = 卡片留在流式态，对齐
    # _seal_footer/_reseal_panel「装饰性步骤失败仅 warning 绝不上抛」语义。
    try:
        await call_with_seq_recovery(
            state,
            lambda seq: client.cardkit_close_streaming(
                state.card_id, sequence=seq, summary=_summary_of(state)),
            what="finalize:close",
        )
    except Exception:
        _logger.warning(
            "composer: close_streaming skipped (non-fatal) card=%s",
            str(state.card_id)[:12], exc_info=True,
        )
    _logger.debug(
        "composer: card finalized card=%s elapsed=%.1fs status_lines=%d",
        state.card_id[:12], time.monotonic() - state.created_at, len(state.status_lines),
    )
