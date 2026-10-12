"""v2.0.1 回归测试 — 生产部署夜修复.

覆盖三个生产缺陷的修复语义：

1. 进度帧上下文升格（adapter._upgrade_plain_to_progress）：
   edit-transport 平台的进度气泡首条是裸 send（无任何 metadata 标记），
   进度启发式文本 → CH_PROGRESS 吸收进状态面板；
   v2.0.4 起不再要求活跃卡片（工具帧常早于答案首帧，升格后由 engine 暂存
   并立即建卡）；非进度文本 → 维持 CH_PLAIN（cron / 后台发送不受影响）。

2. 进度快照合并（engine._on_progress）：
   网关 accumulate 模式每 tick 重发「当前全部进度行」的全量快照 →
   按行拆分 + 集合去重幂等合并；separate 单行增量同样正确；上限 12 行。

3. 进度帧返回契约：
   _on_progress 一律返回 SendResult(success=True) 且 **不带 message_id** ——
   防止网关把卡片 id 记作 progress_msg_id 后用 edit 把工具行覆写进答案区。
"""

from __future__ import annotations

import pytest

from hermes_lark_streaming.plugin.adapter import _upgrade_plain_to_progress
from hermes_lark_streaming.composer.classify import CH_PLAIN, CH_PROGRESS, classify_send


# ---------------------------------------------------------------------------
# 1. 上下文升格
# ---------------------------------------------------------------------------


class TestUpgradePlainToProgress:
    """裸 send → CH_PROGRESS 升格规则（v2.0.4：不再要求活跃卡片）."""

    async def test_upgrades_with_active_card(self, runner):
        """活跃卡片 + emoji 短行 → 升格 CH_PROGRESS。"""
        session = await runner.start_message("先建卡。")
        assert runner.get_state(session) is not None  # 卡片活跃

        upgraded = _upgrade_plain_to_progress(runner.composer, session.chat_id, '🔧 terminal："date"')
        assert upgraded is not None
        assert upgraded.channel == CH_PROGRESS
        assert upgraded.kind == "tool"

    async def test_upgrades_thinking_prefix(self, runner):
        """活跃卡片 + 💬 思考前缀 → 升格且 kind=thinking、剥离前缀。"""
        session = await runner.start_message("建卡后思考。")
        upgraded = _upgrade_plain_to_progress(runner.composer, session.chat_id, "💬 让我想想")
        assert upgraded is not None
        assert upgraded.channel == CH_PROGRESS
        assert upgraded.kind == "thinking"
        assert upgraded.thinking_text == "让我想想"

    async def test_upgrades_without_active_card(self, runner):
        """无活跃卡片 + 进度文本 → 仍升格（v2.0.4：由 engine 暂存并立即建卡）。"""
        chat = runner.chat_id + "-nocard"
        upgraded = _upgrade_plain_to_progress(runner.composer, chat, '🔧 terminal："date"')
        assert upgraded is not None
        assert upgraded.channel == CH_PROGRESS
        assert upgraded.kind == "tool"

    async def test_no_upgrade_for_long_text(self, runner):
        """长文本（>300 字符）不是进度行 → 不升格（cron 长答案保护）。"""
        session = await runner.start_message("建卡。")
        long_text = "✅ " + "x" * 400
        assert _upgrade_plain_to_progress(runner.composer, session.chat_id, long_text) is None

    async def test_no_upgrade_for_plain_prose(self, runner):
        """普通无 emoji 短文本 → 不升格。"""
        session = await runner.start_message("建卡。")
        assert _upgrade_plain_to_progress(runner.composer, session.chat_id, "这是一句普通的话") is None

    async def test_finalized_card_still_upgrades(self, runner):
        """已定稿卡片 + 进度文本 → 仍升格（v2.0.4：下一个 _on_progress 暂存/建新卡；
        cron / 后台投递带 notify=True → CH_FINAL 不受影响）。"""
        session = await runner.start_message("建卡后定稿。")
        await runner.complete(session, answer="完")
        upgraded = _upgrade_plain_to_progress(runner.composer, session.chat_id, '🔧 terminal："ls"')
        assert upgraded is not None
        assert upgraded.channel == CH_PROGRESS

    async def test_empty_content_no_upgrade(self, runner):
        """空内容 → 不升格、不抛异常。"""
        assert _upgrade_plain_to_progress(runner.composer, runner.chat_id, "") is None

    def test_never_raises_on_broken_composer(self):
        """composer 异常对象也不阻断（v2.0.4：升格不再查询 composer 状态）。"""
        class _Boom:
            def active_state(self, _chat: str):
                raise RuntimeError("boom")

        # 不抛异常即通过（v2.0.4 不再访问 composer；返回升格结果）
        result = _upgrade_plain_to_progress(_Boom(), "chat", '🔧 t："x"')
        assert result is None or result.channel == CH_PROGRESS


# ---------------------------------------------------------------------------
# 2+3. 进度快照合并 + 返回契约（经 composer.handle_send 全链路）
# ---------------------------------------------------------------------------


class TestProgressSnapshotMerge:
    """accumulate 全量快照 / separate 单行增量 → 幂等合并."""

    async def test_accumulate_snapshot_idempotent_merge(self, runner):
        """全量快照重放：['🔧 a', '🔧 a\\n🔧 b', '🔧 a\\n🔧 b\\n🔧 c'] → 3 行不膨胀。"""
        session = await runner.start_message("建卡。")

        r1 = await runner.send_raw('🔧 a', {"_interim_send": True})
        st = runner.get_state(session)
        assert st.status_lines == ["🔧 a"]
        assert r1.success and r1.message_id is None  # 契约：不带 message_id

        r2 = await runner.send_raw("🔧 a\n🔧 b", {"_interim_send": True})
        st = runner.get_state(session)
        assert st.status_lines == ["🔧 a", "🔧 b"], f"快照合并错误: {st.status_lines!r}"
        assert r2.message_id is None

        r3 = await runner.send_raw("🔧 a\n🔧 b\n🔧 c", {"_interim_send": True})
        st = runner.get_state(session)
        assert st.status_lines == ["🔧 a", "🔧 b", "🔧 c"]
        assert r3.message_id is None

    async def test_separate_single_lines_append(self, runner):
        """separate 模式单行增量逐条追加，重复行去重。"""
        session = await runner.start_message("建卡。")
        await runner.feed_tool_update(session, "terminal", "date")
        await runner.feed_tool_update(session, "terminal", "ls")
        st = runner.get_state(session)
        assert len(st.status_lines) == 2

        # 重复行（同文本重发）不重复入列
        await runner.feed_tool_update(session, "terminal", "date")
        st = runner.get_state(session)
        assert len(st.status_lines) == 2

    async def test_thinking_and_tool_mixed_snapshot(self, runner):
        """思考行 + 工具行混合快照正确合并。"""
        session = await runner.start_message("建卡。")
        await runner.send_raw("💬 思考第一段\n🔧 a：\"x\"", {"_interim_send": True})
        st = runner.get_state(session)
        assert st.status_lines == ["💬 思考第一段", '🔧 a："x"']

    async def test_status_lines_capped_at_12(self, runner):
        """快照超过 12 行 → 保留最近 12 行。"""
        session = await runner.start_message("建卡。")
        snapshot = "\n".join(f"🔧 tool{i}" for i in range(20))
        await runner.send_raw(snapshot, {"_interim_send": True})
        st = runner.get_state(session)
        assert len(st.status_lines) == 12
        assert st.status_lines[-1] == "🔧 tool19"

    async def test_progress_before_card_creates_card_and_merges(self, runner):
        """建卡前裸进度帧（经适配器升格）→ 暂存并立即建占位卡（v2.0.4 s70 回流）.

        生产路径：adapter.send → classify_send(CH_PLAIN) → _upgrade_plain_to_progress
        → handle_send(CH_PROGRESS) → 暂存 pending + 立即建卡（卡片先现，V1 体验）。
        """
        chat = runner.chat_id + "-pending"
        cls = classify_send('🔧 early："setup"', None)  # 裸 send → CH_PLAIN
        assert cls.channel == CH_PLAIN
        upgraded = _upgrade_plain_to_progress(runner.composer, chat, '🔧 early："setup"')
        # v2.0.4：不再要求活跃卡片——工具帧常早于答案首帧
        assert upgraded is not None and upgraded.channel == CH_PROGRESS
        r = await runner.send_raw('🔧 early："setup"', {"_interim_send": True}, chat_id=chat)
        assert r.success and r.message_id is None  # 不回传 message_id（防网关 anchor 复写）
        st = runner.composer._states.get(chat)
        assert st is not None and st.card_id, "进度帧应立即建占位卡"
        assert st.pending_status == [], "已并入首卡面板"
        assert '🔧 early："setup"' in st.status_lines


# ---------------------------------------------------------------------------
# 4. 分类器回归（标记语义不受升格影响）
# ---------------------------------------------------------------------------


class TestClassifyUntouched:
    """原有 metadata 标记语义不变（升格只作用于无标记帧）."""

    def test_expect_edits_still_stream(self):
        cls = classify_send("答案首帧", {"expect_edits": True})
        assert cls.channel == "stream"

    def test_interim_long_text_still_interim(self):
        long_text = "x" * 400
        cls = classify_send(long_text, {"_interim_send": True})
        assert cls.channel == "interim"

    def test_notify_still_final(self):
        cls = classify_send("定稿", {"notify": True})
        assert cls.channel == "final"

    def test_unmarked_long_text_still_plain(self):
        cls = classify_send("x" * 400, None)
        assert cls.channel == CH_PLAIN
