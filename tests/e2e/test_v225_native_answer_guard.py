"""v2.0.25 回归测试 — native 通道 answer 快照误拆面板根治（行语法扫描）.

生产事故（2026-10-27 09:17 CST，gateway37/v2.0.24 在线）：
用户私聊「天气加系统巡检吧」，agent 调 cronjob_manage 创建定时任务后流式
输出确认 answer。finalize 日志铁证：``card finalized answer_len=393
status_lines=12`` —— 折叠面板出现 12 行泄漏内容（✅ 开头的任务表格 +
「内容构成」小节），正文反而正确。

根因（state.db 消息 1366 + hermes 源码三重取证）：
- 最终 answer 以 ``✅ 定时任务已创建`` 开头（emoji!），全文 393 字符；
- native 通道 answer 帧为**累积快照**（stream_consumer_transport._native_push），
  早期快照 ≤300 字符且 emoji 开头 → 旧「emoji 开头 ≤300 即进度」启发式
  （``_is_progress_text``）把整帧判为 progress-only interim 帧；
- ``split_progress_segments`` 逐行拆散 → 表格行/小节行全部并入面板
  （``_absorb_status_lines`` 只增不删）→ 面板永久污染；
- 正文自愈（下一快照 >300 或非 emoji 判否后整帧按 answer 覆盖 answer_text），
  但已并入的面板行无人清理 → 用户看到「answer 在折叠面板里且是裸 MD」。

修复（主流程，无兜底）：``_split_native_composite`` 重写为**行语法扫描**
（``_is_progress_frame_text``）——官方 native 通道的进度帧行只有四类：
💬 思考块（含续行）/ 围栏命令块 / 通知轨前缀行 / 具名工具行（emoji+可解析
工具名，含紧随的缩进命令体行）。answer 的正文行（表格行、非 emoji 小节、
非缩进段落）任一不合法即整帧按 answer 处理。同时剥除官方流式光标尾巴
`` ▉``（DEFAULT_STREAMING_CURSOR，display 装饰，官方 ACK 侧同款剥除语义）。
"""

from __future__ import annotations

import pytest
from hermes_lark_streaming.cardkit.elements import UNIFIED_PANEL_ELEMENT_ID
from hermes_lark_streaming.composer.engine import (
    _is_named_tool_line,
    _is_progress_frame_text,
    _split_native_composite,
    _strip_native_cursor,
)

from .test_v213_native_streaming import _FullClient, _make_composer

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

# 事故真实 answer（state.db 消息 1366，393 字符）——逐字复原
_INCIDENT_ANSWER = (
    "✅ 定时任务已创建\n\n"
    "| 字段 | 内容 |\n"
    "|---|---|\n"
    "| **名称** | afternoon-weather-syscheck-1500 |\n"
    "| **ID** | bca7341f3d5d |\n"
    "| **计划** | 每天 15:00 |\n"
    "| **下次运行** | 2026-10-27 15:00（明天） |\n"
    "| **投递** | 本聊天 |\n"
    "| **工具集** | terminal + web |\n\n"
    "**内容构成**：\n"
    "- 🌤️ 成都成华区傍晚/明日天气（通勤相关）\n"
    "- 🔧 系统巡检（内存/Swap/磁盘/负载/更新包/SSH扫描/服务健康）\n"
    "- ⚠️ 异常阈值告警（Swap>1.2G、磁盘>60%）\n\n"
    "现在你每天有两个定时任务：8:30 新闻早报 + 15:00 天气+巡检。明天下班前见 ☕"
)
# 事故中被误拆进面板的早期快照（表格部分，≤300 字符，emoji 开头）
_INCIDENT_SNAPSHOT = (
    "✅ 定时任务已创建\n\n"
    "| 字段 | 内容 |\n"
    "|---|---|\n"
    "| **名称** | afternoon-weather-syscheck-1500 |\n"
    "| **ID** | bca7341f3d5d |\n"
    "| **计划** | 每天 15:00 |\n"
    "| **下次运行** | 2026-10-27 15:00（明天） |\n"
    "| **投递** | 本聊天 |"
)
assert len(_INCIDENT_SNAPSHOT) <= 300


# ---------------------------------------------------------------------------
# 1. 光标剥除
# ---------------------------------------------------------------------------


class TestCursorStrip:
    def test_strip_official_cursor(self):
        assert _strip_native_cursor("草稿 ▉") == "草稿"

    def test_no_cursor_noop(self):
        assert _strip_native_cursor("草稿") == "草稿"

    def test_body_block_char_not_cursor(self):
        """▉ 出现在行中（非官方光标位置）不动."""
        assert _strip_native_cursor("进度 ▉ 50%") == "进度 ▉ 50%"

    def test_cursor_only(self):
        assert _strip_native_cursor(" ▉").strip() == ""


# ---------------------------------------------------------------------------
# 2. 行语法扫描器（_is_progress_frame_text / _is_named_tool_line）
# ---------------------------------------------------------------------------


class TestNamedToolLine:
    def test_pending_row(self):
        assert _is_named_tool_line("🔍 Searching...") is True

    def test_verb_row(self):
        assert _is_named_tool_line("🗓️ 安排定时任务 · afternoon-weather-syscheck-1500") is True

    def test_head_row(self):
        assert _is_named_tool_line("💻 terminal") is True

    def test_generic_emoji_line_rejected(self):
        """✅ 确认语 / 🌤️ 正文小节：emoji 开头但解析不出工具名 → 不是工具行."""
        assert _is_named_tool_line("✅ 定时任务已创建") is False
        assert _is_named_tool_line("🌤️ 成都成华区傍晚/明日天气（通勤相关）") is False

    def test_non_emoji_two_words_rejected(self):
        """head 模板无 emoji 前置条件："hello world" 曾命中 tool="world"."""
        assert _is_named_tool_line("hello world") is False


class TestProgressFrameText:
    def test_incident_snapshot_is_answer(self):
        """事故帧：✅ 开头的表格快照 → 不是进度帧（根治点）."""
        assert _is_progress_frame_text(_INCIDENT_SNAPSHOT) is False

    def test_incident_short_snapshot_is_answer(self):
        assert _is_progress_frame_text("✅ 定时任务已创建") is False

    def test_emoji_section_line_is_answer(self):
        assert _is_progress_frame_text("🌤️ 成都成华区傍晚/明日天气（通勤相关）") is False

    def test_plain_text_is_answer(self):
        assert _is_progress_frame_text("hello world") is False

    def test_verb_row_frame_is_progress(self):
        assert _is_progress_frame_text("🗓️ 安排定时任务 · afternoon-weather-syscheck-1500") is True

    def test_pending_row_frame_is_progress(self):
        assert _is_progress_frame_text("🔍 Searching...") is True

    def test_thinking_block_is_progress(self):
        assert _is_progress_frame_text("💬 我先查一下天气再创建任务") is True

    def test_thinking_multiline_is_progress(self):
        """思考块多行吸附（v2.0.8 语义）在扫描器下保持合法."""
        text = "💬 先梳理巡检项\n内存、Swap、磁盘、负载都要看\n还有 SSH 失败计数"
        assert _is_progress_frame_text(text) is True

    def test_fenced_block_is_progress(self):
        text = "💻 运行命令\n```\ndf -h\n```"
        assert _is_progress_frame_text(text) is True

    def test_bare_fence_is_progress(self):
        """连续 terminal 调用省略 header 的裸围栏块."""
        assert _is_progress_frame_text("```\nuptime\n```") is True

    def test_notice_line_is_progress(self):
        assert _is_progress_frame_text('⚠️ Subagent failed — "goal" after 60s: timeout') is True

    def test_unfenced_multiline_tool_row_is_progress(self):
        """无围栏多行工具行（2026-10-12 14:37 实录帧）：缩进命令体挂靠工具行."""
        text = '💻 Terminal\n    date "+%Y-%m-%d %H:%M:%S %Z (%A)"'
        assert _is_progress_frame_text(text) is True

    def test_tool_row_then_unindented_text_is_answer(self):
        """工具行后跟非缩进正文行 → 不是进度帧（命令体必须缩进）."""
        text = "💻 Terminal\n正文段落直接跟上来"
        assert _is_progress_frame_text(text) is False

    def test_mixed_rows_and_fence_is_progress(self):
        text = "🔍 Searching...\n```\ncurl example.com\n```\n📖 读取文件 /tmp/x"
        assert _is_progress_frame_text(text) is True


# ---------------------------------------------------------------------------
# 3. 拆分器端到端（两条路径 + 光标）
# ---------------------------------------------------------------------------


class TestSplitNativeCompositeV225:
    def test_incident_snapshot_no_separator(self):
        """事故主路径：无分隔符快照 → 整帧 answer（修复前整帧拆进面板）."""
        a, p = _split_native_composite(_INCIDENT_SNAPSHOT)
        assert a == _INCIDENT_SNAPSHOT
        assert p == []

    def test_incident_snapshot_with_cursor(self):
        """interim tick 形态（answer + 光标）→ 剥光标后仍是 answer."""
        a, p = _split_native_composite(_INCIDENT_SNAPSHOT + " ▉")
        assert a == _INCIDENT_SNAPSHOT
        assert p == []

    def test_full_incident_answer_with_cursor(self):
        """393 字符全量快照 + 光标 → answer，光标不进正文."""
        a, p = _split_native_composite(_INCIDENT_ANSWER + " ▉")
        assert a == _INCIDENT_ANSWER
        assert p == []

    def test_official_placeholder_is_answer(self):
        """工具专用回合的官方占位终帧（stream_consumer.py:869 ``or "✅"``）."""
        a, p = _split_native_composite("✅")
        assert a == "✅"
        assert p == []

    def test_progress_only_frame_still_progress(self):
        """官方 progress-only interim 帧（accumulated 为空）不受影响."""
        a, p = _split_native_composite("🗓️ 安排定时任务 · afternoon-weather-syscheck-1500 ▉")
        assert a == ""
        assert p == ["🗓️ 安排定时任务 · afternoon-weather-syscheck-1500"]

    def test_composite_named_tail_still_splits(self):
        a, p = _split_native_composite("前奏\n\n---\n🔍 Searching the web")
        assert a == "前奏"
        assert p == ["🔍 Searching the web"]

    def test_composite_tail_cursor_glued(self):
        """复合帧行尾光标：剥除后照常拆，光标不再粘进面板行."""
        a, p = _split_native_composite("前奏\n\n---\n🔍 Searching... ▉")
        assert a == "前奏"
        assert p == ["🔍 Searching..."]

    def test_composite_tail_generic_emoji_line_not_split(self):
        """复合尾含通用 emoji 行 → 不拆（内容留正文，方向安全）."""
        a, p = _split_native_composite("答案\n\n---\n🌤️ 成都天气不错")
        assert a == "答案\n\n---\n🌤️ 成都天气不错"
        assert p == []

    def test_answer_internal_hr_with_cursor_not_split(self):
        """正文自带 markdown 分隔线 + 光标（interim 无进度形态）→ 整帧 answer."""
        text = "章节一\n\n---\n章节二 ▉"
        a, p = _split_native_composite(text)
        assert a == "章节一\n\n---\n章节二"
        assert p == []

    def test_answer_containing_named_tool_like_first_line_not_split(self):
        """正文首行恰似工具行、后续为非缩进正文 → 整帧 answer（内容不丢）."""
        text = "💻 Terminal 使用指南\n终端是执行命令的工具"
        a, p = _split_native_composite(text)
        assert a == text
        assert p == []


# ---------------------------------------------------------------------------
# 4. 引擎集成：事故帧序列 → 面板零污染、正文自愈
# ---------------------------------------------------------------------------


class TestEngineIncidentSequence:
    async def test_incident_turn_panel_stays_clean(self):
        """复刻事故帧序：种子 → 短 ✅ 快照（旧版在此泄漏）→ 全量快照 → 定稿.

        v2.0.24 懒渲染语义下，面板全程不创建（status_lines 恒空）；
        正文为最终全量 answer。"""
        client = _FullClient()
        comp = _make_composer(client)
        chat = "oc_v225_incident"
        # 种子帧
        assert await comp.handle_stream_frame(chat, "", finalize=False) is True
        st = comp.active_state(chat)
        assert st is not None
        # 早期短快照（interim tick 形态：带光标）——旧版此帧整帧拆进面板
        assert await comp.handle_stream_frame(
            chat, "✅ 定时任务已创建 ▉", finalize=False) is True
        assert st.status_lines == [], "✅ 开头的短快照不得进面板（事故根治点）"
        # 全量快照（含表格）
        assert await comp.handle_stream_frame(
            chat, _INCIDENT_ANSWER, finalize=False) is True
        assert st.status_lines == []
        assert st.answer_text == _INCIDENT_ANSWER
        # 定稿
        assert await comp.handle_stream_frame(
            chat, _INCIDENT_ANSWER, finalize=True) is True
        assert st.finalized
        assert st.answer_text == _INCIDENT_ANSWER
        assert st.status_lines == []
        # 卡片从未包含面板元素（懒渲染 + 零污染）
        import json as _json

        card = client.cards[st.card_id]["card"]
        assert UNIFIED_PANEL_ELEMENT_ID not in _json.dumps(card, ensure_ascii=False), (
            "纯 answer 回合不得渲染折叠面板（v2.0.24 懒渲染语义）"
        )

    async def test_tool_turn_panel_still_works(self):
        """真工具行帧照常进面板（防误杀回归）."""
        client = _FullClient()
        comp = _make_composer(client)
        chat = "oc_v225_tool"
        assert await comp.handle_stream_frame(
            chat, "🗓️ 安排定时任务 · afternoon-weather-syscheck-1500", finalize=False) is True
        st = comp.active_state(chat)
        assert st is not None
        assert st.status_lines == ["🗓️ 安排定时任务 · afternoon-weather-syscheck-1500"]
        # 后续 answer 快照不污染面板
        assert await comp.handle_stream_frame(
            chat, "✅ 定时任务已创建", finalize=False) is True
        assert st.status_lines == ["🗓️ 安排定时任务 · afternoon-weather-syscheck-1500"]
        assert st.answer_text == "✅ 定时任务已创建"

    async def test_placeholder_only_turn_body_is_check_mark(self):
        """工具专用回合：官方 ✅ 占位终帧 → 正文 ✅（官方语义），面板不渲染."""
        client = _FullClient()
        comp = _make_composer(client)
        chat = "oc_v225_placeholder"
        assert await comp.handle_stream_frame(
            chat, "🔍 Searching... ▉", finalize=False) is True
        st = comp.active_state(chat)
        assert st is not None
        assert st.status_lines == ["🔍 Searching..."]
        assert await comp.handle_stream_frame(chat, "✅", finalize=True) is True
        assert st.finalized
        assert st.answer_text == "✅"
        assert st.status_lines == ["🔍 Searching..."]
