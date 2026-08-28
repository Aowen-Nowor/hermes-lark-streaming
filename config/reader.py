"""读取 Hermes 配置. 刷新: /aowen config reload 或重启网关. 不做 mtime 检测."""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from pathlib import Path
from typing import Any

import yaml

_logger = logging.getLogger("hermes_lark_streaming")

def _get_hermes_config_path() -> Path:
    """动态获取 Hermes 配置文件路径."""
    return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "config.yaml"

_RELOAD_CACHE_TTL = 60.0  # 运行时可变配置缓存 TTL.

def _to_bool(val: Any, default: bool = False) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ("true", "1", "yes", "on")
    if isinstance(val, (int, float)):
        return val != 0
    return default

def _to_int(val: Any, default: int) -> int:
    if isinstance(val, bool):
        return int(val)
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        # int(float('inf')/'nan') raises OverflowError/ValueError.
        try:
            return int(val)
        except (OverflowError, ValueError):
            _logger.warning("HLS: config float value %r cannot convert to int, using default %d", val, default)
            return default
    if isinstance(val, str):
        try:
            return int(val)
        except ValueError:
            _logger.warning("HLS: config value %r is not a valid int, using default %d", val, default)
            return default
    return default

def _to_float(val: Any, default: float) -> float:
    """安全 float 转换, 拒绝 nan/inf (NaN 破坏 max/min 比较节流逻辑)."""
    if isinstance(val, bool):
        return float(val)
    if isinstance(val, (int, float)):
        result = float(val)
        if math.isnan(result) or math.isinf(result):
            _logger.warning("HLS: config float value %r is nan/inf, using default %f", val, default)
            return default
        return result
    if isinstance(val, str):
        try:
            result = float(val)
        except ValueError:
            _logger.warning("HLS: config value %r is not a valid float, using default %f", val, default)
            return default
        if math.isnan(result) or math.isinf(result):
            _logger.warning("HLS: config float value %r is nan/inf, using default %f", val, default)
            return default
        return result
    return default

class Config:
    """插件配置, 惰性读取. 单例模式: reload() 才能清掉 controller 持有实例缓存."""

    _instance: "Config | None" = None

    def __new__(cls) -> "Config":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        self._raw: dict[str, Any] | None = None
        self._reload_cache: dict[str, Any] | None = None
        self._reload_cache_at: float = 0.0
        # _lock: Config singleton shared across event-loop + worker threads.
        self._lock = threading.Lock()
        self._initialized = True

    def reload(self) -> None:
        """Force reload from disk. Called by /aowen config reload."""
        with self._lock:
            self._raw = None
            self._reload_cache = None
            self._reload_cache_at = 0.0
        _logger.info("HLS: config reload triggered — caches cleared")

    @property
    def enabled(self) -> bool:
        """默认 True."""
        sec = self._plugin_sec()
        return _to_bool(sec.get("enabled", True), default=True)

    @property
    def linear(self) -> bool:
        """默认 True."""
        sec = self._plugin_sec()
        return _to_bool(sec.get("linear", True), default=True)

    @property
    def panel_expanded(self) -> bool:
        sec = self._plugin_sec()
        return _to_bool(sec.get("panel_expanded", False))

    @property
    def streaming_panel_expanded(self) -> bool:
        """与 panel_expanded 独立."""
        sec = self._plugin_sec()
        return _to_bool(sec.get("streaming_panel_expanded", False))

    @property
    def max_tool_steps(self) -> int:
        sec = self._plugin_sec()
        val = _to_int(sec.get("max_tool_steps", 20), default=20)
        return max(1, min(100, val))

    @property
    def max_reasoning_rounds(self) -> int:
        sec = self._plugin_sec()
        val = _to_int(sec.get("max_reasoning_rounds", 20), default=20)
        return max(1, min(100, val))

    @property
    def print_strategy(self) -> str:
        """"fast" 或 "delay". 默认 delay."""
        sec = self._plugin_sec()
        strategy = sec.get("print_strategy", "delay")
        return strategy if strategy in ("fast", "delay") else "delay"

    @property
    def print_step(self) -> int:
        """飞书打字机每次渲染字符数. 默认 4, 范围 1~10."""
        sec = self._plugin_sec()
        val = _to_int(sec.get("print_step", 4), default=4)
        return max(1, min(10, val))

    @property
    def flush_interval_ms(self) -> float:
        """stream_element API 节流间隔 (ms). 默认 200."""
        sec = self._plugin_sec()
        ms = _to_float(sec.get("flush_interval_ms", 200), default=200.0)
        return max(70.0, min(2000.0, ms))

    @property
    def flush_interval_sec(self) -> float:
        return self.flush_interval_ms / 1000.0

    @property
    def answer_flush_interval_ms(self) -> float:
        """answer-only 流式刷新间隔 (ms). 默认 150.

        v1.7.0 (R2-09): previously hardcoded as _ANSWER_FAST_STREAM_MS=150 in
        controller/linear_mixin.py with no way to tune it apart from the
        general flush_interval_ms (which also gates panel updates)."""
        sec = self._plugin_sec()
        ms = _to_float(sec.get("answer_flush_interval_ms", 150), default=150.0)
        return max(70.0, min(2000.0, ms))

    @property
    def answer_flush_interval_sec(self) -> float:
        return self.answer_flush_interval_ms / 1000.0

    @property
    def show_reasoning(self) -> bool:
        """TTL 缓存读取 (/reasoning 命令运行时修改配置)."""
        display = self._reload_cached().get("display")
        if not isinstance(display, dict):
            return False
        platforms = display.get("platforms")
        if isinstance(platforms, dict):
            feishu = platforms.get("feishu")
            if isinstance(feishu, dict) and "show_reasoning" in feishu:
                return _to_bool(feishu["show_reasoning"])
        return _to_bool(display.get("show_reasoning", False))

    @property
    def feishu_app_id(self) -> str:
        return str(self._platform_cfg().get("app_id", ""))

    @property
    def feishu_app_secret(self) -> str:
        return str(self._platform_cfg().get("app_secret", ""))

    @property
    def feishu_base_url(self) -> str:
        return str(self._platform_cfg().get("base_url", "https://open.feishu.cn/open-apis"))

    @property
    def card_duration_sec(self) -> int:
        return _to_int(self._plugin_sec().get("card_ttl_sec", 600), default=600)

    @property
    def footer_fields(self) -> list[list[str]]:
        sec = self._plugin_sec()
        footer = sec.get("footer", {})
        if not isinstance(footer, dict):
            return self._default_footer_fields()
        fields = footer.get("fields")
        if not fields:
            return self._default_footer_fields()
        if not isinstance(fields, list):
            return self._default_footer_fields()
        if fields and isinstance(fields[0], str):
            return [fields]
        return fields

    @property
    def footer_show_label(self) -> bool:
        sec = self._plugin_sec()
        footer = sec.get("footer", {})
        return _to_bool(footer.get("show_label", False))

    @property
    def gateway_cards(self) -> bool:
        """默认 True. TTL 缓存读取."""
        sec = self._reload_cached().get("hermes_lark_streaming")
        if not isinstance(sec, dict):
            return True
        return _to_bool(sec.get("gateway_cards", True), default=True)

    @staticmethod
    def _default_footer_fields() -> list[list[str]]:
        return [["status", "elapsed", "model", "cost", "compression_exhausted"]]

    @property
    def env_app_id(self) -> str:
        return os.environ.get("FEISHU_APP_ID") or os.environ.get("LARK_APP_ID") or ""

    @property
    def env_app_secret(self) -> str:
        return os.environ.get("FEISHU_APP_SECRET") or os.environ.get("LARK_APP_SECRET") or ""

    def _plugin_sec(self) -> dict[str, Any]:
        raw = self._load()
        sec = raw.get("hermes_lark_streaming")
        if isinstance(sec, dict):
            return sec
        return {}

    def _platform_cfg(self) -> dict[str, Any]:
        """从环境变量或平台配置找飞书凭据."""
        if self.env_app_id and self.env_app_secret:
            base_url = (
                os.environ.get("FEISHU_BASE_URL")
                or os.environ.get("LARK_BASE_URL")
                or None
            )
            if not base_url:
                domain = os.environ.get("FEISHU_DOMAIN", "").lower()
                if domain == "lark":
                    base_url = "https://open.larksuite.com/open-apis"
                else:
                    base_url = "https://open.feishu.cn/open-apis"
            return {
                "app_id": self.env_app_id,
                "app_secret": self.env_app_secret,
                "base_url": base_url,
            }
        raw = self._load()
        for key in ("feishu", "lark"):
            pf = raw.get(key)
            if isinstance(pf, dict) and pf.get("app_id"):
                return pf
        return {}

    def _load(self) -> dict[str, Any]:
        with self._lock:
            if self._raw is not None:
                return self._raw
            config_path = _get_hermes_config_path()
            if config_path.exists():
                try:
                    text = config_path.read_text(encoding="utf-8")
                    self._raw = yaml.safe_load(text) or {}
                except yaml.YAMLError:
                    _logger.warning("HLS: config YAML syntax error in %s, using empty config", config_path)
                    self._raw = {}
                except (OSError, UnicodeDecodeError):
                    _logger.warning("HLS: config file read error in %s, using empty config", config_path)
                    self._raw = {}
            else:
                self._raw = {}
            return self._raw

    def _reload_cached(self) -> dict[str, Any]:
        """带 TTL 缓存的磁盘重读 (运行时可变配置项). 避免高频属性访问读磁盘."""
        now = time.monotonic()
        with self._lock:
            if self._reload_cache is not None and (now - self._reload_cache_at) < _RELOAD_CACHE_TTL:
                return self._reload_cache
            config_path = _get_hermes_config_path()
            if config_path.exists():
                try:
                    text = config_path.read_text(encoding="utf-8")
                    self._reload_cache = yaml.safe_load(text) or {}
                except yaml.YAMLError:
                    _logger.warning("HLS: config YAML syntax error in %s (reload), using empty config", config_path)
                    self._reload_cache = {}
                except (OSError, UnicodeDecodeError):
                    _logger.warning("HLS: config file read error in %s (reload), using empty config", config_path)
                    self._reload_cache = {}
            else:
                self._reload_cache = {}
            self._reload_cache_at = now
            return self._reload_cache
