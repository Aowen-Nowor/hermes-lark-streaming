"""feishu-streaming 平台注册与 standalone 投递契约。"""

from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hermes_lark_streaming.plugin import adapter as adapter_module


def test_register_declares_scoped_env_enablement_and_custom_standalone() -> None:
    captured = {}

    class Ctx:
        def register_platform(self, **kwargs):
            captured.update(kwargs)

    env_cred_calls = []

    class EnvCred:
        def __init__(self, platform, creds, **kwargs):
            env_cred_calls.append((platform, creds, kwargs))

        def __call__(self, config):
            return None

    class Platform:
        def __new__(cls, name):
            return name

    gateway_env = types.ModuleType("gateway.platform_env")
    gateway_env.EnvCred = EnvCred
    gateway_config = types.ModuleType("gateway.config")
    gateway_config.Platform = Platform

    with patch.dict(sys.modules, {
        "gateway.platform_env": gateway_env,
        "gateway.config": gateway_config,
    }), patch.object(adapter_module, "_rebuild_class_with_real_base") as rebuild, \
            patch.object(adapter_module, "_prewarm_config"), \
            patch.object(adapter_module, "_builtin_attr", side_effect=lambda _name: (lambda *a, **k: None)):
        rebuild.return_value = SimpleNamespace(PLATFORM_LABEL="test")
        adapter_module.register(Ctx())

    assert captured["env_enable_credentials"] == ("FEISHU_APP_ID", "FEISHU_APP_SECRET")
    assert captured["allow_bots_env"] == "FEISHU_ALLOW_BOTS"
    assert captured["allowlist_suppresses_pairing"] is True
    assert captured["standalone_sender_fn"] is adapter_module._standalone_send
    # 平台注册前不得实例化 EnvCred：动态 Platform 此时尚未注册。
    assert env_cred_calls == []
    # Hermes 应用 env overrides 时平台已注册，此刻才应创建 EnvCred。
    with patch.dict(sys.modules, {
        "gateway.platform_env": gateway_env,
        "gateway.config": gateway_config,
    }):
        captured["env_overrides"][0](SimpleNamespace())
    assert env_cred_calls
    platform, creds, kwargs = env_cred_calls[0]
    assert platform == "feishu-streaming"
    assert creds == ("FEISHU_APP_ID", "FEISHU_APP_SECRET")
    assert ("app_id", "FEISHU_APP_ID") in kwargs["fixed"]
    assert ("app_secret", "FEISHU_APP_SECRET") in kwargs["fixed"]


def test_standalone_send_uses_streaming_adapter_and_platform_name() -> None:
    instance = SimpleNamespace()
    instance._domain_name = "feishu"
    instance._build_lark_client = lambda domain: object()
    instance.send = AsyncMock(return_value=SimpleNamespace(success=True, message_id="om_test"))

    def builtin_attr(name):
        values = {
            "_load_lark_oapi": lambda: True,
            "_sdk_domain": lambda name: "https://open.feishu.cn",
            "send_error": lambda message: {"success": False, "error": message},
        }
        return values[name]

    with patch.object(adapter_module, "FeishuStreamingAdapter", return_value=instance) as adapter_cls, \
            patch.object(adapter_module, "_builtin_attr", side_effect=builtin_attr), \
            patch.object(adapter_module, "_load_lark_oapi", return_value=True, create=True):
        result = asyncio.run(adapter_module._standalone_send(
            SimpleNamespace(), "oc_test", "hello", media_files=[],
        ))

    adapter_cls.assert_called_once()
    instance.send.assert_awaited_once()
    assert instance.send.await_args.kwargs["metadata"]["non_conversational"] is True
    assert result["success"] is True
    assert result["platform"] == "feishu-streaming"
    assert result["message_id"] == "om_test"
