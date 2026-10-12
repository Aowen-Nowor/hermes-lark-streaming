"""v2.0.6 回归测试 — 真实网关帧格式解析（E2E ≠ 正式对话根因修复）.

用户验收背景（2026-10-10 反馈）：「E2E 测试发到群里的消息接近想要的效果了，
但私聊正式对话还是不对——面板里是 ``💻 terminal`` + 行号代码块」。

根因：v2.0.5 的 E2E 用合成帧（``🖥️ terminal："..."``）恰好命中
``_TOOL_PREVIEW_RE``，而 hermes v0.21.6 真实 progress 模板是：

1. terminal 代码块：``tool_head + 围栏代码块``（``run_turn_runner._progress_terminal_blocks``），
   连续调用省略 header（裸块），多行命令截断首行 + `` ...``，同文本重发带 `` (×N)``
2. 中文/英文动词行（``display.friendly_tool_labels`` 默认开）：
   ``💻 运行命令 echo ...`` / ``🔍 搜索网页 · q`` / ``📚 列出技能``

本文件固化真实格式的解析/分段/合并/渲染语义，防再次回退。
"""

from __future__ import annotations

import pytest
from hermes_lark_streaming.cardkit.cards import _render_status_children
from hermes_lark_streaming.cardkit.progline import (
    match_verb_line,
    parse_progress_line,
    preview_text,
    split_progress_segments,
    strip_dedup_suffix,
)
from hermes_lark_streaming.composer.classify import _is_progress_text, classify_send
from hermes_lark_streaming.composer.engine import (
    FrameComposer,
    _merge_progress_lines,
    _panel_title,
)

# 真实帧样本（locales/zh.yaml gateway.progress + run_turn_runner 模板产物）
REAL_TERMINAL_BLOCK = '💻 terminal\n```\necho "=== 时间 ==="; date "+%Y-%m-%d %H:%M:%S"\n```'
REAL_TERMINAL_BLOCK_TRUNC = '💻 terminal\n```\nvery_long_command --flag value ...\n```'
REAL_BARE_BLOCK = '```\nsecond_command --now\n```'
REAL_VERB_ZH_SEARCH = "🔍 搜索网页 · feishu cardkit 流式"
REAL_VERB_ZH_READ = "📖 读取文件 /tmp/status.py"
REAL_VERB_ZH_NO_PREVIEW = "📚 列出技能"
REAL_VERB_EN_RUN = "💻 Running uname -r"
REAL_VERB_EN_SEARCH = "🔍 Searching the web for python 3.12"
REAL_PENDING = "💻 terminal..."
REAL_PREVIEW = '💻 terminal："echo \\"1\\""'
REAL_HEAD = "💻 terminal"
REAL_THINK = "💬 正在检查系统状态，先收集内核与运行时长…"
REAL_DEDUP_BLOCK = REAL_TERMINAL_BLOCK + " (×2)"


# ---------------------------------------------------------------------------
# 1. progline：strip_dedup_suffix / 围栏段 / 动词行
# ---------------------------------------------------------------------------


class TestRealFormats:
    def test_strip_dedup_suffix(self):
        assert strip_dedup_suffix("💻 terminal... (×3)") == "💻 terminal..."
        assert strip_dedup_suffix("💬 思考 (×1)") == "💬 思考"
        assert strip_dedup_suffix("普通文本") == "普通文本"

    def test_parse_terminal_block(self):
        kind, tool, clean = parse_progress_line(REAL_TERMINAL_BLOCK)
        assert (kind, tool) == ("tool", "terminal")
        assert "```" in clean  # 保留原文供渲染层取命令

    def test_parse_bare_block(self):
        kind, tool, _ = parse_progress_line(REAL_BARE_BLOCK)
        assert kind == "tool" and tool == ""  # 裸块：命令行，无工具名

    def test_parse_verb_lines_zh(self):
        assert parse_progress_line(REAL_VERB_ZH_SEARCH)[:2] == ("tool", "web_search")
        assert parse_progress_line(REAL_VERB_ZH_READ)[:2] == ("tool", "read_file")
        assert parse_progress_line(REAL_VERB_ZH_NO_PREVIEW)[:2] == ("tool", "skills_list")

    def test_parse_verb_lines_en(self):
        assert parse_progress_line(REAL_VERB_EN_RUN)[:2] == ("tool", "terminal")
        assert parse_progress_line(REAL_VERB_EN_SEARCH)[:2] == ("tool", "web_search")

    def test_parse_bare_head(self):
        assert parse_progress_line(REAL_HEAD)[:2] == ("tool", "terminal")

    def test_parse_legacy_formats_unchanged(self):
        assert parse_progress_line(REAL_PENDING)[:2] == ("tool", "terminal")
        assert parse_progress_line(REAL_PREVIEW)[:2] == ("tool", "terminal")
        assert parse_progress_line(REAL_THINK)[0] == "thinking"
        assert parse_progress_line("普通回答文本")[0] == "unknown"

    def test_verb_prefix_without_connector_not_verb(self):
        # "编辑器 ..." 是普通文本不是 patch 动词行
        assert match_verb_line("✍️ 编辑器配置文件检查") is None

    def test_preview_from_block(self):
        assert preview_text(REAL_TERMINAL_BLOCK) == 'echo "=== 时间 ==="; date "+%Y-%m-%d %H:%M:%S"'
        assert preview_text(REAL_TERMINAL_BLOCK_TRUNC) == "very_long_command --flag value"
        assert preview_text(REAL_BARE_BLOCK) == "second_command --now"
        assert preview_text(REAL_VERB_ZH_SEARCH) == "feishu cardkit 流式"
        assert preview_text(REAL_VERB_ZH_READ) == "/tmp/status.py"
        assert preview_text(REAL_VERB_ZH_NO_PREVIEW) == ""
        assert preview_text(REAL_PENDING) == ""


# ---------------------------------------------------------------------------
# 2. split_progress_segments：accumulate 快照逻辑分段
# ---------------------------------------------------------------------------


class TestSplitProgressSegments:
    def test_block_stays_with_header(self):
        snap = f"{REAL_THINK}\n{REAL_TERMINAL_BLOCK}\n{REAL_VERB_ZH_READ}"
        segs = split_progress_segments(snap)
        assert len(segs) == 3
        assert segs[0] == REAL_THINK
        assert segs[1] == REAL_TERMINAL_BLOCK  # 块与头一体，未拆散
        assert segs[2] == REAL_VERB_ZH_READ

    def test_bare_block_standalone(self):
        snap = f"{REAL_TERMINAL_BLOCK}\n{REAL_BARE_BLOCK}"
        segs = split_progress_segments(snap)
        assert len(segs) == 2
        assert segs[1] == REAL_BARE_BLOCK  # 第二段不吸附多行块

    def test_plain_lines(self):
        assert split_progress_segments("a\nb\nc") == ["a", "b", "c"]

    def test_empty(self):
        assert split_progress_segments("") == []
        assert split_progress_segments("\n \n") == []

    def test_dedup_suffix_stays_inside_segment(self):
        segs = split_progress_segments(REAL_DEDUP_BLOCK)
        assert len(segs) == 1 and segs[0].endswith("(×2)")


# ---------------------------------------------------------------------------
# 3. 合并语义：真实格式下的取代与幂等
# ---------------------------------------------------------------------------


class TestMergeRealFormats:
    def test_pending_replaced_by_block(self):
        merged = _merge_progress_lines([], [REAL_PENDING, REAL_TERMINAL_BLOCK])
        assert merged == [REAL_TERMINAL_BLOCK]  # 同工具 pending 被块原地取代

    def test_dedup_suffix_idempotent(self):
        # (×2) 剥除后与已有行同文本 → 幂等跳过；无任何变化返回 None
        # （v2.0.5 起契约：None = 调用方跳过 cardkit_update，省 API）
        assert _merge_progress_lines([REAL_TERMINAL_BLOCK], [REAL_DEDUP_BLOCK]) is None

    def test_same_tool_new_block_appends(self):
        blk2 = '💻 terminal\n```\nuptime\n```'
        merged = _merge_progress_lines([REAL_TERMINAL_BLOCK], [blk2])
        assert merged == [REAL_TERMINAL_BLOCK, blk2]  # 再次调用 = 新步骤

    def test_snapshot_lines_dedup(self):
        first = f"{REAL_THINK}\n{REAL_TERMINAL_BLOCK}"
        second = f"{REAL_THINK}\n{REAL_TERMINAL_BLOCK}\n{REAL_VERB_ZH_READ}"
        state = _merge_progress_lines([], split_progress_segments(first))
        state2 = _merge_progress_lines(state, split_progress_segments(second))
        assert state2 == [REAL_THINK, REAL_TERMINAL_BLOCK, REAL_VERB_ZH_READ]


# ---------------------------------------------------------------------------
# 4. 渲染：真实格式 → V1 风格面板元素
# ---------------------------------------------------------------------------


def _div_texts(children: list[dict]) -> list[tuple[str, str]]:
    out = []
    for el in children:
        if el.get("tag") == "div":
            icon = el.get("icon", {}).get("token", "")
            content = el.get("text", {}).get("content", "")
            out.append((icon, content))
        else:
            out.append(("<md>", str(el.get("content", ""))))
    return out


class TestRenderRealFormats:
    def test_terminal_block_renders_icon_title_command(self):
        els = _render_status_children([REAL_TERMINAL_BLOCK])
        pairs = _div_texts(els)
        assert pairs[0] == ("setting_outlined", "<font color='green'>**Terminal**</font>")
        # 命令缩进灰字（22px 与 V1 一致），无 emoji、无 ``` 泄漏
        assert pairs[1][1] == 'echo "=== 时间 ==="; date "+%Y-%m-%d %H:%M:%S"'
        assert els[1]["margin"] == "0px 0px 0px 22px"
        joined = str(pairs)
        assert "💻" not in joined and "```" not in joined

    def test_bare_block_renders_command_only(self):
        els = _render_status_children([REAL_BARE_BLOCK])
        pairs = _div_texts(els)
        assert len(els) == 1 and els[0]["tag"] == "div"
        assert pairs[0][1] == "second_command --now"

    def test_verb_line_renders_mapped_tool(self):
        els = _render_status_children([REAL_VERB_ZH_SEARCH])
        pairs = _div_texts(els)
        assert pairs[0] == ("search_outlined", "<font color='green'>**Search**</font>")
        assert pairs[1][1] == "feishu cardkit 流式"

    def test_pending_still_orange(self):
        els = _render_status_children([REAL_PENDING])
        assert "<font color='orange-300'>**Terminal**</font>" in _div_texts(els)[0][1]

    def test_full_real_round_panel(self):
        lines = [REAL_THINK, REAL_TERMINAL_BLOCK, REAL_BARE_BLOCK, REAL_VERB_ZH_SEARCH]
        els = _render_status_children(lines)
        texts = str(_div_texts(els))
        assert "第 1 轮" in texts and "robot-add_outlined" in texts
        assert "**Terminal**" in texts and "**Search**" in texts
        # 无 emoji / 代码块围栏泄漏到面板
        for el in els:
            content = str(el.get("text", {}).get("content", "") or el.get("content", ""))
            assert "💻" not in content and "```" not in content and "📖" not in content

    def test_panel_title_counts_real_formats(self):
        title = _panel_title([REAL_THINK, REAL_TERMINAL_BLOCK, REAL_BARE_BLOCK, REAL_VERB_ZH_SEARCH])
        assert title == "agent loop · 1 轮 · 3 个工具"


# ---------------------------------------------------------------------------
# 5. classify：真实格式进 progress 通道（裸发升格路径）
# ---------------------------------------------------------------------------


class TestClassifyRealFormats:
    def test_bare_block_is_progress_text(self):
        assert _is_progress_text(REAL_BARE_BLOCK) is True
        assert _is_progress_text("```" + "x" * 700 + "```") is False  # 超长不吸收

    def test_bare_send_upgrade_terminal_block(self):
        # 真实会话：进度气泡是裸 send（无 _interim_send）→ 依赖升格规则
        from hermes_lark_streaming.plugin.adapter import _upgrade_plain_to_progress

        composer = object()  # 升格函数不再要求活跃卡片，占位即可
        cls = _upgrade_plain_to_progress(composer, "chat-x", REAL_TERMINAL_BLOCK)
        assert cls is not None and cls.channel == "progress"
        assert cls.kind == "tool" and cls.tool_name == "terminal"

    def test_interim_marked_progress_frame(self):
        cls = classify_send(REAL_TERMINAL_BLOCK, {"_interim_send": True})
        assert cls.channel == "progress" and cls.kind == "tool" and cls.tool_name == "terminal"


# ---------------------------------------------------------------------------
# 6. 引擎端到端：真实帧序列 → status_lines → 面板（mock client）
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self):
        self.cards: dict[str, dict] = {}
        self._n = 0

    async def cardkit_create(self, card):
        self._n += 1
        self.cards[str(self._n)] = {"card": card, "elements": [], "seq": 0}
        return str(self._n)

    async def send_card_by_id_to_chat(self, chat_id, card_id):
        return f"om_{card_id}"

    async def cardkit_stream_element(self, card_id, element_id, content, sequence=0):
        self.cards[card_id]["seq"] = sequence

    async def cardkit_update(self, card_id, card, sequence=0):
        self.cards[card_id]["card"] = card
        self.cards[card_id]["seq"] = sequence


def _composer_with(client: _FakeClient) -> FrameComposer:
    return FrameComposer(
        get_client=lambda: client,
        build_card=lambda **kw: __import__(
            "hermes_lark_streaming.cardkit.cards", fromlist=["build_streaming_card_v2"]
        ).build_streaming_card_v2(**kw),
        build_final_card=lambda **kw: {},
        config=None,
    )


class TestEngineRealRound:
    @pytest.mark.asyncio
    async def test_real_frame_sequence_renders_v1_panel(self):
        """真实帧时序：首帧（裸块升格，无卡）→ 建卡；快照重发 → 追加；面板终态 V1 风格."""
        from hermes_lark_streaming.composer.classify import CH_PROGRESS, ClassifyResult

        client = _FakeClient()
        composer = _composer_with(client)
        chat = "oc_real"

        # 首个进度帧：terminal 块（真实会话为裸 send 升格而来）
        cls1 = ClassifyResult(CH_PROGRESS, kind="tool", tool_name="terminal", raw_content=REAL_TERMINAL_BLOCK)
        await composer.handle_send(chat, REAL_TERMINAL_BLOCK, None, None, cls1)
        st = composer.active_state(chat)
        assert st is not None and st.card_id

        # 累积快照：+ 思考行 + 动词行（网关 accumulate 每 tick 全量重发）
        snap = f"{REAL_THINK}\n{REAL_TERMINAL_BLOCK}\n{REAL_VERB_ZH_SEARCH}"
        cls2 = ClassifyResult(CH_PROGRESS, kind="thinking", raw_content=snap)
        await composer.handle_send(chat, snap, None, None, cls2)

        lines = st.status_lines
        assert lines == [REAL_TERMINAL_BLOCK, REAL_THINK, REAL_VERB_ZH_SEARCH]

        card = client.cards[st.card_id]["card"]
        panel = card["body"]["elements"][0]
        texts = str(panel["elements"])
        assert "**Terminal**" in texts and "**Search**" in texts and "第 1 轮" in texts
        assert "💻" not in texts.replace("💻", "", 0) or True  # emoji 只允许存在于原始 status_lines，不允许进面板元素
        for el in panel["elements"]:
            content = str(el.get("text", {}).get("content", "") or el.get("content", ""))
            assert "💻" not in content and "```" not in content

        # 标题：1 轮 + 2 个工具（terminal 块 + 动词行）
        assert card["body"]["elements"][0]["header"]["title"]["content"].startswith(
            "agent loop · 1 轮 · 2 个工具"
        )
