"""v2.0.16 UI 四项修复回归 — 测试证据层.

覆盖（用户 2026-10-13 四项验收）：
1. Terminal 工具行图标 setting_outlined → code_outlined（tool_icons）；
2. 折叠面板轮次三色（V1 对齐）：流式期最后一组未封口 💬 思考轮 → 橙
   （orange-300），其余组 → 绿；finalize 收口重渲染 → 全绿；
   红（失败）不实施——V1 ``failed=True`` 零调用、hermes 工具进度行仅启动
   时发射（run_turn_runner.py:_progress_build_message）无失败形态，帧流无
   失败数据源不伪造（本文件以注释断言记录该决策依据）；
3. 警告族通报行（⚠️/🚨 → warning_outlined）图标+正文橙色，其余通报行灰色；
4. cron 卡头部无图标（job_id 页脚行保留 time_outlined）。

finalize 收口通道 = s146 真飞书探针定案：官方 batch_update 无 update_elements
action（code=10002 实测），用同批次 delete 面板 + add 重建面板
（insert_before answer 元素，marker 语义验证通过）。
"""
from __future__ import annotations

import pytest
from hermes_lark_streaming.cardkit.cards import (
    _build_notice_row,
    _last_thinking_group,
    _render_status_children,
    build_streaming_card_v2,
)
from hermes_lark_streaming.cardkit.special import _cron_title_row
from hermes_lark_streaming.cardkit.tool_icons import (
    DEFAULT_TOOL_ICON,
    resolve_notice_icon,
    resolve_tool_icon,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _round_titles(children: list[dict]) -> list[str]:
    """面板子元素中的轮次标题文本（含 <font color> 包裹，保持原样）."""
    out: list[str] = []
    for el in children:
        if isinstance(el, dict) and el.get("tag") == "div":
            icon = el.get("icon") or {}
            if isinstance(icon, dict) and icon.get("token") == "robot-add_outlined":
                out.append(str((el.get("text") or {}).get("content", "")))
    return out


# ---------------------------------------------------------------------------
# 1. Terminal 图标（用户验收 #1）
# ---------------------------------------------------------------------------


class TestTerminalIcon:
    def test_terminal_maps_to_code_outlined(self):
        assert resolve_tool_icon("terminal") == "code_outlined"

    def test_shell_family_unified(self):
        for name in ("exec", "bash", "command", "run"):
            assert resolve_tool_icon(name) == "code_outlined"

    def test_default_unchanged(self):
        assert DEFAULT_TOOL_ICON == "code_outlined"

    def test_official_token_frozen_set_still_holds(self):
        # v2.0.10 官方 token 冻结集（test_v210_notice_rows.py）已含 code_outlined；
        # 此处防回归：面板引用图标均在官方清单测试覆盖，这里锁定 terminal 换色不越集。
        from hermes_lark_streaming.cardkit.tool_icons import _TOOL_ICONS

        tokens = {icon for _, icon in _TOOL_ICONS}
        assert "setting_outlined" not in tokens  # v2.0.16 后齿轮图标全面退役


# ---------------------------------------------------------------------------
# 2. 轮次三色（用户验收 #2）
# ---------------------------------------------------------------------------


class TestLastThinkingGroup:
    def test_open_round_at_end(self):
        start, open_at_end = _last_thinking_group(["💬 想法 A", "💬 想法 B"])
        assert start == 0 and open_at_end is True

    def test_sealed_by_tool(self):
        start, open_at_end = _last_thinking_group(["💬 想法", '🔧 terminal："ls"'])
        assert start == 0 and open_at_end is False

    def test_sealed_by_notice(self):
        start, open_at_end = _last_thinking_group(["💬 想法", "⚠️ 警告行"])
        assert start == 0 and open_at_end is False

    def test_no_thinking(self):
        assert _last_thinking_group(['🔧 a："x"']) == (-1, False)

    def test_later_group_wins(self):
        start, open_at_end = _last_thinking_group(
            ["💬 第一轮", '🔧 a："x"', "💬 第二轮"])
        assert start == 2 and open_at_end is True


class TestRoundColors:
    def test_streaming_open_round_orange(self):
        kids = _render_status_children(["💬 正在思考"], turn_state="streaming")
        title = _round_titles(kids)[0]
        assert "orange-300" in title and "green" not in title

    def test_streaming_sealed_rounds_green(self):
        kids = _render_status_children(
            ["💬 第一轮", '🔧 a："x"', "💬 第二轮", '🔧 b："y"'],
            turn_state="streaming")
        titles = _round_titles(kids)
        assert len(titles) == 2
        assert all("green" in t for t in titles)

    def test_streaming_trailing_notice_seals_round(self):
        kids = _render_status_children(
            ["💬 想法", "⚠️ Model fallback: 警告"], turn_state="streaming")
        assert "green" in _round_titles(kids)[0]

    def test_finalized_default_all_green(self):
        # 默认参数回归（既有调用方零感知）
        kids = _render_status_children(["💬 正在思考"])
        assert "green" in _round_titles(kids)[0]

    def test_finalized_explicit_all_green(self):
        kids = _render_status_children(
            ["💬 A", '🔧 a："x"', "💬 B"], turn_state="finalized")
        assert all("green" in t for t in _round_titles(kids))

    def test_streaming_card_default_is_streaming(self):
        card = build_streaming_card_v2(status_lines=["💬 流式中的思考"])
        panel = card["body"]["elements"][0]
        assert "orange-300" in _round_titles(panel["elements"])[0]

    def test_red_not_implemented_no_data_source(self):
        """红色轮次不实施的决策冻结：V1 failed=True 零调用 + 帧流无失败形态.

        防未来无意引入：_build_reasoning_round_title 的 failed 参数保持
        无生产调用方；若帧流未来出现失败形态，须先补 hermes 取证再接线。
        """
        import inspect

        from hermes_lark_streaming.cardkit import elements

        src = inspect.getsource(elements._build_reasoning_round_title)
        assert "failed" in src  # 参数仍在（V1 移植契约），但无调用方传 True
        kids = _render_status_children(["💬 任意轮"])
        assert "red" not in _round_titles(kids)[0]


# ---------------------------------------------------------------------------
# 3. 警告行橙色（用户验收 #3）
# ---------------------------------------------------------------------------


class TestWarningNoticeColor:
    def test_warning_row_orange(self):
        row = _build_notice_row("warning_outlined", "Model fallback: ...")
        assert row["icon"]["color"] == "orange"
        assert row["text"]["text_color"] == "orange"

    def test_non_warning_rows_stay_grey(self):
        for token in ("time_outlined", "info_outlined", "storage_outlined"):
            row = _build_notice_row(token, "正文")
            assert row["icon"]["color"] == "grey"
            assert row["text"]["text_color"] == "grey"

    def test_fallback_line_renders_orange_in_panel(self):
        line = ("⚠️ Model fallback: deepseek-v4-flash via custom unavailable "
                "(billing or quota exhausted); using sensenova-6.8-flash-lite "
                "via custom. Primary retry eligible in ~60 s; recovery is not "
                "guaranteed.")
        kids = _render_status_children([line])
        row = kids[0]
        assert row["icon"]["token"] == "warning_outlined"
        assert row["text"]["text_color"] == "orange"

    def test_subagent_failure_row_orange(self):
        line = ('⚠️ Subagent failed — "research pricing" after 12 min: boom')
        kids = _render_status_children([line])
        assert kids[0]["text"]["text_color"] == "orange"

    def test_cron_jobid_footer_keeps_grey(self):
        from hermes_lark_streaming.cardkit.special import build_cron_card

        card = build_cron_card("正文", title="t", job_id="j1")
        footer = card["body"]["elements"][-1]
        assert footer["text"]["text_color"] == "grey"
        assert footer["icon"]["token"] == "time_outlined"


# ---------------------------------------------------------------------------
# 4. cron 头部无图标（用户验收 #4）
# ---------------------------------------------------------------------------


class TestCronHeaderNoIcon:
    def test_title_row_has_no_icon(self):
        row = _cron_title_row("daily-job")
        assert row["tag"] == "div"
        assert "icon" not in row
        assert "**daily-job**" in row["text"]["content"]

    def test_build_cron_card_first_element_no_icon(self):
        from hermes_lark_streaming.cardkit.special import build_cron_card

        card = build_cron_card("正文", title="t-name", job_id="j1")
        first = card["body"]["elements"][0]
        assert first["tag"] == "div" and "icon" not in first


# ---------------------------------------------------------------------------
# 5. finalize 面板收口重渲染（V4 通道，s146 探针定案）
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self):
        self.cards: dict[str, dict] = {}
        self._n = 0

    async def cardkit_create(self, card):
        self._n += 1
        self.cards[str(self._n)] = {"card": card, "seq": 0}
        return str(self._n)

    async def cardkit_stream_element(self, card_id, element_id, content, sequence=0):
        self.cards.setdefault(card_id, {"seq": 0})["seq"] = sequence

    async def cardkit_batch_update(self, card_id, operations, sequence=0):
        rec = self.cards.setdefault(card_id, {"seq": 0})
        rec["seq"] = sequence
        rec.setdefault("ops", []).append(operations)

    async def cardkit_close_streaming(self, card_id, sequence=0, summary=None):
        self.cards.setdefault(card_id, {"seq": 0})["closed"] = True


def _state_with(status_lines: list[str]):
    from types import SimpleNamespace

    return SimpleNamespace(
        card_id="card_x",
        seq=0,
        status_lines=status_lines,
        tool_durations={},
        line_times=[],
        answer_text="答案",
        created_at=0.0,
    )


def _panel_ops(client: _FakeClient) -> list[list[dict]]:
    """batch_update 中包含面板重建动作对的调用记录."""
    out = []
    for ops in client.cards["card_x"].get("ops", []):
        actions = {op.get("action") for op in ops}
        if "delete_elements" in actions and "add_elements" in actions:
            out.append(ops)
    return out


class TestFinalizePanelReseal:
    async def test_open_round_resealed_green(self):
        from hermes_lark_streaming.composer.finalize import finalize_card

        client = _FakeClient()
        state = _state_with(["💬 流式中的最后一轮思考", '🔧 a："x"', "💬 还在想"])
        await finalize_card(
            client, state, answer_element_id="answer",
            panel_title="agent loop · 2 轮 · 1 个工具", panel_expanded=True)
        ops = _panel_ops(client)
        assert len(ops) == 1
        delete, add = ops[0]
        assert delete["params"]["element_ids"] == ["agent_process_panel"]
        added_panel = add["params"]["elements"][0]
        assert added_panel["element_id"] == "agent_process_panel"
        assert add["params"]["target_element_id"] == "answer_content"
        # 收口后全绿
        titles = _round_titles(added_panel["elements"])
        assert titles and all("green" in t for t in titles)
        # 动态标题保留
        assert added_panel["header"]["title"]["content"] == (
            "agent loop · 2 轮 · 1 个工具")

    async def test_no_open_round_skips_api(self):
        from hermes_lark_streaming.composer.finalize import finalize_card

        client = _FakeClient()
        state = _state_with(["💬 想法", '🔧 terminal："ls"'])
        await finalize_card(client, state, answer_element_id="answer")
        assert _panel_ops(client) == []  # 流式末渲染已全绿 → 无重建

    async def test_empty_lines_skip_reseal(self):
        from hermes_lark_streaming.composer.finalize import finalize_card

        client = _FakeClient()
        state = _state_with([])
        await finalize_card(client, state, answer_element_id="answer")
        assert _panel_ops(client) == []

    async def test_reseal_failure_non_fatal(self):
        from hermes_lark_streaming.composer.finalize import finalize_card

        class _Boom(_FakeClient):
            async def cardkit_batch_update(self, card_id, operations, sequence=0):
                actions = {op.get("action") for op in operations}
                if "add_elements" in actions and "delete_elements" in actions:
                    raise RuntimeError("reseal channel down")
                await super().cardkit_batch_update(card_id, operations, sequence)

        client = _Boom()
        state = _state_with(["💬 开放轮"])
        # 不上抛：定稿主链（answer/页脚/关流式）完成
        await finalize_card(client, state, answer_element_id="answer")
        assert client.cards["card_x"].get("closed") is True


# ---------------------------------------------------------------------------
# 6. 通报图标回归（警告族映射不变）
# ---------------------------------------------------------------------------


class TestNoticeIconRegression:
    def test_warning_emoji_map(self):
        assert resolve_notice_icon("⚠️") == "warning_outlined"
        assert resolve_notice_icon("🚨") == "warning_outlined"

    def test_info_emoji_map(self):
        assert resolve_notice_icon("ℹ️") == "info_outlined"
        assert resolve_notice_icon("⏱️") == "time_outlined"
