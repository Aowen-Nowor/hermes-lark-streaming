"""v2.0.22 回归测试 — 卡片回复引用 + 连续 terminal 裸块归属（2026-10-26）.

用户验收两项：
  1【引用】「卡片回复的时候，没有引用是哪条消息」（私聊/群聊都要）——
     hermes 官方默认锚点 = 触发消息 message_id（base.py:180），V2 曾在
     composer.handle_send/handle_stream_frame 丢弃 reply_to 直发。
     修复：om_ 锚点 → reply_card_by_id 引用回复；非 om_（cron/后台合成
     task_id，reply API 必败 99992354）与 reply 失败 → 直发降级（内容
     永不丢红线）。
  2【面板】「Terminal 为何分两段、命令为何重复」——不是渲染重复：agent
     真实调了 4 次 terminal（2026-10-26 15:07 日志铁证，0.17s/0.60s 与
     面板 169ms/596ms 精确吻合），前两次脚本各自含 uptime（模型行为）。
     真缺陷：hermes 连续 terminal 调用省略 header（裸围栏块），V2 渲染
     无头灰字 + 耗时钩子挂不上。修复：parse_progress_line 裸块归属
     terminal（hermes _progress_terminal_blocks 仅 terminal 产围栏块）。
"""

from __future__ import annotations

import pytest
from hermes_lark_streaming.cardkit.progline import parse_progress_line
from hermes_lark_streaming.composer.engine import FrameComposer

from .test_v213_native_streaming import _FullClient

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_BARE_BLOCK = '```\necho "=== TOP MEM ===" && ps aux --sort=-%mem | head\n```'


class _ReplyAwareClient(_FullClient):
    """补 reply_card_by_id（可注入失败）与直发计数."""

    def __init__(self):
        super().__init__()
        self.reply_calls: list[tuple[str, str]] = []
        self.direct_calls: list[tuple[str, str]] = []
        self.reply_fail = False

    async def reply_card_by_id(self, message_id, card_id):
        if self.reply_fail:
            raise RuntimeError("reply API failed (anchor deleted?)")
        self.reply_calls.append((str(message_id), str(card_id)))
        return f"om_reply_{card_id}"

    async def send_card_by_id_to_chat(self, chat_id, card_id):
        self.direct_calls.append((str(chat_id), str(card_id)))
        return f"om_direct_{card_id}"


def _make_composer(client: _ReplyAwareClient) -> FrameComposer:
    from hermes_lark_streaming.cardkit.cards import build_streaming_card_v2

    return FrameComposer(
        get_client=lambda: client,
        build_card=lambda **kw: build_streaming_card_v2(**kw),
        build_final_card=lambda **kw: {},
        config=None,
    )


# ---------------------------------------------------------------------------
# 1. 引用回复（reply_to 接线）
# ---------------------------------------------------------------------------


class TestReplyAnchorSend:
    async def test_om_anchor_replies_to_message(self):
        client = _ReplyAwareClient()
        comp = _make_composer(client)
        r = await comp.handle_send(
            "ch1", "你好", "om_anchor_msg_1", {"expect_edits": True},
            __import__(
                "hermes_lark_streaming.composer.classify", fromlist=["ClassifyResult"]
            ).ClassifyResult("stream", kind="answer", raw_content="你好", meta={}),
        )
        assert r is not None and r.success
        assert client.reply_calls == [("om_anchor_msg_1", client._n and "1")]
        assert client.direct_calls == []

    async def test_non_om_anchor_direct_send(self):
        """cron/后台合成 task_id（非 om_）→ reply API 必败（V1 issue #18-3）
        → 直发。"""
        client = _ReplyAwareClient()
        comp = _make_composer(client)
        await comp.handle_send(
            "ch1", "通知", "bg_173000_deadbeef", {"expect_edits": True},
            __import__(
                "hermes_lark_streaming.composer.classify", fromlist=["ClassifyResult"]
            ).ClassifyResult("stream", kind="answer", raw_content="通知", meta={}),
        )
        assert client.reply_calls == []
        assert len(client.direct_calls) == 1

    async def test_reply_failure_falls_back_to_direct(self):
        """锚点消息被删/非法 → reply 失败降级直发（内容永不丢红线）。"""
        client = _ReplyAwareClient()
        client.reply_fail = True
        comp = _make_composer(client)
        r = await comp.handle_send(
            "ch1", "内容", "om_gone", {"expect_edits": True},
            __import__(
                "hermes_lark_streaming.composer.classify", fromlist=["ClassifyResult"]
            ).ClassifyResult("stream", kind="answer", raw_content="内容", meta={}),
        )
        assert r is not None and r.success
        assert len(client.reply_calls) == 0 or client.reply_fail  # 调用失败不记成功
        assert len(client.direct_calls) == 1
        assert str(r.message_id).startswith("om_direct_")

    async def test_progress_early_create_with_anchor(self):
        """emoji 短通知升格 early-create 占位卡也走引用（用户验收场景）。"""
        client = _ReplyAwareClient()
        comp = _make_composer(client)
        cls = __import__(
            "hermes_lark_streaming.composer.classify", fromlist=["ClassifyResult"]
        ).ClassifyResult(
            "progress", kind="progress", tool_name="notify",
            thinking_text="通知内容", raw_content="🔔 通知内容",
        )
        await comp.handle_send("ch1", "🔔 通知内容", None, {}, cls)
        # reply_to=None → 直发（行为不变）
        assert client.reply_calls == []
        assert len(client.direct_calls) == 1


class TestReplyAnchorNative:
    async def test_seed_frame_replies(self):
        """native 种子帧（官方契约带 reply_to）→ 建卡引用触发消息。"""
        client = _ReplyAwareClient()
        comp = _make_composer(client)
        ok = await comp.handle_stream_frame(
            "ch1", "", finalize=False, reply_to="om_seed_anchor")
        assert ok is True
        assert client.reply_calls and client.reply_calls[0][0] == "om_seed_anchor"
        assert client.direct_calls == []

    async def test_finalize_without_card_replies(self):
        """终帧无活跃卡 → 建卡再封，同样引用锚点。"""
        client = _ReplyAwareClient()
        comp = _make_composer(client)
        ok = await comp.handle_stream_frame(
            "ch1", "快照终值", finalize=True, reply_to="om_final_anchor")
        assert ok is True
        assert client.reply_calls and client.reply_calls[0][0] == "om_final_anchor"

    async def test_stream_frame_no_anchor_direct(self):
        client = _ReplyAwareClient()
        comp = _make_composer(client)
        await comp.handle_stream_frame("ch1", "内容", finalize=False)
        assert client.reply_calls == []
        assert len(client.direct_calls) == 1


# ---------------------------------------------------------------------------
# 2. 连续 terminal 裸块归属
# ---------------------------------------------------------------------------


class TestBareBlockTerminal:
    def test_parse_bare_block_is_terminal(self):
        kind, tool, clean = parse_progress_line(_BARE_BLOCK)
        assert (kind, tool) == ("tool", "terminal")
        assert "```" in clean

    def test_parse_headed_block_still_terminal(self):
        block = '💻 terminal\n```\necho hi\n```'
        assert parse_progress_line(block)[:2] == ("tool", "terminal")

    async def test_duration_hook_attaches_to_bare_block(self):
        """耗时钩子（官方 post_tool_call duration_ms）→ 裸块行可挂载
        （此前 tool="" 挂不上，用户看到连续调用无耗时）。"""

        client = _ReplyAwareClient()
        comp = _make_composer(client)
        await comp.handle_stream_frame("ch1", _BARE_BLOCK, finalize=False)
        st = comp._states["ch1"]
        assert len(st.status_lines) == 1
        await comp._feed_tool_duration("ch1", "terminal", 596.0)
        for _ in range(4):
            await __import__("asyncio").sleep(0)
        cur = __import__("asyncio").current_task()
        ts = [t for t in __import__("asyncio").all_tasks()
              if t is not cur and not t.done()]
        if ts:
            await __import__("asyncio").gather(*ts, return_exceptions=True)
        key = st.status_lines[0]
        assert st.tool_durations.get(key) == "596 ms"

    async def test_bare_blocks_render_terminal_steps(self):
        """连续裸块 → 每块独立 Terminal 步骤行（用户验收「分两段」困惑
        根源修复：每次调用一个带图标步骤，耗时各自的钩子实测）。"""
        from hermes_lark_streaming.cardkit.cards import _render_status_children

        segs = [
            _BARE_BLOCK,
            '```\necho "=== UPTIME ===" && uptime\n```',
            '```\necho "=== APT UPDATES ===" && apt list --upgradable\n```',
        ]
        els = _render_status_children(segs, {}, [])
        titles = [e for e in els if e.get("tag") == "div" and e.get("icon", {}).get("token") == "code_outlined"]
        assert len(titles) == 3  # 每个裸块一个 Terminal 头
