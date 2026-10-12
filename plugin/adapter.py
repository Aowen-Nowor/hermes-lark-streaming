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


# 导入期即尝试绑定真实基类（v2.0.0 生产修复）：hermes 进程内（网关/CLI/插件
# 加载器）此刻 ``plugins.platforms.feishu.adapter`` 已可导入，直接绑定 →
# 类身份唯一、零参 super 的 ``__class__`` cell 与类一致。仅纯 SDK 环境（无
# hermes）回退占位基类。
#
# 历史：原实现惰性到 register() 才换基，而 ``type()`` 重建会借用旧类的方法
# 函数对象（``__class__`` cell 指向旧类）→ 首次实例化 super() 身份错乱；
# ``__bases__`` 原地赋值在 CPython 3.12+ 直接 TypeError（deallocator 差异）。
# 两条路都不通，唯一稳妥做法就是导入期绑定。
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


# 导入期绑定：hermes 在 → 真实基类（生产路径，类身份唯一）；hermes 不在 → 占位基类
try:
    _FeishuAdapter: Any = _get_builtin().FeishuAdapter
except Exception:  # noqa: BLE001 — 纯 SDK / 卡片引擎独立使用 / 测试环境
    _FeishuAdapter = _MissingBuiltin


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
    from ..feishu.client import FeishuClientConfig

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

    def streaming(
        self,
        *,
        status_lines: list[str] | None = None,
        answer_text: str = "",
        panel_title: str = "",
        status_durations: dict[str, str] | None = None,
        status_times: list[float] | None = None,
    ) -> dict[str, Any]:
        from ..cardkit.cards import build_streaming_card_v2

        params = self._params()
        return build_streaming_card_v2(
            status_lines=status_lines, answer_text=answer_text, panel_title=panel_title,
            status_durations=status_durations, status_times=status_times, **params,
        )

    def final(self, **_: Any) -> dict[str, Any]:
        # V2 终态不重建卡片（answer 元素由 stream_element 落终值）。
        # 保留工厂位：V2.1 折叠状态区 / footer 时在此扩展。
        from ..cardkit.cards import build_streaming_card_v2

        params = self._params()
        params["include_loading_hint"] = False
        return build_streaming_card_v2(**params)


# ── 适配器 ──────────────────────────────────────────────────────────────────
class FeishuStreamingAdapter(_FeishuAdapter):  # type: ignore[misc,valid-type]
    """流式卡片版飞书适配器 — 内置能力全继承，出站语义层重写."""

    PLATFORM_LABEL = "Feishu / Lark (Streaming Cards)"

    # v2.0.13（Task 18 P1-B）：声明 hermes native streaming 能力（官方探测点
    # gateway/stream_consumer_transport.py:_resolve_native_streaming:151-164
    # 检查类常量 + supports_native_streaming probe）。开启后答案流帧走
    # send_stream_frame（native 帧通道）：segment-break finalize 被官方吞掉
    # （transport:389 ``finalize = finalize and is_turn_final``）且 native =
    # cumulative transport（stream_consumer.py:906 ``_end_segment`` 直接
    # return）→ 工具边界不再封卡，单卡全回合（多卡片同族缺陷的通道级根治；
    # 事实先例：Task 18 取证 16:54 轮次——call#1 前奏被工具边界 seal 成独立卡）。
    # 失败语义：任何帧 False/异常 → 官方禁用 native 回落 edit/send 现有链路
    # （transport:399-426），降级红线官方契约保证。
    SUPPORTS_NATIVE_STREAMING = True

    def __init__(self, config: Any) -> None:
        super().__init__(config)
        # v2.0.4（原服务器 v2.0.2 热补丁回流仓库）：平台身份对齐注册名。内置基类
        # 硬编码 Platform.FEISHU，使 source.platform(feishu) 与 adapter 注册键
        # (feishu-streaming) 错配：_owning_profile/_delivery_adapter_for 查不到
        # 本适配器 → stream consumer 不建 → 答案帧无 expect_edits → 分类全落
        # CH_PLAIN 纯文本。对齐后：卡片流式 / 状态面板 / 思考进度 / 澄清卡整链恢复。
        try:
            from gateway.config import Platform as _Platform

            self.platform = _Platform(PLATFORM_NAME)
        except Exception:  # 无 hermes 环境（测试/独立使用）跳过
            _logger.debug(
                "feishu-streaming: platform identity alignment skipped", exc_info=True,
            )
        self._probe_override_surface()
        self._card_config = self._make_config()
        self._card_factory = _CardFactory(self._card_config)
        self._card_client: Any = None
        from ..composer.engine import FrameComposer

        self._composer = FrameComposer(
            get_client=self._get_card_client,
            build_card=self._card_factory.streaming,
            build_final_card=self._card_factory.final,
            config=self._card_config,
        )
        # v2.0.7：reasoning 隐藏流 → 面板思考行（第 N 轮）落点注册
        try:
            from .reasoning_feed import bind_composer

            bind_composer(self._composer)
        except Exception:  # 思考回灌是增强能力，失败不影响卡片主链（BLE 未启用，noqa 触发 RUF100）
            _logger.debug("feishu-streaming: reasoning feed bind skipped", exc_info=True)
        # v2.0.8：post_tool_call 官方钩子 → 单工具耗时（「Terminal (2.3 s)」）落点注册
        try:
            from .tool_timing import bind_composer as _bind_tool_timing

            _bind_tool_timing(self._composer)
        except Exception:  # 耗时是增强能力，失败不影响卡片主链
            _logger.debug("feishu-streaming: tool timing bind skipped", exc_info=True)
        _ACTIVE_ADAPTERS.add(self)

    # ── 初始化辅助 ──────────────────────────────────────────────────────
    def _make_config(self) -> Any:
        try:
            from ..config import Config

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
                from ..feishu.client import FeishuClient

                self._card_client = FeishuClient(_client_config_for(self))
            except Exception:
                _logger.warning("feishu-streaming: FeishuClient init failed", exc_info=True)
                return None
        return self._card_client

    # ── 出站语义层重写 ──────────────────────────────────────────────────
    async def send(
        self, chat_id: str, content: str, reply_to: str | None = None, metadata: dict[str, Any] | None = None,
    ) -> Any:
        from ..composer.classify import CH_PLAIN, classify_send

        try:
            cls = classify_send(content, metadata)
        except Exception:  # noqa: BLE001 — 分类失败按 plain 走原生，永不阻断
            cls = None
        if cls is not None and cls.channel == CH_PLAIN:
            # v2.0.1 上下文升格：裸进度气泡吸收进状态面板
            #（详见 _upgrade_plain_to_progress；条件不满足维持原分类）。
            # v2.0.8：透传 metadata——非对话通知/notify 帧永不升格。
            upgraded = _upgrade_plain_to_progress(self._composer, chat_id, content, metadata)
            if upgraded is not None:
                cls = upgraded
                _logger.debug("feishu-streaming: plain send upgraded to progress (progress lane)")
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

    def supports_native_streaming(
        self, chat_type: str | None = None, metadata: dict[str, Any] | None = None,
    ) -> bool:
        """官方 native 探测点（每回合 _start_transports 调用，v2.0.13）.

        官方调用签名（stream_consumer_transport.py:161）
        ``probe(chat_type=..., metadata=...)``；TypeError 时降参重试由官方处理。

        灰度开关：插件配置 ``feishu_streaming.native_streaming``（默认 True）。
        False → 整平台回到 edit-based 流（v2.0.12 行为）。CardKit 凭证缺失
        一并返回 False（建卡必败，不值得开 native——官方种子帧失败同样回落，
        这里提前返回省一次无谓的 seed 尝试）。永不抛异常。
        """
        try:
            if self._card_config is not None and not bool(
                getattr(self._card_config, "native_streaming", True)
            ):
                return False
        except Exception:  # 配置读取失败按默认开启（装饰性判断，仅 debug 留痕）
            _logger.debug("feishu-streaming: native probe config read failed", exc_info=True)
        try:
            return self._get_card_client() is not None
        except Exception:  # noqa: BLE001
            return False

    async def send_stream_frame(
        self,
        text: str,
        *,
        finalize: bool = False,
        chat_id: str | None = None,
        reply_to: str | None = None,
        **kwargs: Any,
    ) -> bool:
        """hermes native streaming 帧（官方契约范例
        plugins/platforms/wecom/streaming.py:261）— v2.0.13.

        官方调用形态（stream_consumer_transport.py:50-52, 66-70）：
        - 种子帧：``send_stream_frame("", chat_id=..., reply_to=..., turn_id=...)``
        - 内容/终帧：``send_stream_frame(text, finalize=..., chat_id=..., ... )``

        帧语义全部由 FrameComposer.handle_stream_frame 承接（种子建卡 /
        快照更新 / 复合帧工具行拆分进面板 / finalize 幂等封卡）。返回 False
        （含任何异常）＝本帧未消费，官方 consumer 自动回落 edit/send 现有
        链路（帧永不丢）。永不抛异常（官方 _try_frame 虽吞异常，这里自吞
        保日志在插件命名空间内）。
        """
        chat = (chat_id or "").strip()
        if not chat:
            return False
        try:
            return await self._composer.handle_stream_frame(
                chat, text or "", finalize=finalize,
                turn_id=str(kwargs.get("turn_id") or "") or None,
            )
        except Exception:  # 降级红线：native 帧失败回落（BLE 未启用，noqa 触发 RUF100）
            _logger.warning(
                "feishu-streaming: send_stream_frame failed — native disabled, "
                "consumer falls back to edit/send",
                exc_info=True,
            )
            return False

    async def send_or_update_status(
        self, chat_id: str, status_key: str, content: str, metadata: dict[str, Any] | None = None,
    ) -> Any:
        """网关 status 通道（lifecycle/provider-error 状态通报）语义层（v2.0.10）.

        官方契约：适配器实现 ``send_or_update_status`` 时状态走此入口而非
        ``send()``，且 metadata 仅带 thread 信息、**无 notify/expect_edits/
        _interim_send 标记**。v2.0.10 之前无实现 → 兑底 plain send() →
        v2.0.1 升格启发式把「⏱️ The AI model service is rate-limiting…」
        终态错误通报当进度行 → 无活跃卡时建占位卡（spinner 永不定稿，
        2026-10-12 "卡住了"事故根因）。

        本平台语义：状态通报 = advisory——
        - 活跃卡片期：并入面板（composer._on_interim 同路径，emoji 通报行
          由渲染层挂官方图标）；
        - 无卡片：原生文本气泡（用户立即可见，绝不建卡）；
        - 永不建卡 / 永不升格。
        刻意偏离官方「按 status_key 原地编辑气泡」契约：面板本身就是本平台
        的原地状态面，再编辑一份气泡会双份呈现。
        """
        from ..composer.classify import CH_INTERIM, ClassifyResult

        try:
            cls = ClassifyResult(CH_INTERIM, kind="advisory", raw_content=content or "", meta={})
            result = await self._composer.handle_send(chat_id, content, None, metadata, cls)
        except Exception:  # 降级红线：任何失败走原生
            _logger.warning("feishu-streaming: status advisory failed — native fallback", exc_info=True)
            result = None
        if result is None:
            return await super().send(chat_id, content, metadata=_strip_internal(metadata))
        return result

    async def edit_message(
        self, chat_id: str, message_id: str, content: str, *, finalize: bool = False,
    ) -> Any:
        from ..composer.classify import classify_edit

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


def _upgrade_plain_to_progress(
    composer: Any, chat_id: str, content: str, metadata: dict[str, Any] | None = None,
) -> Any | None:
    """v2.0.1 上下文升格：裸 send → CH_PROGRESS（v2.0.4 起不再要求活跃卡片）.

    edit-transport 平台（feishu）的工具进度气泡首条是裸 send —— hermes 只给
    native-stream / 预览路径打 ``expect_edits``，工具进度气泡
    （``run_turn_runner._send_progress_text``）首发不带任何标记。若无此规则，
    气泡会落 CH_PLAIN 成独立纯文本消息，卡片状态面板永远为空。

    升格条件：
      文本命中进度启发式（💬 前缀或 emoji 开头的短行）。

    v2.0.4（s70 回流）：去掉"本 chat 存在活跃流式卡片"前置——工具帧常早于
    答案首帧（卡片尚未建立），必须先升格才能被 engine 暂存并立即建卡；
    无卡片时升格结果只是进入 pending 暂存（TTL 兜底），无副作用。非 turn
    场景（cron / 后台 / 独立投递）带 notify=True → CH_FINAL，不受影响。

    误吸收代价评估：短 emoji 文本只有进度气泡一种生产形态（cron 定时投递
    不落此路径），可接受。

    返回升格后的 ``ClassifyResult``；不满足条件返回 ``None``（维持原分类）。
    永不抛异常（内部全吞，失败维持原分类）。
    """
    try:
        from ..composer.classify import (
            CH_PROGRESS,
            ClassifyResult,
            _is_notice_text,
            is_progress_text,
            parse_progress_line,
        )

        meta = metadata or {}
        # v2.0.8：notify/非对话通知（💾 Self-improvement review 等）永不升格 ——
        # 这类帧在建卡条件不成立时会新建跑马灯卡（用户验收问题4 根因家族）。
        if meta.get("notify") or meta.get("non_conversational"):
            return None
        if not content or _is_notice_text(content):
            return None
        if not is_progress_text(content):
            return None
        # v2.0.4（s70 回流）：不再要求"必须有活跃卡片"——工具帧常早于答案首帧
        #（卡片尚未建立），升格后由 engine._on_progress 暂存并立即建卡。非 turn
        # 场景（cron/standalone/notify 投递）带 notify=True → CH_FINAL，不受影响。
        kind, tool, clean = parse_progress_line(content)
        return ClassifyResult(
            channel=CH_PROGRESS,
            kind=kind,
            tool_name=tool,
            thinking_text=clean if kind == "thinking" else "",
            raw_content=content,
        )
    except Exception:  # noqa: BLE001 — 升格失败维持原分类，永不阻断
        return None


# ── 注册入口 ────────────────────────────────────────────────────────────────
def _rebuild_class_with_real_base() -> Any:
    """占位基类兜底路径：hermes 缺席时导入的类，在 register() 时换真实基类.

    正常生产路径**不会走到这里**：模块导入期的 try 绑定（见模块头注释）在
    hermes 进程内直接把 ``_FeishuAdapter`` 绑到真实基类，本函数检测到
    ``__bases__[0]`` 不是占位类时直接原样返回（no-op，身份零变化）。

    兜底路径仅覆盖"导入时 hermes 不可用、register() 时可用"的边缘场景，
    用 ``type()`` 重建（注册可用）。注意：重建借用的方法函数 ``__class__``
    cell 仍指向旧类，该路径下的实例化会 fail-fast——这正是导入期绑定成为
    主路径的原因。任何路径下 register() 都不会静默产出坏适配器。
    """
    base = _get_builtin().FeishuAdapter
    cls = FeishuStreamingAdapter
    if cls.__bases__ and cls.__bases__[0] is _MissingBuiltin:
        ns = {
            k: v for k, v in vars(cls).items()
            if k not in ("__dict__", "__weakref__", "__module__", "__qualname__", "__doc__")
        }
        cls = type(
            "FeishuStreamingAdapter", (base,),
            {"__module__": __name__, "__qualname__": "FeishuStreamingAdapter",
             "__doc__": FeishuStreamingAdapter.__doc__, **ns},
        )
        globals()["FeishuStreamingAdapter"] = cls
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
    # v2.0.9 P2-9 可观测性：网关默认日志级别 WARNING 会吞掉插件 INFO 生命周期
    # 日志（card created / finalized / sequence 自愈预警——生产排障的关键证据链，
    # gateway19 审计实证缺口）。插件 logger 树显式提到 INFO：自有命名空间不受
    # 网关全局级别影响，第三方日志零污染。
    logging.getLogger("hermes_lark_streaming").setLevel(logging.INFO)

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
        # v2.0.1：显式声明 display tier。TIER_HIGH = streaming 跟随全局开关 +
        # tool_progress "all"（进度行必须到达适配器才能进卡片状态面板）+
        # interim_assistant_messages 开（commentary → CH_INTERIM → 面板）。
        # 不声明时插件平台落 ""（全局默认），行为等价但受未来默认漂移影响；
        # 显式声明把流式卡片体验所需的显示基线固化下来。
        display_tier="high",
    )
    _logger.info("feishu-streaming: platform registered (zero monkey-patch)")

    # /aowen 指令：官方 hook API 注册（非补丁）
    try:
        from ..aowen import handle_pre_gateway_dispatch

        ctx.register_hook("pre_gateway_dispatch", handle_pre_gateway_dispatch)
        _logger.info("feishu-streaming: /aowen commands registered (help, status, monitor)")
    except Exception:
        _logger.debug("feishu-streaming: /aowen hook registration skipped", exc_info=True)

    # v2.0.7：reasoning 隐藏流观察者（on_stream_delta/on_stream_end）——
    # 需 config `plugins.stream_reasoning_deltas: true` 才有数据投递；
    # 钩子回调内部自兑底，注册失败仅降级为「无思考行」。
    try:
        from . import reasoning_feed

        reasoning_feed.register_hooks(ctx)
    except Exception:
        _logger.debug("feishu-streaming: reasoning feed registration skipped", exc_info=True)

    # v2.0.8：post_tool_call 官方钩子（单工具耗时 →「Terminal (2.3 s)」）——
    # 钩子回调内部自兑底，注册失败仅降级为「无耗时」。
    try:
        from . import tool_timing

        tool_timing.register_hooks(ctx)
    except Exception:
        _logger.debug("feishu-streaming: tool timing registration skipped", exc_info=True)


def _prewarm_config() -> None:
    """注册期配置注入与 lark-oapi 门槛检查（自 V1 plugin/ 移植语义）."""
    try:
        from . import check_lark_oapi_floor, ensure_streaming_config

        check_lark_oapi_floor()
        ensure_streaming_config()
    except Exception:
        _logger.debug("feishu-streaming: prewarm skipped", exc_info=True)
