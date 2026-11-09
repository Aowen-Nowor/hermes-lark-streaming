"""UX 三项修复回归测试（2026-11-09 审计）.

对应修复（全部主链路径，非兑底 hack）：

- UX-1b 面板收口重建的 200 元素上限守卫：finalize._reseal_panel 直拼
  state.status_lines 全量重建且此前无裁剪——流式期曾被元素/字节预算裁过的
  回合，定稿收口会把裁掉的行原样拼回 → delete+add 原子 batch 被飞书整体
  拒收（300305/11310）→ 面板留在流式渲染态。修复 = 重建后、入 batch 前
  过 cards._enforce_card_element_limit（与 build_streaming_card_v2 出厂
  同一机制，包裹卡 panel 独占 body 口径，阈值 195 与建卡一致）。
- UX-2 i18n 接线：engine._panel_title 产出 ``(en, zh)`` 双语对（i18n 表
  rounds/tools_count 死键接线）；cards._apply_panel_title 写 content=en
  兑底 + i18n_content 双语（house 约定同 elements._build_unified_panel_
  placeholder / _build_footer_elements）；elements._build_reasoning_round_
  title 不再丢弃 en_label；finalize 空 answer 摘要兑底不再单语 zh。
- UX-3a oneshot reply 失败 → 直发群：镜像 composer.engine._create_card
  流式路径的生产实证模式（reply 失败（消息被删/非法）→ 记 warning →
  send_card_to_chat 直发，内容永不丢红线），thread_id 与 reply_to 两个
  reply_card 调用位同款。
- UX-3b cron 卡分块上限：build_cron_card 此前按 _split_long_text（2400
  字符/块）无限切块 → 超大 cron 产物产出必被拒收的超限卡；截到 10 块并
  追加双语截断标记行。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hermes_lark_streaming.cardkit.cards import (
    _apply_panel_title,
    build_streaming_card_v2,
)
from hermes_lark_streaming.cardkit.elements import (
    UNIFIED_PANEL_ELEMENT_ID,
    _build_reasoning_round_title,
)
from hermes_lark_streaming.cardkit.special import build_cron_card
from hermes_lark_streaming.composer.classify import classify_send
from hermes_lark_streaming.composer.engine import _panel_title
from hermes_lark_streaming.plugin.adapter import FeishuStreamingAdapter

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _panel_of(card: dict) -> dict | None:
    for elem in card.get("body", {}).get("elements", []):
        if elem.get("element_id") == UNIFIED_PANEL_ELEMENT_ID:
            return elem
    return None


# ---------------------------------------------------------------------------
# UX-1b：_reseal_panel 重建面板先过元素上限（超限回合定稿不再产出拒收 batch）
# ---------------------------------------------------------------------------


class _ResealFakeClient:
    """finalize 四步所需的最小 fake（test_v216_ui_polish._FakeClient 同款）."""

    def __init__(self):
        self.batches: list[list[dict]] = []
        self.streamed: list[str] = []

    async def cardkit_stream_element(self, card_id, element_id, content, sequence=0):
        self.streamed.append(content)

    async def cardkit_batch_update(self, card_id, operations, sequence=0):
        self.batches.append(operations)

    async def cardkit_close_streaming(self, card_id, sequence=0, summary=None):
        return None


def _reseal_state(status_lines: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        card_id="card_reseal",
        seq=0,
        status_lines=status_lines,
        tool_durations={},
        line_times=[],
        answer_text="",
        created_at=0.0,
    )


def _reseal_add_action(operations: list[dict]) -> dict | None:
    """batch 中 delete 面板 + add 重建的动作对 → 返回 add 动作（无则 None）."""
    actions = {op.get("action") for op in operations}
    if "delete_elements" in actions and "add_elements" in actions:
        for op in operations:
            if op.get("action") == "add_elements":
                return op
    return None


class TestUx1bResealElementLimit:
    async def test_oversized_lines_reseal_batch_stays_within_limit(self):
        """>200 元素量级的 status_lines 定稿：重建面板裁到阈值内 + 诚实双语提示.

        事故链复现：流式期 200 元素/30KB 预算会裁面板行（仅作用于上传视图，
        state.status_lines 恒存全量）→ 旧 _reseal_panel 用全量行重建且无守卫
        → add 超限 → 原子 batch 被拒 → 面板收口失败。修复后重建面板与建卡
        同一 195 阈值口径，且提示行为 UX-1a 诚实双语措辞（非「已折叠」）。
        """
        from hermes_lark_streaming.cardkit.cards import _count_tag_objects
        from hermes_lark_streaming.composer.finalize import finalize_card

        # 80 条工具行（每行 3 tag：图标标题×2 + 摘要×1）+ 尾部未封口思考轮
        # （流式橙 → 定稿绿，触发收口重建而非幂等跳过）
        lines = [f'🖥️ terminal："cmd-{i:02d} ' + "x" * 60 + '"' for i in range(80)]
        lines.append("💬 流式中的最后一轮思考")
        client = _ResealFakeClient()
        await finalize_card(
            client, _reseal_state(lines), answer_element_id="answer_content",
            panel_title=("agent loop", "agent loop"), panel_expanded=True)

        reseal_batches = [b for b in client.batches if _reseal_add_action(b)]
        assert reseal_batches, "未封口思考轮必须触发面板收口重建"
        add = _reseal_add_action(reseal_batches[0])
        assert add["params"]["target_element_id"] == "answer_content"
        panel = add["params"]["elements"][0]
        assert panel["element_id"] == UNIFIED_PANEL_ELEMENT_ID
        # 重建面板整体（含 header）不超建卡同款阈值 195
        assert _count_tag_objects(panel) <= 195, _count_tag_objects(panel)
        # 诚实双语提示在面板首部（en 兑底 content + i18n zh），无「已折叠」谎报
        first = panel["elements"][0]
        assert "earlier steps omitted" in first["content"]
        assert "已折叠" not in first["content"]
        assert "项已省略" in first["i18n_content"]["zh_cn"]
        blob = json.dumps(panel, ensure_ascii=False)
        assert "cmd-00" not in blob, "裁剪必须从最旧行开始"
        assert "流式中的最后一轮思考" in blob, "最新行必须保留"

    async def test_small_lines_reseal_unaffected(self):
        """正常体量回合收口：重建面板无提示行、行数不缩水（修复零误伤）."""
        from hermes_lark_streaming.cardkit.cards import _count_tag_objects
        from hermes_lark_streaming.composer.finalize import finalize_card

        lines = ["💬 第一轮", '🔧 a："x"', "💬 流式中的最后一轮"]
        client = _ResealFakeClient()
        await finalize_card(
            client, _reseal_state(lines), answer_element_id="answer_content")
        add = _reseal_add_action(client.batches[-1])
        assert add is not None
        panel = add["params"]["elements"][0]
        assert _count_tag_objects(panel) <= 195
        for child in panel["elements"]:
            assert "earlier steps omitted" not in child.get("content", "")


# ---------------------------------------------------------------------------
# UX-2：面板/轮次标题 i18n 接线 + 摘要兜底
# ---------------------------------------------------------------------------


class TestUx2PanelTitleI18n:
    def test_engine_panel_title_pair(self):
        """:_panel_title 产出 (en, zh) 对——i18n 表 rounds/tools_count 死键接线."""
        en, zh = _panel_title(["💬 A", '🔧 a："x"', '🔧 b："y"'])
        assert en == "agent loop · 1 rounds · 2 tools"
        assert zh == "agent loop · 1 轮 · 2 个工具"

    def test_card_panel_title_bilingual(self):
        """locales=["zh_cn","en_us"] 卡：标题 content=en 兑底 + i18n 双语对."""
        lines = ["💬 A", '🔧 a："x"']
        card = build_streaming_card_v2(
            status_lines=lines, panel_title=_panel_title(lines))
        assert card["config"]["locales"] == ["zh_cn", "en_us"]
        title = _panel_of(card)["header"]["title"]
        assert title["content"] == "agent loop · 1 rounds · 1 tools"
        assert title["i18n_content"] == {
            "zh_cn": "agent loop · 1 轮 · 1 个工具",
            "en_us": "agent loop · 1 rounds · 1 tools",
        }

    def test_card_panel_title_str_monolingual_compat(self):
        """zh-only 文案（str 直传，旧调用方/测试契约）：content 与 i18n 同文."""
        card = build_streaming_card_v2(status_lines=["💬 A"], panel_title="agent loop")
        title = _panel_of(card)["header"]["title"]
        assert title["content"] == "agent loop"
        assert title["i18n_content"] == {"zh_cn": "agent loop", "en_us": "agent loop"}

    def test_apply_panel_title_direct(self):
        panel = {"header": {"title": {"tag": "plain_text", "content": "old"}}}
        _apply_panel_title(panel, ("en-title", "zh-标题"))
        title = panel["header"]["title"]
        assert title["content"] == "en-title"
        assert title["i18n_content"] == {"zh_cn": "zh-标题", "en_us": "en-title"}


class TestUx2RoundTitleI18n:
    def test_round_title_pair_with_elapsed(self):
        el = _build_reasoning_round_title(2, 2500.0, finalized=True)
        assert el["text"]["content"] == "<font color='green'>**Round 2 · 2.5s**</font>"
        assert el["text"]["i18n_content"]["zh_cn"] == (
            "<font color='green'>**第 2 轮 · 2.5s**</font>")

    def test_round_title_pair_streaming_color(self):
        el = _build_reasoning_round_title(1, 0.0, finalized=False)
        assert "<font color='orange-300'>**Round 1**</font>" in el["text"]["content"]
        assert "**第 1 轮**" in el["text"]["i18n_content"]["zh_cn"]


class TestUx2EmptyAnswerSummary:
    def test_empty_answer_bilingual_fallback(self):
        from hermes_lark_streaming.composer.finalize import _summary_of

        assert _summary_of(SimpleNamespace(answer_text="")) == "已完成 · Completed"
        assert _summary_of(SimpleNamespace(answer_text=None)) == "已完成 · Completed"

    def test_nonempty_answer_unchanged(self):
        from hermes_lark_streaming.composer.finalize import _summary_of

        assert _summary_of(SimpleNamespace(answer_text="第一行\n第二行")) == "第一行"


# ---------------------------------------------------------------------------
# UX-3a：oneshot reply 失败 → 直发群（镜像 engine._create_card 流式路径）
# ---------------------------------------------------------------------------

# 官方 _deliver_result 包装头（test_v214_oneshot_cards 同款逐字结构）
_WRAPPED = (
    "Cronjob Response: daily-morning-news-830\n"
    "(job_id: 967430ee4ab2)\n"
    "-------------\n"
    "\n"
    "📰 **每日要闻**\n\n- 胡塞——以色列空袭\n- 美股收涨\n"
)


class _ReplyFailClient:
    """reply_card 恒抛（消息被删/非法锚点生产形态）；直发记录入参."""

    def __init__(self):
        self.reply_attempts: list[str] = []
        self.sent_cards: list[tuple[str, dict]] = []

    async def reply_card(self, message_id, card):
        self.reply_attempts.append(message_id)
        raise RuntimeError("im:message not found (deleted anchor)")

    async def send_card_to_chat(self, chat_id, card):
        self.sent_cards.append((chat_id, card))
        return f"om_direct{len(self.sent_cards)}"


class _NoActiveComposer:
    def active_state(self, chat_id):
        return None

    async def handle_send(self, chat_id, content, reply_to, metadata, cls):  # pragma: no cover
        raise AssertionError("oneshot 成功时不得回落 composer")


def _bare_adapter(client):
    adapter = FeishuStreamingAdapter.__new__(FeishuStreamingAdapter)
    adapter._card_config = SimpleNamespace(oneshot_cards=True)
    adapter._card_client = client
    adapter._composer = _NoActiveComposer()
    return adapter


class TestUx3aOneshotReplyFallback:
    async def test_thread_anchor_reply_failure_falls_to_direct_send(self):
        """thread_id om_ 锚 reply 失败 → 记日志 + 直发群（卡不弃、内容不丢）."""
        client = _ReplyFailClient()
        adapter = _bare_adapter(client)
        meta = {"job_id": "j-ux3a", "thread_id": "om_root_msg"}
        cls = classify_send(_WRAPPED, meta)
        result = await adapter._send_oneshot_card("oc_chat", _WRAPPED, meta, cls)
        assert client.reply_attempts == ["om_root_msg"], "必须先尝试 reply"
        assert len(client.sent_cards) == 1, "reply 失败后必须直发同卡"
        chat_id, card = client.sent_cards[0]
        assert chat_id == "oc_chat"
        assert card["body"]["elements"][0]["text"]["content"] == "**daily-morning-news-830**"
        assert result.success is True
        assert result.message_id == "om_direct1"

    async def test_reply_to_anchor_reply_failure_falls_to_direct_send(self):
        """reply_to om_ 消息锚 reply 失败 → 同款直发兜底."""
        client = _ReplyFailClient()
        adapter = _bare_adapter(client)
        meta = {"job_id": "j-ux3a2", "notify": True}
        cls = classify_send(_WRAPPED, meta)
        result = await adapter._send_oneshot_card(
            "oc_chat", _WRAPPED, meta, cls, reply_to="om_anchor")
        assert client.reply_attempts == ["om_anchor"]
        assert len(client.sent_cards) == 1
        assert result.success is True

    async def test_no_anchor_goes_direct_without_reply_attempt(self):
        """无 om_ 锚（cron 合成 task_id 等）：原直发路径不回归、不白试 reply."""
        client = _ReplyFailClient()
        adapter = _bare_adapter(client)
        meta = {"job_id": "j-ux3a3"}
        await adapter._send_oneshot_card(
            "oc_chat", _WRAPPED, meta, classify_send(_WRAPPED, meta))
        assert client.reply_attempts == []
        assert len(client.sent_cards) == 1


# ---------------------------------------------------------------------------
# UX-3b：cron 卡分块上限 + 双语截断标记
# ---------------------------------------------------------------------------


class TestUx3bCronChunkCap:
    @staticmethod
    def _big_body(n: int) -> str:
        # 每段 ~2305 字符（<2400 块限、>1200 半窗），段间 \n\n → _split_long_text
        # 恰切出 n 块（段落边界切分，围栏/表格双检查天然通过）
        return "\n\n".join(f"段落{i:02d} 开头\n" + "x" * 2296 for i in range(n))

    def test_fifteen_chunks_capped_to_ten_with_bilingual_marker(self):
        """需 15 块的超大 cron 正文 → 恰好 10 块 + 截断标记（截尾不截头）."""
        card = build_cron_card(self._big_body(15), title="big-job", job_id="j1")
        elements = card["body"]["elements"]
        md_chunks = [e for e in elements if e.get("tag") == "markdown"]
        assert len(md_chunks) == 11, "10 个正文块 + 1 个截断标记"
        # 标记行：en 兑底 content + i18n 双语对（special.py 既有 i18n 风格）
        marker = elements[-2]  # 末位是 job_id 溯源页脚行
        assert marker["content"] == "…(truncated)"
        assert marker["i18n_content"] == {
            "zh_cn": "…（内容过长，已截断）",
            "en_us": "…(truncated)",
        }
        # 截尾不截头：前 10 块保留，后 5 块丢弃
        blob = json.dumps(card, ensure_ascii=False)
        assert "段落00" in blob and "段落09" in blob
        assert "段落10" not in blob and "段落14" not in blob
        # 元素上限安全（title/hr/块/标记/页脚全算）
        from hermes_lark_streaming.cardkit.cards import _count_tag_objects

        assert _count_tag_objects(card) <= 200

    def test_under_limit_no_marker_and_shape_frozen(self):
        """块数 ≤10：无标记行，V1 缺省形态逐字节兼容（既有冻结契约）."""
        card = build_cron_card(self._big_body(3), title="t", job_id="j1")
        elements = card["body"]["elements"]
        assert elements[0]["tag"] == "div"
        assert elements[1]["tag"] == "hr"
        assert len([e for e in elements if e.get("tag") == "markdown"]) == 3
        assert "truncated" not in json.dumps(elements[-1], ensure_ascii=False)

    def test_job_id_footer_still_last_after_marker(self):
        """标记行在 job_id 页脚之前（溯源行恒末位，v2.0.14 契约）."""
        card = build_cron_card(self._big_body(12), title="t", job_id="j1")
        footer = card["body"]["elements"][-1]
        assert footer["tag"] == "div" and "job_id: j1" in footer["text"]["content"]
        marker = card["body"]["elements"][-2]
        assert marker["content"] == "…(truncated)"
