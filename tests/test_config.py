"""config.py 测试 — 配置加载、footer 字段容错、平台配置优先级、_reload_cached TTL 缓存."""

from __future__ import annotations

import os
import time
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


class TestEnabled:
    def test_enabled_true(self) -> None:
        cfg = _make_config({"hermes_lark_streaming": {"enabled": True}})
        assert cfg.enabled is True

    def test_enabled_false(self) -> None:
        cfg = _make_config({"hermes_lark_streaming": {"enabled": False}})
        assert cfg.enabled is False

    def test_enabled_missing(self) -> None:
        # v1.3.0: enabled defaults to True (no longer needs explicit config)
        cfg = _make_config({"hermes_lark_streaming": {}})
        assert cfg.enabled is True

    def test_no_hermes_lark_streaming_section(self) -> None:
        cfg = _make_config({})
        assert cfg.enabled is True

    def test_hermes_lark_streaming_section_not_dict(self) -> None:
        cfg = _make_config({"hermes_lark_streaming": "invalid"})
        assert cfg.enabled is True


class TestFooterFields:
    _DEFAULT_FIELDS = [["status", "elapsed", "model", "cost", "compression_exhausted"]]

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


class TestFeishuBaseURL:
    def test_default_url(self) -> None:
        cfg = _make_config({"feishu": {"app_id": "id", "app_secret": "s"}})
        with patch.dict(os.environ, {}, clear=True):
            assert cfg.feishu_base_url == "https://open.feishu.cn/open-apis"

    def test_custom_url_from_config(self) -> None:
        cfg = _make_config({"feishu": {"app_id": "id", "app_secret": "s", "base_url": "https://custom.com"}})
        with patch.dict(os.environ, {}, clear=True):
            assert cfg.feishu_base_url == "https://custom.com"

    def test_from_env(self) -> None:
        cfg = _make_config({})
        with patch.dict(
            os.environ, {"FEISHU_APP_ID": "id", "FEISHU_APP_SECRET": "s", "FEISHU_BASE_URL": "https://env.com"}
        ):
            assert cfg.feishu_base_url == "https://env.com"


class TestShowReasoning:
    def _make_reasoning_config(self, raw: dict[str, Any]) -> Config:
        """Create a Config with _reload_cached mocked to return given raw dict."""
        cfg = Config()
        cfg._reload_cached = lambda: raw  # type: ignore[assignment]
        return cfg

    def test_platform_level_true(self) -> None:
        cfg = self._make_reasoning_config({"display": {"platforms": {"feishu": {"show_reasoning": True}}}})
        assert cfg.show_reasoning is True

    def test_platform_level_false(self) -> None:
        cfg = self._make_reasoning_config({"display": {"platforms": {"feishu": {"show_reasoning": False}}}})
        assert cfg.show_reasoning is False

    def test_global_fallback_true(self) -> None:
        cfg = self._make_reasoning_config({"display": {"show_reasoning": True}})
        assert cfg.show_reasoning is True

    def test_global_fallback_false(self) -> None:
        cfg = self._make_reasoning_config({"display": {"show_reasoning": False}})
        assert cfg.show_reasoning is False

    def test_default_false(self) -> None:
        cfg = self._make_reasoning_config({})
        assert cfg.show_reasoning is False

    def test_display_not_dict(self) -> None:
        cfg = self._make_reasoning_config({"display": "invalid"})
        assert cfg.show_reasoning is False

    def test_platforms_not_dict(self) -> None:
        cfg = self._make_reasoning_config({"display": {"platforms": "invalid"}})
        assert cfg.show_reasoning is False

    def test_feishu_section_missing_key(self) -> None:
        cfg = self._make_reasoning_config({"display": {"platforms": {"feishu": {"other": True}}}})
        assert cfg.show_reasoning is False

    def test_platform_takes_priority_over_global(self) -> None:
        cfg = self._make_reasoning_config({
            "display": {
                "platforms": {"feishu": {"show_reasoning": False}},
                "show_reasoning": True,
            }
        })
        assert cfg.show_reasoning is False

    def test_no_display_section(self) -> None:
        cfg = self._make_reasoning_config({"hermes_lark_streaming": {"enabled": True}})
        assert cfg.show_reasoning is False


class TestPlatformCfg:
    def test_env_takes_priority(self) -> None:
        cfg = _make_config({"feishu": {"app_id": "config_id", "app_secret": "config_secret"}})
        with patch.dict(os.environ, {"FEISHU_APP_ID": "env_id", "FEISHU_APP_SECRET": "env_secret"}):
            result = cfg._platform_cfg()
            assert result["app_id"] == "env_id"

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
class TestLinear:
    def test_linear_true(self) -> None:
        cfg = _make_config({"hermes_lark_streaming": {"linear": True}})
        assert cfg.linear is True

    def test_linear_false(self) -> None:
        cfg = _make_config({"hermes_lark_streaming": {"linear": False}})
        assert cfg.linear is False

    def test_linear_missing_defaults_true(self) -> None:
        cfg = _make_config({"hermes_lark_streaming": {}})
        assert cfg.linear is True


class TestPanelExpanded:
    def test_panel_expanded_true(self) -> None:
        cfg = _make_config({"hermes_lark_streaming": {"panel_expanded": True}})
        assert cfg.panel_expanded is True

    def test_panel_expanded_false(self) -> None:
        cfg = _make_config({"hermes_lark_streaming": {"panel_expanded": False}})
        assert cfg.panel_expanded is False

    def test_panel_expanded_missing_defaults_false(self) -> None:
        cfg = _make_config({"hermes_lark_streaming": {}})
        assert cfg.panel_expanded is False


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


    def test_flush_interval_ms_default(self) -> None:
        """flush_interval_ms 默认 200ms (v1.3.1: 恢复 v1.2.1 P1-01 的默认值)."""
        cfg = Config()
        assert cfg.flush_interval_ms == 200.0

    def test_flush_interval_sec_default(self) -> None:
        """flush_interval_sec 默认 0.2 秒 (v1.3.1)."""
        cfg = Config()
        assert cfg.flush_interval_sec == 0.2

    def test_flush_interval_ms_custom(self, tmp_path: object) -> None:
        """flush_interval_ms 可配置."""
        import yaml
        from pathlib import Path
        config_path = Path(str(tmp_path)) / "config.yaml"
        config_path.write_text(yaml.dump({
            "hermes_lark_streaming": {"enabled": True, "flush_interval_ms": 300},
        }))
        # Override config path
        from unittest.mock import patch
        with patch("hermes_lark_streaming.config.reader._get_hermes_config_path", return_value=config_path):
            cfg = Config()
            assert cfg.flush_interval_ms == 300.0
            assert cfg.flush_interval_sec == 0.3

    def test_flush_interval_ms_clamped(self, tmp_path: object) -> None:
        """flush_interval_ms 限制在 70~2000ms（70ms = 飞书官方 print_frequency_ms 默认值）."""
        import yaml
        from pathlib import Path
        from unittest.mock import patch
        config_path = Path(str(tmp_path)) / "config.yaml"
        config_path.write_text(yaml.dump({
            "hermes_lark_streaming": {"enabled": True, "flush_interval_ms": 30},
        }))
        with patch("hermes_lark_streaming.config.reader._get_hermes_config_path", return_value=config_path):
            cfg = Config()
            assert cfg.flush_interval_ms == 70.0  # clamped to min (≈ official print_frequency_ms)

        config_path.write_text(yaml.dump({
            "hermes_lark_streaming": {"enabled": True, "flush_interval_ms": 5000},
        }))
        with patch("hermes_lark_streaming.config.reader._get_hermes_config_path", return_value=config_path):
            # v1.2.0: Config 单例后，需 reload 清缓存才能读新配置
            cfg.reload()
            cfg2 = Config()  # 单例，与 cfg 同一实例
            assert cfg2.flush_interval_ms == 2000.0  # clamped to max


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
        走 _plugin_sec() 的属性（如 enabled）不生效。
        单例后所有 Config() 共享实例，reload 全局生效。
        """
        import yaml
        from pathlib import Path
        from unittest.mock import patch
        from hermes_lark_streaming.config.reader import Config

        config_path = Path(str(tmp_path)) / "config.yaml"
        config_path.write_text(yaml.dump({
            "hermes_lark_streaming": {"enabled": False},
        }))

        with patch("hermes_lark_streaming.config.reader._get_hermes_config_path", return_value=config_path):
            # controller 持有的实例（模拟 StreamCardController.__init__）
            ctrl_cfg = Config()
            assert ctrl_cfg.enabled is False

        # 改配置文件
        config_path.write_text(yaml.dump({
            "hermes_lark_streaming": {"enabled": True},
        }))

        # 模拟 /aowen config reload（aowen 新建 Config 调 reload）
        with patch("hermes_lark_streaming.config.reader._get_hermes_config_path", return_value=config_path):
            reload_cfg = Config()  # 单例：与 ctrl_cfg 同一实例
            reload_cfg.reload()

            # controller 持有的实例现在应读到新值
            assert ctrl_cfg.enabled is True, \
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


# ── v1.5.0: config backward compat (stale header.enabled) ──


class TestConfigBackwardCompatV150:
    """v1.5.0: header.enabled deleted from config. Stale value in config.yaml
    must be safely ignored (not crash)."""

    def test_stale_header_enabled_ignored(self, tmp_path: object) -> None:
        """Config with stale header.enabled should not crash on access."""
        cfg = _make_config({
            "hermes_lark_streaming": {
                "enabled": True,
                "linear": True,
                "header": {"enabled": True},  # stale, should be ignored
            }
        })
        # Accessing deleted header_enabled should raise AttributeError (safe)
        with pytest.raises(AttributeError):
            _ = cfg.header_enabled
        # But enabled/linear should work fine
        assert cfg.enabled is True
        assert cfg.linear is True
