"""v2.0.12 回归测试 — 仓库布局契约 + 面板思考正文 markdown 渲染.

用户验收（2026-10-12）：
1. 「gitee 仓库层级结构还不够美观，根目录下除了 __init__.py、__main__.py
   就不应该有其他 py 脚本。只能有文档和配置文件」→ 布局契约测试（根仅两个
   入口 .py；运行时模块全部收进 plugin/ 等直接子级子包——hermes 目录插件
   加载器 spec_from_file_location + __path__ 契约下，加载器只看根
   __init__ 暴露的 register，子包布局对加载器不可见）。
2. 「折叠面板里面的文字没有做 md 渲染，比如 **Planning tool use**」→
   思考正文 plain_text → lark_md（_build_reasoning_body_detail：逐行
   <font color='grey'> 包裹保持灰色缩进视觉，markdown 语义生效）；
   命令摘要保持 plain_text（命令是代码非 markdown 语义）。
3. python 版本对齐 hermes 官方（>=3.11,<3.15，官方注释 "we *only* support
   3.14"）→ 见 test_version.py::TestPyprojectVersionContract。
"""

from __future__ import annotations

from pathlib import Path

import hermes_lark_streaming
from hermes_lark_streaming.cardkit.cards import _render_status_children
from hermes_lark_streaming.cardkit.elements import (
    _build_reasoning_body_detail,
    _build_tool_step_detail,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# 仓库布局契约（用户验收 ③）
# ---------------------------------------------------------------------------


class TestRepoLayoutContract:
    """根目录只允许 __init__.py / __main__.py 两个 .py 文件。"""

    def test_root_has_only_entry_py_files(self) -> None:
        root_py = sorted(
            p.name for p in _REPO_ROOT.glob("*.py") if p.is_file()
        )
        assert root_py == ["__init__.py", "__main__.py"]

    def test_runtime_modules_live_in_plugin_package(self) -> None:
        """四个 hermes 集成层模块收进 plugin/（直接子级子包，加载器契约内）。"""
        plugin_dir = _REPO_ROOT / "plugin"
        for name in ("adapter.py", "clarify.py", "reasoning_feed.py", "tool_timing.py"):
            assert (plugin_dir / name).is_file(), f"plugin/{name} missing"

    def test_root_init_reexports_register_surface(self) -> None:
        """加载器唯一契约：根 __init__ 暴露 register / FeishuStreamingAdapter。"""
        assert hasattr(hermes_lark_streaming, "register")
        assert hasattr(hermes_lark_streaming, "FeishuStreamingAdapter")
        assert hasattr(hermes_lark_streaming, "__version__")

    def test_pyproject_includes_plugin_package(self) -> None:
        text = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert '"plugin*"' in text


# ---------------------------------------------------------------------------
# 思考正文 markdown 渲染（用户验收 ①）
# ---------------------------------------------------------------------------


class TestReasoningBodyDetail:
    """_build_reasoning_body_detail：lark_md + 逐行 font-grey 包裹。"""

    def test_bold_renders_via_lark_md(self) -> None:
        el = _build_reasoning_body_detail("**Planning tool use**")
        assert el is not None
        assert el["tag"] == "div"
        assert el["margin"] == "0px 0px 0px 22px"
        text = el["text"]
        assert text["tag"] == "lark_md"
        assert text["text_size"] == "notation"  # v2.0.20：默认 = v2.0.18 字面值
        assert "text_color" not in text  # text_color 仅 plain_text 支持，不落在 lark_md
        assert text["content"] == "<font color='grey'>**Planning tool use**</font>"

    def test_multiline_wraps_each_line(self) -> None:
        el = _build_reasoning_body_detail("line **one**\n\nline **two**")
        content = el["text"]["content"]
        # 每个非空行独立包裹（<font> 跨空行行为无契约，逐行包裹确定性最高）
        assert "<font color='grey'>line **one**</font>" in content
        assert "<font color='grey'>line **two**</font>" in content
        # 空行不包裹
        assert content.count("<font color='grey'>") == 2

    def test_line_with_existing_font_not_double_wrapped(self) -> None:
        el = _build_reasoning_body_detail("<font color='red'>hot</font>\nplain")
        content = el["text"]["content"]
        assert "<font color='red'>hot</font>" in content
        assert "<font color='grey'>plain</font>" in content

    def test_blank_and_none_body(self) -> None:
        assert _build_reasoning_body_detail("") is None
        assert _build_reasoning_body_detail("   ") is None
        assert _build_reasoning_body_detail(None) is None  # type: ignore[arg-type]

    def test_visual_style_preserved(self) -> None:
        """缩进（22px）与字号（notation）与 V1/V2 灰字视觉一致。"""
        el = _build_reasoning_body_detail("x")
        assert el["margin"] == "0px 0px 0px 22px"
        assert el["text"]["text_size"] == "notation"


class TestToolPreviewStaysPlainText:
    """命令摘要保持 plain_text（命令是代码非 markdown 语义）。"""

    def test_detail_is_plain_text(self) -> None:
        el = _build_tool_step_detail({"detail": 'date "+%Y-%m-%d"'})
        assert el is not None
        assert el["text"]["tag"] == "plain_text"
        assert el["text"]["text_color"] == "grey"


class TestPanelThinkingBodyRendering:
    """_render_status_children 集成：思考正文 lark_md，工具摘要 plain_text。"""

    def test_thinking_body_uses_lark_md(self) -> None:
        kids = _render_status_children(["💬 **Planning tool use**\n正文第二行"])
        # kids: [轮次标题 div, 思考正文 div]
        assert len(kids) == 2
        body = kids[1]
        assert body["text"]["tag"] == "lark_md"
        assert "**Planning tool use**" in body["text"]["content"]

    def test_tool_preview_stays_plain(self) -> None:
        kids = _render_status_children(['🔧 terminal："ls -la"'])
        # kids: [工具标题 div, 命令摘要 div]
        previews = [k for k in kids if k.get("tag") == "div" and k.get("margin")]
        assert previews
        assert all(p["text"]["tag"] == "plain_text" for p in previews)
