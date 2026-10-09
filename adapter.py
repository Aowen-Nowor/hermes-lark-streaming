"""FeishuStreamingAdapter — V2 纯 platform 流式卡片适配器（零钩子）.

架构：继承 hermes 内置 ``FeishuAdapter``（连接/入站/媒体/threads 等全部能力
零损失），只重写出站语义层：``send`` / ``edit_message`` / ``delete_message`` /
``send_clarify`` / ``_handle_card_action_event``。出站帧经 ``FrameComposer``
分类后路由到 CardKit 流式卡片或原生路径。

注册协议：``register(ctx)`` 调用官方 ``ctx.register_platform``，平台名
``feishu-streaming``；依赖检查 / 独立发送 / yaml 配置等辅助函数全部复用内置
实现（同一模块导入，签名漂移在启动时 fail-fast 暴露）。
"""

from __future__ import annotations

import logging
import os
from typing import Any

_logger = logging.getLogger("hermes_lark_streaming")

# ── 内置适配器导入（多候选路径，跨版本防御）────────────────────────────────
_BUILTIN_CANDIDATES = (
    "plugins.platforms.feishu.adapter",
    "hermes.plugins.platforms.feishu.adapter",
    "hermes_plugins.platforms.feishu.adapter",
)


def _import_builtin() -> Any:
    import importlib

    errors: list[str] = []
    for path in _BUILTIN_CANDIDATES:
        try:
            return importlib.import_module(path)
        except Exception as exc:  # noqa: BLE001 — 记录并尝试下一个候选
            errors.append(f"{path}: {exc}")
    raise ImportError(
        "feishu-streaming: cannot import hermes built-in FeishuAdapter. "
        "Tried:\n  " + "\n  ".join(errors)
    )


# 惰性导入：hermes 不在（纯 SDK / 卡片引擎独立使用 / 测试）时包导入不炸，
# register()/实例化时 fail-fast 给出原始错误。
_builtin: Any = None
_BUILTIN_IMPORT_ERROR: Exception | None = None


def _get_builtin() -> Any:
    global _builtin, _BUILTIN_IMPORT_ERROR
    if _builtin is None:
        try:
            _builtin = _import_builtin()
        except ImportError as exc:
            _BUILTIN_IMPORT_ERROR = exc
            raise
    return _builtin


def _builtin_attr(name: str) -> Any:
    return getattr(_get_builtin(), name)


class _MissingBuiltin:
    """hermes 缺失时的占位基类：类型可定义，register/实例化时 fail-fast。"""


_FeishuAdapter: Any = _MissingBuiltin


def _ensure_builtin() -> None:
    if _FeishuAdapter is _MissingBuiltin:
        _get_builtin()  # 成功则 rebind 不可行（类已定义）——直接报原始错误
        raise RuntimeError("feishu-streaming: builtin adapter became available after class creation")

PLATFORM_NAME = "feishu-streaming"

# ── 活跃适配器注册表（供 /aowen 状态卡等模块级设施获取运行状态）──────────
import weakref

_ACTIVE_ADAPTERS: weakref.WeakSet[Any] = weakref.WeakSet()


def get_active_adapters() -> list[Any]:
    """当前存活的 FeishuStreamingAdapter 实例列表。"""
    return list(_ACTIVE_ADAPTERS)


def _any_card_client() -> Any:
    """任一活跃适配器的 CardKit 客户端（无则 None）。"""
    for adapter in _ACTIVE_ADAPTERS:
        client = adapter._get_card_client()
        if client is not None:
            return client
    return None


# ── CardKit 客户端构建 ──────────────────────────────────────────────────────
def _client_config_for(adapter: Any) -> Any:
    """从内置适配器实例提取凭证，构造 FeishuClientConfig."""
    from .feishu.client import FeishuClientConfig

    domain_name = str(getattr(adapter, "_domain_name", "") or os.environ.get("FEISHU_DOMAIN", "feishu")).lower()
    if domain_name == "lark":
        base = "https://open.larksuite.com/open-apis"
    else:
        base = "https://open.feishu.cn/open-apis"
    return FeishuClientConfig(
        app_id=str(getattr(adapter, "_app_id", "") or ""),
        app_secret=str(getattr(adapter, "_app_secret", "") or ""),
        base_url=base,
    )


# ── 卡片构建工厂（桥接 cardkit 引擎与插件配置）──────────────────────────────
class _CardFactory:
    """按插件配置构建流式卡片 / 终态卡片."""

    def __init__(self, config: Any) -> None:
        self._config = config

    def _params(self) -> dict[str, Any]:
        cfg = self._config
        boolget = lambda name, default: bool(getattr(cfg, name, default))
        return {
            "streaming_panel_expanded": boolget("streaming_panel_expanded", True),
            "print_strategy": str(getattr(cfg, "print_strategy", "delay") or "delay"),
            "print_step": int(getattr(cfg, "print_step", 4) or 4),
        }

    def streaming(self, *, status_lines: list[str] | None = None) -> dict[str, Any]:
        from .cardkit.cards import build_streaming_card_v2

        params = self._params()
        return build_streaming_card_v2(status_lines=status_lines, **params)

    def final(self, **_: Any) -> dict[str, Any]:
        # V2 终态不重建卡片（answer 元素由 stream_element 落终值）。
        # 保留工厂位：V2.1 折叠状态区 / footer 时在此扩展。
        from .cardkit.cards import build_streaming_card_v2

        params = self._params()
        params["include_loading_hint"] = False
        return build_streaming_card_v2(**params)


# ── 适配器 ──────────────────────────────────────────────────────────────────
class FeishuStreamingAdapter(_FeishuAdapter):  # type: ignore[misc,valid-type]
    """流式卡片版飞书适配器 — 内置能力全继承，出站语义层重写."""

    PLATFORM_LABEL = "Feishu / Lark (Streaming Cards)"

    def __init__(self, config: Any) -> None:
        super().__init__(config)
        self._probe_override_surface()
        self._card_config = self._make_config()
        self._card_factory = _CardFactory(self._card_config)
        self._card_client: Any = None
        from .composer.engine import FrameComposer

        self._composer = FrameComposer(
            get_client=self._get_card_client,
            build_card=self._card_factory.streaming,
            build_final_card=self._card_factory.final,
            config=self._card_config,
        )
        _ACTIVE_ADAPTERS.add(self)

    # ── 初始化辅助 ──────────────────────────────────────────────────────
    def _make_config(self) -> Any:
        try:
            from .config import Config

            return Config()
        except Exception:
            _logger.debug("feishu-streaming: plugin Config unavailable — defaults in use", exc_info=True)
            return None

    def _probe_override_surface(self) -> None:
        """继承面探测：重写点缺失即 fail-fast（跨版本重构的显式暴露）."""
        if self.__class__.__bases__[0] is _MissingBuiltin:  # pragma: no cover — register 前不会实例化
            raise RuntimeError(
                "feishu-streaming: cannot instantiate before register() — hermes builtin missing"
            )
        missing = [name for name in ("send", "edit_message", "send_clarify") if not hasattr(super(), name)]
        if missing:
            raise RuntimeError(
                f"feishu-streaming: built-in FeishuAdapter surface drift — missing: {missing}. "
                "Refusing to start with a broken inheritance chain."
            )
        if not hasattr(self, "_handle_card_action_event") and not hasattr(type(self), "_handle_card_action_event"):
            _logger.warning(
                "feishu-streaming: built-in FeishuAdapter has no _handle_card_action_event "
                "— clarify card callbacks will be inert on this hermes version"
            )

    def _get_card_client(self) -> Any:
        if self._card_client is None:
            if not getattr(self, "_app_id", "") or not getattr(self, "_app_secret", ""):
                return None
            try:
                from .feishu.client import FeishuClient

                self._card_client = FeishuClient(_client_config_for(self))
            except Exception:
                _logger.warning("feishu-streaming: FeishuClient init failed", exc_info=True)
                return None
        return self._card_client

    # ── 出站语义层重写 ──────────────────────────────────────────────────
    async def send(
        self, chat_id: str, content: str, reply_to: str | None = None, metadata: dict[str, Any] | None = None,
    ) -> Any:
        from .composer.classify import CH_PLAIN, classify_send

        try:
            cls = classify_send(content, metadata)
        except Exception:  # noqa: BLE001 — 分类失败按 plain 走原生，永不阻断
            cls = None
        if cls is None or cls.channel == CH_PLAIN:
            return await super().send(
                chat_id, content, reply_to=reply_to, metadata=_strip_internal(metadata),
            )
        try:
            result = await self._composer.handle_send(chat_id, content, reply_to, metadata, cls)
        except Exception:
            _logger.warning("feishu-streaming: composer send failed — native fallback", exc_info=True)
            result = None
        if result is None:
            return await super().send(chat_id, content, reply_to=reply_to, metadata=_strip_internal(metadata))
        return result

    async def edit_message(
        self, chat_id: str, message_id: str, content: str, *, finalize: bool = False,
    ) -> Any:
        from .composer.classify import classify_edit

        try:
            if self._composer.state_by_message(message_id) is not None:
                cls = classify_edit(content, finalize)
                result = await self._composer.handle_edit(chat_id, message_id, content, finalize, cls)
                if result is not None:
                    return result
        except Exception:
            _logger.warning("feishu-streaming: composer edit failed — native fallback", exc_info=True)
        return await super().edit_message(chat_id, message_id, content, finalize=finalize)

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        try:
            if self._composer.state_by_message(message_id) is not None:
                self._composer.forget(chat_id)
        except Exception:
            _logger.debug("feishu-streaming: delete state cleanup failed", exc_info=True)
        return await super().delete_message(chat_id, message_id)

    # ── 澄清卡片（闭环见 clarify.py）────────────────────────────────────
    async def send_clarify(
        self, chat_id: str, question: str, choices: list | None, clarify_id: str,
        session_key: str | None = None, metadata: dict[str, Any] | None = None, **kwargs: Any,
    ) -> Any:
        from . import clarify as _clarify

        client = self._get_card_client()
        if client is not None:
            try:
                result = await _clarify.send_clarify_card(
                    self, client, chat_id, question, choices, clarify_id, metadata=metadata,
                )
                if result is not None:
                    return result
            except Exception:
                _logger.warning("feishu-streaming: clarify card failed — text fallback", exc_info=True)
        return await super().send_clarify(
            chat_id, question, choices, clarify_id, session_key=session_key,
            metadata=metadata, **kwargs,
        )

    async def _handle_card_action_event(self, data: Any) -> None:
        """卡片动作回调：clarify 动作本实现消费；其余抑制 /card 合成命令."""
        from . import clarify as _clarify

        event = getattr(data, "event", None)
        action_value = getattr(getattr(event, "action", None), "value", {}) or {}
        if isinstance(action_value, dict) and action_value.get("hermes_clarify_action"):
            try:
                return _clarify.handle_clarify_card_action(self, self._get_card_client, data)
            except Exception:
                _logger.warning("feishu-streaming: clarify callback failed", exc_info=True)
                return
        # 非 clarify 卡片动作：hermes 会合成 "/card <tag> <value>" 命令，
        # 网关必然拒绝（Unknown command）——抑制并留痕。
        action_tag = str(getattr(getattr(event, "action", None), "tag", "") or "button")
        _logger.debug(
            "feishu-streaming: card action %r suppressed (no /card synthesis)", action_tag,
        )
        return


def _strip_internal(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    """Gateway 内部标记不上线（官方约定：adapters strip it before the wire）."""
    if not metadata:
        return metadata
    clean = {k: v for k, v in metadata.items() if k != "_interim_send"}
    return clean or None


# ── 注册入口 ────────────────────────────────────────────────────────────────
def _rebuild_class_with_real_base() -> Any:
    """hermes 就绪后把适配器类切换到真实基类（原地换基）.

    模块导入时 hermes 可能不在（占位基类 ``_MissingBuiltin``）；register() 是
    必然运行在 hermes 进程内的入口，在此完成真实继承链组装。

    实现说明（v2.0.0 生产修复）：不能用 ``type()`` 重建类——借用的方法函数
    对象带有指向旧类的 ``__class__`` cell，零参 ``super()`` 在首次实例化时
    抛 ``TypeError: obj ... is not an instance or subtype of type``（V2 上线
    当日在 hermes 网关实测暴露；测试环境真实基类在导入期即绑定，rebuild 分支
    从未执行，故 584 项测试无法捕获）。原地改 ``__bases__`` 保持类对象身份与
    全部方法闭包一致，零参 super 语义不变。
    """
    base = _get_builtin().FeishuAdapter
    cls = FeishuStreamingAdapter
    if cls.__bases__ and cls.__bases__[0] is _MissingBuiltin:
        cls.__bases__ = (base,)  # 原地换基：身份/cell/MRO 一次到位，无第二类对象
    return cls


def register(ctx: Any) -> None:
    """平台插件注册（hermes 官方协议）."""
    try:
        adapter_cls = _rebuild_class_with_real_base()
    except ImportError as exc:
        raise RuntimeError(
            "feishu-streaming: cannot register — hermes built-in FeishuAdapter unavailable. "
            f"Original error:\n{exc}"
        ) from exc
    _prewarm_config()

    ctx.register_platform(
        name=PLATFORM_NAME,
        label=adapter_cls.PLATFORM_LABEL,
        adapter_factory=adapter_cls,
        check_fn=_builtin_attr("feishu_deps_present"),
        ensure_deps_fn=_builtin_attr("check_feishu_requirements"),
        is_connected=_builtin_attr("_is_connected"),
        validate_config=_builtin_attr("_is_connected"),
        required_env=["FEISHU_APP_ID", "FEISHU_APP_SECRET"],
        install_hint="Run `hermes setup` to install Feishu support.",
        setup_fn=_builtin_attr("interactive_setup"),
        apply_yaml_config_fn=_builtin_attr("_apply_yaml_config"),
        allowed_users_env="FEISHU_ALLOWED_USERS",
        allow_all_env="FEISHU_ALLOW_ALL_USERS",
        cron_deliver_env_var="FEISHU_HOME_CHANNEL",
        standalone_sender_fn=_builtin_attr("_standalone_send"),
        max_message_length=8000,
        emoji="🪽",
        allow_update_command=True,
    )
    _logger.info("feishu-streaming: platform registered (zero monkey-patch)")

    # /aowen 指令：官方 hook API 注册（非补丁）
    try:
        from .aowen import handle_pre_gateway_dispatch

        ctx.register_hook("pre_gateway_dispatch", handle_pre_gateway_dispatch)
        _logger.info("feishu-streaming: /aowen commands registered (help, status, monitor)")
    except Exception:
        _logger.debug("feishu-streaming: /aowen hook registration skipped", exc_info=True)


def _prewarm_config() -> None:
    """注册期配置注入与 lark-oapi 门槛检查（自 V1 plugin/ 移植语义）."""
    try:
        from .plugin import check_lark_oapi_floor, ensure_streaming_config

        check_lark_oapi_floor()
        ensure_streaming_config()
    except Exception:
        _logger.debug("feishu-streaming: prewarm skipped", exc_info=True)
