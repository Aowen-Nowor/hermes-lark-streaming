"""读取 Hermes 配置，提供本插件所需的配置项."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import yaml


def _get_hermes_config_path() -> Path:
    """动态获取 Hermes 配置文件路径。
    
    在多 Profile 场景下，HERMES_HOME 环境变量会在 Gateway 启动时
    通过 _apply_profile_override() 设置。如果在模块导入时就读取
    该变量，可能会读到错误的路径。
    
    此函数每次调用时都重新读取环境变量，确保始终使用正确的路径。
    """
    return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "config.yaml"


# 向后兼容：保留常量名，但改为使用 getter 函数
# 注意：这仍然在模块导入时求值，但大多数场景下是正确的
# 对于多 Profile 场景，请使用 _get_hermes_config_path()
_HERMES_CONFIG_PATH = _get_hermes_config_path()

_RELOAD_CACHE_TTL = 5.0  # 运行时可变配置的缓存 TTL（秒）


class Config:
    """插件配置，惰性读取 Hermes 主配置."""

    def __init__(self) -> None:
        self._raw: dict[str, Any] | None = None
        self._reload_cache: dict[str, Any] | None = None
        self._reload_cache_at: float = 0.0

    @property
    def enabled(self) -> bool:
        """是否启用流式卡片."""
        sec = self._streaming_sec()
        return bool(sec.get("enabled", False))

    @property
    def linear(self) -> bool:
        """是否启用线性单卡模式，按事件顺序动态更新卡片元素."""
        sec = self._streaming_sec()
        return bool(sec.get("linear", True))

    @property
    def panel_expanded(self) -> bool:
        """完成态卡片中面板（工具、推理）是否保持展开."""
        sec = self._streaming_sec()
        return bool(sec.get("panel_expanded", False))

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
                return bool(feishu["show_reasoning"])
        return bool(display.get("show_reasoning", False))

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
        """卡片存活检测超时."""
        return int(self._streaming_sec().get("card_ttl_sec", 600))

    @property
    def footer_fields(self) -> list[list[str]]:
        """Footer 字段布局（二维数组）."""
        sec = self._streaming_sec()
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
    def inject_time(self) -> bool:
        """是否在用户消息前注入当前时间，让模型无需调用 date 工具即可感知时间.

        默认关闭。开启后，每条用户消息前会添加 ``<time>HH:MM:SS</time>`` 前缀，
        前缀同时写入 DB（保证 prefix cache 一致性）。
        使用 XML 标签格式而非方括号格式，避免 LLM 忽略或模仿时间前缀。

        通过 TTL 缓存读取，用户运行时修改配置文件后最多延迟
        _RELOAD_CACHE_TTL 秒生效，避免高频访问时反复读磁盘。
        """
        sec = self._reload_cached().get("streaming")
        if not isinstance(sec, dict):
            return False
        return bool(sec.get("inject_time", False))

    @property
    def footer_show_label(self) -> bool:
        """Footer 是否显示字段标签."""
        sec = self._streaming_sec()
        footer = sec.get("footer", {})
        return bool(footer.get("show_label", False))

    @staticmethod
    def _default_footer_fields() -> list[list[str]]:
        return [["status", "elapsed", "model", "api_calls"], ["tokens", "context", "history_offset", "compression_exhausted"]]

    @property
    def env_app_id(self) -> str:
        return os.environ.get("FEISHU_APP_ID") or os.environ.get("LARK_APP_ID") or ""

    @property
    def env_app_secret(self) -> str:
        return os.environ.get("FEISHU_APP_SECRET") or os.environ.get("LARK_APP_SECRET") or ""

    def _streaming_sec(self) -> dict[str, Any]:
        raw = self._load()
        sec = raw.get("streaming")
        if isinstance(sec, dict):
            return sec
        return {}

    def _platform_cfg(self) -> dict[str, Any]:
        """从环境变量或平台配置找飞书凭据."""
        if self.env_app_id and self.env_app_secret:
            return {
                "app_id": self.env_app_id,
                "app_secret": self.env_app_secret,
                "base_url": os.environ.get(
                    "FEISHU_BASE_URL",
                    os.environ.get("LARK_BASE_URL", "https://open.feishu.cn/open-apis"),
                ),
            }
        raw = self._load()
        for key in ("feishu", "lark"):
            pf = raw.get(key)
            if isinstance(pf, dict) and pf.get("app_id"):
                return pf
        return {}

    def _load(self) -> dict[str, Any]:
        if self._raw is not None:
            return self._raw
        # 每次都动态读取 HERMES_HOME，支持多 Profile 场景
        config_path = _get_hermes_config_path()
        if config_path.exists():
            text = config_path.read_text(encoding="utf-8")
            self._raw = yaml.safe_load(text) or {}
        else:
            self._raw = {}
        return self._raw

    def _reload_cached(self) -> dict[str, Any]:
        """带 TTL 缓存的磁盘重读，供运行时可变的配置项使用.

        在 _RELOAD_CACHE_TTL 秒内复用上次读取结果，避免高频属性访问
        （如流式输出期间反复检查 inject_time / show_reasoning）反复读磁盘。
        配置变更最多延迟 _RELOAD_CACHE_TTL 秒生效。
        """
        now = time.monotonic()
        if self._reload_cache is not None and (now - self._reload_cache_at) < _RELOAD_CACHE_TTL:
            return self._reload_cache
        # 每次都动态读取 HERMES_HOME，支持多 Profile 场景
        config_path = _get_hermes_config_path()
        if config_path.exists():
            text = config_path.read_text(encoding="utf-8")
            self._reload_cache = yaml.safe_load(text) or {}
        else:
            self._reload_cache = {}
        self._reload_cache_at = now
        return self._reload_cache
