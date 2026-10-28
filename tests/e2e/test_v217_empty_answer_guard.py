"""v2.0.17 回归测试 — stream_element 空 content 守卫（99992402 多卡片事故根治）.

生产事故（2026-10-14 14:37 CST，gateway30/v2.0.16 在线）：
「几点了」回合拆 3 张卡。时序铁证（agent.log/gateway30.log）：
  14:37:12.184 卡 1 创建（种子，answer 空）
  14:37:20.243 API call #1 完成（思考💬+terminal 工具，answer 通道全程空）
  14:37:21.646 cardkit_stream_element code=99992402 field validation failed
             → composer degrade → active_state 返回 None（degraded）
  14:37:22.692 卡 2 创建（status_lines=1，Terminal 行暂存并入）
  14:37:22.840 99992402 再次 → 再 degrade
  14:37:22.841 stream_consumer「Native fallback: finalized stream」
  14:37:24.180 卡 3 创建（fallback native text 通道）→ 正常定稿 ✓

根因（s152 真飞书探针 2026-10-14 二分定案）：飞书 stream_element 的
content 必填非空——content="" → 99992402 field validation failed；未闭合
markdown / 未闭合围栏 / markdown 分隔线 / 截图前导文本等其余 9 种形态全部
code=0。native 复合帧「answer 空 + 进度行非空」是工具边界后的固有低频形态
（API call #2 流首帧时模型 content 尚未输出），``_stream_answer`` 自
v2.0.13 引入起无空守卫，空串直发 → degrade → active_state=None → 下一帧
连锁建卡（v2.0.11 家族同症状新根因——v2.0.11 是并发竞态，本次是 API 契约）。

潜伏期说明：v2.0.13/14 因影子副本事故未上线（Task 20），native streaming
真实生产运行 = v2.0.15（10-13）起一天余，低频形态未暴露属正常样本量。

修复：``_stream_answer`` 与 ``finalize_card`` 第一步（answer 终值 patch）
对空/空白 content 跳过调用——cumulative 快照语义下空 = 无新内容，API 经济
性同向（v2.0.0「无变化不触发 cardkit_update」同语义家族）。
"""

from __future__ import annotations

import pytest
from hermes_lark_streaming.cardkit.cards import build_streaming_card_v2
from hermes_lark_streaming.composer.engine import FrameComposer

from .test_v213_native_streaming import _FullClient, _make_composer, _SEP

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


class _RecordingClient(_FullClient):
    """记录 stream_element 调用（element_id, content）序列的 fake."""

    def __init__(self):
        super().__init__()
        self.stream_calls: list[tuple[str, str]] = []

    async def cardkit_stream_element(self, card_id, element_id, content, sequence=0):
        self.stream_calls.append((str(element_id), str(content)))
        await super().cardkit_stream_element(card_id, element_id, content, sequence=sequence)


# ---------------------------------------------------------------------------
# 1. _stream_answer 空守卫（修复点 1）
# ---------------------------------------------------------------------------


class TestStreamAnswerGuard:
    async def test_empty_answer_skips_api(self):
        """answer="" → 不发 stream_element（修复前：直发 → 99992402）."""
        client = _RecordingClient()
        comp = _make_composer(client)
        chat = "oc_guard_empty"
        await comp.handle_stream_frame(chat, "", finalize=False)  # 种子（骨架卡）
        st = comp.active_state(chat)
        st.answer_text = ""
        await comp._stream_answer(st)
        assert client.stream_calls == [], "空 content 不得触发 stream_element"

    async def test_whitespace_answer_skips_api(self):
        """纯空白 answer（" \\n"）→ strip 后为空 → 同样跳过."""
        client = _RecordingClient()
        comp = _make_composer(client)
        chat = "oc_guard_ws"
        await comp.handle_stream_frame(chat, "", finalize=False)
        st = comp.active_state(chat)
        st.answer_text = "  \n  "
        await comp._stream_answer(st)
        assert client.stream_calls == []

    async def test_nonempty_answer_calls_api(self):
        """非空 answer → 正常调用（守卫不误伤流式主路径）."""
        client = _RecordingClient()
        comp = _make_composer(client)
        chat = "oc_guard_ok"
        await comp.handle_stream_frame(chat, "", finalize=False)
        st = comp.active_state(chat)
        st.answer_text = "**14:37**（2026年10月14日 周三）"
        await comp._stream_answer(st)
        assert len(client.stream_calls) == 1
        assert client.stream_calls[0][1] == "**14:37**（2026年10月14日 周三）"


# ---------------------------------------------------------------------------
# 2. 生产事故场景复现：空 answer + 进度行复合帧 → 单卡不 degrade
# ---------------------------------------------------------------------------


class TestEmptyAnswerCompositeFrame:
    async def test_tool_boundary_empty_answer_single_card(self):
        """事故帧形态：answer 空 → 官方 join 只发纯 progress（无分隔符，
        stream_consumer.py:251-255 `if p` 空段过滤）→ 不 degrade、单卡、行进面板."""
        client = _RecordingClient()
        comp = _make_composer(client)
        chat = "oc_incident"
        assert await comp.handle_stream_frame(chat, "", finalize=False) is True
        # 事故 14:37:21.646 帧：API#2 流首帧，answer 尚空，进度已有 Terminal 行
        frame = "💻 Terminal\n    date \"+%Y-%m-%d %H:%M:%S %Z (%A)\""
        assert await comp.handle_stream_frame(chat, frame, finalize=False) is True
        st = comp.active_state(chat)
        assert st is not None, "不得 degrade（修复前此处为 None）"
        assert not st.degraded
        assert st.answer_text == ""
        assert any("Terminal" in ln for ln in st.status_lines), "工具行进面板"
        assert client._n == 1, "单卡"

    async def test_double_empty_answer_frames_still_single_card(self):
        """事故二连（14:37:21.646 + 14:37:22.840）：连续两个纯 progress 帧 → 仍单卡."""
        client = _RecordingClient()
        comp = _make_composer(client)
        chat = "oc_incident_x2"
        await comp.handle_stream_frame(chat, "", finalize=False)
        await comp.handle_stream_frame(chat, "💻 Terminal\n    date", finalize=False)
        await comp.handle_stream_frame(chat, "💻 Terminal (153 ms)\n    date", finalize=False)
        st = comp.active_state(chat)
        assert st is not None and not st.degraded
        assert client._n == 1, "修复前此场景连锁建卡 2（14:37:22.692）"

    async def test_empty_frame_then_real_answer_updates_normally(self):
        """空帧后真 answer 帧 → answer 正常流式（内容永不丢红线）."""
        client = _RecordingClient()
        comp = _make_composer(client)
        chat = "oc_incident_recovery"
        await comp.handle_stream_frame(chat, "", finalize=False)
        await comp.handle_stream_frame(chat, "💬 思考中", finalize=False)
        await comp.handle_stream_frame(chat, "**14:37**（周三）", finalize=False)
        st = comp.active_state(chat)
        assert st is not None
        assert st.answer_text == "**14:37**（周三）"
        assert client._n == 1
        assert any(content == "**14:37**（周三）" for _, content in client.stream_calls)

    async def test_all_answer_empty_turn_finalize_ok(self):
        """纯工具回合（answer 全程空）→ finalize 走通、finalized=True."""
        client = _RecordingClient()
        comp = _make_composer(client)
        chat = "oc_tool_only"
        await comp.handle_stream_frame(chat, "", finalize=False)
        await comp.handle_stream_frame(chat, "💻 Terminal\n    date", finalize=False)
        ok = await comp.handle_stream_frame(chat, "", finalize=True)
        assert ok is True
        st = comp._states[chat]
        assert st.finalized


# ---------------------------------------------------------------------------
# 3. finalize 第一步空守卫（修复点 2）
# ---------------------------------------------------------------------------


class TestFinalizeEmptyAnswer:
    async def test_finalize_empty_answer_skips_answer_patch(self):
        """answer 空 → finalize:answer 不调 stream_element，页脚照常."""
        client = _RecordingClient()
        comp = _make_composer(client)
        chat = "oc_fin_empty"
        await comp.handle_stream_frame(chat, "", finalize=False)
        st = comp.active_state(chat)
        st.answer_text = ""
        await comp._finalize(st, "")
        assert st.finalized
        assert client.stream_calls == [], "定稿空 answer 不得触发 stream_element"
        assert client.close_calls == 1, "关流式照常"
        assert client.batch_actions, "页脚 seal 照常"

    async def test_finalize_with_answer_patches_final_value(self):
        """answer 非空 → 终值 patch 照常（不误伤既有定稿契约）."""
        client = _RecordingClient()
        comp = _make_composer(client)
        chat = "oc_fin_val"
        await comp.handle_stream_frame(chat, "", finalize=False)
        st = comp.active_state(chat)
        st.answer_text = "最终答案"
        await comp._finalize(st, "最终答案")
        assert st.finalized
        assert any(
            content == "最终答案" for _, content in client.stream_calls
        ), "定稿终值 patch 必须照常"

    async def test_finalize_whitespace_answer_skips(self):
        client = _RecordingClient()
        comp = _make_composer(client)
        chat = "oc_fin_ws"
        await comp.handle_stream_frame(chat, "", finalize=False)
        st = comp.active_state(chat)
        st.answer_text = "\n "
        await comp._finalize(st, "\n ")
        assert st.finalized
        assert client.stream_calls == []
