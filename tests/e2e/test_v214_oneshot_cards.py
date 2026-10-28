"""v2.0.14 回归测试 — 回合外正式投递一次性定稿卡（cron 投递族卡片化）.

Task 20 取证定案的修复（用户 2026-10-13 截图事故）：

事故链：cron job 967430ee4ab2（daily-morning-news-830）08:31:15 经 live
adapter lane 投递（agent.log:2530 `delivered to feishu-streaming:oc_3855…`
message_id=om_x100b63e3f3f570a8b10b002385d3782），im GET 实锤
msg_type=post —— cron 响应以富文本而非卡片到达。

根因：hermes cron 投递走 ``adapter.send``（cron/scheduler_delivery.py:1458
metadata 携带官方标记 ``{"job_id", "notify"}``）→ classify CH_FINAL →
engine._on_final_send 回合外无活跃卡返回 None → super().send() 原生 post。
V1 曾以 monkey-patch _deliver_result 卡片化（patching/gateway.py:853），
v2 移除补丁层后能力断档。

修复：adapter._send_oneshot_card 三规则（R1 job_id / R2 回合外 final /
R3 ≥200 字 plain）→ 一次性静态定稿卡（build_cron_card / build_gateway_card），
无状态不参与回合，降级红线回落原生路径。

官方格式冻结源：cron/scheduler_delivery.py:2024-2031（包装头逐行镜像）。
"""

from __future__ import annotations

import pytest
from hermes_lark_streaming.cardkit.special import parse_cron_delivery
from hermes_lark_streaming.composer.classify import classify_send
from hermes_lark_streaming.plugin.adapter import FeishuStreamingAdapter

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

# 官方 _deliver_result 包装头（scheduler_delivery.py:2025-2031 逐字结构）
_WRAPPED = (
    "Cronjob Response: daily-morning-news-830\n"
    "(job_id: 967430ee4ab2)\n"
    "-------------\n"
    "\n"
    "📰 **每日要闻**\n\n- 胡塞——以色列空袭\n- 美股收涨\n"
)


# ---------------------------------------------------------------------------
# 1. parse_cron_delivery — 官方包装头解析
# ---------------------------------------------------------------------------


class TestParseCronDelivery:
    def test_wrapped_full(self):
        title, job_id, body = parse_cron_delivery(_WRAPPED)
        assert title == "daily-morning-news-830"
        assert job_id == "967430ee4ab2"
        assert body.startswith("📰 **每日要闻**")
        assert "Cronjob Response:" not in body
        assert "(job_id:" not in body

    def test_unwrapped_passthrough(self):
        raw = "raw job output without wrap_response"
        assert parse_cron_delivery(raw) == ("", "", raw)

    def test_empty(self):
        assert parse_cron_delivery("") == ("", "", "")
        assert parse_cron_delivery(None) == ("", "", "")

    def test_header_without_job_id(self):
        title, job_id, body = parse_cron_delivery("Cronjob Response: t\n-----\nbody")
        assert title == "t"
        assert job_id == ""
        assert body == "body"

    def test_header_without_dashes(self):
        title, job_id, body = parse_cron_delivery("Cronjob Response: t\n(job_id: j)\nbody")
        assert (title, job_id, body) == ("t", "j", "body")

    def test_body_with_dashes_not_consumed(self):
        # 正文中的 ---- 行不属于包装头（无头部时不跳过）
        raw = "line1\n----\nline2"
        assert parse_cron_delivery(raw) == ("", "", raw)

    def test_never_raises(self):
        # 非常输入降级原样透传
        weird = "Cronjob Response: \n(job_id:)\n---\n\n"
        title, job_id, body = parse_cron_delivery(weird)
        assert isinstance(title, str) and isinstance(job_id, str) and isinstance(body, str)


# ---------------------------------------------------------------------------
# 2. build_cron_card — 向后兼容扩展（标题行 / job_id 页脚）
# ---------------------------------------------------------------------------


class TestBuildCronCardExtended:
    def test_legacy_shape_frozen(self):
        """缺省参数 = V1 行为逐字节一致（既有测试冻结契约）."""
        from hermes_lark_streaming.cardkit import build_cron_card

        card = build_cron_card("Hello **world**")
        assert card["schema"] == "2.0"
        assert all(e["tag"] == "markdown" for e in card["body"]["elements"])

    def test_title_row_and_hr(self):
        from hermes_lark_streaming.cardkit import build_cron_card

        card = build_cron_card("正文", title="daily-job", job_id="j1")
        elements = card["body"]["elements"]
        assert elements[0]["tag"] == "div"
        assert "icon" not in elements[0]  # v2.0.16：cron 头部无图标（用户验收）
        assert "**daily-job**" in elements[0]["text"]["content"]
        assert elements[1]["tag"] == "hr"
        assert elements[2]["tag"] == "markdown"
        # 页脚：job_id 溯源行（灰色 notation，_build_notice_row 同构）
        footer = elements[-1]
        assert footer["tag"] == "div"
        assert "job_id: j1" in footer["text"]["content"]
        assert footer["text"]["text_color"] == "grey"

    def test_title_summary(self):
        from hermes_lark_streaming.cardkit import build_cron_card

        card = build_cron_card("x" * 300, title="t-name")
        assert card["config"]["summary"] == {"content": "t-name"}

    def test_job_id_footer_without_title(self):
        from hermes_lark_streaming.cardkit import build_cron_card

        card = build_cron_card("正文", job_id="abc")
        tags = [e["tag"] for e in card["body"]["elements"]]
        assert tags.count("div") == 1  # 仅页脚行
        assert "job_id: abc" in card["body"]["elements"][-1]["text"]["content"]


# ---------------------------------------------------------------------------
# 3. _send_oneshot_card — 三规则门控
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self):
        self.sent_cards: list[dict] = []
        self.replies: list[tuple[str, dict]] = []
        self.fail = False

    async def send_card_to_chat(self, chat_id, card):
        if self.fail:
            raise RuntimeError("feishu api down")
        self.sent_cards.append(card)
        return f"om_fake{len(self.sent_cards)}"

    async def reply_card(self, message_id, card):
        if self.fail:
            raise RuntimeError("feishu api down")
        self.replies.append((message_id, card))
        return f"om_fake_reply{len(self.replies)}"


class _FakeComposer:
    def __init__(self, active: bool = False):
        self._active = active
        self.handled: list[tuple] = []

    def active_state(self, chat_id):
        return object() if self._active else None

    async def handle_send(self, chat_id, content, reply_to, metadata, cls):
        self.handled.append((chat_id, content, metadata, cls))
        return "composer-sentinel"


class _Cfg:
    def __init__(self, oneshot: bool = True):
        self.oneshot_cards = oneshot


def _bare_adapter(client, composer, cfg=None):
    adapter = FeishuStreamingAdapter.__new__(FeishuStreamingAdapter)
    adapter._card_config = cfg if cfg is not None else _Cfg()
    adapter._card_client = client
    adapter._composer = composer
    return adapter


class TestOneshotGates:
    async def test_r1_job_id_wrapped(self):
        client, comp = _FakeClient(), _FakeComposer()
        adapter = _bare_adapter(client, comp)
        cls = classify_send(_WRAPPED, {"job_id": "j1", "notify": True})
        result = await adapter._send_oneshot_card("oc_c", _WRAPPED, {"job_id": "j1", "notify": True}, cls)
        assert result.success is True and result.message_id == "om_fake1"
        card = client.sent_cards[0]
        assert card["body"]["elements"][0]["text"]["content"] == "**daily-morning-news-830**"
        assert "job_id: j1" in card["body"]["elements"][-1]["text"]["content"]
        # 无状态红线：composer 未被触碰
        assert comp.handled == []

    async def test_r1_job_id_only_no_notify(self):
        """notify=false 配置下 job_id 仍识别（R1 与分类无关）."""
        client, comp = _FakeClient(), _FakeComposer()
        adapter = _bare_adapter(client, comp)
        cls = classify_send(_WRAPPED, {"job_id": "j2"})
        result = await adapter._send_oneshot_card("oc_c", _WRAPPED, {"job_id": "j2"}, cls)
        assert result.success is True and len(client.sent_cards) == 1

    async def test_r1_thread_reply(self):
        client, comp = _FakeClient(), _FakeComposer()
        adapter = _bare_adapter(client, comp)
        meta = {"job_id": "j3", "thread_id": "om_root_msg"}
        await adapter._send_oneshot_card("oc_c", _WRAPPED, meta, classify_send(_WRAPPED, meta))
        assert client.replies and client.replies[0][0] == "om_root_msg"
        assert client.sent_cards == []

    async def test_r1_unwrapped_content(self):
        """wrap_response=false：无包装头 → 兜底标题 Cronjob."""
        client, comp = _FakeClient(), _FakeComposer()
        adapter = _bare_adapter(client, comp)
        raw = "plain output"
        meta = {"job_id": "j4"}
        await adapter._send_oneshot_card("oc_c", raw, meta, classify_send(raw, meta))
        card = client.sent_cards[0]
        assert card["body"]["elements"][0]["text"]["content"] == "**Cronjob**"

    async def test_r2_final_without_active_card(self):
        client, comp = _FakeClient(), _FakeComposer()
        adapter = _bare_adapter(client, comp)
        content = "out-of-band final notice"
        meta = {"notify": True}
        result = await adapter._send_oneshot_card(
            "oc_c", content, meta, classify_send(content, meta))
        assert result.success is True
        # gateway 卡：无标题行、无 job_id 页脚，纯 markdown 正文
        assert client.sent_cards[0]["body"]["elements"][0]["tag"] == "markdown"

    async def test_r2_with_active_card_returns_none(self):
        """有活跃卡 = 回合定稿语义 → 保持既有 finalize 路径（不适用）."""
        client, comp = _FakeClient(), _FakeComposer(active=True)
        adapter = _bare_adapter(client, comp)
        meta = {"notify": True}
        result = await adapter._send_oneshot_card(
            "oc_c", "final", meta, classify_send("final", meta))
        assert result is None and client.sent_cards == []

    async def test_r3_longform_plain(self):
        client, comp = _FakeClient(), _FakeComposer()
        adapter = _bare_adapter(client, comp)
        content = "长文投递测试" + "x" * 250
        result = await adapter._send_oneshot_card("oc_c", content, None, classify_send(content, None))
        assert result.success is True

    async def test_r3_short_plain_returns_none(self):
        client, comp = _FakeClient(), _FakeComposer()
        adapter = _bare_adapter(client, comp)
        result = await adapter._send_oneshot_card(
            "oc_c", "短消息", None, classify_send("短消息", None))
        assert result is None and client.sent_cards == []

    async def test_config_off_returns_none(self):
        client, comp = _FakeClient(), _FakeComposer()
        adapter = _bare_adapter(client, comp, cfg=_Cfg(oneshot=False))
        meta = {"job_id": "j5"}
        result = await adapter._send_oneshot_card("oc_c", _WRAPPED, meta, classify_send(_WRAPPED, meta))
        assert result is None and client.sent_cards == []

    async def test_no_client_returns_none(self):
        adapter = FeishuStreamingAdapter.__new__(FeishuStreamingAdapter)
        adapter._card_config = _Cfg()
        adapter._card_client = None
        adapter._app_id = ""
        adapter._app_secret = ""
        adapter._composer = _FakeComposer()
        meta = {"job_id": "j6"}
        result = await adapter._send_oneshot_card("oc_c", _WRAPPED, meta, classify_send(_WRAPPED, meta))
        assert result is None

    async def test_send_failure_raises_red_line(self):
        """发送异常向上抛（send() 捕获后回落原生）——内容永不静默丢失."""
        client, comp = _FakeClient(), _FakeComposer()
        client.fail = True
        adapter = _bare_adapter(client, comp)
        meta = {"job_id": "j7"}
        with pytest.raises(RuntimeError):
            await adapter._send_oneshot_card("oc_c", _WRAPPED, meta, classify_send(_WRAPPED, meta))


# ---------------------------------------------------------------------------
# 4. send() 接线 — 一次性卡优先 + 降级回落
# ---------------------------------------------------------------------------


class TestOneshotSendWiring:
    async def test_send_routes_cron_to_oneshot(self):
        client, comp = _FakeClient(), _FakeComposer()
        adapter = _bare_adapter(client, comp)
        meta = {"job_id": "j8", "notify": True}
        result = await adapter.send("oc_c", _WRAPPED, metadata=meta)
        assert result.success is True
        assert len(client.sent_cards) == 1
        assert comp.handled == []  # 不进 composer 回合通道

    async def test_send_oneshot_failure_falls_to_composer(self):
        """降级红线：oneshot 异常 → CH_FINAL 继续走 composer.handle_send."""
        client, comp = _FakeClient(), _FakeComposer()
        client.fail = True
        adapter = _bare_adapter(client, comp)
        meta = {"notify": True}
        result = await adapter.send("oc_c", "final content", metadata=meta)
        assert result == "composer-sentinel"
        assert len(comp.handled) == 1

    async def test_send_reply_to_skips_oneshot(self):
        """reply_to 语义（回复锚点）不适用一次性卡 → 直接进既有通道."""
        client, comp = _FakeClient(), _FakeComposer()
        adapter = _bare_adapter(client, comp)
        meta = {"job_id": "j9", "notify": True}
        result = await adapter.send("oc_c", _WRAPPED, reply_to="om_anchor", metadata=meta)
        assert result == "composer-sentinel"
        assert client.sent_cards == [] and client.replies == []

    async def test_send_short_plain_skips_oneshot(self):
        """短 plain（<200）维持既有路由（升格启发式 / 原生）——本例进 composer."""
        client, comp = _FakeClient(), _FakeComposer()
        adapter = _bare_adapter(client, comp)
        # 💬 思考行：升格 CH_PROGRESS → composer
        result = await adapter.send("oc_c", "💬 thinking line", metadata={})
        assert result == "composer-sentinel"
        assert client.sent_cards == []
