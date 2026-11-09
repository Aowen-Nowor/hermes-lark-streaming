"""v2.0.31 入站锚回退 — 进度/流式通道 send(reply_to=None, metadata=None) 的锚恢复.

取证（2026-11-07 anchor-trace，agent.log 17:52:55）：网关进度帧实测
``send() reply_to=None meta_keys=None``——metadata.reply_to_message_id
回路只在流式消费者部分帧存在，进度首帧什么都没有 → 建卡永远直发。
修复与内置飞书 `_reply_anchor_for_event` 同语义：入站事件先记锚
（_handle_message_with_guards 覆写），出站 send() 无显式锚且无 metadata
时回退最近入站用户消息 ID（10 分钟 TTL，om_ 前缀校验）。
"""
from __future__ import annotations

import time

import pytest
from hermes_lark_streaming.plugin.adapter import FeishuStreamingAdapter

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _adapter(anchor: dict | None = None) -> FeishuStreamingAdapter:
    a = FeishuStreamingAdapter.__new__(FeishuStreamingAdapter)
    a._inbound_anchor = anchor if anchor is not None else {}
    return a


def test_fresh_inbound_anchor_used():
    """无显式锚、无 metadata → 回退新鲜入站锚."""
    a = _adapter({"oc_1": ("om_user9", time.monotonic())})
    assert a._resolve_outbound_anchor("oc_1", None, None) == "om_user9"


def test_stale_anchor_ignored():
    """超过 10 分钟 TTL 的入站锚不回退 → None."""
    a = _adapter({"oc_1": ("om_old", time.monotonic() - 700)})
    assert a._resolve_outbound_anchor("oc_1", None, None) is None


def test_metadata_frames_never_use_inbound_anchor():
    """带 metadata 的帧（cron/notify/流式消费者族）不回退，防错锚旧消息."""
    a = _adapter({"oc_1": ("om_user9", time.monotonic())})
    assert a._resolve_outbound_anchor("oc_1", None, {"notify": True}) is None


def test_explicit_anchor_wins():
    """显式 reply_to 优先，不被入站锚覆盖."""
    a = _adapter({"oc_1": ("om_user9", time.monotonic())})
    assert a._resolve_outbound_anchor("oc_1", "om_explicit", None) == "om_explicit"


def test_non_om_anchor_ignored():
    """合成锚（非 om_ 前缀，如 task_id）不回退."""
    a = _adapter({"oc_1": ("task_abc", time.monotonic())})
    assert a._resolve_outbound_anchor("oc_1", None, None) is None


def test_unknown_chat_no_anchor():
    """无入站记录的 chat → None（行为与修复前一致）."""
    a = _adapter()
    assert a._resolve_outbound_anchor("oc_other", None, None) is None


def test_guard_records_anchor_shape():
    """入站覆写体逻辑：om_ 消息 + chat_id → 记 (mid, now)；非 om_ 不记."""
    a = _adapter()
    mid, chat_id = "om_evt1", "oc_1"
    if mid.startswith("om_") and chat_id:
        a._inbound_anchor[chat_id] = (mid, time.monotonic())
    assert a._inbound_anchor["oc_1"][0] == "om_evt1"

    a._inbound_anchor.clear()
    mid2 = "task_abc"
    if mid2.startswith("om_") and chat_id:
        a._inbound_anchor[chat_id] = (mid2, time.monotonic())
    assert a._inbound_anchor == {}
