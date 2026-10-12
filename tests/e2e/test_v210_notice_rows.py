"""v2.0.10 回归测试 — 面板通报行官方图标 + status 通道语义层.

事故来源（2026-10-12 用户飞书验收）：

- 限流终态错误通报「⏱️ The AI model service is rate-limiting requests.
  Wait a moment, then use /retry.」（网关 ``_gateway_provider_error_reply``
  官方 i18n 文案）经 status 通道到达适配器（metadata 无 notify）→ v2.0.1
  升格启发式误判为进度行 → 无活跃卡时建占位卡（spinner 永不定稿 = 用户
  "卡住了"体感）→ 渲染层不匹配任何已知模式 → 裸 markdown 兜底（字体与
  其他行不一致 + emoji 原样保留）。

修复两层：

- 渲染层（本文件 TestNoticeRows）：解析不出工具结构的 emoji 行 → 官方
  standard_icon + notation 灰字统一行样式，emoji 从正文剥除；默认工具图标
  tool_02 → 官方枚举 ``code_outlined``（tool_02 不在官方
  enumerations-for-icons 清单内）。
- 通道层（TestStatusLane）：适配器实现官方 ``send_or_update_status`` 契约
  ——status 通报 = advisory：活跃卡并入面板 / 无卡原生文本 / 永不建卡。
"""

from __future__ import annotations

import asyncio

import pytest
from hermes_lark_streaming.cardkit.cards import _render_status_children
from hermes_lark_streaming.cardkit.tool_icons import (
    _NOTICE_ICONS,
    _TOOL_ICONS,
    DEFAULT_NOTICE_ICON,
    DEFAULT_TOOL_ICON,
    resolve_notice_icon,
    split_notice_lead,
)
from hermes_lark_streaming.composer.engine import FrameComposer

from .test_v208_stream_quality import _FakeClient, _seed_active, _settle


@pytest.fixture()
def composer():
    """v2.0.10 本地副本（fixture 不随模块导入注册；结构同 v208）."""
    from hermes_lark_streaming import tool_timing
    from hermes_lark_streaming.cardkit.cards import build_streaming_card_v2

    client = _FakeClient()
    comp = FrameComposer(
        get_client=lambda: client,
        build_card=lambda **kw: build_streaming_card_v2(**kw),
        build_final_card=lambda **kw: {},
        config=None,
    )
    tool_timing.reset_for_tests()
    tool_timing.bind_composer(comp)
    yield comp
    tool_timing.reset_for_tests()

# 官方 enumerations-for-icons 清单冻结样本（2026-10 抓取存档 /tmp/feishu_icons.md；
# 215KB 全量 1141 token，此处固化本插件实际引用的 19 个作回归防线）
_OFFICIAL_TOKENS = frozenset({
    "app-default_outlined", "file-link-text_outlined", "edit_outlined",
    "search_outlined", "language_outlined", "doc-search_outlined",
    "folder_outlined", "setting_outlined", "browser-mac_outlined",
    "robot_outlined", "list-check_outlined", "report_outlined",
    "code_outlined", "time_outlined", "warning_outlined", "info_outlined",
    "storage_outlined", "refresh_outlined", "robot-add_outlined",
})

# 2026-10-12 生产事故原文（gateway20，turn "卡住了" 的 status 通报行）
_INCIDENT_LINE = (
    "⏱️ The AI model service is rate-limiting requests. "
    "Wait a moment, then use /retry."
)


def _notice_rows(children: list[dict]) -> list[dict]:
    """过滤出通报行元素（div + standard_icon + plain_text 灰字）."""
    return [
        ch for ch in children
        if ch.get("tag") == "div" and isinstance(ch.get("icon"), dict)
        and ch["icon"].get("tag") == "standard_icon"
        and isinstance(ch.get("text"), dict) and ch["text"].get("tag") == "plain_text"
    ]


class TestNoticeLead:
    """split_notice_lead / resolve_notice_icon 纯函数语义."""

    def test_incident_line_maps_time_icon_and_strips_emoji(self):
        icon, body = split_notice_lead(_INCIDENT_LINE)
        assert icon == "time_outlined"
        assert body == (
            "The AI model service is rate-limiting requests. "
            "Wait a moment, then use /retry."
        )
        assert "⏱" not in body and "\ufe0f" not in body

    def test_family_mapping(self):
        assert resolve_notice_icon("⏳") == "time_outlined"
        assert resolve_notice_icon("⏰") == "time_outlined"
        assert resolve_notice_icon("⚠️") == "warning_outlined"
        assert resolve_notice_icon("🚨") == "warning_outlined"
        assert resolve_notice_icon("💾") == "storage_outlined"
        assert resolve_notice_icon("🔄") == "refresh_outlined"
        assert resolve_notice_icon("💡") == "info_outlined"
        # 未知 emoji → 默认 info（而非报错/裸样式）
        assert resolve_notice_icon("🌟") == DEFAULT_NOTICE_ICON == "info_outlined"

    def test_u2139_info_sign_in_map_even_outside_emoji_re(self):
        # ℹ (U+2139) 不在 _EMOJI_RE 区间，但命中通报表 → 仍走官方图标
        icon, body = split_notice_lead("ℹ️ details available")
        assert icon == "info_outlined" and body == "details available"

    def test_non_emoji_and_bare_emoji_rejected(self):
        assert split_notice_lead("Computer says no") is None
        assert split_notice_lead("") is None
        assert split_notice_lead("⏱️") is None  # 无正文：无处渲染，维持兜底
        # 首 token 含非 emoji 字符（非纯 emoji lead）→ None
        assert split_notice_lead("#tag note") is None
        assert split_notice_lead("A⏱ mixed") is None


class TestNoticeRows:
    """_render_status_children 兜底分支：通报行统一官方图标样式."""

    def test_incident_line_renders_notice_row(self):
        children = _render_status_children([_INCIDENT_LINE])
        rows = _notice_rows(children)
        assert len(rows) == 1
        assert rows[0]["icon"]["token"] == "time_outlined"
        assert rows[0]["icon"]["color"] == "grey"
        assert rows[0]["text"]["text_color"] == "grey"
        assert rows[0]["text"]["text_size"] == "notation"
        assert "rate-limiting requests" in rows[0]["text"]["content"]
        assert "⏱" not in rows[0]["text"]["content"]

    def test_notice_rows_after_tool_rows(self):
        """事故场景还原：工具行在前、通报行随后 → 样式统一、emoji 零泄漏."""
        lines = [
            "💻 运行命令 uname -r",
            "🔍 搜索网页 · 新闻",
            _INCIDENT_LINE,
            "⚠️ Provider rate limited after 3 retries",
        ]
        rendered = "".join(str(ch) for ch in _render_status_children(lines))
        assert "⏱" not in rendered and "⚠" not in rendered
        rows = _notice_rows(_render_status_children(lines))
        assert [r["icon"]["token"] for r in rows] == ["time_outlined", "warning_outlined"]

    def test_plain_unknown_line_still_markdown_fallback(self):
        out = _render_status_children(["普通说明文字（无 emoji）"])
        assert out == [{"tag": "markdown", "content": "普通说明文字（无 emoji）"}]

    def test_truncated_notice_line_pending_form(self):
        """以 ... 截断的通报行（pending 形态）同样走通报样式."""
        children = _render_status_children(["⏳ Working on it..."])
        rows = _notice_rows(children)
        assert len(rows) == 1 and rows[0]["icon"]["token"] == "time_outlined"


class TestOfficialTokens:
    """面板引用的全部图标 token 必须在官方 enumerations-for-icons 清单内."""

    def test_default_tool_icon_official(self):
        assert DEFAULT_TOOL_ICON == "code_outlined"
        assert DEFAULT_TOOL_ICON in _OFFICIAL_TOKENS

    def test_all_tool_icons_official(self):
        for _aliases, token in _TOOL_ICONS:
            assert token in _OFFICIAL_TOKENS, token

    def test_all_notice_icons_official(self):
        assert set(_NOTICE_ICONS.values()) <= _OFFICIAL_TOKENS
        assert DEFAULT_NOTICE_ICON in _OFFICIAL_TOKENS


# ---------------------------------------------------------------------------
# status 通道语义层（adapter.send_or_update_status）
# ---------------------------------------------------------------------------


class TestStatusLane:
    def _stub(self, comp, monkeypatch):
        """免 __init__ 的适配器桩：注入 composer，并把占位基类 send 换成记录器."""
        import hermes_lark_streaming.adapter as adp

        native: list[tuple] = []

        async def _fake_send(self, chat_id, content, reply_to=None, metadata=None):
            native.append((chat_id, content, metadata))
            return "native"

        monkeypatch.setattr(adp._MissingBuiltin, "send", _fake_send, raising=False)
        stub = object.__new__(adp.FeishuStreamingAdapter)
        stub._composer = comp
        return stub, native

    def test_status_with_active_card_joins_panel_never_native(self, composer, monkeypatch):
        stub, native = self._stub(composer, monkeypatch)
        chat = "oc_status"

        async def run():
            await _seed_active(composer, chat)
            comp = composer
            comp._loop = asyncio.get_running_loop()
            before = len(comp.active_state(chat).status_lines)
            result = await type(stub).send_or_update_status(
                stub, chat, "lifecycle", "⏳ Working", metadata={"status_thread": "x"})
            await _settle()
            st = comp.active_state(chat)
            assert result is not None  # 通报被面板吸收（非原生发送）
            assert len(st.status_lines) == before + 1
            assert st.status_lines[-1] == "⏳ Working"
            assert native == []  # 活跃卡期绝不原生发文本

        asyncio.get_event_loop().run_until_complete(run())

    def test_status_without_card_native_never_creates_card(self, composer, monkeypatch):
        stub, native = self._stub(composer, monkeypatch)
        chat = "oc_no_card"

        async def run():
            composer._loop = asyncio.get_running_loop()
            result = await type(stub).send_or_update_status(
                stub, chat, "lifecycle", _INCIDENT_LINE, metadata=None)
            await _settle()
            assert result == "native"  # 原生文本气泡（用户立即可见）
            assert native and native[0][0] == chat and _INCIDENT_LINE in native[0][1]
            assert composer.active_state(chat) is None  # 永不建卡（幽灵卡根因闭合）
            assert not composer._states

        asyncio.get_event_loop().run_until_complete(run())

    def test_status_composer_failure_falls_back_native(self, composer, monkeypatch):
        stub, native = self._stub(composer, monkeypatch)

        async def _boom(*a, **k):
            raise RuntimeError("composer down")

        monkeypatch.setattr(type(composer), "handle_send", _boom)

        async def run():
            result = await type(stub).send_or_update_status(
                stub, "oc_f", "lifecycle", "⏱️ x", metadata=None)
            await _settle()
            assert result == "native"
            assert len(native) == 1

        asyncio.get_event_loop().run_until_complete(run())


class TestFakeClientCompat:
    def test_fake_client_shape(self):
        """_FakeClient 与 _seed_active 复用自 v208 模块——冒烟防漂移."""
        client = _FakeClient()
        assert hasattr(client, "cardkit_create")
