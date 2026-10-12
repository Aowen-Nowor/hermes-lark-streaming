"""v2.0.5 回归测试 — 状态面板 V1(1.8.1) 视觉对齐 + 进度行合并语义升级.

用户验收背景（2026-10-10 反馈）：正式对话的面板「还是不是插件的效果」。
对照 V1(1.8.1) 源码与效果截图（assets/screenshots/img2.png），v2.0.5 补齐：

1. 面板标题三段式：``agent loop · N 轮 · M 个工具 · X.Xs``
   （V1 build_panel_header 同款结构；elapsed 为 composer 本地实测，非伪造）
2. 工具行人性化：terminal→Terminal（V1 截图原样）、grep→Search text（描述符标题）；
   pending 行（尾部 ``...``）橙色 running，已执行行绿色 success（V1 1.8.1 只靠
   颜色区分状态，无 Succeeded 文字——见 V1 CHANGELOG v1.7.0 R 条目）
3. 💬 思考行分组为推理轮次：robot-add_outlined 图标 + 绿色粗体「第 N 轮」
   + 缩进灰字正文（V1 _build_reasoning_round_title 同款）
4. 合并语义：pending→preview 原地取代（同一次调用一个步骤行）；思考累积
   原地替换（不膨胀、不计新轮）；同工具再次调用 = 新步骤追加
5. 建卡前暂存行过同一套合并（首卡面板不出现 pending+preview 双行）
"""

from __future__ import annotations

import time

import pytest
from hermes_lark_streaming.cardkit.cards import _render_status_children
from hermes_lark_streaming.cardkit.elements import UNIFIED_PANEL_ELEMENT_ID
from hermes_lark_streaming.cardkit.progline import parse_progress_line, preview_text
from hermes_lark_streaming.cardkit.tool_icons import (
    humanize_tool_name,
    resolve_tool_icon,
    tool_display_title,
)
from hermes_lark_streaming.composer.engine import (
    CardTurnState,
    _merge_progress_lines,
    _panel_title,
    _turn_elapsed_ms,
)

# ---------------------------------------------------------------------------
# 1. progline 解析（叶子模块下沉后的回归）
# ---------------------------------------------------------------------------


class TestProgline:
    def test_parse_thinking(self):
        kind, tool, clean = parse_progress_line("💬 正在检查内核")
        assert (kind, tool, clean) == ("thinking", "", "正在检查内核")

    def test_parse_preview_with_colon(self):
        kind, tool, _ = parse_progress_line("🖥️ terminal：\"uname -r\"")
        assert kind == "tool" and tool == "terminal"

    def test_parse_pending(self):
        kind, tool, _ = parse_progress_line("🖥️ terminal...")
        assert kind == "tool" and tool == "terminal"

    def test_preview_text_extracts_command(self):
        # v2.0.6：包裹引号在 preview_text 内剥除（渲染层不再二次剥壳，
        # 不会误伤命令自身的尾部引号），legacy 引号预览返回干净命令
        assert preview_text("🖥️ terminal：\"uname -r\"") == "uname -r"
        assert preview_text("🖥️ terminal...") == ""
        assert preview_text("普通文本") == ""

    def test_never_raises(self):
        assert parse_progress_line("")[0] == "unknown"
        assert parse_progress_line(None if False else "x")


# ---------------------------------------------------------------------------
# 2. 工具标题/图标（V1 state/tooluse.py 移植面）
# ---------------------------------------------------------------------------


class TestToolDisplay:
    def test_terminal_humanized_like_v1_screenshot(self):
        assert tool_display_title("terminal") == "Terminal"

    def test_descriptor_titles(self):
        assert tool_display_title("grep") == "Search text"
        assert tool_display_title("web_search") == "Search"
        assert tool_display_title("read") == "Read"
        assert tool_display_title("bash") == "Run command"

    def test_humanize_fallback(self):
        assert tool_display_title("weird_tool") == "Weird tool"
        assert humanize_tool_name("") == "Tool"
        assert humanize_tool_name("web-fetch") == "Web fetch"

    def test_icons(self):
        assert resolve_tool_icon("terminal") == "setting_outlined"
        assert resolve_tool_icon("grep") == "doc-search_outlined"
        assert resolve_tool_icon("read_file") == "file-link-text_outlined"
        assert resolve_tool_icon("unknown_thing") == "code_outlined"  # v2.0.10 官方枚举默认


# ---------------------------------------------------------------------------
# 3. 面板标题三段式（V1 build_panel_header 结构）
# ---------------------------------------------------------------------------


class TestPanelTitle:
    def test_empty(self):
        assert _panel_title([]) == "agent loop"

    def test_tools_only(self):
        assert _panel_title(['🔧 a："x"']) == "agent loop · 1 个工具"

    def test_round_and_tool(self):
        title = _panel_title(["💬 A", '🔧 a："x"'])
        assert title == "agent loop · 1 轮 · 1 个工具"

    def test_two_rounds(self):
        title = _panel_title(["💬 A", '🔧 a："x"', "💬 B"])
        assert title == "agent loop · 2 轮 · 1 个工具"

    def test_elapsed_segment(self):
        assert _panel_title(['🔧 a："x"'], elapsed_ms=4100) == "agent loop · 1 个工具 · 4.1s"
        assert _panel_title(["💬 A", '🔧 a："x"'], elapsed_ms=65_000) == "agent loop · 1 轮 · 1 个工具 · 1m 5s"

    def test_tiny_elapsed_omitted(self):
        # <0.1s 不显示（避免 "0.0s" 噪音）
        assert _panel_title(['🔧 a："x"'], elapsed_ms=50) == "agent loop · 1 个工具"

    def test_pending_counts_once_after_merge(self):
        # 合并后 pending 已被取代 → 只计一次；未合并才可能双计
        assert _panel_title(["🖥️ terminal..."]) == "agent loop · 1 个工具"


# ---------------------------------------------------------------------------
# 4. 面板子元素渲染（V1 观感断言）
# ---------------------------------------------------------------------------


def _texts(children: list[dict]) -> list[str]:
    out = []
    for el in children:
        text = el.get("text")
        out.append(text.get("content", "") if isinstance(text, dict) else str(el.get("content", "")))
    return out


class TestRenderStatusChildren:
    def test_thinking_becomes_round(self):
        kids = _render_status_children(["💬 正在检查内核"])
        assert len(kids) == 2
        title_el, detail_el = kids
        assert title_el["icon"]["token"] == "robot-add_outlined"
        assert "第 1 轮" in _texts([title_el])[0]
        assert "green" in _texts([title_el])[0]  # 定稿轮绿色（V1 finalized 语义）
        assert _texts([detail_el])[0] == "正在检查内核"
        assert detail_el["text"]["text_color"] == "grey"
        assert detail_el["margin"].endswith("22px")  # V1 同款缩进

    def test_consecutive_thinking_single_round(self):
        kids = _render_status_children(["💬 第一句", "💬 第二句"])
        round_titles = [el for el in kids if el.get("icon", {}).get("token") == "robot-add_outlined"]
        assert len(round_titles) == 1

    def test_two_rounds_split_by_tool(self):
        kids = _render_status_children(["💬 A", '🔧 a："x"', "💬 B"])
        rounds = [t for t in _texts(kids) if "第" in t and "轮" in t]
        assert len(rounds) == 2
        assert "第 1 轮" in rounds[0] and "第 2 轮" in rounds[1]

    def test_tool_row_green_with_humanized_title_and_preview(self):
        kids = _render_status_children(["🖥️ terminal：\"uname -r\""])
        assert len(kids) == 2
        title_el, detail_el = kids
        assert title_el["icon"]["token"] == "setting_outlined"
        content = _texts([title_el])[0]
        assert "**Terminal**" in content and "green" in content
        assert _texts([detail_el])[0] == "uname -r"  # 引号已剥壳
        assert detail_el["text"]["text_color"] == "grey"

    def test_pending_row_orange_no_detail(self):
        kids = _render_status_children(["🖥️ terminal..."])
        assert len(kids) == 1
        content = _texts(kids)[0]
        assert "**Terminal**" in content and "orange-300" in content

    def test_unknown_line_falls_back_to_markdown(self):
        kids = _render_status_children(["这是一条普通播报"])
        assert kids == [{"tag": "markdown", "content": "这是一条普通播报"}]

    def test_empty_lines_blank_markdown(self):
        kids = _render_status_children([])
        assert kids == [{"tag": "markdown", "content": " "}]


# ---------------------------------------------------------------------------
# 5. 合并语义（pending→preview 取代 / 思考累积 / 幂等）
# ---------------------------------------------------------------------------


class TestMergeProgressLines:
    def test_idempotent_no_change_returns_none(self):
        assert _merge_progress_lines(['🔧 a："x"'], ['🔧 a："x"']) is None
        assert _merge_progress_lines([], []) is None

    def test_pending_replaced_by_preview_in_place(self):
        merged = _merge_progress_lines(["🖥️ terminal..."], ['🖥️ terminal："ls"'])
        assert merged == ['🖥️ terminal："ls"']

    def test_same_tool_new_call_appends(self):
        # 同一批快照里的 pending→preview = 同一次调用 → 原地取代；
        # 已执行行（"a"）不受影响 → 新调用追加在后。
        merged = _merge_progress_lines(['🔧 t："a"'], ["🔧 t...", '🔧 t："b"'])
        assert merged == ['🔧 t："a"', '🔧 t："b"']

    def test_thinking_accumulation_replaced_in_place(self):
        merged = _merge_progress_lines(["💬 正在检查"], ["💬 正在检查内核版本"])
        assert merged == ["💬 正在检查内核版本"]

    def test_thinking_new_round_appends(self):
        existing = ["💬 第一轮思考", '🔧 t："x"']
        merged = _merge_progress_lines(existing, ["💬 第二轮思考"])
        assert merged == ["💬 第一轮思考", '🔧 t："x"', "💬 第二轮思考"]

    def test_mixed_snapshot(self):
        existing = ["💬 A"]
        incoming = ["💬 A", "🖥️ terminal...", '🖥️ terminal："ls"']
        merged = _merge_progress_lines(existing, incoming)
        assert merged == ["💬 A", '🖥️ terminal："ls"']


# ---------------------------------------------------------------------------
# 6. turn 计时（标题 elapsed 段数据源）
# ---------------------------------------------------------------------------


class TestTurnElapsed:
    def test_none_start_returns_zero(self):
        st = CardTurnState(chat_id="c")
        assert _turn_elapsed_ms(st) == 0.0

    def test_measures_since_start(self):
        st = CardTurnState(chat_id="c", turn_started_at=time.monotonic() - 2.0)
        elapsed = _turn_elapsed_ms(st)
        assert 1900.0 <= elapsed <= 5000.0


# ---------------------------------------------------------------------------
# 7. 全链路：暂存行合并进首卡 + 标题带轮次/工具/耗时（mock 客户端）
# ---------------------------------------------------------------------------


class TestFirstCardCarriesMergedPanel:
    async def test_pending_merged_and_title_v1_format(self, runner):
        pend = runner.pending_session()
        await runner.feed_reasoning(pend, "卡片还没建，先暂存这条思考")
        await runner.feed_tool_update(pend, tool_name="terminal", detail="pytest tests/ -q")
        # 同工具 pending → preview 的状态推进也发生在暂存期
        await runner.send_raw('🖥️ browser...', {"_interim_send": True}, chat_id=pend.chat_id)
        await runner.send_raw('🖥️ browser："open feishu.cn"', {"_interim_send": True}, chat_id=pend.chat_id)

        session = await runner.start_message("首帧建卡")
        runner.assert_card_created(session)
        st = runner.get_state(session)

        # 💬 + terminal preview + browser preview（pending 已被取代，无残留）
        assert st.pending_status == []
        assert len(st.status_lines) == 3, f"status_lines={st.status_lines!r}"
        assert not any(ln.endswith("...") for ln in st.status_lines)

        if not runner.is_real_mode:
            card = runner.get_card(session)
            panel = card.elements.get(UNIFIED_PANEL_ELEMENT_ID, {})
            header_title = (panel.get("header", {}).get("title", {}) or {}).get("content", "")
            # V1 三段式：轮次 + 工具数（+ 实测耗时，mock 环境建卡极快可能 <0.1s 省略）
            assert header_title == "agent loop · 1 轮 · 2 个工具" or header_title.startswith(
                "agent loop · 1 轮 · 2 个工具 · "
            ), header_title

            def _flat(el: dict) -> str:
                text = el.get("text")
                nested = text.get("content", "") if isinstance(text, dict) else ""
                return f"{el.get('content', '')} {nested}"

            contents = [_flat(el) for el in panel.get("elements", [])]
            assert any("第 1 轮" in c for c in contents), contents
            assert any("**Terminal**" in c for c in contents), contents
            assert any("**Browser**" in c for c in contents), contents
            assert any("pytest tests/ -q" in c for c in contents), contents
            # pending 行不得出现在面板（已被 preview 取代）
            assert not any("browser..." in c for c in contents), contents

    async def test_active_path_pending_replaced_no_duplicate(self, runner):
        """活跃卡片期 pending→preview 原地取代（accumulate 快照混推 不膨胀）."""
        session = await runner.start_message("建卡。")
        await runner.send_raw("🖥️ terminal...", {"_interim_send": True})
        await runner.send_raw('🖥️ terminal："ls -la"', {"_interim_send": True})
        st = runner.get_state(session)
        assert st.status_lines == ['🖥️ terminal："ls -la"'], st.status_lines


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
