"""独立审查发现的投递、分类与配置回归。"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from hermes_lark_streaming.composer.classify import CH_STREAM, ClassifyResult, classify_send
from hermes_lark_streaming.composer.engine import FrameComposer
from hermes_lark_streaming.plugin.adapter import FeishuStreamingAdapter, _upgrade_plain_to_progress
from hermes_lark_streaming.config.reader import Config
from hermes_lark_streaming import __main__ as cli
from tests.test_batch_b_runtime_faults import _DegradedSealClient, _UnknownReplyOutcomeClient


def _composer(client):
    return FrameComposer(
        get_client=lambda: client,
        build_card=lambda **kwargs: {},
        build_final_card=lambda **kwargs: {},
    )


@pytest.mark.parametrize("path", ["stream", "oneshot"])
async def test_unknown_reply_is_not_retryable(path):
    client = _UnknownReplyOutcomeClient()
    if path == "stream":
        result = await _composer(client).handle_send(
            "oc_unknown", "正文", "om_anchor", None,
            ClassifyResult(CH_STREAM, kind="answer", raw_content="正文"),
        )
    else:
        adapter = FeishuStreamingAdapter.__new__(FeishuStreamingAdapter)
        adapter._card_config = SimpleNamespace(oneshot_cards=True)
        adapter._card_client = client
        adapter._composer = SimpleNamespace(active_state=lambda chat_id: None)
        result = await adapter._send_oneshot_card(
            "oc_unknown", "正文", None, classify_send("正文", None), reply_to="om_anchor",
        )
    assert result is not None and not result.success
    assert result.retryable is False, "已应用但丢响应时，Hermes 自动重试会双发"


class _UnknownDirectClient:
    def __init__(self):
        self.direct_calls = 0

    async def cardkit_create(self, card):
        return "card_direct_unknown"

    async def send_card_by_id_to_chat(self, chat_id, card_id):
        self.direct_calls += 1
        raise RuntimeError("direct send response lost")

    async def send_card_to_chat(self, chat_id, card):
        self.direct_calls += 1
        raise RuntimeError("direct send response lost")


@pytest.mark.parametrize("path", ["stream", "oneshot"])
async def test_unknown_direct_send_is_failure_not_native_redelivery(path):
    client = _UnknownDirectClient()
    if path == "stream":
        result = await _composer(client).handle_send(
            "oc_direct_unknown", "正文", None, None,
            ClassifyResult(CH_STREAM, kind="answer", raw_content="正文"),
        )
    else:
        adapter = FeishuStreamingAdapter.__new__(FeishuStreamingAdapter)
        adapter._card_config = SimpleNamespace(oneshot_cards=True)
        adapter._card_client = client
        adapter._composer = SimpleNamespace(active_state=lambda chat_id: None)
        result = await adapter._send_oneshot_card(
            "oc_direct_unknown", "正文", None, classify_send("正文", None),
        )
    assert result is not None and not result.success
    assert result.retryable is False
    assert client.direct_calls == 1


class _CloseAlwaysFailsClient(_DegradedSealClient):
    async def cardkit_close_streaming(self, card_id, sequence=0, *, summary=""):
        self.close_calls += 1
        raise RuntimeError("close failed")


async def test_degraded_content_delivery_does_not_depend_on_close_success():
    client = _CloseAlwaysFailsClient()
    composer = _composer(client)
    initial = await composer.handle_send(
        "oc_close_degraded", "首段", None, None,
        ClassifyResult(CH_STREAM, kind="answer", raw_content="首段"),
    )
    state = composer._states["oc_close_degraded"]
    composer._by_message[state.card_message_id] = state.chat_id
    result = await composer.handle_edit(
        state.chat_id, initial.message_id, "终值全文", False,
        ClassifyResult(CH_STREAM, kind="answer", raw_content="终值全文"),
    )
    assert result is not None and result.success, "全文已落卡，关流失败不得触发原生全文重投"
    assert client.streamed_text == "终值全文"
    assert state.streaming_closed is False


async def test_non_turn_interim_does_not_early_create_streaming_card():
    client = SimpleNamespace(create_calls=0)

    async def create(card):
        client.create_calls += 1
        return "card_unexpected"

    client.cardkit_create = create
    composer = _composer(client)
    cls = classify_send("插件独立通知", {"_interim_send": True})
    result = await composer.handle_send("oc_plugin_notice", cls.raw_content, None, None, cls)
    assert result is None, "未确认回合锚点的 interim 应走原生，不得创建孤儿流式卡"
    assert client.create_calls == 0


def test_plain_independent_prose_is_not_absorbed_into_active_status():
    composer = SimpleNamespace(active_state=lambda chat_id: object())
    assert _upgrade_plain_to_progress(
        composer, "oc_active", "这是 send_message 的独立正文", None,
    ) is None


@pytest.mark.parametrize("text", [
    "⚠️ Hermes 正在關閉 — 你目前的任務將被中斷。",
    "⚠️ Hermes 正在重新啟動 — 你目前的任務將被中斷。",
    "⚠️ 排程任務 'daily' 因 Hermes 正在關閉而被中斷；請稍後重試。",
])
def test_zh_hant_shutdown_templates_are_not_progress(text):
    cls = classify_send(text, {"_interim_send": True})
    assert cls.channel == "interim" and cls.kind == "advisory"
    assert not cls.meta.get("conversational_commentary")


def test_non_mapping_yaml_is_treated_as_empty_config(tmp_path):
    (tmp_path / "config.yaml").write_text("- foo\n", encoding="utf-8")
    with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
        Config._instance = None
        assert Config()._plugin_sec() == {}


def test_doctor_adapter_failure_is_not_pass(tmp_path):
    cfg = SimpleNamespace(
        env_app_id="cli_test", env_app_secret="secret",
        feishu_app_id="cli_test", feishu_app_secret="secret",
        card_duration_sec=600, print_strategy="delay", print_step=4,
        streaming_panel_expanded=False, footer_fields=[], footer_show_label=False,
        native_streaming=False, oneshot_cards=True,
    )
    with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}), \
            patch("hermes_lark_streaming.config.Config", return_value=cfg), \
            patch("importlib.metadata.version", return_value="1.6.8"), \
            patch("hermes_lark_streaming.__main__._find_hermes_python", return_value=sys.executable), \
            patch("hermes_lark_streaming.plugin.adapter.get_active_adapters", side_effect=RuntimeError("boom")):
        assert cli._cmd_doctor() == 1


def test_verify_rejects_module_with_missing_required_symbol():
    cfg = SimpleNamespace(env_app_id="id", env_app_secret="secret", feishu_app_id="id", feishu_app_secret="secret")
    with patch("hermes_lark_streaming.config.Config", return_value=cfg), \
            patch("importlib.import_module", return_value=SimpleNamespace()):
        assert cli._cmd_verify() == 1


async def test_single_final_close_failure_is_retried_by_watchdog():
    from tests.test_batch_b_runtime_faults import _CloseFailsOnceClient
    from hermes_lark_streaming.composer.engine import CardTurnState

    client = _CloseFailsOnceClient()
    composer = _composer(client)
    state = CardTurnState(
        chat_id="oc_close_retry",
        card_id="card_footer_retry",
        card_message_id="om_footer_retry",
        answer_text="已经完整落卡的正文",
        last_streamed_answer="已经完整落卡的正文",
    )
    composer._states[state.chat_id] = state
    await composer._finalize(state, state.answer_text)
    assert state.streaming_closed is False

    await composer._watchdog_seal(state.chat_id)

    assert client.close_calls == 2
    assert state.streaming_closed is True


async def test_footer_failure_is_retried_without_replaying_add():
    class Client:
        def __init__(self):
            self.add_calls = 0
            self.update_calls = 0
            self.close_calls = 0

        async def cardkit_stream_element(self, *args, **kwargs):
            return None

        async def cardkit_batch_update(self, card_id, actions, *, sequence=0):
            if actions[0]["action"] == "add_elements":
                self.add_calls += 1
                raise RuntimeError("add outcome unknown")
            self.update_calls += actions[0]["action"] == "partial_update_element"
            if self.update_calls == 1:
                raise RuntimeError("update outcome unknown")

        async def cardkit_close_streaming(self, card_id, sequence=0, *, summary=""):
            self.close_calls += 1

    from hermes_lark_streaming.composer.engine import CardTurnState

    client = Client()
    composer = _composer(client)
    state = CardTurnState(
        chat_id="oc_footer_retry",
        card_id="card_footer_retry",
        card_message_id="om_footer_retry",
        answer_text="已经完整落卡的正文",
        last_streamed_answer="已经完整落卡的正文",
    )
    composer._states[state.chat_id] = state
    await composer._finalize(state, state.answer_text)
    assert state.footer_sealed is False
    assert state.streaming_closed is True

    await composer._watchdog_seal(state.chat_id)

    assert client.add_calls == 1, "后续恢复不得重放非幂等 add"
    assert client.update_calls == 2
    assert client.close_calls == 1
    assert state.footer_sealed is True
