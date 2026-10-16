"""v2.0.19 回归测试 — footer reasoning 等级字段 + 卡片自定义字号（card_style.text_size）.

用户三需求实施（本轮只测 1b 与 3；需求 1a 七字段架构不可达按用户原则放弃、
需求 2 复制为平台能力零代码）：

  1b footer reasoning：display.show_reasoning: true 且 agent.reasoning_effort
     有效时，页脚 reasoning 字段显示等级（默认字段自动追加；用户显式配置
     fields 时不干预）。等级值经官方解析器 hermes_constants.resolve_reasoning_config
     （测试以 fake 模块替身复刻官方 parse 行为，测的是接线与渲染）。
  3  字号：config.style.text_size 注入 cus-ans/cus-aux 两 token 三档值
     （s157/s157b 真飞书探针 2026-10-16 定案 CardKit 接受），元素 text_size
     全量引用 token（answer=cus-ans、辅助行=cus-aux）。默认三档与 v2.0.18
     观感对齐（answer=normal、aux=notation）。
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

import pytest
from hermes_lark_streaming.cardkit.cards import build_streaming_card_v2
from hermes_lark_streaming.cardkit.elements import (
    ANSWER_ELEMENT_ID,
    ANSWER_TEXT_SIZE_TOKEN,
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
        assert cfg.footer_fields == [["status", "elapsed", "model", "cost", "compression_exhausted"]]

    def test_default_fields_append_reasoning(self, monkeypatch):
        _install_fake_hermes_constants(monkeypatch)
        cfg = _make_config(_reasoning_raw(True, "medium"))
        assert cfg.footer_fields == [
            ["status", "elapsed", "model", "cost", "compression_exhausted", "reasoning"]]

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
# 3. Config.card_text_sizes（配置层）
# ---------------------------------------------------------------------------


class TestCardTextSizes:
    def test_defaults_align_with_pre_v219_look(self):
        sizes = _make_config({}).card_text_sizes
        assert sizes["answer"] == {"default": "normal", "pc": "normal", "mobile": "normal"}
        assert sizes["aux"] == {"default": "notation", "pc": "notation", "mobile": "notation"}

    def test_partial_override(self):
        cfg = _make_config({"hermes_lark_streaming": {"card_style": {"text_size": {
            "answer": {"mobile": "x-large"},
        }}}})
        sizes = cfg.card_text_sizes
        assert sizes["answer"] == {"default": "normal", "pc": "normal", "mobile": "x-large"}
        assert sizes["aux"]["mobile"] == "notation"

    def test_malformed_sections_fall_back(self):
        cfg = _make_config({"hermes_lark_streaming": {"card_style": "oops", "text_size": None}})
        sizes = cfg.card_text_sizes
        assert sizes["answer"]["pc"] == "normal"
        cfg2 = _make_config({"hermes_lark_streaming": {"card_style": {"text_size": {
            "answer": "not-a-dict", "aux": {"pc": 18},
        }}}})
        assert cfg2.card_text_sizes["answer"]["pc"] == "normal"
        assert cfg2.card_text_sizes["aux"]["pc"] == "18"


# ---------------------------------------------------------------------------
# 4. 骨架卡/网关卡 style 注入与 token 引用
# ---------------------------------------------------------------------------


class TestCardStyleInjection:
    def test_streaming_card_config_style(self):
        card = build_streaming_card_v2()
        style = card["config"]["style"]["text_size"]
        assert style[ANSWER_TEXT_SIZE_TOKEN] == {"default": "normal", "pc": "normal", "mobile": "normal"}
        assert style[AUX_TEXT_SIZE_TOKEN]["default"] == "notation"

    def test_streaming_card_answer_token(self):
        card = build_streaming_card_v2(answer_text="hi")
        answer = next(e for e in card["body"]["elements"]
                      if e.get("element_id") == ANSWER_ELEMENT_ID)
        assert answer["text_size"] == ANSWER_TEXT_SIZE_TOKEN == "cus-ans"

    def test_streaming_card_custom_values_passthrough(self, monkeypatch):
        cfg = Config()
        monkeypatch.setattr(cfg, "_raw", {"hermes_lark_streaming": {"card_style": {"text_size": {
            "answer": {"default": "normal", "pc": "large", "mobile": "x-large"},
            "aux": {"default": "small", "pc": "small", "mobile": "small"},
        }}}}, raising=False)
        card = build_streaming_card_v2()
        style = card["config"]["style"]["text_size"]
        assert style["cus-ans"] == {"default": "normal", "pc": "large", "mobile": "x-large"}
        assert style["cus-aux"] == {"default": "small", "pc": "small", "mobile": "small"}

    def test_gateway_card_style_and_status_token(self):
        card = build_gateway_card("done", status_label="完成", status_emoji="✅")
        assert "style" in card["config"]
        status = card["body"]["elements"][0]
        assert status["text"]["text_size"] == AUX_TEXT_SIZE_TOKEN == "cus-aux"

    def test_panel_rows_use_aux_token(self):
        card = build_streaming_card_v2(
            status_lines=["💬 思考中…", "🖥️ terminal: ls"],
            show_streaming_element=False,
        )
        texts: list[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                if node.get("text_size") is not None:
                    texts.append(node["text_size"])
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(card["body"]["elements"])
        assert texts, "面板应有带字号的行"
        assert set(texts) <= {AUX_TEXT_SIZE_TOKEN}, "面板行字号应统一 aux token"


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
