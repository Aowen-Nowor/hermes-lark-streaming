"""批次 B：故障注入下的投递幂等、分类与生命周期回归。"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest
from hermes_lark_streaming.composer.classify import (
    CH_FINAL,
    CH_PROGRESS,
    CH_STREAM,
    ClassifyResult,
    classify_send,
)
from hermes_lark_streaming.composer.engine import CardTurnState, FrameComposer
from hermes_lark_streaming.composer.finalize import finalize_card
from hermes_lark_streaming.feishu.client import (
    CARDKIT_SEQUENCE_CONFLICT,
    FeishuAPIError,
    FeishuClient,
)
from hermes_lark_streaming.plugin.adapter import (
    FeishuStreamingAdapter,
    _upgrade_plain_to_progress,
)


class _AppliedAddThenConflictClient:
    """第一次 add 已在服务端生效，仅响应中的 sequence 校验失败。"""

    def __init__(self) -> None:
        self.batches: list[list[dict]] = []
        self._conflicted = False

    async def cardkit_stream_element(self, card_id, element_id, content, *, sequence=0):
        raise AssertionError("等值正文不应重复上传")

    async def cardkit_batch_update(self, card_id, actions, *, sequence=0):
        self.batches.append(actions)
        if not self._conflicted and any(a.get("action") == "add_elements" for a in actions):
            self._conflicted = True
            raise FeishuAPIError("sequence compare failed", CARDKIT_SEQUENCE_CONFLICT)

    async def cardkit_close_streaming(self, card_id, sequence=0, *, summary=""):
        return None


def _final_state() -> SimpleNamespace:
    return SimpleNamespace(
        card_id="card_footer_retry",
        card_message_id="om_footer_retry",
        seq=0,
        answer_text="已经完整落卡的正文",
        last_streamed_answer="已经完整落卡的正文",
        answer_md_cache={},
        md_lines_cache={},
        status_lines=[],
        line_times=[],
        tool_durations={},
        panel_on_card=False,
        created_at=time.monotonic(),
    )


async def test_footer_sequence_retry_updates_fixed_ids_instead_of_adding_twice():
    client = _AppliedAddThenConflictClient()

    await finalize_card(client, _final_state(), answer_element_id="answer_content")

    actions = [batch[0]["action"] for batch in client.batches]
    assert actions == ["add_elements", "partial_update_element", "delete_elements"]
    added = client.batches[0][0]["params"]["elements"]
    updated = client.batches[1]
    assert all(element.get("element_id") for element in added)
    assert {op["params"]["element_id"] for op in updated} == {
        element["element_id"] for element in added
    }
    assert all("tag" not in op["params"]["partial_element"] for op in updated), \
        "partial_update_element 不得携带飞书禁止修改的 tag"


async def test_footer_add_is_not_replayed_when_network_outcome_is_unknown():
    attempts = 0

    class _CardEndpoint:
        async def abatch_update(self, request):
            nonlocal attempts
            attempts += 1  # 模拟服务端已应用，但响应在传输中丢失。
            raise httpx.RequestError("response lost")

    client = FeishuClient.__new__(FeishuClient)
    client._client = SimpleNamespace(
        cardkit=SimpleNamespace(v1=SimpleNamespace(card=_CardEndpoint()))
    )
    footer_add = [{
        "action": "add_elements",
        "params": {"elements": [{"tag": "markdown", "content": "done"}]},
    }]

    try:
        await client.cardkit_batch_update("card_unknown", footer_add, sequence=1)
    except httpx.RequestError:
        pass
    else:
        raise AssertionError("故障注入必须到达调用方")

    assert attempts == 1, "未知投递结果不得自动重放非幂等 add_elements"


class _CloseFailsOnceClient(_AppliedAddThenConflictClient):
    def __init__(self) -> None:
        super().__init__()
        self._conflicted = True  # 本用例不走 sequence 故障。
        self.close_calls = 0

    async def cardkit_close_streaming(self, card_id, sequence=0, *, summary=""):
        self.close_calls += 1
        if self.close_calls == 1:
            raise RuntimeError("close_streaming: injected transport failure")


async def test_repeated_finalize_after_close_failure_is_close_only():
    client = _CloseFailsOnceClient()
    state = _final_state()

    await finalize_card(client, state, answer_element_id="answer_content")
    first_batch_count = len(client.batches)
    await finalize_card(client, state, answer_element_id="answer_content")

    assert client.close_calls == 2, "第二次 finalize 必须补关流式"
    assert len(client.batches) == first_batch_count, "close-only 不得重复页脚或面板操作"
    assert sum(
        action.get("action") == "add_elements"
        for batch in client.batches for action in batch
    ) == 1


async def test_composer_repeated_finalize_retries_only_missing_close():
    client = _CloseFailsOnceClient()
    composer = FrameComposer(
        get_client=lambda: client,
        build_card=lambda **kwargs: {},
        build_final_card=lambda **kwargs: {},
    )
    state = CardTurnState(
        chat_id="oc_close_retry",
        card_id="card_close_retry",
        card_message_id="om_close_retry",
        answer_text="终值",
        last_streamed_answer="终值",
    )
    composer._states[state.chat_id] = state

    await composer._finalize(state, "终值")
    await composer._finalize(state, "终值")

    assert client.close_calls == 2
    assert len(client.batches) == 2, "首轮页脚与 spinner 后，重试不得再发 batch"


class _CloseRetryComposerClient:
    def __init__(self) -> None:
        self.cards: dict[str, dict] = {}
        self.stream_calls = 0
        self.streamed_text = ""
        self.close_calls = 0
        self.batches: list[list[dict]] = []
        self.create_calls = 0

    async def cardkit_create(self, card):
        self.create_calls += 1
        card_id = f"card_close_{self.create_calls}"
        self.cards[card_id] = {"card": card}
        return card_id

    async def cardkit_stream_element(self, card_id, element_id, content, *, sequence=0):
        self.stream_calls += 1
        self.streamed_text = content

    async def send_card_by_id_to_chat(self, chat_id, card_id):
        return f"om_{card_id}"

    async def cardkit_update(self, card_id, card, *, sequence=0):
        self.cards[card_id]["card"] = card

    async def cardkit_batch_update(self, card_id, actions, *, sequence=0):
        self.batches.append(actions)

    async def cardkit_close_streaming(self, card_id, sequence=0, *, summary=""):
        self.close_calls += 1
        if self.close_calls == 1:
            raise RuntimeError("close_streaming: injected failure")


@pytest.mark.parametrize("mode", ["send", "native"])
async def test_repeated_final_frame_after_close_failure_does_not_create_or_reseal(mode):
    client = _CloseRetryComposerClient()
    composer = FrameComposer(
        get_client=lambda: client,
        build_card=lambda **kwargs: {},
        build_final_card=lambda **kwargs: {},
    )
    chat = f"oc_final_close_{mode}"

    if mode == "send":
        await composer.handle_send(
            chat, "正文", None, None,
            ClassifyResult(CH_STREAM, kind="answer", raw_content="正文"),
        )
        for _ in range(2):
            result = await composer.handle_send(
                chat, "正文", None, {"notify": True},
                ClassifyResult(CH_FINAL, kind="final", raw_content="正文"),
            )
            assert result is not None and result.success
    else:
        await composer.handle_stream_frame(chat, "正文", finalize=False, turn_id="turn_1")
        for _ in range(2):
            assert await composer.handle_stream_frame(
                chat, "正文", finalize=True, turn_id="turn_1",
            ) is True

    assert client.create_calls == 1, "重复 final 不得创建第二张卡"
    assert client.close_calls == 2, "第二次 final 只补上一次失败的 close"
    assert sum(
        action.get("action") == "add_elements"
        for batch in client.batches for action in batch
    ) == 1, "close-only 不得重复页脚"


class _DegradedSealClient:
    def __init__(self) -> None:
        self.cards: dict[str, dict] = {}
        self.stream_calls = 0
        self.streamed_text = ""
        self.close_calls = 0
        self.batches: list[list[dict]] = []

    async def cardkit_create(self, card):
        card_id = "card_degraded"
        self.cards[card_id] = {"card": card}
        return card_id

    async def send_card_by_id_to_chat(self, chat_id, card_id):
        return "om_degraded"

    async def cardkit_stream_element(self, card_id, element_id, content, *, sequence=0):
        self.stream_calls += 1
        if self.stream_calls == 2:
            raise RuntimeError("stream_element: injected failure")
        self.streamed_text = content

    async def cardkit_update(self, card_id, card, *, sequence=0):
        self.cards[card_id]["card"] = card

    async def cardkit_batch_update(self, card_id, actions, *, sequence=0):
        self.batches.append(actions)

    async def cardkit_close_streaming(self, card_id, sequence=0, *, summary=""):
        self.close_calls += 1


@pytest.mark.parametrize("mode", ["send", "edit", "native"])
async def test_successful_degraded_seal_consumes_full_frame_without_native_redelivery(mode):
    client = _DegradedSealClient()
    composer = FrameComposer(
        get_client=lambda: client,
        build_card=lambda **kwargs: {},
        build_final_card=lambda **kwargs: {},
    )
    chat = f"oc_degraded_{mode}"
    first = ClassifyResult(CH_STREAM, kind="answer", raw_content="首段")
    initial = await composer.handle_send(chat, "首段", None, None, first)
    state = composer._states[chat]
    composer._by_message[state.card_message_id] = chat

    if mode == "send":
        result = await composer.handle_send(
            chat, "终段", None, None,
            ClassifyResult(CH_STREAM, kind="answer", raw_content="终段"),
        )
        expected = "首段\n\n终段"
    elif mode == "edit":
        result = await composer.handle_edit(
            chat, initial.message_id, "终值全文", False,
            ClassifyResult(CH_STREAM, kind="answer", raw_content="终值全文"),
        )
        expected = "终值全文"
    else:
        result = await composer.handle_stream_frame(chat, "终值全文", finalize=False)
        expected = "终值全文"

    if mode == "native":
        assert result is True, "全文已落卡且关流成功，native 不得回落重投"
    else:
        assert result is not None and result.success, "degraded seal 成功应返回结构化成功"
        assert result.message_id == state.card_message_id
    assert client.streamed_text == expected
    assert client.close_calls == 1
    assert state.degraded and state.finalized


class _UnknownReplyOutcomeClient:
    def __init__(self) -> None:
        self.direct_calls: list[str] = []

    async def cardkit_create(self, card):
        return "card_reply_unknown"

    async def reply_card_by_id(self, anchor, card_id):
        raise RuntimeError("reply response lost; outcome unknown")

    async def reply_card(self, anchor, card):
        raise RuntimeError("reply response lost; outcome unknown")

    async def send_card_by_id_to_chat(self, chat_id, card_id):
        self.direct_calls.append("stream-direct")
        return "om_stream_direct"

    async def send_card_to_chat(self, chat_id, card):
        self.direct_calls.append("oneshot-direct")
        return "om_oneshot_direct"


@pytest.mark.parametrize("path", ["stream", "oneshot"])
async def test_unknown_reply_outcome_never_blindly_sends_a_second_copy(path):
    client = _UnknownReplyOutcomeClient()

    if path == "stream":
        composer = FrameComposer(
            get_client=lambda: client,
            build_card=lambda **kwargs: {},
            build_final_card=lambda **kwargs: {},
        )
        result = await composer.handle_send(
            "oc_reply_unknown", "正文", "om_anchor", None,
            ClassifyResult(CH_STREAM, kind="answer", raw_content="正文"),
        )
    else:
        adapter = FeishuStreamingAdapter.__new__(FeishuStreamingAdapter)
        adapter._card_config = SimpleNamespace(oneshot_cards=True)
        adapter._card_client = client
        adapter._composer = SimpleNamespace(active_state=lambda chat_id: None)
        result = await adapter._send_oneshot_card(
            "oc_reply_unknown", "正文", None,
            classify_send("正文", None), reply_to="om_anchor",
        )

    assert result is not None and not result.success
    assert result.retryable is False, "未知传输结果不得触发 Hermes 自动重试双发"
    assert client.direct_calls == []


def test_interim_commentary_is_recognized_without_reply_to():
    cls = classify_send("我先检查依赖，再给出结论。", {"_interim_send": True})

    assert cls.channel == "interim"
    assert cls.kind == "advisory"
    assert cls.meta.get("conversational_commentary") is True


@pytest.mark.parametrize("text", [
    "⏳ 正在处理——3 分钟",
    "⏳ 處理中 — 3 分鐘",
    "💾 自我改进回顾：Skill 'x' patched",
    "💾 自我改進檢閱：技能 'x' 已修補",
])
def test_chinese_background_templates_never_become_commentary_or_progress(text):
    cls = classify_send(text, {"_interim_send": True})

    assert cls.channel == "interim" and cls.kind == "advisory"
    assert not cls.meta.get("conversational_commentary")
    assert _upgrade_plain_to_progress(None, "oc_notice", text, None) is None


class _ActiveComposerProbe:
    def __init__(self) -> None:
        self.calls = 0
        self.active = SimpleNamespace(card_id="card_active_turn")

    def active_state(self, chat_id):
        return self.active

    async def handle_send(self, *args):
        self.calls += 1
        return SimpleNamespace(success=True, message_id="om_active_turn")


class _StaticCardClient:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_card_to_chat(self, chat_id, card):
        self.sent.append(card)
        return "om_bg_static"


@pytest.mark.parametrize("content", [
    "✅ 后台任务完成\n提示词：\"查天气\"\n\n今天晴天",
    "✅ 背景任務完成\n提示：\"查天氣\"\n\n今天晴天",
    "✅ Background task complete\nPrompt: \"weather\"\n\nSunny",
])
async def test_background_completion_uses_independent_static_card_not_active_turn(content):
    client = _StaticCardClient()
    composer = _ActiveComposerProbe()
    adapter = FeishuStreamingAdapter.__new__(FeishuStreamingAdapter)
    adapter._card_config = SimpleNamespace(oneshot_cards=True)
    adapter._card_client = client
    adapter._composer = composer

    result = await adapter._send_impl("oc_bg", content, None, None)

    assert result.success and result.message_id == "om_bg_static"
    assert len(client.sent) == 1, "完成结果必须独立静态发卡"
    assert composer.calls == 0, "不得升格 progress、串入活跃卡或 early-create"
    assert composer.active.card_id == "card_active_turn"


def _without_client() -> FrameComposer:
    return FrameComposer(
        get_client=lambda: None,
        build_card=lambda **kwargs: {},
        build_final_card=lambda **kwargs: {},
    )


async def test_progress_without_client_returns_native_instead_of_fake_success():
    composer = _without_client()

    result = await composer.handle_send(
        "oc_no_client", "🔧 terminal：\"date\"", None, None,
        ClassifyResult(CH_PROGRESS, kind="tool", raw_content="🔧 terminal：\"date\""),
    )

    assert result is None


async def test_native_without_client_is_not_reported_as_consumed():
    composer = _without_client()

    assert await composer.handle_stream_frame("oc_no_client_native", "正文") is False


async def test_pending_status_dedupes_and_caps_at_64_lines():
    composer = _without_client()
    chat = "oc_pending_cap"
    for index in range(70):
        text = f'🔧 tool{index}："value"'
        await composer.handle_send(
            chat, text, None, None,
            ClassifyResult(CH_PROGRESS, kind="tool", raw_content=text),
        )
    repeated = '🔧 tool69："value"'
    await composer.handle_send(
        chat, repeated, None, None,
        ClassifyResult(CH_PROGRESS, kind="tool", raw_content=repeated),
    )

    pending = composer._states[chat].pending_status
    assert len(pending) == 64
    assert len(pending) == len(set(pending))
    assert pending[-1] == repeated


def _watchdog_composer(chat: str) -> tuple[FrameComposer, CardTurnState, _DegradedSealClient]:
    client = _DegradedSealClient()
    composer = FrameComposer(
        get_client=lambda: client,
        build_card=lambda **kwargs: {},
        build_final_card=lambda **kwargs: {},
    )
    state = CardTurnState(
        chat_id=chat,
        card_id=f"card_{chat}",
        card_message_id=f"om_{chat}",
        answer_text="正文",
        last_streamed_answer="正文",
        turn_id="turn_current",
    )
    composer._states[chat] = state
    composer._loop = asyncio.get_running_loop()
    return composer, state, client


async def test_watchdog_rearm_keeps_one_cancellable_handle_per_chat():
    composer, state, _ = _watchdog_composer("oc_watchdog_single")

    composer._arm_watchdog(state.chat_id)
    first = composer._watchdog_timers[state.chat_id]
    composer._arm_watchdog(state.chat_id)
    second = composer._watchdog_timers[state.chat_id]

    assert first.cancelled()
    assert not second.cancelled()
    assert list(composer._watchdog_timers) == [state.chat_id]
    composer.clear()


@pytest.mark.parametrize("cleanup", ["finalize", "forget", "gc", "clear", "disconnect"])
async def test_watchdog_handle_is_cancelled_by_lifecycle_cleanup(cleanup):
    chat = f"oc_watchdog_{cleanup}"
    composer, state, _ = _watchdog_composer(chat)
    composer._arm_watchdog(chat)
    handle = composer._watchdog_timers[chat]

    if cleanup == "finalize":
        await composer._finalize(state, state.answer_text)
    elif cleanup == "forget":
        composer.forget(chat)
    elif cleanup == "gc":
        state.last_activity -= 700
        composer._gc()
        await asyncio.sleep(0)
    elif cleanup == "clear":
        composer.clear()
    else:
        await composer.seal_all_on_shutdown()

    assert handle.cancelled()
    assert chat not in composer._watchdog_timers


async def test_watchdog_callback_rejects_stale_turn_and_card():
    chat = "oc_watchdog_stale"
    composer, state, client = _watchdog_composer(chat)
    state.last_activity -= 1000

    composer._watchdog_due(
        chat, expected_card_id="card_old", expected_turn_id="turn_old",
    )
    await asyncio.sleep(0)
    assert client.close_calls == 0

    composer._watchdog_due(
        chat,
        expected_card_id=state.card_id,
        expected_turn_id=state.turn_id,
    )
    for _ in range(3):
        await asyncio.sleep(0)
    assert client.close_calls == 1
