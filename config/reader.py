"""读取 Hermes 配置，提供本插件所需的配置项.

配置刷新方式：
- /aowen config reload 命令：用户在飞书发送命令，立即重新加载
- 重启网关：下次启动时自动加载新配置
- 不做自动 mtime 检测（避免每 token 一次 stat() 系统调用）
"""

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


_RELOAD_CACHE_TTL = 60.0  # 运行时可变配置的缓存 TTL（秒）— 仅用于 _reload_cached 路径


def _to_bool(val: Any, default: bool = False) -> bool:
    """严格 bool 转换，防止字符串 'false' 被当作 True."""
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ("true", "1", "yes", "on")
    if isinstance(val, (int, float)):
        return val != 0
    return default


def _to_int(val: Any, default: int) -> int:
    """安全 int 转换，类型错误时返回 default。

    防止用户在 config.yaml 中写非数字字符串（如 ``print_step: abc``）
    导致插件首次访问该属性时抛出未捕获 ValueError 崩溃。
    """
    if isinstance(val, bool):
        return int(val)
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        # v1.3.2 fix: int(float('inf')) raises OverflowError,
        # int(float('nan')) raises ValueError — both uncaught.
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
    """安全 float 转换，类型错误时返回 default。

    v1.3.2 fix: 拒绝 nan/inf 值。``float('nan')`` 和 ``float('inf')`` 是
    合法的 Python float，不会触发 ValueError，但会导致下游 ``max()``/
    ``min()`` 比较失效（NaN 传播特性），节流逻辑永远不触发或永远立即触发。
    """
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
    """插件配置，惰性读取 Hermes 主配置.

    配置刷新方式：
    1. /aowen config reload — 立即清缓存重新读取
    2. 重启网关 — 下次启动自动加载
    不做自动 mtime 检测。

    v1.2.0: 改为单例模式。所有 Config() 调用返回同一实例，
    /aowen config reload 调 reload() 才能清掉 controller 持有实例的缓存。
    之前 Config() 每次新建实例，reload 只清新实例缓存，controller 持有的
    旧实例缓存不清 → 改配置 + reload 后 header_enabled 等走 _plugin_sec()
    的属性不生效（必须重启网关）。
    """

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
        # v1.3.0 P1-02: removed _on_reload_callbacks / on_reload() — dead code
        # (never called by any module). v1.3.0 MEDIUM: added _lock for thread-safe
        # _load() / _reload_cached() / reload() (Config singleton is shared across
        # event-loop and worker threads).
        self._lock = threading.Lock()
        self._initialized = True

    # ── 配置刷新 ──

    def reload(self) -> None:
        """Force reload configuration from disk.

        Clears all caches. Called by /aowen config reload command.
        """
        with self._lock:
            self._raw = None
            self._reload_cache = None
            self._reload_cache_at = 0.0
        _logger.info("HLS: config reload triggered — caches cleared")

    @property
    def enabled(self) -> bool:
        """是否启用流式卡片. 默认 True（无需配置）."""
        sec = self._plugin_sec()
        return _to_bool(sec.get("enabled", True), default=True)

    @property
    def linear(self) -> bool:
        """线性单卡模式. 默认 True（无需配置，v1.1.0 起唯一模式）."""
        sec = self._plugin_sec()
        return _to_bool(sec.get("linear", True), default=True)

    @property
    def panel_expanded(self) -> bool:
        """完成态卡片中面板（工具、推理）是否保持展开."""
        sec = self._plugin_sec()
        return _to_bool(sec.get("panel_expanded", False))

    @property
    def streaming_panel_expanded(self) -> bool:
        """流式态卡片中面板（工具、推理）是否保持展开.

        默认 False（保持现有行为：流式态面板收起）。
        与 panel_expanded（完成态面板）独立配置。
        """
        sec = self._plugin_sec()
        return _to_bool(sec.get("streaming_panel_expanded", False))

    @property
    def max_tool_steps(self) -> int:
        """统一面板中最多显示的工具步骤数.

        超出此数量的早期工具步骤会被折叠为一行提示。
        飞书卡片2.0元素上限200，每个工具步骤最多占7个元素（标题3+详情2+结果2）。
        默认20，确保即使在极端情况下也不会超限。
        """
        sec = self._plugin_sec()
        val = _to_int(sec.get("max_tool_steps", 20), default=20)
        return max(1, min(100, val))

    @property
    def max_reasoning_rounds(self) -> int:
        """统一面板中最多显示的推理轮次数.

        超出此数量的早期推理轮次会被折叠为一行提示。
        飞书卡片2.0元素上限200，每个推理轮次最多占4个元素（标题3+文本1）。
        默认20，确保即使在极端情况下也不会超限。
        """
        sec = self._plugin_sec()
        val = _to_int(sec.get("max_reasoning_rounds", 20), default=20)
        return max(1, min(100, val))

    @property
    def print_strategy(self) -> str:
        """流式卡片上屏策略: "fast" 或 "delay".

        - fast: 新内容到达时，未上屏的旧内容立即全部上屏，然后开始新内容上屏（默认）
        - delay: 未上屏的旧内容继续按打字机效果输出，全部完成后才开始新内容上屏（更丝滑）

        默认 "delay"（更丝滑的阅读体验）。
        """
        sec = self._plugin_sec()
        strategy = sec.get("print_strategy", "delay")
        return strategy if strategy in ("fast", "delay") else "delay"

    @property
    def print_step(self) -> int:
        """飞书打字机每次渲染的字符数（print_step）.

        飞书 CardKit 流式更新参数：每次 70ms 渲染 N 个字符。
        默认 4（每次渲染4字符，速度是 step=1 的4倍）。
        范围 1~10：太小打字机太慢，太大失去打字机效果。
        需飞书 7.23+ 客户端支持自定义参数。
        """
        sec = self._plugin_sec()
        val = _to_int(sec.get("print_step", 4), default=4)
        return max(1, min(10, val))

    @property
    def flush_interval_ms(self) -> float:
        """插件调用 stream_element API 的节流间隔（毫秒）.

        这是插件侧的发送频率，不是飞书打字机速度。
        默认 200ms（v1.2.1 P1-01：生产日志显示 100ms 与 200ms 打字机效果无差别，
        但 200ms 可将 API 调用量减半）。
        """
        sec = self._plugin_sec()
        ms = _to_float(sec.get("flush_interval_ms", 200), default=200.0)
        return max(70.0, min(2000.0, ms))

    @property
    def flush_interval_sec(self) -> float:
        """流式卡片刷新间隔（秒），可配置.

        默认 0.2 秒（200ms）。降低此值使打字效果更流畅但增加API调用量和客户端负担；
        提高此值减少API调用量但文字出现稍有延迟。

        注意：此值仅影响 CardKit 流式通道，IM PATCH 降级通道固定为 1.5 秒。
        """
        return self.flush_interval_ms / 1000.0

    @property
    def show_reasoning(self) -> bool:
        """是否展示推理过程（display.platforms.feishu.show_reasoning → display.show_reasoning）.

        通过 TTL 缓存读取，因为 /reasoning 命令会在运行时修改配置文件，
        但不需要每次属性访问都读磁盘。最多延迟 _RELOAD_CACHE_TTL 秒生效。
        """
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
    def width_mode(self) -> str:
        """卡片宽度模式: default(600px), compact(400px), fill(撑满窗口)."""
        sec = self._plugin_sec()
        mode = sec.get("width_mode", "default")
        return mode if mode in ("default", "compact", "fill") else "default"

    @property
    def card_duration_sec(self) -> int:
        """卡片存活检测超时."""
        return _to_int(self._plugin_sec().get("card_ttl_sec", 600), default=600)

    @property
    def footer_fields(self) -> list[list[str]]:
        """Footer 字段布局（二维数组）."""
        sec = self._plugin_sec()
        footer = sec.get("footer", {})
        if not isinstance(footer, dict):
            return self._default_footer_fields()
        fields = footer.get("fields")
        if not fields:
            return self._default_footer_fields()
        if not isinstance(fields, list):
            return self._default_footer_fields()
        # 一维数组自动包装为二维
        if fields and isinstance(fields[0], str):
            return [fields]
        return fields

    @property
    def footer_show_label(self) -> bool:
        """Footer 是否显示字段标签."""
        sec = self._plugin_sec()
        footer = sec.get("footer", {})
        return _to_bool(footer.get("show_label", False))

    @property
    def header_enabled(self) -> bool:
        """流式卡片和完成态卡片是否显示 header."""
        sec = self._plugin_sec()
        header = sec.get("header", {})
        if not isinstance(header, dict):
            return False
        return bool(header.get("enabled", False))

    @property
    def gateway_cards(self) -> bool:
        """是否将飞书渠道的网关内部消息（slash命令、错误、通知等）转为卡片.

        默认开启。关闭后，网关消息仍以原始文本发送，仅 AI 回复和
        Cron 消息使用卡片。

        通过 TTL 缓存读取，用户运行时修改配置文件后最多延迟
        _RELOAD_CACHE_TTL 秒生效。
        """
        sec = self._reload_cached().get("hermes_lark_streaming")
        if not isinstance(sec, dict):
            return True  # 默认开启
        return _to_bool(sec.get("gateway_cards", True), default=True)

    @staticmethod
    def _default_footer_fields() -> list[list[str]]:
        return [["status", "elapsed", "model", "provider", "cost", "compression_exhausted"]]

    @property
    def env_app_id(self) -> str:
        return os.environ.get("FEISHU_APP_ID") or os.environ.get("LARK_APP_ID") or ""

    @property
    def env_app_secret(self) -> str:
        return os.environ.get("FEISHU_APP_SECRET") or os.environ.get("LARK_APP_SECRET") or ""

    def _plugin_sec(self) -> dict[str, Any]:
        """Return the ``hermes_lark_streaming`` section from config."""
        raw = self._load()
        sec = raw.get("hermes_lark_streaming")
        if isinstance(sec, dict):
            return sec
        return {}

    def _platform_cfg(self) -> dict[str, Any]:
        """从环境变量或平台配置找飞书凭据."""
        if self.env_app_id and self.env_app_secret:
            # base_url 优先级：
            # 1. FEISHU_BASE_URL / LARK_BASE_URL 环境变量（显式指定，最高优先级）
            # 2. FEISHU_DOMAIN 环境变量推断（feishu→国内, lark→国际）
            # 3. 默认 https://open.feishu.cn/open-apis
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
            # 每次都动态读取 HERMES_HOME，支持多 Profile 场景
            config_path = _get_hermes_config_path()
            if config_path.exists():
                try:
                    text = config_path.read_text(encoding="utf-8")
                    self._raw = yaml.safe_load(text) or {}
                except yaml.YAMLError:
                    _logger.warning("HLS: config YAML syntax error in %s, using empty config", config_path)
                    self._raw = {}
                except (OSError, UnicodeDecodeError):
                    # v1.3.2 fix: 文件读取可能因权限不足、磁盘错误或编码问题失败
                    _logger.warning("HLS: config file read error in %s, using empty config", config_path)
                    self._raw = {}
            else:
                self._raw = {}
            return self._raw

    def _reload_cached(self) -> dict[str, Any]:
        """带 TTL 缓存的磁盘重读，供运行时可变的配置项使用.

        在 _RELOAD_CACHE_TTL 秒内复用上次读取结果，避免高频属性访问
        （如流式输出期间反复检查 show_reasoning / gateway_cards）反复读磁盘。
        配置变更最多延迟 _RELOAD_CACHE_TTL 秒生效。
        """
        now = time.monotonic()
        with self._lock:
            if self._reload_cache is not None and (now - self._reload_cache_at) < _RELOAD_CACHE_TTL:
                return self._reload_cache
            # 每次都动态读取 HERMES_HOME，支持多 Profile 场景
            config_path = _get_hermes_config_path()
            if config_path.exists():
                try:
                    text = config_path.read_text(encoding="utf-8")
                    self._reload_cache = yaml.safe_load(text) or {}
                except yaml.YAMLError:
                    _logger.warning("HLS: config YAML syntax error in %s (reload), using empty config", config_path)
                    self._reload_cache = {}
                except (OSError, UnicodeDecodeError):
                    # v1.3.2 fix: 文件读取可能因权限不足、磁盘错误或编码问题失败
                    _logger.warning("HLS: config file read error in %s (reload), using empty config", config_path)
                    self._reload_cache = {}
            else:
                self._reload_cache = {}
            self._reload_cache_at = now
            return self._reload_cache
