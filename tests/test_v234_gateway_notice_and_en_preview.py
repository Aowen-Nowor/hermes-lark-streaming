"""v2.0.34 回归测试 — en locale 工具预览行识别 + 网关生命周期通知识别.

生产取证（用户症状②③，2026-11-10 日志包 logs-v33 + hermes-agent@8b50838
源码 + 插件 v2.0.33 三方互证，逐字节核验）：

- 症状②（tool_search 未识别成「tool_search 标题 + Searching tools ·
  session search past... 内容」）：hermes preview 模板 zh 全角冒号
  （locales/zh.yaml:841）、en 半角冒号（locales/en.yaml:864
  ``tool_preview: "{emoji} {tool}: \"{preview}\""``）。bridge 工具
  （tool_search/tool_describe，tools/tool_labels.py:133 _bridge_labels）
  无 curated 动词必走 preview 模板；label.text = ``app · action`` =
  "Searching tools · " + queries[0]（· 为官方连接符 tool_labels.py:54），
  经 agent/display.py _preview_bridge_call 注入 preview 槽位。插件
  _TOOL_PREVIEW_RE 分隔符类 [：（(] 缺 ASCII ``:`` → en 环境切不出工具名
  → 灰色通报行兜底（split_notice_lead）。生产实证：agent.log:21570
  ``tool tool_search completed (3.10s, 784 chars)``。
- 症状③（shutdown 卡永久跑马灯）：停机/重启通知模板 locales
  gateway.shutdown.*（en.yaml:881-883 / zh.yaml:856-858），发射点
  gateway/run_shutdown.py _notify_active_sessions_of_shutdown →
  adapter.send 强制 _interim_send（run_shutdown.py:1006-1013，官方
  #98432）。插件 _NOTICE_RES 此前无规则 → ⚠️ emoji 开头 ≤300 命中
  _is_progress_text → CH_PROGRESS → _on_progress 无卡分支 early-create
  流式卡 → 网关 ~1.1s 后进程退出无 finalize → 永久跑马灯
  （agent.log:21619 card=7706182798772669698 全日志唯一命中，13 起
  shutdown notification 全部无 finalized）。修复：_NOTICE_RES 补 3 条
  规则（en+zh）→ notice 语义（活跃卡并入面板 / 无卡原生文本），永不建卡。
- 症状①（Mem0 add 橙色不变绿、折叠面板无内容）按用户裁决不修：工具真实
  完成态（绿/红+耗时）与结果内容仅存在于钩子/结构化帧通道（gateway
  tool.complete / post_tool_call），本插件零钩子架构（用户裁决：第一原则
  不注册钩子；plugin.yaml provides_hooks: []）。hermes 文本流对 mem0 类
  工具只发 pending 行（agent.log:18728 route-trace 逐字节
  ``⚙️ mem0_add...``），结果 90 chars 从不入流（agent.log:18731 executor
  完成 vs status_lines 恒 1）——mem0 pending 行为锁定不变（下含回归断言）。
"""

from __future__ import annotations

import pytest
from hermes_lark_streaming.cardkit.cards import _render_status_children
from hermes_lark_streaming.cardkit.progline import (
    parse_progress_line,
    preview_text,
)
from hermes_lark_streaming.cardkit.tool_icons import (
    DEFAULT_TOOL_ICON,
    resolve_tool_icon,
    tool_display_title,
)
from hermes_lark_streaming.composer.classify import (
    CH_INTERIM,
    CH_PLAIN,
    _is_notice_text,
    _is_progress_text,
    classify_send,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

# ── 官方模板逐字节样例（hermes-agent@8b50838 locales 直抄）─────────────────

# en tool_preview（en.yaml:864，ASCII 冒号）+ tool_search bridge label.text
# （tool_labels.py:133-139，app="Searching tools" · action=queries[0] 透传）
EN_TOOL_SEARCH_PREVIEW = '⚙️ tool_search: "Searching tools · session search past..."'
# zh tool_preview（zh.yaml:841，全角冒号；YAML \" 解码为裸引号）——既有行为回归
ZH_PREVIEW = '💻 terminal："echo "1""'
# en tool_pending（en.yaml:865）——mem0 生产实测形状（agent.log:18728 route-trace）
MEM0_PENDING = "⚙️ mem0_add..."
# gateway.shutdown.notice_*（en.yaml:882-883 / zh.yaml:857-858）
EN_SHUTDOWN = (
    "⚠️ Hermes is shutting down — your current task will be interrupted. "
    "When it is back online, send any message and I'll try to pick up where we left off."
)
EN_RESTART = (
    "⚠️ Hermes is restarting — your current task will be interrupted. "
    "Send any message after the restart and I'll try to resume where you left off."
)
ZH_SHUTDOWN = (
    "⚠️ Hermes 正在关闭——你当前的任务将被中断。"
    "恢复上线后，发送任意消息，我会尝试从中断处继续。"
)
ZH_RESTART = (
    "⚠️ Hermes 正在重启——你当前的任务将被中断。"
    "重启后发送任意消息，我会尝试从你离开的地方继续。"
)
# gateway.shutdown.cron_interrupted（en.yaml:881 / zh.yaml:856）
EN_CRON_CUT = (
    "⚠️ Scheduled job 'nightly-sync' was cut short because Hermes is shutting down; "
    "no result this run. It will run again on schedule, or run it now with "
    "`hermes cron run nightly-sync` once Hermes is back."
)
ZH_CRON_CUT = "⚠️ 定时任务 'nightly-sync' 因 Hermes 正在重启而被中断；本次运行没有结果。"


# ---------------------------------------------------------------------------
# 1. en 工具预览行（ASCII 半角冒号）→ 结构化工具行（症状②）
# ---------------------------------------------------------------------------


class TestEnToolPreview:
    def test_en_preview_parses_tool_name(self) -> None:
        kind, tool, _clean = parse_progress_line(EN_TOOL_SEARCH_PREVIEW)
        assert (kind, tool) == ("tool", "tool_search")

    def test_en_preview_content_extraction(self) -> None:
        # 干净样例：preview 原样提取（剥包裹引号）
        assert preview_text('⚙️ tool_search: "Searching tools · gmail send email"') == (
            "Searching tools · gmail send email"
        )

    def test_en_preview_truncation_tail_stripped(self) -> None:
        # hermes _tail_trunc（display.py:181-187）超 80 字符补 ASCII "..."；
        # 插件沿用既定截断标记语义剥尾（与 terminal 命令截断尾一致）
        assert preview_text(EN_TOOL_SEARCH_PREVIEW) == "Searching tools · session search past"

    def test_zh_fullwidth_colon_regression(self) -> None:
        # 全角冒号既有行为不变（v2.0.6 REAL_PREVIEW 同源）
        assert parse_progress_line(ZH_PREVIEW)[:2] == ("tool", "terminal")
        assert preview_text(ZH_PREVIEW) == 'echo "1"'

    def test_pending_and_head_unaffected(self) -> None:
        # pending/head 模板不含冒号，行为不变；mem0 行锁定（症状①裁决见 docstring）
        assert parse_progress_line(MEM0_PENDING)[:2] == ("tool", "mem0_add")
        assert parse_progress_line("⚙️ mem0_add")[:2] == ("tool", "mem0_add")

    def test_multiword_prefix_colon_not_a_tool(self) -> None:
        # 假阳性护栏：多词前缀 + ASCII 冒号（💾 Self-improvement review: …）
        # 不因新补的 `:` 误判出工具名——emoji 兜底行为与修复前一致
        text = (
            "💾 Self-improvement review: Skill 'x' patched · Skill 'x' patched "
            "(references/clarify-callback-routing.md)"
        )
        kind, tool, _clean = parse_progress_line(text)
        assert (kind, tool) == ("tool", "")

    def test_verb_line_still_wins_over_colon(self) -> None:
        # 动词行无冒号，解析顺序不受影响（en verb → read_file）
        assert parse_progress_line("📖 Reading adapter.py L655-699")[:2] == ("tool", "read_file")


# ---------------------------------------------------------------------------
# 2. 网关生命周期通知 → notice 语义（症状③）
# ---------------------------------------------------------------------------

_LIFECYCLE_NOTICES = (
    EN_SHUTDOWN,
    EN_RESTART,
    ZH_SHUTDOWN,
    ZH_RESTART,
    EN_CRON_CUT,
    ZH_CRON_CUT,
)


class TestGatewayLifecycleNotices:
    @pytest.mark.parametrize("text", _LIFECYCLE_NOTICES)
    def test_notice_family_recognized(self, text: str) -> None:
        assert _is_notice_text(text)

    @pytest.mark.parametrize("text", _LIFECYCLE_NOTICES)
    def test_interim_notice_never_classified_as_progress(self, text: str) -> None:
        # 官方发射契约：停机通知强制 _interim_send（run_shutdown.py:1006-1013）；
        # classify 的 notice 检查先于 progress 启发式 → 永不 CH_PROGRESS →
        # 永不触发 _on_progress early-create 流式卡（跑马灯根因）
        result = classify_send(text, {"_interim_send": True})
        assert result.channel == CH_INTERIM
        assert result.kind == "advisory"

    def test_progress_heuristic_shape_unchanged(self) -> None:
        # 启发式本身仍判进度（形状没变）；修复在 classify 的 notice 先判顺序，
        # 不动 _is_progress_text 语义（宽吸收等既有消费方行为保持）
        assert _is_progress_text(EN_SHUTDOWN)

    def test_notice_without_interim_flag_stays_plain(self) -> None:
        # 防御：无标记到达（异常网关）→ CH_PLAIN 原生/oneshot 路径，同样不建流式卡
        assert classify_send(EN_SHUTDOWN, None).channel == CH_PLAIN

    def test_update_notice_family_regression(self) -> None:
        # 既有 notice 规则不受新规则影响
        assert _is_notice_text("✅ Hermes update finished in 42s")
        assert _is_notice_text("♻️ Gateway restarted successfully")


# ---------------------------------------------------------------------------
# 3. tool_search 展示配套（图标 / 标题）
# ---------------------------------------------------------------------------


class TestToolSearchPresentation:
    def test_tool_search_icon_is_search(self) -> None:
        # 此前落默认 code_outlined，搜索语义不符
        assert resolve_tool_icon("tool_search") == "search_outlined"

    def test_mem0_default_icon_regression(self) -> None:
        # mem0 族不在图标表（症状①裁决不修的一部分）——保持默认行为锁定
        assert resolve_tool_icon("mem0_add") == DEFAULT_TOOL_ICON

    def test_tool_search_title_humanized(self) -> None:
        # 与 terminal 同口径：不在 V1 描述符表 → humanize（tool_search → Tool search）
        assert tool_display_title("tool_search") == "Tool search"


# ---------------------------------------------------------------------------
# 4. 渲染层集成：en 预览行 → 工具步骤行（标题 + 缩进内容）
# ---------------------------------------------------------------------------


class TestPanelRender:
    def test_en_preview_line_renders_title_and_detail(self) -> None:
        els = _render_status_children([EN_TOOL_SEARCH_PREVIEW])
        joined = str(els)
        # 标题：humanize + success 绿（行非 ... 结尾）+ 官方搜索图标
        assert "search_outlined" in joined
        assert "**Tool search**" in joined
        assert "orange-300" not in joined
        # 内容：preview 缩进灰字（截断尾按既定语义剥除）
        assert "Searching tools · session search past" in joined
        # 无原始行泄漏（emoji / 引号包裹不进面板）
        assert "⚙️" not in joined and '\\"' not in joined

    def test_mem0_pending_line_renders_orange_no_detail(self) -> None:
        # 症状①现状锁定：pending 行橙色 running、不渲染内容（裁决后最终语义）
        els = _render_status_children([MEM0_PENDING])
        joined = str(els)
        assert "orange-300" in joined
        assert "**Mem0 add**" in joined
