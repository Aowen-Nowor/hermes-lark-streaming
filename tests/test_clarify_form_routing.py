"""Adapter routing for CardKit 2.0 form clarify prompts."""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hermes_lark_streaming.plugin.adapter import FeishuStreamingAdapter


def _install_fake_clarify_gateway(monkeypatch, entry):
    tools = types.ModuleType("tools")
    gateway = types.ModuleType("tools.clarify_gateway")
    gateway.get_pending = lambda clarify_id: entry
    tools.clarify_gateway = gateway
    monkeypatch.setitem(sys.modules, "tools", tools)
    monkeypatch.setitem(sys.modules, "tools.clarify_gateway", gateway)


@pytest.mark.asyncio
async def test_send_clarify_routes_multi_select_to_form_card(monkeypatch):
    adapter = FeishuStreamingAdapter.__new__(FeishuStreamingAdapter)
    adapter._composer = SimpleNamespace(suspend_for_clarify=AsyncMock())
    adapter._clarify_state = {}
    adapter._send_interactive_card = AsyncMock(return_value=SimpleNamespace(success=True))

    entry = SimpleNamespace(multi_select=True, choices=["登录", "支付"])
    _install_fake_clarify_gateway(monkeypatch, entry)

    await adapter.send_clarify("oc_x", "哪些功能？", ["登录", "支付"], "cid_m", "sk")

    card = adapter._send_interactive_card.await_args.args[1]
    tags = []

    def walk(node):
        if isinstance(node, dict):
            tags.append(node.get("tag"))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(card)
    assert "multi_select_static" in tags
    assert adapter._clarify_state == {}


@pytest.mark.asyncio
async def test_send_clarify_routes_open_ended_to_input_form(monkeypatch):
    adapter = FeishuStreamingAdapter.__new__(FeishuStreamingAdapter)
    adapter._composer = SimpleNamespace(suspend_for_clarify=AsyncMock())
    adapter._clarify_state = {}
    adapter._send_interactive_card = AsyncMock(return_value=SimpleNamespace(success=True))

    entry = SimpleNamespace(multi_select=False, choices=None)
    _install_fake_clarify_gateway(monkeypatch, entry)

    await adapter.send_clarify("oc_x", "开放题", None, "cid_t", "sk")

    card = adapter._send_interactive_card.await_args.args[1]
    tags = []

    def walk(node):
        if isinstance(node, dict):
            tags.append(node.get("tag"))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(card)
    assert "input" in tags
