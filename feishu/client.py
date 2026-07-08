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
    """飞书 API 错误，携带 API 错误码和 log_id."""

    def __init__(self, message: str, code: int = 0, log_id: str = "") -> None:
        super().__init__(message)
        self.code = code
        # v1.3.4: 飞书 log_id，用于开放平台后台排查请求链路
        self.log_id = log_id

    def __str__(self) -> str:
        base = super().__str__()
        if self.log_id:
            return f"{base} [log_id={self.log_id}]"
        return base

    def extract_sub_code(self) -> int | None:
        """从 msg 字符串中提取子错误码."""
        m = re.search(r"ErrCode:\s*(\d+)", str(self))
        if m:
            return int(m.group(1))
        return None

    def extract_schema_detail(self) -> str:
        """从 300315 Schema 错误中提取具体非法属性信息."""
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
_RE_ELEMENT_NOT_FOUND = re.compile(r"not find elementID", re.IGNORECASE)

# 参考 Cheerwhy / openclaw-lark: 这三个错误码是飞书 CardKit 的瞬态错误，
# v1.3.4 fix (P1): 新增 99991400 (接口频率限制) — 飞书开放平台对单个 API
CARDKIT_TRANSIENT_CODES = {
    2200,     # CardKit 内部超时
    1663,     # CardKit 服务端瞬态错误
    300000,   # CardKit 通用内部错误
    99991400, # 接口频率限制（per-API rate limit，HTTP 400）
}

# 瞬态错误重试策略 — 指数退避
_TRANSIENT_RETRY_DELAYS = (0.1, 0.3, 0.6)  # 3 次重试，递增延迟
_TRANSIENT_MAX_RETRIES = len(_TRANSIENT_RETRY_DELAYS)

_ELEMENT_NOT_FOUND_RETRY_DELAYS = (0.2, 0.2, 0.2)
_ELEMENT_NOT_FOUND_MAX_RETRIES = len(_ELEMENT_NOT_FOUND_RETRY_DELAYS)

def is_element_limit_error(e: "FeishuAPIError") -> bool:
    """判断 FeishuAPIError 是否为元素超限错误。"""
    return (
        e.code == CARDKIT_ELEMENT_LIMIT_DIRECT
        or (e.code == CARDKIT_CONTENT_FAILED and e.extract_sub_code() == CARDKIT_ELEMENT_LIMIT)
    )

def is_schema_error(e: "FeishuAPIError") -> bool:
    """判断 FeishuAPIError 是否为卡片 Schema 非法属性错误。"""
    if e.code != CARDKIT_SCHEMA_ERROR:
        return False
    # 排除 "not find elementID" 的情况——这是 element not found，不是 schema error
    if _RE_ELEMENT_NOT_FOUND.search(str(e)):
        return False
    return True

def is_element_not_found_error(e: "FeishuAPIError") -> bool:
    """判断 FeishuAPIError 是否为"元素不存在"错误。"""
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
    """判断 FeishuAPIError 是否为 CardKit 瞬态错误（可重试）."""
    if e.code in CARDKIT_TRANSIENT_CODES:
        return True
    # 230099 是通用码，需检查子错误码：11310(元素超限)不可重试
    if e.code == CARDKIT_CONTENT_FAILED:
        sub = e.extract_sub_code()
        return sub is not None and sub not in (CARDKIT_ELEMENT_LIMIT,)
    return False

class FeishuClient:
    """飞书 REST API 封装 — 基于 lark-oapi SDK."""

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
        """执行协程，遇到 CardKit 瞬态错误时自动重试."""
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
        """检查 SDK 响应，失败时抛出 FeishuAPIError（携带 log_id）."""
        if not response.success():
            code = response.code or 0
            msg = response.msg or ""
            # 返回 None（SDK bug），需从 response.error dict 兜底提取。
            log_id = ""
            try:
                log_id = response.get_log_id() or ""
            except Exception:
                pass
            if not log_id:
                # SDK async 路径 bug 兜底：error 是 dict，含 log_id 字段
                err = getattr(response, 'error', None)
                if isinstance(err, dict):
                    log_id = err.get("log_id", "") or ""
                elif isinstance(err, str):
                    # error 可能是 JSON 字符串或含 log_id 的文本
                    import re as _re
                    m = _re.search(r'log_id["\']?\s*[:=]\s*["\']?([A-Za-z0-9]{20,})', err)
                    if m:
                        log_id = m.group(1)
            # v1.1.0: Record API error metrics
            try:
                from ..aowen import record_api_error
                record_api_error(code, operation)
            except Exception:
                _logger.debug("metrics: record_api_error failed", exc_info=True)
            raise FeishuAPIError(
                _sanitize_message(f"{operation}: code={code}, msg={msg}"),
                code,
                log_id=log_id,
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
        """PATCH 更新已发送的卡片（IM PATCH 通道）."""
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
        """流式更新卡片内指定 element 的内容（打字机效果）."""
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
                pass
            self._check(resp, "cardkit_stream_element")

        last_error: FeishuAPIError | None = None
        for attempt in range(_ELEMENT_NOT_FOUND_MAX_RETRIES + 1):
            try:
                await self._retry_transient("cardkit_stream_element", _do)
                # 成功时打 DEBUG 日志（降级自 INFO — 流式输出期间每 70ms 一次，
                # 单次会话可产生数十条 INFO 日志，生产环境日志爆炸）
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
        """关闭 CardKit 卡片的流式模式，并可选更新会话摘要."""
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
        """Update the card summary text WITHOUT closing streaming mode."""
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
        """Upload a local image file to Feishu and return the img_key."""
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
