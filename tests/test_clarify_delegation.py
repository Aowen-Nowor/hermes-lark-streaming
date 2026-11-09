"""回归（2026-11-06）：clarify 逻辑归位内置，插件只保留流式卡挂起.

事故：插件自建 clarify 平行协议（自有状态字典 + select/input_submit
token + _handle_card_action_event 旁路入口）与 Hermes 0.23.0 内置同步
路由 _CARD_ACTION_ROUTES 撞键——点击先被内置 handler 按空 _clarify_state
静默丢弃，插件入口成死代码，用户点击零反馈。

修复后契约：
1. send_clarify 只做 composer.suspend_for_clarify()，其余 super()——
   发卡/状态/token 语义/回调/retire 全走内置；
2. _handle_card_action_event 只兜底未命中路由的动作，不再消费 clarify。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

_SCRIPT = r"""
import asyncio, importlib.util, os, sys, types

sys.path.insert(0, __REPO_ROOT__)

class RealStubBase:
    _CARD_ACTION_ROUTES = (("hermes_clarify_action", "_handle_clarify_card_action"),)
    def __init__(self, config):
        self.config = config
    async def send_clarify(self, chat_id, question, choices, clarify_id,
                           session_key=None, metadata=None, **kw):
        self.super_calls.append((chat_id, clarify_id, session_key))
        return "SUPER_RESULT"

_stub_pkg = types.ModuleType("plugins")
_stub_platforms = types.ModuleType("plugins.platforms")
_stub_feishu = types.ModuleType("plugins.platforms.feishu")
_stub_adapter = types.ModuleType("plugins.platforms.feishu.adapter")
_stub_adapter.FeishuAdapter = RealStubBase
_stub_pkg.platforms = _stub_platforms
_stub_platforms.feishu = _stub_feishu
_stub_feishu.adapter = _stub_adapter
for name, mod in [("plugins", _stub_pkg), ("plugins.platforms", _stub_platforms),
                  ("plugins.platforms.feishu", _stub_feishu),
                  ("plugins.platforms.feishu.adapter", _stub_adapter)]:
    sys.modules[name] = mod

_spec = importlib.util.spec_from_file_location(
    "hermes_lark_streaming", os.path.join(__REPO_ROOT__, "__init__.py"),
    submodule_search_locations=[__REPO_ROOT__])
_pkg = importlib.util.module_from_spec(_spec)
_pkg.__package__ = "hermes_lark_streaming"
_pkg.__path__ = [__REPO_ROOT__]
sys.modules["hermes_lark_streaming"] = _pkg
_spec.loader.exec_module(_pkg)

import hermes_lark_streaming.plugin.adapter as A

class FakeComposer:
    def __init__(self):
        self.suspended = []
    async def suspend_for_clarify(self, chat_id, question=""):
        self.suspended.append((chat_id, question))
        return True

async def main():
    inst = A.FeishuStreamingAdapter.__new__(A.FeishuStreamingAdapter)
    inst._composer = FakeComposer()
    inst.super_calls = []

    # 1) 发卡：挂起流式卡 + 全权交内置
    result = await inst.send_clarify(
        "oc_test", "选哪个？", ["甲", "乙"], "cid-123",
        session_key="sess-1", metadata=None)
    assert result == "SUPER_RESULT", result
    assert inst.super_calls == [("oc_test", "cid-123", "sess-1")], inst.super_calls
    assert inst._composer.suspended == [("oc_test", "选哪个？")], inst._composer.suspended

    # 2) 回调兜底：clarify 动作不再被插件消费（内置路由先行），只抑制不抛错
    action = types.SimpleNamespace(tag="select_static", value={"hermes_clarify_action": "select", "clarify_id": "cid-123"})
    data = types.SimpleNamespace(event=types.SimpleNamespace(action=action))
    assert await inst._handle_card_action_event(data) is None

    print("CLARIFY_DELEGATION_OK")

asyncio.run(main())
""".replace("__REPO_ROOT__", repr(str(_REPO_ROOT)))


def test_clarify_delegates_to_builtin_and_suspends_streaming_card() -> None:
    """send_clarify 挂起流式卡后交内置；卡片动作兜底不再截胡 clarify。"""
    proc = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, (
        f"clarify delegation regression failed\nstdout: {proc.stdout}\nstderr: {proc.stderr}")
    assert "CLARIFY_DELEGATION_OK" in proc.stdout
