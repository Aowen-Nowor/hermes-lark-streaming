"""v2.0.15 回归测试 — hermes 平台通知轨卡片化（子代理失败通知 >300 字符漏洞）.

Task 21 取证（用户追问「子代理的消息是否也做成卡片消息了」）：

hermes 子代理失败/超时通知经官方通知轨直发 ``adapter.send``
（gateway/run_notifications.py:176-208 ``_deliver_platform_notice`` →
``adapter.send(chat_id, line, metadata=_thread_metadata_for_source(...))``；
飞书 thread_id=None 时 metadata=None）→ v2 ``classify_send`` 落 CH_PLAIN →
``_upgrade_plain_to_progress`` 启发式判定。官方行格式
（tools/delegate_tool_progress.py ``format_subagent_failure_line``:64-93，
f-string 硬编码前缀）::

    ⚠️ Subagent failed — "{goal}" after {t}: {reason}. Details: /agents, …
    ⚠️ Subagent timed out … without finishing. …

``reason`` 经官方 ``_clean_error_text`` cap 200 字符（:38-45），整行最长
≈359 字符 —— 超出工具行默认 300 上限（``_is_progress_text``）：
「长通知 + 活跃卡在场」既不升格进面板、又命中不了 oneshot R3（要求无活跃
卡）→ ``super().send()`` 原生 post 文本，孤挂卡片旁。

修复（方案 A，用户拍板）：``_is_progress_text`` 对平台通知轨固定前缀豁免
300 上限（600 防御，与围栏块一致）→ 复用既有升格路径：
有活跃卡 → 并入面板（v2.0.10 通报行样式，⚠️ → ``warning_outlined`` 官方
图标）；无活跃卡 → early-create 回合卡 + 暂存（后续 answer 帧接管，单卡
全回合）。回合外唯一可能是调度 race 窗口（官方 gate ``_run_still_current``
锁定回合内发出），暂存并入下一卡语义（Task 18 P2-C 已知限制家族），
不丢消息、不多卡。

拒绝的替代设计：oneshot 新增 R4「通知 → 独立 gateway 卡」——通知被官方
gate 锁定回合内，独立静态卡会与回合卡并存（v2.0.11 多卡片观感回归）；
升格路径的 early-create 回合卡才是与现状 ≤300 字符通知行为一致的最优解。

警告族 AgentNotice（credits_tracker.py 唯一 Python 生产方）行全部 <100
字符，默认上限已覆盖，无特判。hermes 通知行硬编码英文（locales 全库无
中文通知文案），正则不猜测中文变体。
"""

from __future__ import annotations

import asyncio

import pytest
from hermes_lark_streaming.cardkit.cards import _render_status_children
from hermes_lark_streaming.cardkit.progline import parse_progress_line
from hermes_lark_streaming.composer.classify import (
    CH_PLAIN,
    CH_PROGRESS,
    _is_progress_text,
    classify_send,
)
from hermes_lark_streaming.plugin.adapter import FeishuStreamingAdapter, _upgrade_plain_to_progress

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

# 官方 format_subagent_failure_line 形态构造（delegate_tool_progress.py:64-93）
# failed 行：18(前缀) + 66(goal≤60 含引号破折号) + 13(elapsed) + 2(: ) + 200(reason cap) + 60(固定尾) ≈ 359
_LONG_REASON = "x" * 200
_FAILED_LINE = (
    "⚠️ Subagent failed — \"research competitor pricing across three markets\" after 12 min: "
    f"{_LONG_REASON}. Details: /agents, or ask me to retry with a smaller task."
)
# timeout 行：21 + 66 + 13 + 165(固定尾) ≈ 265；goal 顶格也不超 300 —— 现状已覆盖，
# 豁免后行为不变（进面板）
_TIMEOUT_LINE = (
    "⚠️ Subagent timed out — \"scrape the full changelog archive\" after 45 min without finishing. "
    "I will carry on without it; ask me to retry it, or raise delegation.child_timeout_seconds "
    "in config.yaml if these tasks legitimately take longer."
)
# credits 警告族样例（credits_tracker.py:200 —— 全部 <100 字符，现状已覆盖）
_CREDITS_LINE = "⚠ You've used $12.34 of your $20.00 cap"


def _long_nonspecific_emoji_line() -> str:
    """>300 字符的非通知 emoji 行（不豁免）."""
    return "⚠️ 自定义超长错误说明" + "。这件事与子代理无关，只是一段很长的文本" * 20


# ---------------------------------------------------------------------------
# 1. _is_progress_text — 通知轨豁免
# ---------------------------------------------------------------------------


class TestPlatformNoticeHeuristic:
    def test_long_failed_line_over_300(self):
        # 修复目标行：>300 且官方前缀 → 豁免生效
        assert len(_FAILED_LINE) > 300
        assert _is_progress_text(_FAILED_LINE) is True

    def test_timeout_line(self):
        assert _is_progress_text(_TIMEOUT_LINE) is True

    def test_bare_warning_sign_variant(self):
        # 裸 ⚠（U+26A0 无 FE0F）变体容忍
        line = _FAILED_LINE.replace("⚠️", "⚠", 1)
        assert _is_progress_text(line) is True

    def test_defensive_cap_600(self):
        line = _FAILED_LINE + "y" * 300  # >600
        assert _is_progress_text(line) is False

    def test_nonspecific_long_emoji_line_not_exempt(self):
        # >300 非 Subagent 前缀行：不豁免（防误吸收红线）
        assert _is_progress_text(_long_nonspecific_emoji_line()) is False

    def test_mid_sentence_mention_not_exempt(self):
        line = "请看这个 ⚠️ Subagent failed 说明 " + "z" * 320
        assert _is_progress_text(line) is False

    def test_lowercase_not_exempt(self):
        # 官方硬编码首字母大写；小写变体非官方形态，不豁免（>300 时）
        line = "⚠️ subagent failed " + "w" * 320
        assert _is_progress_text(line) is False

    def test_short_lines_regression(self):
        # 现状回归：≤300 emoji 行 / credits 警告行 / 💬 思考行照常
        assert _is_progress_text("🔍 Searching the web") is True
        assert _is_progress_text(_CREDITS_LINE) is True
        assert _is_progress_text("💬 我来验证一下这个思路") is True
        assert _is_progress_text(_FAILED_LINE.replace(_LONG_REASON, "模型未找到")) is True

    def test_empty_and_plain(self):
        assert _is_progress_text("") is False
        assert _is_progress_text("普通长文本 " + "q" * 320) is False


# ---------------------------------------------------------------------------
# 2. 分类层不变 — 通知行走 CH_PLAIN，升格在适配器层
# ---------------------------------------------------------------------------


class TestClassifyLayer:
    def test_notice_without_metadata_is_plain(self):
        cls = classify_send(_FAILED_LINE, None)
        assert cls.channel == CH_PLAIN and cls.kind == "plain"

    def test_notice_with_empty_metadata_is_plain(self):
        cls = classify_send(_FAILED_LINE, {})
        assert cls.channel == CH_PLAIN


# ---------------------------------------------------------------------------
# 3. 升格层 — 豁免生效 + 渲染先例
# ---------------------------------------------------------------------------


class TestUpgradeLayer:
    def test_long_notice_upgrades_to_progress(self):
        cls = _upgrade_plain_to_progress(None, "oc_c", _FAILED_LINE, None)
        assert cls is not None and cls.channel == CH_PROGRESS
        assert cls.kind == "tool"  # parse_progress_line: emoji 行 → tool，tool_name=""

    def test_parse_progress_line_tool_empty_name(self):
        kind, tool, clean = parse_progress_line(_FAILED_LINE)
        assert kind == "tool" and tool == ""
        assert "Subagent failed" in clean

    def test_long_nonspecific_not_upgraded(self):
        assert _upgrade_plain_to_progress(
            None, "oc_c", _long_nonspecific_emoji_line(), None) is None

    def test_credits_line_upgrades_regression(self):
        cls = _upgrade_plain_to_progress(None, "oc_c", _CREDITS_LINE, None)
        assert cls is not None and cls.channel == CH_PROGRESS

    def test_notice_with_notify_meta_never_upgrades(self):
        # v2.0.8 红线：notify 帧永不升格（通知轨不带 notify——防未来冲突）
        assert _upgrade_plain_to_progress(
            None, "oc_c", _FAILED_LINE, {"notify": True}) is None


class TestNoticeRowRendering:
    """通知行进面板后的渲染（v2.0.10 通报行样式先例）."""

    def test_renders_warning_icon(self):
        from hermes_lark_streaming.cardkit.tool_icons import resolve_notice_icon

        assert resolve_notice_icon("⚠️") == "warning_outlined"

    def test_panel_row_shape(self):
        children = _render_status_children([_FAILED_LINE], durations={}, line_times=[])
        assert children, "通知行必须渲染出面板行元素"
        row = children[0]
        # v2.0.10 通报行结构：div + standard_icon 图标 + 文本
        assert row.get("tag") == "div"
        assert row.get("icon", {}).get("tag") == "standard_icon"
        body = row.get("text", {}).get("content", "")
        assert "Subagent failed" in body


# ---------------------------------------------------------------------------
# 4. composer 集成 — 有卡并入 / 无卡 early-create
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self):
        self.cards: dict[str, dict] = {}
        self._n = 0

    async def cardkit_create(self, card):
        self._n += 1
        self.cards[str(self._n)] = {"card": card, "seq": 0}
        return str(self._n)

    async def send_card_by_id_to_chat(self, chat_id, card_id):
        return f"om_{card_id}"

    async def cardkit_stream_element(self, card_id, element_id, content, sequence=0):
        self.cards[card_id]["seq"] = sequence

    async def cardkit_update(self, card_id, card, sequence=0):
        self.cards[card_id]["card"] = card
        self.cards[card_id]["seq"] = sequence

    async def cardkit_batch_update(self, card_id, operations, sequence=0):
        # finalize 三步定稿用（v2.0.9 契约）；记录即可
        self.cards.setdefault(card_id, {"card": {}, "seq": 0})["seq"] = sequence
        self.cards[card_id].setdefault("ops", []).append(operations)

    async def cardkit_close_streaming(self, card_id, sequence=0, summary=None):
        # finalize 收尾步（v2.0.9 契约）：关流式 + summary
        self.cards.setdefault(card_id, {"card": {}, "seq": 0})["seq"] = sequence
        self.cards[card_id]["closed"] = True


@pytest.fixture()
def composer():
    from hermes_lark_streaming.cardkit.cards import build_streaming_card_v2
    from hermes_lark_streaming.composer.engine import FrameComposer
    from hermes_lark_streaming.plugin import tool_timing

    client = _FakeClient()
    comp = FrameComposer(
        get_client=lambda: client,
        build_card=lambda **kw: build_streaming_card_v2(**kw),
        build_final_card=lambda **kw: {},
        config=None,
    )
    tool_timing.reset_for_tests()
    tool_timing.bind_composer(comp)
    yield comp
    tool_timing.reset_for_tests()


async def _settle() -> None:
    for _ in range(4):
        await asyncio.sleep(0)
    cur = asyncio.current_task()
    ts = [t for t in asyncio.all_tasks() if t is not cur and not t.done()]
    if ts:
        await asyncio.gather(*ts, return_exceptions=True)


class TestComposerRouting:
    async def test_notice_joins_active_panel(self, composer):
        """有活跃卡：>300 通知行并入面板（修复前漏原生 post 的场景）."""
        from tests.e2e.test_v208_stream_quality import _seed_active

        chat = "oc_notice_active"
        await _seed_active(composer, chat)
        cls = _upgrade_plain_to_progress(None, chat, _FAILED_LINE, None)
        result = await composer.handle_send(chat, _FAILED_LINE, None, None, cls)
        assert result is not None and result.success is True
        st = composer.active_state(chat)
        assert any("Subagent failed" in ln for ln in st.status_lines)

    async def test_notice_without_card_creates_early_card(self, composer):
        """无活跃卡：>300 通知行 early-create 回合卡 + 暂存（卡片先现）."""
        chat = "oc_notice_nocard"
        cls = _upgrade_plain_to_progress(None, chat, _FAILED_LINE, None)
        result = await composer.handle_send(chat, _FAILED_LINE, None, None, cls)
        assert result is not None and result.success is True
        st = composer.active_state(chat)
        assert st is not None and st.card_id is not None
        # v2.0.4「卡片先现」：early-create 时 pending 并入首卡面板后清空
        assert any("Subagent failed" in ln for ln in st.status_lines)

    async def test_notice_then_answer_finalize_single_card(self, composer):
        """通知先现 → answer 流接管 → finalize：单卡全回合（native 设计）."""
        from tests.e2e.test_v208_stream_quality import _seed_active

        chat = "oc_notice_fullturn"
        st = await _seed_active(composer, chat)
        cls = _upgrade_plain_to_progress(None, chat, _FAILED_LINE, None)
        await composer.handle_send(chat, _FAILED_LINE, None, None, cls)
        await composer.handle_send(
            chat, "子代理失败了，我直接用本地数据完成分析：……", None,
            {"expect_edits": True}, classify_send("answer", {"expect_edits": True}))
        result = await composer._on_final_send(chat, "最终答案：分析结果……")
        assert result is not None and result.success is True
        assert st.finalized is True
        assert any("Subagent failed" in ln for ln in st.status_lines)
        # 全程只有一张卡
        assert len(composer._get_client().cards) == 1


# ---------------------------------------------------------------------------
# 5. adapter.send 接线 — 通知不再漏原生
# ---------------------------------------------------------------------------


class _WiringClient:
    def __init__(self):
        self.sent_cards: list[dict] = []

    async def send_card_to_chat(self, chat_id, card):
        self.sent_cards.append(card)
        return "om_wired"


class _WiringComposer:
    def __init__(self, active: bool = False):
        self._active = active
        self.handled: list[tuple] = []

    def active_state(self, chat_id):
        return object() if self._active else None

    async def handle_send(self, chat_id, content, reply_to, metadata, cls):
        self.handled.append((chat_id, content, metadata, cls))
        return "composer-sentinel"


class _WiringCfg:
    oneshot_cards = True


def _wiring_adapter(client, composer):
    adapter = FeishuStreamingAdapter.__new__(FeishuStreamingAdapter)
    adapter._card_config = _WiringCfg()
    adapter._card_client = client
    adapter._composer = composer
    return adapter


class TestAdapterSendWiring:
    async def test_long_notice_with_active_card_reaches_panel(self):
        """核心漏洞场景（Task 21 定案）：>300 通知 + 活跃卡 → 面板，不再原生 post."""
        client, comp = _WiringClient(), _WiringComposer(active=True)
        adapter = _wiring_adapter(client, comp)
        result = await adapter.send("oc_c", _FAILED_LINE, metadata=None)
        assert result == "composer-sentinel"
        assert len(comp.handled) == 1
        assert comp.handled[0][3].channel == CH_PROGRESS
        assert client.sent_cards == []  # 不产生独立卡（多卡片红线）

    async def test_long_notice_without_card_early_creates(self):
        """无活跃卡：升格 → composer early-create（而非 oneshot/原生）."""
        client, comp = _WiringClient(), _WiringComposer(active=False)
        adapter = _wiring_adapter(client, comp)
        result = await adapter.send("oc_c", _FAILED_LINE, metadata=None)
        assert result == "composer-sentinel"
        assert comp.handled[0][3].channel == CH_PROGRESS
        assert client.sent_cards == []

    async def test_nonspecific_long_line_with_active_card_still_native(self, monkeypatch):
        """非通知长文 + 活跃卡：维持既有语义（R3 不适用 → 原生 post）.

        mock 先例照抄 test_v210 TestStatusLane._stub：占位基类 send 换记录器."""
        import hermes_lark_streaming.plugin.adapter as adp

        native: list[tuple] = []

        async def _fake_send(self, chat_id, content, reply_to=None, metadata=None):
            native.append((chat_id, content))
            return "native-sentinel"

        monkeypatch.setattr(adp._MissingBuiltin, "send", _fake_send, raising=False)
        client, comp = _WiringClient(), _WiringComposer(active=True)
        adapter = _wiring_adapter(client, comp)
        result = await adapter.send("oc_c", _long_nonspecific_emoji_line(), metadata=None)
        assert result == "native-sentinel"
        assert comp.handled == [] and client.sent_cards == []
        assert len(native) == 1
