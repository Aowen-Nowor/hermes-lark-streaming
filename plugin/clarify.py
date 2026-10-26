"""澄清卡片闭环 — 澄清问题以交互卡片呈现，按钮/输入直接解析.

自 V1 架构完整移植，去钩子化：不再是 monkey-patch 拦截层，而是
``FeishuStreamingAdapter`` 重写 ``send_clarify`` / ``_handle_card_action_event``
时调用的正式实现模块。

三态流程：选择（soft-lock 卡片 + 重试按钮）→ resolve_gateway_clarify →
服务端确认更新（hard-lock）→ 清理。

安全红线：操作者授权检查 fail-closed（无授权方法 / 校验失败一律拒绝点击）。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

_logger = logging.getLogger("hermes_lark_streaming.clarify")

_CLARIFY_TTL_SEC = 300

_clarify_lock = threading.Lock()
_clarify_choices: dict[str, list[str]] = {}
_clarify_questions: dict[str, str] = {}
_clarify_timestamps: dict[str, float] = {}
_clarify_card_msg_ids: dict[str, str] = {}
_clarify_selections: dict[str, str] = {}


def prune_expired() -> None:
    """清理过期 clarify 记录（幂等）。"""
    now = time.time()
    expired: list[str] = []
    with _clarify_lock:
        for cid, ts in _clarify_timestamps.items():
            if now - ts > _CLARIFY_TTL_SEC:
                expired.append(cid)
        for cid in expired:
            _clarify_choices.pop(cid, None)
            _clarify_questions.pop(cid, None)
            _clarify_card_msg_ids.pop(cid, None)
            _clarify_selections.pop(cid, None)
            _clarify_timestamps.pop(cid, None)


def _cleanup(cid: str) -> None:
    """移除单个 clarify_id 的全部记录（幂等）。"""
    with _clarify_lock:
        _clarify_choices.pop(cid, None)
        _clarify_questions.pop(cid, None)
        _clarify_card_msg_ids.pop(cid, None)
        _clarify_selections.pop(cid, None)
        _clarify_timestamps.pop(cid, None)


# ── SDK 回调响应构建 ────────────────────────────────────────────────────────
def _load_response_classes() -> tuple[Any, Any]:
    """多候选路径加载 SDK 回调响应类（跨版本防御）.

    v2.0.21 审计发现：lark-oapi 新版（≥1.7.x）把 CallBackCard/
    P2CardActionTriggerResponse 从 ``lark_oapi.api.cardkit.v1`` 迁移到
    ``lark_oapi.event.callback.model.p2_card_action_trigger``——旧路径
    ImportError 时回调卡片静默退化为空响应（_empty_response），用户点击
    零反馈。多候选依序尝试，全部失败才退化。
    """
    candidates = (
        "lark_oapi.api.cardkit.v1",
        "lark_oapi.event.callback.model.p2_card_action_trigger",
    )
    for mod_path in candidates:
        try:
            mod = __import__(mod_path, fromlist=("CallBackCard", "P2CardActionTriggerResponse"))
            return mod.P2CardActionTriggerResponse, mod.CallBackCard
        except (ImportError, AttributeError):
            # AttributeError：模块存在但类已迁走（1.7.x 实测——旧路径模块仍可导入）
            continue
    return None, None


def _empty_response() -> Any:
    P2CardActionTriggerResponse, _ = _load_response_classes()
    if P2CardActionTriggerResponse is None:
        return None
    return P2CardActionTriggerResponse()


def _expired_response() -> Any:
    """v2.0.21：过期/未知 clarify_id 点击的回调卡片（替代静默空响应）.

    触发面：网关进程重启（选择记录全内存，重启即丢）/ TTL 300s 过期被
    prune / 记录被新 clarify 覆盖。此前返回 _empty_response() —— 用户点击
    选项/重试/提交后卡片无任何反馈，只能干等（P2 审计 R7）。现回一张
    「已过期」提示卡，明确引导重新发起提问。
    """
    P2CardActionTriggerResponse, CallBackCard = _load_response_classes()
    if P2CardActionTriggerResponse is None or CallBackCard is None:
        return _empty_response()
    from ..cardkit.i18n import _LOCALES, _T, _i18n

    en_text, zh_text = _T["clarify_expired"]
    card_data = {
        "schema": "2.0",
        "config": {"locales": _LOCALES},
        "body": {
            "elements": [
                {
                    "tag": "div",
                    "icon": {
                        "tag": "standard_icon",
                        "token": "time_outlined",
                        "size": "20px 20px",
                        "color": "grey",
                    },
                    "text": {
                        "tag": "lark_md",
                        "content": en_text,
                        "i18n_content": _i18n(en_text, zh_text),
                    },
                },
            ],
        },
    }
    response = P2CardActionTriggerResponse()
    card = CallBackCard()
    card.type = "raw"
    card.data = card_data
    response.card = card
    return response


def _submitted_card_response(selected_text: str, q: str, cid: str) -> Any:
    """soft-lock 已提交态回调卡片（含重试按钮）。"""
    P2CardActionTriggerResponse, CallBackCard = _load_response_classes()
    if P2CardActionTriggerResponse is None or CallBackCard is None:
        return _empty_response()
    from ..cardkit import build_clarify_submitted_card

    card_data = build_clarify_submitted_card(question=q, selected=selected_text, clarify_id=cid)
    response = P2CardActionTriggerResponse()
    card = CallBackCard()
    card.type = "raw"
    card.data = card_data
    response.card = card
    return response


# ── 发送 ────────────────────────────────────────────────────────────────────
async def send_clarify_card(
    adapter: Any,
    client: Any,
    chat_id: str,
    question: str,
    choices: list[str] | None,
    clarify_id: str,
    metadata: dict[str, Any] | None = None,
) -> Any | None:
    """以交互卡片发送澄清问题。失败返回 None（调用方降级为官方文本路径）。"""
    from ..cardkit import build_clarify_card, normalize_clarify_choices

    prune_expired()
    normalized = normalize_clarify_choices(choices) if choices else None
    card = build_clarify_card(question=question, choices=normalized, clarify_id=clarify_id)

    with _clarify_lock:
        if normalized:
            _clarify_choices[clarify_id] = list(normalized)
        _clarify_questions[clarify_id] = question
        _clarify_timestamps[clarify_id] = time.time()

    reply_to: str | None = None
    if metadata and isinstance(metadata, dict):
        reply_to = metadata.get("reply_to") or metadata.get("message_id")

    if reply_to:
        card_msg_id = await client.reply_card(reply_to, card)
    else:
        card_msg_id = await client.send_card_to_chat(chat_id, card)

    with _clarify_lock:
        if card_msg_id:
            _clarify_card_msg_ids[clarify_id] = str(card_msg_id)

    try:
        from tools.clarify_gateway import mark_awaiting_text

        mark_awaiting_text(clarify_id)
    except Exception as e:  # noqa: BLE001 — best-effort：失败由卡片回调兜底
        _logger.debug("clarify: mark_awaiting_text failed (%s) — card callback will resolve", e)

    try:
        from gateway.platforms.base import SendResult

        return SendResult(success=True, message_id=str(card_msg_id) if card_msg_id else None)
    except ImportError:  # pragma: no cover
        return None


# ── 回调处理 ────────────────────────────────────────────────────────────────
def _authorized(adapter: Any, data: Any) -> bool:
    """操作者授权检查 — fail-closed."""
    event = getattr(data, "event", None)
    operator = getattr(event, "operator", None)
    open_id = str(getattr(operator, "open_id", "") or "")
    authz = getattr(adapter, "_is_interactive_operator_authorized", None)
    if not callable(authz):
        _logger.warning(
            "clarify: adapter lacks _is_interactive_operator_authorized (old hermes?) — fail CLOSED"
        )
        return False
    if not authz(open_id):
        _logger.warning("clarify: unauthorized click by %s", open_id or "<unknown>")
        return False
    return True


def _resolve_async(adapter: Any, clarify_id: str, value: str, client_getter: Callable[[], Any]) -> None:
    """把 resolve_gateway_clarify 调度到适配器事件循环（回调可能在 SDK 线程）。"""
    from tools.clarify_gateway import resolve_gateway_clarify

    loop = getattr(adapter, "_loop", None)

    async def _do() -> None:
        try:
            resolve_gateway_clarify(clarify_id, value)
        except Exception:
            _logger.warning("clarify: resolve failed for %s", clarify_id[:12], exc_info=True)
            return
        await _schedule_confirm_card(client_getter=client_getter, cid=clarify_id)

    if loop is not None and loop.is_running():
        try:
            from agent.async_utils import safe_schedule_threadsafe

            safe_schedule_threadsafe(
                _do(), loop, logger=_logger,
                log_message="clarify: resolve scheduling failed",
                log_level=logging.WARNING,
            )
            return
        except Exception:
            _logger.warning("clarify: threadsafe scheduling failed — sync fallback", exc_info=True)
    # 同步 fallback：后台线程执行（避免阻塞 SDK 回调线程）。
    # v2.0.21 修复（审计 R2）：_do 是协程函数——原写法
    # ``Thread(target=lambda: _do())`` 只创建协程对象、从不 await（协程永不
    # 执行 + RuntimeWarning），且 ``get_running_loop().create_task`` 在 SDK
    # 回调线程通常无 running loop（RuntimeError 路径才是常态）。触发面：
    # adapter._loop 为 None/已停转（hermes 内置 adapter.py:1311 初始 None、
    # :1566 disconnect 清空）的断连窗口期点击卡片 → 用户看到「已提交」但
    # resolve_gateway_clarify 永不运行 → agent 永不恢复，点重试同死。
    # 修复：核心动作 resolve_gateway_clarify 是同步调用，线程内直接执行
    # （agent 恢复不依赖事件循环）；服务端确认卡更新（async 网络调用）
    # asyncio.run 独立 loop 尽力而为——失败仅告警（lark client 连接池绑定
    # 主 loop 时可能拒绝，核心目标已达成，卡片停在 soft-lock 态可接受）。
    def _run_sync_fallback() -> None:
        try:
            resolve_gateway_clarify(clarify_id, value)
        except Exception:
            _logger.warning("clarify: resolve failed for %s", clarify_id[:12], exc_info=True)
            return
        try:
            asyncio.run(_schedule_confirm_card(client_getter=client_getter, cid=clarify_id))
        except Exception:
            _logger.warning(
                "clarify: confirm card update failed (sync fallback) cid=%s",
                clarify_id[:12], exc_info=True,
            )

    try:
        asyncio.get_running_loop().create_task(_do())
    except RuntimeError:
        threading.Thread(target=_run_sync_fallback, name="hls-clarify-resolve", daemon=True).start()


async def _schedule_confirm_card(*, client_getter: Callable[[], Any], cid: str) -> None:
    """服务端卡片更新：soft-lock → hard-lock（confirmed 态），成功后清理。"""
    await asyncio.sleep(1.0)  # 等 CallBackCard（submitted 态）先生效
    with _clarify_lock:
        card_msg_id = _clarify_card_msg_ids.get(cid, "")
        question = _clarify_questions.get(cid, "")
        selected = _clarify_selections.get(cid, "")

    if not card_msg_id or not selected:
        _logger.warning("clarify: cannot confirm cid=%s (msg=%s sel=%s)", cid[:12], bool(card_msg_id), bool(selected))
        _cleanup(cid)
        return

    client = client_getter()
    if client is None:
        _logger.warning("clarify: cannot confirm, card client unavailable cid=%s", cid[:12])
        return
    try:
        from ..cardkit import build_clarify_confirmed_card

        card_data = build_clarify_confirmed_card(question=question, selected=selected)
        await client.update_card(card_msg_id, card_data)
        # 确认成功后才清理：失败时保留 selection，重试按钮保持可用
        _cleanup(cid)
    except Exception:
        _logger.warning("clarify: server-side confirm update failed cid=%s (selection KEPT)", cid[:12], exc_info=True)


def handle_clarify_card_action(adapter: Any, client_getter: Callable[[], Any], data: Any) -> Any:
    """clarify 卡片动作回调主入口。返回 SDK 回调响应（空响应/已提交卡/过期卡）。"""
    event = getattr(data, "event", None)
    action_value = getattr(getattr(event, "action", None), "value", {}) or {}
    clarify_action = action_value.get("hermes_clarify_action") if isinstance(action_value, dict) else ""
    clarify_id = str(action_value.get("clarify_id", "") or "") if isinstance(action_value, dict) else ""

    if not clarify_id:
        return _empty_response()
    if not _authorized(adapter, data):
        return _empty_response()

    # v2.0.21（审计 R7）：未知/过期 clarify_id 统一回「已过期」提示卡。
    # 此前 choices 为空（重启/TTL 过期）时各分支返回 _empty_response()，
    # 用户点击零反馈。timestamp 无记录 = 记录已被 prune/清理 = 过期。
    prune_expired()
    with _clarify_lock:
        known = clarify_id in _clarify_timestamps
        question = _clarify_questions.get(clarify_id, "")
        choices_list = list(_clarify_choices.get(clarify_id, []))
    if not known:
        return _expired_response()

    # ── retry_submit：重发上次选择 ──
    if clarify_action == "retry_submit":
        with _clarify_lock:
            stored = _clarify_selections.get(clarify_id, "")
        if not stored:
            return _empty_response()
        _resolve_async(adapter, clarify_id, stored, client_getter)
        return _submitted_card_response(stored, question, clarify_id)

    # ── select：下拉/按钮选择 ──
    if clarify_action == "select":
        selected_option = str(getattr(getattr(event, "action", None), "option", "") or "")
        choice_text = ""
        try:
            idx = int(selected_option)
            if 0 <= idx < len(choices_list):
                choice_text = choices_list[idx]
        except (ValueError, TypeError):
            choice_text = ""
        if not choice_text and selected_option and selected_option in choices_list:
            choice_text = selected_option
        if not choice_text:
            _logger.warning("clarify: invalid option %r cid=%s", selected_option, clarify_id[:12])
            return _empty_response()
        with _clarify_lock:
            _clarify_selections[clarify_id] = choice_text
        _resolve_async(adapter, clarify_id, choice_text, client_getter)
        return _submitted_card_response(choice_text, question, clarify_id)

    # ── input_submit / button_submit：自由输入 ──
    if clarify_action in ("input_submit", "button_submit"):
        action_obj = getattr(event, "action", None)
        input_text = str(getattr(action_obj, "input_value", "") or "").strip()
        if not input_text:
            return _empty_response()
        with _clarify_lock:
            _clarify_selections[clarify_id] = input_text
        _resolve_async(adapter, clarify_id, input_text, client_getter)
        return _submitted_card_response(input_text, question, clarify_id)

    return _empty_response()
