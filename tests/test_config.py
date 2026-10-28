"""config.py 测试 — 配置加载、footer 字段容错、平台配置优先级、Config 单例 reload.

v2.0.17：移除 V1 遗留死属性用例（enabled/linear/panel_expanded/
flush_interval_*/answer_flush_interval_*/feishu_base_url/show_reasoning/
gateway_cards/max_tool_steps/max_reasoning_rounds）——属性已删，
详见 Task 24 审计。
"""

from __future__ import annotations

import os

from pathlib import Path
from typing import Any

import pytest
from unittest.mock import MagicMock, patch

from hermes_lark_streaming.config import Config, _get_hermes_config_path


def _make_config(raw: dict[str, Any]) -> Config:
    """Create a Config pre-loaded with given raw dict."""
    cfg = Config()
    cfg._raw = raw
    return cfg


class TestRemovedV1Properties:
    """v2.0.17：V1 遗留死属性已移除，访问应 AttributeError（防无意回引）."""

    @pytest.mark.parametrize("prop", [
        "enabled", "linear", "panel_expanded", "flush_interval_ms",
        "flush_interval_sec", "answer_flush_interval_ms",
        "answer_flush_interval_sec", "feishu_base_url", "show_reasoning",
        "gateway_cards", "max_tool_steps", "max_reasoning_rounds",
    ])
    def test_removed_property_raises(self, prop: str) -> None:
        cfg = _make_config({"hermes_lark_streaming": {}})
        with pytest.raises(AttributeError):
            getattr(cfg, prop)


class TestFooterFields:
    # v2.0.21（审计 R10）：默认字段对齐 finalize 兜底常量——cost/
    # compression_exhausted 在 V2 契约下数据不可达（Task 26 定案），
    # 渲染层永远静默跳过，默认表不再收录。
    _DEFAULT_FIELDS = [["status", "elapsed", "model"]]

    def test_normal_2d_fields(self) -> None:
        cfg = _make_config({"hermes_lark_streaming": {"footer": {"fields": [["a", "b"], ["c"]]}}})
        assert cfg.footer_fields == [["a", "b"], ["c"]]

    def test_1d_auto_wrapped(self) -> None:
        cfg = _make_config({"hermes_lark_streaming": {"footer": {"fields": ["status", "elapsed"]}}})
        assert cfg.footer_fields == [["status", "elapsed"]]

    def test_empty_fields_returns_default(self) -> None:
        cfg = _make_config({"hermes_lark_streaming": {"footer": {"fields": []}}})
        assert cfg.footer_fields == self._DEFAULT_FIELDS

    def test_no_fields_returns_default(self) -> None:
        cfg = _make_config({"hermes_lark_streaming": {"footer": {}}})
        assert cfg.footer_fields == self._DEFAULT_FIELDS

    def test_no_footer_returns_default(self) -> None:
        cfg = _make_config({"hermes_lark_streaming": {}})
        assert cfg.footer_fields == self._DEFAULT_FIELDS

    def test_footer_not_dict_returns_default(self) -> None:
        cfg = _make_config({"hermes_lark_streaming": {"footer": "invalid"}})
        assert cfg.footer_fields == self._DEFAULT_FIELDS

    def test_no_hermes_lark_streaming_section_returns_default(self) -> None:
        cfg = _make_config({})
        assert cfg.footer_fields == self._DEFAULT_FIELDS

    def test_fields_non_list_returns_default(self) -> None:
        cfg = _make_config({"hermes_lark_streaming": {"footer": {"fields": "status"}}})
        assert cfg.footer_fields == self._DEFAULT_FIELDS

    def test_fields_int_returns_default(self) -> None:
        cfg = _make_config({"hermes_lark_streaming": {"footer": {"fields": 42}}})
        assert cfg.footer_fields == self._DEFAULT_FIELDS


class TestFooterShowLabel:
    def test_true(self) -> None:
        cfg = _make_config({"hermes_lark_streaming": {"footer": {"show_label": True}}})
        assert cfg.footer_show_label is True

    def test_false(self) -> None:
        cfg = _make_config({"hermes_lark_streaming": {"footer": {"show_label": False}}})
        assert cfg.footer_show_label is False

    def test_missing_defaults_false(self) -> None:
        cfg = _make_config({"hermes_lark_streaming": {"footer": {}}})
        assert cfg.footer_show_label is False


class TestCardDurationSec:
    def test_custom(self) -> None:
        cfg = _make_config({"hermes_lark_streaming": {"card_ttl_sec": 300}})
        assert cfg.card_duration_sec == 300

    def test_default(self) -> None:
        cfg = _make_config({"hermes_lark_streaming": {}})
        assert cfg.card_duration_sec == 600


class TestFeishuAppId:
    def test_from_env(self) -> None:
        cfg = _make_config({})
        with patch.dict(os.environ, {"FEISHU_APP_ID": "env_id", "FEISHU_APP_SECRET": "env_secret"}):
            assert cfg.feishu_app_id == "env_id"

    def test_from_config(self) -> None:
        cfg = _make_config({"feishu": {"app_id": "cfg_id", "app_secret": "cfg_secret"}})
        with patch.dict(os.environ, {}, clear=True):
            assert cfg.feishu_app_id == "cfg_id"

    def test_empty_when_missing(self) -> None:
        cfg = _make_config({})
        with patch.dict(os.environ, {}, clear=True):
            assert cfg.feishu_app_id == ""


class TestPlatformCfg:
    def test_env_takes_priority(self) -> None:
        cfg = _make_config({"feishu": {"app_id": "config_id", "app_secret": "config_secret"}})
        with patch.dict(os.environ, {"FEISHU_APP_ID": "env_id", "FEISHU_APP_SECRET": "env_secret"}):
            result = cfg._platform_cfg()
            assert result["app_id"] == "env_id"

    def test_env_cfg_no_base_url_key(self) -> None:
        """v2.0.17：V1 遗留 base_url 键已随 feishu_base_url 属性移除
        （client 的 base_url 由 adapter._domain_name 派生，不读插件配置）."""
        cfg = _make_config({})
        with patch.dict(os.environ, {"FEISHU_APP_ID": "env_id", "FEISHU_APP_SECRET": "env_secret"}):
            assert "base_url" not in cfg._platform_cfg()

    def test_lark_section_fallback(self) -> None:
        cfg = _make_config({"lark": {"app_id": "lark_id", "app_secret": "lark_secret"}})
        with patch.dict(os.environ, {}, clear=True):
            result = cfg._platform_cfg()
            assert result["app_id"] == "lark_id"

    def test_feishu_before_lark(self) -> None:
        cfg = _make_config(
            {
                "feishu": {"app_id": "feishu_id", "app_secret": "fs"},
                "lark": {"app_id": "lark_id", "app_secret": "ls"},
            }
        )
        with patch.dict(os.environ, {}, clear=True):
            result = cfg._platform_cfg()
            assert result["app_id"] == "feishu_id"

    def test_empty_when_nothing(self) -> None:
        cfg = _make_config({})
        with patch.dict(os.environ, {}, clear=True):
            assert cfg._platform_cfg() == {}


class TestStreamingPanelExpanded:
    def test_streaming_panel_expanded_true(self) -> None:
        cfg = _make_config({"hermes_lark_streaming": {"streaming_panel_expanded": True}})
        assert cfg.streaming_panel_expanded is True

    def test_streaming_panel_expanded_false(self) -> None:
        cfg = _make_config({"hermes_lark_streaming": {"streaming_panel_expanded": False}})
        assert cfg.streaming_panel_expanded is False

    def test_streaming_panel_expanded_missing_defaults_false(self) -> None:
        cfg = _make_config({"hermes_lark_streaming": {}})
        assert cfg.streaming_panel_expanded is False


class TestPrintStrategy:
    def test_print_strategy_fast(self) -> None:
        cfg = _make_config({"hermes_lark_streaming": {"print_strategy": "fast"}})
        assert cfg.print_strategy == "fast"

    def test_print_strategy_delay(self) -> None:
        cfg = _make_config({"hermes_lark_streaming": {"print_strategy": "delay"}})
        assert cfg.print_strategy == "delay"

    def test_print_strategy_missing_defaults_delay(self) -> None:
        cfg = _make_config({"hermes_lark_streaming": {}})
        assert cfg.print_strategy == "delay"

    def test_print_strategy_invalid_defaults_delay(self) -> None:
        cfg = _make_config({"hermes_lark_streaming": {"print_strategy": "invalid"}})
        assert cfg.print_strategy == "delay"


class TestGetHermesConfigPath:
    """_get_hermes_config_path() 动态路径解析测试 — 多 Profile 场景."""

    def test_default_path_when_no_env(self) -> None:
        """无 HERMES_HOME 环境变量时，使用 ~/.hermes/config.yaml."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("HERMES_HOME", None)
            path = _get_hermes_config_path()
            assert path == Path.home() / ".hermes" / "config.yaml"

    def test_custom_path_from_env(self) -> None:
        """HERMES_HOME 设置时，使用自定义路径."""
        with patch.dict(os.environ, {"HERMES_HOME": "/custom/hermes"}):
            path = _get_hermes_config_path()
            assert path == Path("/custom/hermes/config.yaml")

    def test_multi_profile_path(self) -> None:
        """多 Profile 场景：HERMES_HOME 指向 profile 目录."""
        with patch.dict(os.environ, {"HERMES_HOME": str(Path.home() / ".hermes" / "profiles" / "bailu")}):
            path = _get_hermes_config_path()
            assert path == Path.home() / ".hermes" / "profiles" / "bailu" / "config.yaml"

    def test_path_changes_with_env(self) -> None:
        """每次调用都重新读取环境变量，不同 HERMES_HOME 返回不同路径."""
        with patch.dict(os.environ, {"HERMES_HOME": "/path/a"}):
            path_a = _get_hermes_config_path()
        with patch.dict(os.environ, {"HERMES_HOME": "/path/b"}):
            path_b = _get_hermes_config_path()
        assert path_a != path_b
        assert str(path_a).startswith("/path/a")
        assert str(path_b).startswith("/path/b")


class TestConfigSingleton:
    """v1.2.0: Config 单例模式 + /aowen config reload 全局生效."""

    def test_singleton_same_instance(self) -> None:
        """Config() 多次调用返回同一实例."""
        from hermes_lark_streaming.config.reader import Config
        cfg1 = Config()
        cfg2 = Config()
        assert cfg1 is cfg2

    def test_reload_clears_cache_for_all_holders(self, tmp_path: object) -> None:
        """v1.2.0 修复: /aowen config reload 后 controller 持有的实例缓存也清掉.

        之前 Config() 每次新建实例，reload 只清新实例缓存，
        controller 持有的旧实例缓存不清 → 改配置 + reload 后
        走 _plugin_sec() 的属性（如 streaming_panel_expanded）不生效。
        单例后所有 Config() 共享实例，reload 全局生效。
        """
        import yaml
        from pathlib import Path
        from unittest.mock import patch
        from hermes_lark_streaming.config.reader import Config

        config_path = Path(str(tmp_path)) / "config.yaml"
        config_path.write_text(yaml.dump({
            "hermes_lark_streaming": {"streaming_panel_expanded": False},
        }))

        with patch("hermes_lark_streaming.config.reader._get_hermes_config_path", return_value=config_path):
            # controller 持有的实例（模拟 adapter.__init__）
            ctrl_cfg = Config()
            assert ctrl_cfg.streaming_panel_expanded is False

        # 改配置文件
        config_path.write_text(yaml.dump({
            "hermes_lark_streaming": {"streaming_panel_expanded": True},
        }))

        # 模拟 /aowen config reload（aowen 新建 Config 调 reload）
        with patch("hermes_lark_streaming.config.reader._get_hermes_config_path", return_value=config_path):
            reload_cfg = Config()  # 单例：与 ctrl_cfg 同一实例
            reload_cfg.reload()

            # controller 持有的实例现在应读到新值
            assert ctrl_cfg.streaming_panel_expanded is True, \
                "reload 后 controller 持有实例的缓存应被清除，读到新配置"


class TestPrintStep:
    """v1.3.0: print_step 配置项测试."""

    def test_default(self) -> None:
        cfg = _make_config({"hermes_lark_streaming": {}})
        assert cfg.print_step == 4

    def test_custom(self) -> None:
        cfg = _make_config({"hermes_lark_streaming": {"print_step": 2}})
        assert cfg.print_step == 2

    def test_min_clamp(self) -> None:
        cfg = _make_config({"hermes_lark_streaming": {"print_step": 0}})
        assert cfg.print_step == 1

    def test_max_clamp(self) -> None:
        cfg = _make_config({"hermes_lark_streaming": {"print_step": 99}})
        assert cfg.print_step == 10

    def test_no_section(self) -> None:
        cfg = _make_config({})
        assert cfg.print_step == 4


# ── v1.5.0 backward compat + v2.0.17 防回引 ──


class TestConfigBackwardCompatV150:
    """陈旧配置键安全忽略：header.enabled（v1.5.0 删除）与 v2.0.17 删除的
    V1 遗留键（enabled/linear）留在 config.yaml 里也不崩——属性已移除，
    访问得到 AttributeError 而非静默生效."""

    def test_stale_keys_ignored(self, tmp_path: object) -> None:
        cfg = _make_config({
            "hermes_lark_streaming": {
                "enabled": True,
                "linear": True,
                "header": {"enabled": True},  # stale since v1.5.0
            }
        })
        # 已删除属性的访问应 AttributeError（不是读到陈旧配置值）
        with pytest.raises(AttributeError):
            _ = cfg.header_enabled
        with pytest.raises(AttributeError):
            _ = cfg.enabled
        with pytest.raises(AttributeError):
            _ = cfg.linear
        # 存活属性不受 stale 键影响
        assert cfg.card_duration_sec == 600
