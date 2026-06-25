"""飞书 Open API 客户端 — 基于 lark-oapi SDK."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import time as _time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

import lark_oapi as lark
from lark_oapi.api.cardkit.v1 import (
    BatchUpdateCardRequest,
    BatchUpdateCardRequestBody,
    Card,
    ContentCardElementRequest,
    ContentCardElementRequestBody,
    CreateCardRequest,
    CreateCardRequestBody,
    SettingsCardRequest,
    SettingsCardRequestBody,
    UpdateCardRequest,
    UpdateCardRequestBody,
)
from lark_oapi.api.im.v1 import (
    CreateImageRequest,
    CreateImageRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    PatchMessageRequest,
    PatchMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

# v1.3.4 fix (P1): lark_oapi SDK 的 Transport.aexecute 不捕获网络异常，
# httpx 的 ConnectError/ReadTimeout 等会裸传播；token 刷新失败抛
# ObtainAccessTokenException（Exception 子类，非 FeishuAPIError）。
# _retry_transient 的 except FeishuAPIError 无法捕获这些异常，导致
# 网络瞬断时 cardkit_create 直接失败走 IM 降级（用户看到纯文本而非卡片）。
# 导入这些异常类型用于 _retry_transient 的网络错误重试。
try:
    import httpx
    _NETWORK_ERROR_BASES: tuple = (httpx.RequestError, httpx.TimeoutException)
except ImportError:  # 极端情况下 httpx 不可用（lark_oapi 依赖它，不应发生）
    _NETWORK_ERROR_BASES = ()
try:
    from lark_oapi.core.exception import ObtainAccessTokenException
    _TOKEN_ERROR_BASES: tuple = (ObtainAccessTokenException,)
except ImportError:
    _TOKEN_ERROR_BASES = ()

_logger = logging.getLogger("hermes_lark_streaming")


def _sanitize_message(msg: str) -> str:
    """从错误消息中移除 token 和 secret."""
    msg = re.sub(r'(tenant_access_token["\s:=]+)([A-Za-z0-9_-]{10,})', r"\1***", msg)
    msg = re.sub(r'(app_secret["\s:=]+)([A-Za-z0-9]{10,})', r"\1***", msg)
    msg = re.sub(r"(Bearer\s+)([A-Za-z0-9_-]{10,})", r"\1***", msg)
    return msg


class FeishuAPIError(RuntimeError):
    """飞书 API 错误，携带 API 错误码."""

    def __init__(self, message: str, code: int = 0) -> None:
        super().__init__(message)
        self.code = code

    def extract_sub_code(self) -> int | None:
        """从 msg 字符串中提取子错误码.

        格式: "Failed to create card content, ext=ErrCode: 11310; ..."
        """
        m = re.search(r"ErrCode:\s*(\d+)", str(self))
        if m:
            return int(m.group(1))
        return None

    def extract_schema_detail(self) -> str:
        """从 300315 Schema 错误中提取具体非法属性信息.

        飞书返回格式示例:
          "invalid card schema: unknown property 'icon' on 'plain_text'"
          "Schema validation failed: property 'margin' is not allowed on tag 'markdown'"

        Returns:
            提取到的细节字符串；无法提取时返回完整错误消息.
        """
        msg = str(self)
        # 尝试匹配 "unknown property 'X' on 'Y'" 模式
        m = re.search(r"unknown property '(\w+)'.*?'(\w+)'", msg)
        if m:
            return f"unknown property '{m.group(1)}' on '{m.group(2)}'"
        # 尝试匹配 "property 'X' is not allowed" 模式
        m = re.search(r"property '(\w+)'.*?not allowed.*?tag '?(\w+)'?", msg)
        if m:
            return f"property '{m.group(1)}' not allowed on '{m.group(2)}'"
        # 尝试匹配 "invalid.*property.*'X'" 模式
        m = re.search(r"(unknown property '[^']+'[^.]*)", msg)
        if m:
            return m.group(1)
        # 兜底：返回完整消息
        return msg[:200]


CARDKIT_CONTENT_FAILED = 230099  # 卡片内容创建失败（通用码，需检查子错误）
CARDKIT_ELEMENT_LIMIT = 11310  # 子码: 卡片元素数量超限
CARDKIT_ELEMENT_LIMIT_DIRECT = 300305  # 直报码: 卡片元素数量超限（cardkit_update 返回此码）
CARDKIT_SCHEMA_ERROR = 300315  # 卡片 Schema 非法属性 (unknown property) OR element not found for insert_before
CARDKIT_STREAMING_CLOSED = 300309  # 卡片流式模式已关闭
CARDKIT_SEQUENCE_CONFLICT = 300317  # sequence 冲突
CARDKIT_ELEMENT_NOT_FOUND = 300313  # 元素不存在（add_elements 后服务端尚未持久化时的竞态）
CARDKIT_ELEMENT_NOT_FOUND_ALT = 300314  # delete_elements 不存在的元素
MSG_NOT_FOUND = 1000023  # 消息不存在/已删除

# v1.3.1 fix: 300315 错误码有两种含义：
# 1. 真正的 Schema Error (unknown property) — msg 含 "unknown property" 或 "invalid"
# 2. Element Not Found for insert_before — msg 含 "not find elementID"
# 飞书 API 对 insert_before 引用不存在的元素返回 300315 而非 300313，
# 导致 is_schema_error 误判，把 element not found 当 schema error 处理，
# 清除脏数据放弃重试，内容丢失。
# 修复：is_schema_error 增加文本检查，排除 "not find elementID" 的情况。
_RE_ELEMENT_NOT_FOUND = re.compile(r"not find elementID", re.IGNORECASE)

# ── CardKit 瞬态错误码 — 可自动重试 ──
# 参考 Cheerwhy / openclaw-lark: 这三个错误码是飞书 CardKit 的瞬态错误，
# 通常由服务端内部超时或并发冲突引起，重试后大概率成功。
# 注意：300313 (元素不存在) 不在此列 — 它需要"等待传播后重试"的特殊处理，
# 而非指数退避重试，因此在 is_transient 判断中返回 False，
# 由调用方（drain/seal 逻辑）决定重试策略。
#
# v1.3.4 fix (P1): 新增 99991400 (接口频率限制) — 飞书开放平台对单个 API
# 设有频率限制（如 cardkit_batch_update 每秒 5 次），超限返回 99991400。
# 这是典型的瞬态错误，指数退避重试后通常成功。原实现未包含此码，导致
# 频控时直接失败传播到 controller，controller 仅 reset _first_flush_done
# 而不重试，增加内容延迟。
# 官方文档：https://open.feishu.cn/document/server-docs/api-call-guide/frequency-control
CARDKIT_TRANSIENT_CODES = {
    2200,     # CardKit 内部超时
    1663,     # CardKit 服务端瞬态错误
    300000,   # CardKit 通用内部错误
    99991400, # 接口频率限制（per-API rate limit，HTTP 400）
}

# 瞬态错误重试策略 — 指数退避
_TRANSIENT_RETRY_DELAYS = (0.1, 0.3, 0.6)  # 3 次重试，递增延迟
_TRANSIENT_MAX_RETRIES = len(_TRANSIENT_RETRY_DELAYS)

# 300313 元素不存在错误的专用重试策略 — 短间隔均匀重试
# 生产日志确认：add_elements 成功后 ~1s 内 stream_element 可能返回 300313，
# 这是飞书服务端元素持久化的传播延迟。200ms 间隔 × 3 次足以覆盖大多数场景。
_ELEMENT_NOT_FOUND_RETRY_DELAYS = (0.2, 0.2, 0.2)
_ELEMENT_NOT_FOUND_MAX_RETRIES = len(_ELEMENT_NOT_FOUND_RETRY_DELAYS)


def is_element_limit_error(e: "FeishuAPIError") -> bool:
    """判断 FeishuAPIError 是否为元素超限错误。

    飞书 API 返回两种错误格式：
    - cardkit_update: code=300305 直报
    - batch_update: code=230099 + ErrCode: 11310 子码
    """
    return (
        e.code == CARDKIT_ELEMENT_LIMIT_DIRECT
        or (e.code == CARDKIT_CONTENT_FAILED and e.extract_sub_code() == CARDKIT_ELEMENT_LIMIT)
    )


def is_schema_error(e: "FeishuAPIError") -> bool:
    """判断 FeishuAPIError 是否为卡片 Schema 非法属性错误。

    飞书 API 返回 code=300315 表示卡片 JSON 包含不支持属性，
    例如在 ``plain_text`` 标签上放置 ``icon`` 属性。
    这类错误是永久性的——重试不会成功，需要修正卡片结构。

    v1.3.1 fix: 300315 也用于 insert_before 引用不存在的元素
    ("not find elementID")。这不是真正的 schema error——元素可能
    在之前的操作中已被删除。用文本检查区分两种情况，避免把
    element not found 误当 schema error 处理导致内容丢失。
    """
    if e.code != CARDKIT_SCHEMA_ERROR:
        return False
    # 排除 "not find elementID" 的情况——这是 element not found，不是 schema error
    if _RE_ELEMENT_NOT_FOUND.search(str(e)):
        return False
    return True


def is_element_not_found_error(e: "FeishuAPIError") -> bool:
    """判断 FeishuAPIError 是否为"元素不存在"错误。

    涵盖三种错误码：
    - 300313: stream_element 引用不存在的元素（服务端传播延迟）
    - 300314: delete_elements 删除不存在的元素
    - 300315 + "not find elementID": insert_before 引用不存在的元素
      （飞书用 300315 而非 300313 表示这种情况，v1.3.1 修复）

    生产日志（2026-06-17）发现：Phase 2 的 add_elements 成功后，
    如果 on_completed 在 ~1s 内触发 drain，cardkit_stream_element
    可能返回 300313 "not find elementID : answer_content"。
    这是飞书服务端元素持久化的传播延迟，等待 200ms 后重试通常成功。
    """
    if e.code == CARDKIT_ELEMENT_NOT_FOUND:
        return True
    if e.code == CARDKIT_ELEMENT_NOT_FOUND_ALT:
        return True
    # 300315 + "not find elementID" = insert_before 引用不存在的元素
    if e.code == CARDKIT_SCHEMA_ERROR and _RE_ELEMENT_NOT_FOUND.search(str(e)):
        return True
    return False


@dataclass(frozen=True)
class FeishuClientConfig:
    app_id: str
    app_secret: str
    base_url: str = "https://open.feishu.cn/open-apis"

    def __post_init__(self) -> None:
        if not isinstance(self.app_id, str) or not self.app_id.strip():
            raise ValueError("app_id is required")
        if not isinstance(self.app_secret, str) or not self.app_secret.strip():
            raise ValueError("app_secret is required")


def _is_transient_error(e: FeishuAPIError) -> bool:
    """判断 FeishuAPIError 是否为 CardKit 瞬态错误（可重试）.

    瞬态错误通常由飞书服务端内部超时或并发冲突引起，
    重试后大概率成功。非瞬态错误（频控、元素超限、消息不存在等）
    不应重试。
    """
    if e.code in CARDKIT_TRANSIENT_CODES:
        return True
    # 230099 是通用码，需检查子错误码：11310(元素超限)不可重试
    if e.code == CARDKIT_CONTENT_FAILED:
        sub = e.extract_sub_code()
        return sub is not None and sub not in (CARDKIT_ELEMENT_LIMIT,)
    return False


class FeishuClient:
    """飞书 REST API 封装 — 基于 lark-oapi SDK.

    SDK 自动管理 tenant_access_token 的获取和刷新.
    CardKit 瞬态错误自动重试（指数退避）.
    """

    def __init__(self, config: FeishuClientConfig) -> None:
        self.config = config
        builder = lark.Client.builder().app_id(config.app_id).app_secret(config.app_secret)
        # .domain() 只接受域名（如 https://open.feishu.cn），不带 /open-apis 后缀
        # 默认域名 https://open.feishu.cn 不需要调 .domain()（SDK 默认就是它）
        domain = config.base_url
        if domain and "/open-apis" in domain:
            domain = domain.split("/open-apis")[0]
        if domain and domain != "https://open.feishu.cn":
            builder = builder.domain(domain)
        self._client = builder.build()
        # Probe for async stream_element method (lark-oapi >= 1.x)
        # Use callable() instead of hasattr() — hasattr only checks existence,
        # callable also verifies the attribute can be invoked. If a future SDK
        # version turns 'acontent' into a read-only property, hasattr would
        # still return True but calling it would raise TypeError.
        self._use_async_stream_element = callable(
            getattr(self._client.cardkit.v1.card_element, 'acontent', None)
        )

    async def _retry_transient(
        self,
        operation: str,
        coro_factory: Callable[[], Any],
        *,
        max_retries: int = _TRANSIENT_MAX_RETRIES,
    ) -> Any:
        """执行协程，遇到 CardKit 瞬态错误时自动重试.

        coro_factory: 返回协程的工厂函数（每次重试创建新协程）.
        非瞬态错误直接抛出，不重试.
        """
        last_error: FeishuAPIError | None = None
        for attempt in range(max_retries + 1):
            try:
                result = await coro_factory()
                # v1.1.0: Record successful API call
                try:
                    from ..aowen import record_api_call
                    record_api_call(operation)
                except Exception:
                    _logger.debug("metrics: record_api_call failed", exc_info=True)
                return result
            except FeishuAPIError as e:
                last_error = e
                if not _is_transient_error(e):
                    raise
                if attempt < max_retries:
                    delay = _TRANSIENT_RETRY_DELAYS[attempt]
                    _logger.info(
                        "transient retry: %s attempt=%d/%d code=%s delay=%.2fs",
                        operation, attempt + 1, max_retries, e.code, delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise
            except asyncio.CancelledError:
                # v1.3.4: 不要吞 CancelledError，让它正常传播
                raise
            except _NETWORK_ERROR_BASES as e:
                # v1.3.4 fix (P1): 网络错误（httpx ConnectError/ReadTimeout 等）
                # 是瞬态的，重试后通常成功。原实现只捕获 FeishuAPIError，
                # 网络错误直接传播导致 cardkit_create 失败走 IM 降级。
                if attempt < max_retries:
                    delay = _TRANSIENT_RETRY_DELAYS[attempt]
                    _logger.info(
                        "transient retry (network): %s attempt=%d/%d error=%s delay=%.2fs",
                        operation, attempt + 1, max_retries, type(e).__name__, delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise
            except _TOKEN_ERROR_BASES as e:
                # v1.3.4 fix (P1): token 刷新失败（ObtainAccessTokenException）
                # 可能是网络瞬断导致 token 接口失败，重试通常成功。
                # 如果是凭证错误（永久），所有重试都失败后异常传播。
                if attempt < max_retries:
                    delay = _TRANSIENT_RETRY_DELAYS[attempt]
                    _logger.info(
                        "transient retry (token): %s attempt=%d/%d error=%s delay=%.2fs",
                        operation, attempt + 1, max_retries, type(e).__name__, delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise
        raise last_error  # unreachable, but type-safe

    @staticmethod
    def _check(response: Any, operation: str) -> None:
        """检查 SDK 响应，失败时抛出 FeishuAPIError."""
        if not response.success():
            code = response.code or 0
            msg = response.msg or ""
            # v1.1.0: Record API error metrics
            try:
                from ..aowen import record_api_error
                record_api_error(code, operation)
            except Exception:
                _logger.debug("metrics: record_api_error failed", exc_info=True)
            raise FeishuAPIError(
                _sanitize_message(f"{operation}: code={code}, msg={msg}"),
                code,
            )

    @staticmethod
    def _dumps(obj: Any) -> str:
        return json.dumps(obj, ensure_ascii=False)

    async def send_card_to_chat(self, chat_id: str, card: dict[str, Any]) -> str:
        """发送独立卡片到聊天（非回复），返回 message_id."""
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("interactive")
                .content(self._dumps(card))
                .build()
            )
            .build()
        )
        resp = await self._client.im.v1.message.acreate(request)
        self._check(resp, "send_card_to_chat")
        if resp.data and resp.data.message_id:
            return str(resp.data.message_id)
        raise FeishuAPIError("send_card_to_chat: response missing message_id")

    async def reply_card(self, message_id: str, card: dict[str, Any]) -> str:
        """回复消息，返回 message_id."""
        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(ReplyMessageRequestBody.builder().msg_type("interactive").content(self._dumps(card)).build())
            .build()
        )
        resp = await self._client.im.v1.message.areply(request)
        self._check(resp, "reply_card")
        if resp.data and resp.data.message_id:
            return str(resp.data.message_id)
        raise FeishuAPIError("reply_card: response missing message_id")

    async def reply_text(self, message_id: str, text: str) -> str:
        """回复纯文本消息，返回 message_id."""
        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type("text")
                .content(self._dumps({"text": text}))
                .build()
            )
            .build()
        )
        resp = await self._client.im.v1.message.areply(request)
        self._check(resp, "reply_text")
        if resp.data and resp.data.message_id:
            return str(resp.data.message_id)
        raise FeishuAPIError("reply_text: response missing message_id")

    async def reply_card_by_id(self, message_id: str, card_id: str) -> str:
        """通过 card_id 回复 CardKit 卡片消息，返回 message_id."""
        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type("interactive")
                .content(self._dumps({"type": "card", "data": {"card_id": card_id}}))
                .build()
            )
            .build()
        )
        resp = await self._client.im.v1.message.areply(request)
        self._check(resp, "reply_card_by_id")
        if resp.data and resp.data.message_id:
            return str(resp.data.message_id)
        raise FeishuAPIError("reply_card_by_id: response missing message_id")

    async def update_card(self, message_id: str, card: dict[str, Any]) -> None:
        """PATCH 更新已发送的卡片（IM 降级通道）."""
        request = (
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(PatchMessageRequestBody.builder().content(self._dumps(card)).build())
            .build()
        )
        resp = await self._client.im.v1.message.apatch(request)
        self._check(resp, "update_card")

    async def cardkit_create(self, card: dict[str, Any]) -> str:
        """创建 CardKit 实体，返回 card_id."""
        async def _do():
            request = (
                CreateCardRequest.builder()
                .request_body(CreateCardRequestBody.builder().type("card_json").data(self._dumps(card)).build())
                .build()
            )
            t0 = _time.monotonic()
            resp = await self._client.cardkit.v1.card.acreate(request)
            elapsed_ms = (_time.monotonic() - t0) * 1000
            _logger.debug("perf: feishu_card_create elapsed=%.0fms", elapsed_ms)
            self._check(resp, "cardkit_create")
            if resp.data and resp.data.card_id:
                return str(resp.data.card_id)
            raise FeishuAPIError("cardkit_create: response missing card_id")

        return await self._retry_transient("cardkit_create", _do)

    async def cardkit_stream_element(
        self,
        card_id: str,
        element_id: str,
        content: str,
        *,
        sequence: int = 0,
    ) -> None:
        """流式更新卡片内指定 element 的内容（打字机效果）.

        对 300313 (元素不存在) 错误做专用重试：add_elements 成功后
        飞书服务端可能有传播延迟，短间隔重试 3 次通常能成功。
        若重试仍失败，抛出 FeishuAPIError(code=300313)，调用方可
        改用 batch_update 的 partial_update_element 绕过此问题。
        """
        async def _do():
            body_builder = ContentCardElementRequestBody.builder().content(content)
            body_builder = body_builder.sequence(sequence)
            request = (
                ContentCardElementRequest.builder()
                .card_id(card_id)
                .element_id(element_id)
                .request_body(body_builder.build())
                .build()
            )
            t0 = _time.monotonic()
            if self._use_async_stream_element:
                resp = await self._client.cardkit.v1.card_element.acontent(request)
            else:
                resp = await asyncio.to_thread(
                    self._client.cardkit.v1.card_element.content,
                    request,
                )
            elapsed_ms = (_time.monotonic() - t0) * 1000
            if elapsed_ms > 200:
                _logger.debug(
                    "HLS: perf stream_element card=%s el=%s elapsed=%.0fms",
                    card_id[:12], element_id[:12], elapsed_ms,
                )
            self._check(resp, "cardkit_stream_element")

        # ── 300313 专用重试：短间隔均匀重试 ──
        # 生产日志确认 add_elements 后 ~1s 内 stream_element 可能返回 300313，
        # 这是飞书服务端元素持久化的传播延迟。
        last_error: FeishuAPIError | None = None
        for attempt in range(_ELEMENT_NOT_FOUND_MAX_RETRIES + 1):
            try:
                await self._retry_transient("cardkit_stream_element", _do)
                # 成功时打 DEBUG 日志（降级自 INFO — 流式输出期间每 70ms 一次，
                # 单次会话可产生数十条 INFO 日志，生产环境日志爆炸）
                _logger.debug(
                    "HLS: stream_element OK card=%s el=%s len=%d seq=%d",
                    card_id[:12], element_id[:16], len(content), sequence,
                )
                return
            except FeishuAPIError as e:
                if not is_element_not_found_error(e):
                    raise
                last_error = e
                if attempt < _ELEMENT_NOT_FOUND_MAX_RETRIES:
                    delay = _ELEMENT_NOT_FOUND_RETRY_DELAYS[attempt]
                    _logger.info(
                        "HLS: stream_element 300313 retry card=%s el=%s attempt=%d/%d delay=%.1fs",
                        card_id[:12], element_id[:16],
                        attempt + 1, _ELEMENT_NOT_FOUND_MAX_RETRIES, delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise
        if last_error:
            raise last_error  # unreachable

    async def cardkit_update(
        self,
        card_id: str,
        card: dict[str, Any],
        sequence: int = 0,
    ) -> None:
        """全量更新 CardKit 卡片."""
        async def _do():
            body_builder = UpdateCardRequestBody.builder().card(
                Card.builder().type("card_json").data(self._dumps(card)).build()
            )
            body_builder = body_builder.sequence(sequence)
            request = UpdateCardRequest.builder().card_id(card_id).request_body(body_builder.build()).build()
            resp = await self._client.cardkit.v1.card.aupdate(request)
            self._check(resp, "cardkit_update")

        await self._retry_transient("cardkit_update", _do)

    async def cardkit_batch_update(
        self,
        card_id: str,
        actions: list[dict[str, Any]],
        *,
        sequence: int = 0,
    ) -> None:
        """局部更新 CardKit 卡片（增删改组件）."""
        async def _do():
            body_builder = BatchUpdateCardRequestBody.builder().sequence(sequence).actions(self._dumps(actions))
            request = BatchUpdateCardRequest.builder().card_id(card_id).request_body(body_builder.build()).build()
            t0 = _time.monotonic()
            resp = await self._client.cardkit.v1.card.abatch_update(request)
            elapsed_ms = (_time.monotonic() - t0) * 1000
            _logger.debug("perf: feishu_batch_update card=%s elapsed=%.0fms actions=%d", card_id[:12], elapsed_ms, len(actions))
            self._check(resp, "cardkit_batch_update")

        await self._retry_transient("cardkit_batch_update", _do)

    async def cardkit_close_streaming(
        self,
        card_id: str,
        sequence: int = 0,
        *,
        summary: str = "",
    ) -> None:
        """关闭 CardKit 卡片的流式模式，并可选更新会话摘要.

        关闭流式模式后，飞书会话列表会显示卡片的 summary 文本。
        如果不更新 summary，会话列表会一直显示创建时设置的"处理中..."，
        即使卡片内容已完成。

        Parameters
        ----------
        summary : str
            完成后的会话摘要文本（截断至 120 字符）。
            为空时不更新 summary（保持原值）。

        Notes
        -----
        同时更新 ``content`` 和 ``i18n_content`` 两个摘要字段。
        飞书会根据用户语言偏好显示 ``i18n_content.<locale>``，
        如果只更新 ``content`` 而不更新 ``i18n_content``，中文用户
        在会话列表中会一直看到"处理中..."——这正是 Bug #3 的根因。
        """
        settings: dict[str, Any] = {
            "config": {
                "streaming_mode": False,
            }
        }
        if summary:
            truncated = summary[:120]
            settings["config"]["summary"] = {
                "content": truncated,
                "i18n_content": {
                    "zh_cn": truncated,
                    "en_us": truncated,
                },
            }

        async def _do():
            body_builder = SettingsCardRequestBody.builder().settings(self._dumps(settings))
            body_builder = body_builder.sequence(sequence)
            request = SettingsCardRequest.builder().card_id(card_id).request_body(body_builder.build()).build()
            resp = await self._client.cardkit.v1.card.asettings(request)
            self._check(resp, "cardkit_close_streaming")

        await self._retry_transient("cardkit_close_streaming", _do)

    async def cardkit_update_summary(
        self,
        card_id: str,
        summary: str,
        *,
        sequence: int = 0,
    ) -> None:
        """Update the card summary text WITHOUT closing streaming mode.

        Belt-and-suspenders for the edge case where streaming was already
        closed (e.g. by Feishu TTL auto-close or a CARDKIT_STREAMING_CLOSED
        error) but the summary was never updated from "处理中..." to the
        actual answer text.

        **IMPORTANT**: This is NOT the primary mechanism for updating the
        conversation list preview.  The primary mechanism is passing
        ``summary`` to :meth:`cardkit_close_streaming`, which atomically
        updates the preview when ``streaming_mode`` transitions to ``false``.
        This method is only used when streaming was already closed and we
        need a fallback to update the summary after the fact.

        See: 飞书开放平台 → 卡片2.0 → 流式更新 → 完成后关闭流式更新模式.
        """
        if not summary:
            return
        truncated = summary[:120]
        settings: dict[str, Any] = {
            "config": {
                "summary": {
                    "content": truncated,
                    "i18n_content": {
                        "zh_cn": truncated,
                        "en_us": truncated,
                    },
                },
            },
        }

        async def _do():
            body_builder = SettingsCardRequestBody.builder().settings(self._dumps(settings))
            body_builder = body_builder.sequence(sequence)
            request = SettingsCardRequest.builder().card_id(card_id).request_body(body_builder.build()).build()
            resp = await self._client.cardkit.v1.card.asettings(request)
            self._check(resp, "cardkit_update_summary")

        await self._retry_transient("cardkit_update_summary", _do)

    async def upload_image(self, image_url: str) -> str | None:
        """下载远程图片并上传到飞书，返回 img_key."""
        try:
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(
                None,
                self._download_image,
                image_url,
            )
        except Exception:
            _logger.debug("image upload failed for %s", image_url, exc_info=True)
            return None

        if data is None:
            return None

        # v1.3.2 fix (P1-03): wrap the upload call in try/except to match
        # upload_local_image's error handling. Without this, a network error
        # or auth failure during upload would propagate uncaught.
        try:
            file = io.BytesIO(data)
            request = (
                CreateImageRequest.builder()
                .request_body(CreateImageRequestBody.builder().image_type("message").image(file).build())
                .build()
            )
            resp = await self._client.im.v1.image.acreate(request)
            if resp.success() and resp.data and resp.data.image_key:
                return str(resp.data.image_key)
            return None
        except Exception:
            _logger.debug("image upload (API call) failed for %s", image_url, exc_info=True)
            return None

    async def upload_local_image(self, image_path: str) -> str | None:
        """Upload a local image file to Feishu and return the img_key.

        Used by the image interception wrapper to upload local images
        and add them to card sessions.
        """
        import os
        try:
            if not os.path.exists(image_path):
                return None
            with open(image_path, "rb") as f:
                image_bytes = f.read()
            image_file = io.BytesIO(image_bytes)
            image_file.name = os.path.basename(image_path)
            request = (
                CreateImageRequest.builder()
                .request_body(
                    CreateImageRequestBody.builder()
                    .image_type("message")
                    .image(image_file)
                    .build()
                )
                .build()
            )
            resp = await self._client.im.v1.image.acreate(request)
            if resp.success() and resp.data and resp.data.image_key:
                return str(resp.data.image_key)
            return None
        except Exception:
            _logger.debug("local image upload failed for %s", image_path, exc_info=True)
            return None

    @staticmethod
    def _download_image(url: str, timeout: int = 15) -> bytes | None:
        """同步下载图片（在线程池中运行）."""
        try:
            req = Request(url, headers={"User-Agent": "hermes-lark-streaming/1.0"})
            with urlopen(req, timeout=timeout) as resp:
                if resp.status != 200:
                    return None
                return bytes(resp.read())
        except (URLError, OSError):
            _logger.debug("image download failed: %s", url)
            return None
