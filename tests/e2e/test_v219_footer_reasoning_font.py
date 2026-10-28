"""v2.0.19 回归测试 — footer reasoning 等级字段 + 卡片自定义字号（card_style.text_size）.

用户三需求实施（本轮只测 1b 与 3；需求 1a 七字段架构不可达按用户原则放弃、
需求 2 复制为平台能力零代码）：

  1b footer reasoning：display.show_reasoning: true 且 agent.reasoning_effort
     有效时，页脚 reasoning 字段显示等级（默认字段自动追加；用户显式配置
     fields 时不干预）。等级值经官方解析器 hermes_constants.resolve_reasoning_config
     （测试以 fake 模块替身复刻官方 parse 行为，测的是接线与渲染）。
  3  字号（v2.0.20 完全 opt-in）：默认（未配置 card_style）零注入——config 无
     style 段、元素字号用 v2.0.18 字面值（answer=normal_v2、aux=notation），
     payload 与 v2.0.18 逐字段一致（用户反馈手机端观感变大，要求默认恢复）；
     配置 card_style.text_size 后注入 config.style.text_size（cus-ans/cus-aux，
     s157/s157b 真飞书探针定案 CardKit 接受），未写的组/档位回落 v2.0.18
     字面值枚举，只改用户写的档位。
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

import pytest
from hermes_lark_streaming.cardkit.cards import build_streaming_card_v2
from hermes_lark_streaming.cardkit.elements import (
    ANSWER_ELEMENT_ID,
    ANSWER_TEXT_SIZE_FALLBACK,
    ANSWER_TEXT_SIZE_TOKEN,
    AUX_TEXT_SIZE_FALLBACK,
    AUX_TEXT_SIZE_TOKEN,
    _build_footer_elements,
    _render_footer_field,
)
from hermes_lark_streaming.cardkit.special import build_gateway_card
from hermes_lark_streaming.composer.engine import FrameComposer
from hermes_lark_streaming.config import Config

from .test_v213_native_streaming import _FullClient

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ---------------------------------------------------------------------------
# 测试替身：hermes_constants（官方 parse_reasoning_effort 行为复刻，测接线）
# ---------------------------------------------------------------------------


def _install_fake_hermes_constants(monkeypatch: pytest.MonkeyPatch) -> None:
    """注入官方 parse/resolve 行为复刻的 hermes_constants 替身模块."""

    def parse_reasoning_effort(effort: Any) -> dict | None:
        if effort is None or effort is True:
            return None
        if isinstance(effort, dict):
            if effort.get("enabled", True) is False:
                return {"enabled": False}
            level = str(effort.get("effort") or "").strip()
            return {"enabled": True, "effort": level} if level else None
        effort = str(effort).strip().lower()
        if effort in {"none", "false", "disabled"}:
            return {"enabled": False}
        if effort in ("minimal", "low", "medium", "high", "xhigh", "max", "ultra"):
            return {"enabled": True, "effort": effort}
        return None

    def resolve_reasoning_config(cfg: dict | None, model: str = "") -> dict | None:
        cfg = cfg if isinstance(cfg, dict) else {}
        agent_cfg = cfg.get("agent") if isinstance(cfg.get("agent"), dict) else {}
        return parse_reasoning_effort(agent_cfg.get("reasoning_effort", ""))

    fake = ModuleType("hermes_constants")
    fake.parse_reasoning_effort = parse_reasoning_effort  # type: ignore[attr-defined]
    fake.resolve_reasoning_config = resolve_reasoning_config  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hermes_constants", fake)


def _make_config(raw: dict[str, Any]) -> Config:
    cfg = Config()
    cfg._raw = raw
    return cfg


def _set_raw(monkeypatch: pytest.MonkeyPatch, raw: dict[str, Any]) -> None:
    """显式设置 Config 单例 _raw（monkeypatch 结束自动还原，隔离用例间污染）.

    _make_config 直接赋值会跨用例残留（card_style 配置尤其危险——会让后续
    默认路径卡构建意外注入 style）；卡构建类测试一律走本助手。"""
    monkeypatch.setattr(Config(), "_raw", raw, raising=False)


def _reasoning_raw(show_reasoning: Any, effort: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {"hermes_lark_streaming": {}}
    if show_reasoning is not None:
        raw["display"] = {"show_reasoning": show_reasoning}
    if effort is not None:
        raw["agent"] = {"reasoning_effort": effort}
    return raw


# ---------------------------------------------------------------------------
# 1. Config.footer_reasoning_level（数据层）
# ---------------------------------------------------------------------------


class TestFooterReasoningLevel:
    def test_no_display_section(self, monkeypatch):
        _install_fake_hermes_constants(monkeypatch)
        cfg = _make_config({"hermes_lark_streaming": {}})
        assert cfg.footer_reasoning_level == ""

    def test_show_reasoning_false(self, monkeypatch):
        _install_fake_hermes_constants(monkeypatch)
        cfg = _make_config(_reasoning_raw(False, "high"))
        assert cfg.footer_reasoning_level == ""

    def test_show_reasoning_true_effort_high(self, monkeypatch):
        _install_fake_hermes_constants(monkeypatch)
        cfg = _make_config(_reasoning_raw(True, "high"))
        assert cfg.footer_reasoning_level == "high"

    def test_show_reasoning_true_effort_disabled(self, monkeypatch):
        _install_fake_hermes_constants(monkeypatch)
        cfg = _make_config(_reasoning_raw(True, False))
        assert cfg.footer_reasoning_level == ""

    def test_show_reasoning_true_effort_dict(self, monkeypatch):
        _install_fake_hermes_constants(monkeypatch)
        cfg = _make_config(_reasoning_raw(True, {"enabled": True, "effort": "xhigh"}))
        assert cfg.footer_reasoning_level == "xhigh"

    def test_show_reasoning_true_effort_dict_disabled(self, monkeypatch):
        _install_fake_hermes_constants(monkeypatch)
        cfg = _make_config(_reasoning_raw(True, {"enabled": False}))
        assert cfg.footer_reasoning_level == ""

    def test_show_reasoning_true_effort_unset(self, monkeypatch):
        _install_fake_hermes_constants(monkeypatch)
        cfg = _make_config(_reasoning_raw(True, None))
        assert cfg.footer_reasoning_level == ""

    def test_no_hermes_constants_module(self, monkeypatch):
        """无 hermes_constants（非 hermes 环境）→ ""（import 失败静默）."""
        monkeypatch.setitem(sys.modules, "hermes_constants", None)  # 强制 ImportError
        cfg = _make_config(_reasoning_raw(True, "high"))
        assert cfg.footer_reasoning_level == ""


class TestDefaultFooterFieldsAppend:
    def test_default_fields_without_reasoning(self, monkeypatch):
        _install_fake_hermes_constants(monkeypatch)
        cfg = _make_config({"hermes_lark_streaming": {}})
        # v2.0.21（审计 R10）：cost/compression_exhausted 数据不可达，默认表不再收录
        assert cfg.footer_fields == [["status", "elapsed", "model"]]

    def test_default_fields_append_reasoning(self, monkeypatch):
        _install_fake_hermes_constants(monkeypatch)
        cfg = _make_config(_reasoning_raw(True, "medium"))
        assert cfg.footer_fields == [["status", "elapsed", "model", "reasoning"]]

    def test_explicit_fields_not_touched(self, monkeypatch):
        """用户显式配置 fields → 不自动追加（自行控制位置）."""
        _install_fake_hermes_constants(monkeypatch)
        cfg = _make_config({
            "hermes_lark_streaming": {"footer": {"fields": [["status", "reasoning"]]}},
            "display": {"show_reasoning": True},
            "agent": {"reasoning_effort": "high"},
        })
        assert cfg.footer_fields == [["status", "reasoning"]]

    def test_explicit_flat_row_wrapped(self, monkeypatch):
        """单层列表（V1 兼容语法）包装成行，reasoning 可直接写入."""
        _install_fake_hermes_constants(monkeypatch)
        cfg = _make_config({
            "hermes_lark_streaming": {"footer": {"fields": ["status", "elapsed"]}},
            "display": {"show_reasoning": True},
            "agent": {"reasoning_effort": "low"},
        })
        assert cfg.footer_fields == [["status", "elapsed"]]


# ---------------------------------------------------------------------------
# 2. 渲染层：_render_footer_field reasoning 分支
# ---------------------------------------------------------------------------


class TestFooterReasoningRender:
    def test_known_level(self):
        en, zh = _render_footer_field("reasoning", {"reasoning_effort": "high"}, False, False, False)
        assert (en, zh) == ("high", "高")

    def test_unknown_level_passes_through(self):
        en, zh = _render_footer_field("reasoning", {"reasoning_effort": "thinking"}, False, False, False)
        assert (en, zh) == ("thinking", "thinking")

    def test_empty_value_skipped(self):
        en, zh = _render_footer_field("reasoning", {"reasoning_effort": ""}, False, False, False)
        assert (en, zh) == (None, None)
        en, zh = _render_footer_field("reasoning", {}, False, False, False)
        assert (en, zh) == (None, None)

    def test_show_label(self):
        en, zh = _render_footer_field("reasoning", {"reasoning_effort": "high"}, False, False, True)
        assert (en, zh) == ("Reasoning high", "思考 高")

    def test_build_footer_elements_with_reasoning(self):
        elements = _build_footer_elements(
            {"duration": 3.2, "model": "deepseek-v4", "reasoning_effort": "medium"},
            fields=[["status", "elapsed", "model", "reasoning"]],
        )
        assert elements, "页脚非空"
        md = elements[-1]
        assert md["tag"] == "markdown"
        assert "Completed" in md["content"]
        assert "medium" in md["content"]  # en 行含等级原值
        assert "中" in md["i18n_content"]["zh_cn"]  # zh 行含映射

    def test_build_footer_elements_without_reasoning_value(self):
        """reasoning 无值 → 行内自动省略（仅在有值时显示）."""
        elements = _build_footer_elements(
            {"duration": 3.2, "model": "m"},
            fields=[["status", "model", "reasoning"]],
        )
        md = elements[-1]
        assert "medium" not in md["content"]
        assert "m" in md["content"]


# ---------------------------------------------------------------------------
# 3. Config.card_text_sizes（配置层）—— v2.0.20 完全 opt-in
# ---------------------------------------------------------------------------


class TestCardTextSizes:
    def test_default_is_optin_noop(self):
        """未配置 card_style → {}（v2.0.20 用户反馈：默认观感不变，零注入）."""
        assert _make_config({}).card_text_sizes == {}
        assert _make_config({"hermes_lark_streaming": {}}).card_text_sizes == {}

    def test_empty_or_malformed_style_is_noop(self):
        cfg = _make_config({"hermes_lark_streaming": {"card_style": "oops", "text_size": None}})
        assert cfg.card_text_sizes == {}
        cfg2 = _make_config({"hermes_lark_streaming": {"card_style": {"text_size": {}}}})
        assert cfg2.card_text_sizes == {}

    def test_partial_override_fills_with_v218_fallbacks(self):
        """只写 answer.mobile → 未写的组/档位回落 v2.0.18 字面值（观感不动）."""
        cfg = _make_config({"hermes_lark_streaming": {"card_style": {"text_size": {
            "answer": {"mobile": "x-large"},
        }}}})
        sizes = cfg.card_text_sizes
        assert sizes["answer"] == {"default": "normal_v2", "pc": "normal_v2", "mobile": "x-large"}
        assert sizes["aux"] == {"default": "notation", "pc": "notation", "mobile": "notation"}

    def test_aux_only_override(self):
        cfg = _make_config({"hermes_lark_streaming": {"card_style": {"text_size": {
            "aux": {"pc": "small"},
        }}}})
        sizes = cfg.card_text_sizes
        assert sizes["answer"] == {"default": "normal_v2", "pc": "normal_v2", "mobile": "normal_v2"}
        assert sizes["aux"]["pc"] == "small"

    def test_malformed_group_falls_back_and_coerces(self):
        cfg = _make_config({"hermes_lark_streaming": {"card_style": {"text_size": {
            "answer": "not-a-dict", "aux": {"pc": 18},
        }}}})
        sizes = cfg.card_text_sizes
        assert sizes["answer"] == {"default": "normal_v2", "pc": "normal_v2", "mobile": "normal_v2"}
        assert sizes["aux"]["pc"] == "18"


# ---------------------------------------------------------------------------
# 4. 骨架卡/网关卡 style 注入与 token 引用（v2.0.20 opt-in：默认零注入）
# ---------------------------------------------------------------------------


def _collect_text_sizes(card: dict[str, Any]) -> list[str]:
    sizes: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("text_size") is not None:
                sizes.append(node["text_size"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(card["body"]["elements"])
    return sizes


class TestCardStyleInjection:
    def test_default_payload_matches_v218(self, monkeypatch):
        """默认（未配置）：config 无 style 段、answer=normal_v2 字面值（v2.0.18 原样）."""
        _set_raw(monkeypatch, {})
        card = build_streaming_card_v2(answer_text="hi")
        assert "style" not in card["config"]
        answer = next(e for e in card["body"]["elements"]
                      if e.get("element_id") == ANSWER_ELEMENT_ID)
        assert answer["text_size"] == ANSWER_TEXT_SIZE_FALLBACK == "normal_v2"

    def test_configured_injects_style_and_tokens(self, monkeypatch):
        _set_raw(monkeypatch, {"hermes_lark_streaming": {"card_style": {"text_size": {
            "answer": {"default": "normal_v2", "pc": "large", "mobile": "x-large"},
        }}}})
        card = build_streaming_card_v2(answer_text="hi")
        style = card["config"]["style"]["text_size"]
        assert style["cus-ans"] == {"default": "normal_v2", "pc": "large", "mobile": "x-large"}
        assert style["cus-aux"] == {"default": "notation", "pc": "notation", "mobile": "notation"}
        answer = next(e for e in card["body"]["elements"]
                      if e.get("element_id") == ANSWER_ELEMENT_ID)
        assert answer["text_size"] == ANSWER_TEXT_SIZE_TOKEN == "cus-ans"

    def test_gateway_card_default_then_configured(self, monkeypatch):
        _set_raw(monkeypatch, {})
        card = build_gateway_card("done", status_label="完成", status_emoji="✅")
        assert "style" not in card["config"]
        assert card["body"]["elements"][0]["text"]["text_size"] == "notation"
        _set_raw(monkeypatch, {"hermes_lark_streaming": {"card_style": {"text_size": {
            "aux": {"mobile": "small"},
        }}}})
        card2 = build_gateway_card("done", status_label="完成", status_emoji="✅")
        assert card2["config"]["style"]["text_size"]["cus-aux"]["mobile"] == "small"
        assert card2["body"]["elements"][0]["text"]["text_size"] == AUX_TEXT_SIZE_TOKEN == "cus-aux"

    def test_panel_rows_default_use_v218_literals(self, monkeypatch):
        _set_raw(monkeypatch, {})
        card = build_streaming_card_v2(
            status_lines=["💬 思考中…", "🖥️ terminal: ls"],
            show_streaming_element=False,
        )
        sizes = _collect_text_sizes(card)
        assert sizes, "面板应有带字号的行"
        assert set(sizes) <= {"normal_v2", "notation"}, \
            f"默认只允许 v2.0.18 字面值，实际: {set(sizes)}"
        assert ANSWER_TEXT_SIZE_FALLBACK in set(sizes) or AUX_TEXT_SIZE_FALLBACK in set(sizes)

    def test_panel_rows_configured_use_tokens(self, monkeypatch):
        _set_raw(monkeypatch, {"hermes_lark_streaming": {"card_style": {"text_size": {
            "answer": {"mobile": "x-large"},
        }}}})
        card = build_streaming_card_v2(
            status_lines=["💬 思考中…", "🖥️ terminal: ls"],
            show_streaming_element=False,
        )
        sizes = _collect_text_sizes(card)
        assert sizes, "面板应有带字号的行"
        assert set(sizes) <= {ANSWER_TEXT_SIZE_TOKEN, AUX_TEXT_SIZE_TOKEN}, \
            f"配置后应全量引用 token，实际: {set(sizes)}"


# ---------------------------------------------------------------------------
# 5. finalize 页脚 reasoning 接线（composer 全链路）
# ---------------------------------------------------------------------------


class TestFooterReasoningSeal:
    async def test_seal_footer_includes_reasoning(self, monkeypatch):
        _install_fake_hermes_constants(monkeypatch)
        cfg = Config()
        monkeypatch.setattr(cfg, "_raw", _reasoning_raw(True, "high"), raising=False)
        client = _FullClient()
        # config 注入真 Config 单例（同生产路径）：engine._footer_fields() 读到
        # 含 reasoning 的默认字段，_seal_footer 的 footer_data 同源供值。
        comp = FrameComposer(
            get_client=lambda: client,
            build_card=lambda **kw: build_streaming_card_v2(**kw),
            build_final_card=lambda **kw: {},
            config=cfg,
        )
        chat = "oc_v219_reason"
        await comp.handle_stream_frame(chat, "", finalize=False)
        st = comp.active_state(chat)
        st.answer_text = "答案"
        await comp._finalize(st, "答案")
        footer_text = "\n".join(
            str(el.get("content", ""))
            for actions in client.batch_actions
            for action in actions
            if action.get("action") == "add_elements"
            for el in action.get("params", {}).get("elements", [])
        )
        assert "high" in footer_text, "页脚应含 reasoning 等级"

    async def test_seal_footer_without_reasoning(self, monkeypatch):
        _install_fake_hermes_constants(monkeypatch)
        cfg = Config()
        monkeypatch.setattr(cfg, "_raw", {"hermes_lark_streaming": {}}, raising=False)
        client = _FullClient()
        comp = FrameComposer(
            get_client=lambda: client,
            build_card=lambda **kw: build_streaming_card_v2(**kw),
            build_final_card=lambda **kw: {},
            config=cfg,
        )
        chat = "oc_v219_noreason"
        await comp.handle_stream_frame(chat, "", finalize=False)
        st = comp.active_state(chat)
        st.answer_text = "答案"
        await comp._finalize(st, "答案")
        footer_text = "\n".join(
            str(el.get("content", ""))
            for actions in client.batch_actions
            for action in actions
            if action.get("action") == "add_elements"
            for el in action.get("params", {}).get("elements", [])
        )
        assert "high" not in footer_text
        assert footer_text, "页脚仍应正常 seal"
