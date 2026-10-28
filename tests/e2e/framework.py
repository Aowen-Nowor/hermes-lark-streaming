"""E2E test framework — V2 FrameComposer 直驱（mock / real 双模式）.

V2.0.0：插件出站架构从 StreamingController（monkey-patch）重构为纯
platform 零钩子的 FrameComposer。本 runner 与 adapter.py 出站语义层
完全同款接线（与 scripts/e2e_v2.py 同构）：

    client（MockFeishuServer 或真实 FeishuClient）
      → adapter._CardFactory(cfg).streaming / .final   （卡片构建）
      → FrameComposer(get_client, build_card, build_final_card, config)

帧驱动复用 classify_send / classify_edit 的真实输出，测试代码在
mock / real 两种模式下完全一致——唯一区别是 client 打到内存 mock
还是真实飞书 API。

模式切换（自动）：
  FEISHU_E2E_APP_ID + FEISHU_E2E_APP_SECRET + FEISHU_E2E_CHAT_ID 全设置
  → real 模式（真实飞书）；否则 mock 模式（内存 MockFeishuServer）。

V2 与 V1 runner 的差异：
  - 无 controller / session 对象；start_message 返回轻量 E2ESession
  - 卡片经 send_card_by_id_to_chat 直发聊天，不再需要 anchor 消息
  - answer 帧为全量语义（edit 替换全文；分段 send 帧由引擎 "\n\n" 追加）
  - engine 状态注册表按 chat_id 索引 —— 测试间 runner.reset() 隔离

V1 遗留能力保留：真飞书模式帧间限速（_pace）+ 测试间 2 秒 guard
（见 conftest._rate_limit_guard）；降级路径（client 为 None →
handle_send 返回 None 原生兜底）可通过 runner.set_client(None) 模拟。

Usage:
    from tests.e2e.framework import E2ETestRunner

    async def test_simple_answer(runner):      # runner 由 conftest 提供
        session = await runner.start_message("hello")
        await runner.feed_answer(session, "Hello world!")
        await runner.complete(session, answer="Hello world!")
        runner.assert_card_created(session)
        await runner.assert_card_sealed(session)
        assert "Hello world!" in runner.get_answer_text(session)
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any

from .mock_feishu import MockCardState, MockFeishuServer

_logger = logging.getLogger("hermes_lark_streaming")

_REAL_FRAME_INTERVAL_SEC = 0.15  # 真飞书帧间限速（单卡 stream/update 限 10 次/秒）


def has_real_feishu_creds() -> bool:
    """real 模式判定：APP_ID + APP_SECRET + CHAT_ID 全设置（V2 无需 OPEN_ID）。"""
    return bool(
        os.environ.get("FEISHU_E2E_APP_ID")
        and os.environ.get("FEISHU_E2E_APP_SECRET")
        and os.environ.get("FEISHU_E2E_CHAT_ID")
    )


@dataclass
class E2ESession:
    """轻量 turn 句柄（V2 无 controller session，仅记录 chat / 卡片 id）."""

    chat_id: str
    message_id: str = ""      # 卡片作为飞书消息的 id（handle_send 返回值）
    card_id: str = ""         # CardKit 实体 id
    use_open_id: bool = False
    last_result: Any = None   # 最近一次 handle_send / handle_edit 的 SendResult | None

    @property
    def ok(self) -> bool:
        """该帧是否被卡片通道消费（建卡 / 更新成功）。"""
        return bool(self.message_id)


class E2ETestRunner:
    """V2 E2E 编排器 — 驱动 FrameComposer 完成 建卡 → 流式 → 定稿 全流程.

    mock 模式（默认）：get_client → MockFeishuServer（方法签名与
    FeishuClient 兼容，见 mock_feishu.py）；real 模式：get_client →
    真实 FeishuClient（hermes_lark_streaming.feishu.client）。
    """

    def __init__(self) -> None:
        self.mock_server = MockFeishuServer()
        self._use_real_feishu = has_real_feishu_creds()
        self._client: Any = None
        self._composer: Any = None
        self._config: Any = None
        self._sessions: dict[str, E2ESession] = {}

    # ── 生命周期 ────────────────────────────────────────────────────────
    async def setup(self) -> None:
        """构造 _CardFactory + FrameComposer（与 adapter.__init__ 同款接线）."""
        from hermes_lark_streaming.composer.engine import FrameComposer
        from hermes_lark_streaming.plugin.adapter import _CardFactory

        try:
            from hermes_lark_streaming.config.reader import Config

            Config._instance = None  # 与 tests/conftest 的单例重置语义一致
            self._config = Config()
        except Exception:  # noqa: BLE001 — 配置缺失时工厂走默认值
            self._config = None

        if self._use_real_feishu:
            self._client = self._build_real_client()
            _logger.info("HLS e2e: using REAL Feishu API (app_id=%s...)",
                         os.environ.get("FEISHU_E2E_APP_ID", "")[:8])
        else:
            # MockFeishuServer 方法签名与 FeishuClient 兼容，直接作 client
            self._client = self.mock_server
            _logger.info("HLS e2e: using MOCK Feishu server")

        factory = _CardFactory(self._config)
        self._composer = FrameComposer(
            get_client=self._get_card_client,
            build_card=factory.streaming,
            build_final_card=factory.final,
            config=self._config,
            clarify_suspend_sec=0.0,
        )

        if self._use_real_feishu:
            await self._real_connectivity_ping()

    async def teardown(self) -> None:
        self.reset()
        self._client = None
        self._composer = None

    def reset(self) -> None:
        """测试间隔离：清 engine 状态注册表 + mock 调用记录 + session 表.

        engine 状态按 chat_id 索引（跨测试残留会让新 turn 复用上一张
        卡片的状态对象），每个测试开始前必须清空。
        """
        if self._composer is not None:
            self._composer.clear()
        self.mock_server.reset()
        self._sessions.clear()

    # ── client 管理 ─────────────────────────────────────────────────────
    def _get_card_client(self) -> Any:
        return self._client

    def set_client(self, client: Any | None) -> None:
        """注入 / 撤销卡片客户端（None → 降级路径：handle_send 返回 None 原生兜底）."""
        self._client = client

    def _build_real_client(self) -> Any:
        from hermes_lark_streaming.feishu.client import FeishuClient, FeishuClientConfig

        return FeishuClient(FeishuClientConfig(
            app_id=os.environ.get("FEISHU_E2E_APP_ID", ""),
            app_secret=os.environ.get("FEISHU_E2E_APP_SECRET", ""),
            base_url=os.environ.get("FEISHU_E2E_BASE_URL", "https://open.feishu.cn/open-apis"),
        ))

    async def _real_connectivity_ping(self) -> None:
        """真飞书连通性自检（V2 无需 anchor 消息，仅发一条文本标记测试开始）."""
        try:
            await self._client.send_text_to_chat(self.chat_id, "[e2e v2 runner ready]")
        except Exception as e:
            raise RuntimeError(
                f"E2E: 真飞书连通性自检失败 chat={self.chat_id[:12]}。"
                "请检查 bot 是否在群内且有发送权限。"
                f"错误: {e}"
            ) from e

    # ── 基本属性 ────────────────────────────────────────────────────────
    @property
    def is_real_mode(self) -> bool:
        """True = 运行在真实飞书 API 上."""
        return self._use_real_feishu

    @property
    def composer(self) -> Any:
        """FrameComposer 实例（供低层断言 / 直接驱动）."""
        return self._composer

    @property
    def client(self) -> Any:
        """当前卡片客户端（MockFeishuServer 或真实 FeishuClient）。"""
        return self._client

    @property
    def chat_id(self) -> str:
        """默认群聊 chat_id（mock: oc_test_chat / real: FEISHU_E2E_CHAT_ID）."""
        return self._resolve_chat()

    def _resolve_chat(self, use_open_id: bool = False, chat_id: str = "") -> str:
        """解析目标 chat：显式 chat_id 优先；real 模式查环境；mock 用固定测试 id."""
        if chat_id:
            return chat_id
        if self._use_real_feishu:
            if use_open_id:
                open_id = os.environ.get("FEISHU_E2E_OPEN_ID", "")
                if not open_id:
                    raise RuntimeError("真飞书模式需要 FEISHU_E2E_OPEN_ID 环境变量（私聊场景）")
                return open_id
            return os.environ.get("FEISHU_E2E_CHAT_ID", "")
        return "ou_test_user" if use_open_id else "oc_test_chat"

    async def _pace(self) -> None:
        """真飞书限速：帧间 0.15s（单卡 stream/update 10 次/秒）。mock 不等待。"""
        if self._use_real_feishu:
            await asyncio.sleep(_REAL_FRAME_INTERVAL_SEC)

    # ── 帧驱动（与 adapter.py 出站语义层同款）───────────────────────────
    async def start_message(
        self, content: str = "test message", *, use_open_id: bool = False, chat_id: str = "",
    ) -> E2ESession:
        """首条 answer 帧（expect_edits=True → CH_STREAM）→ 建卡.

        返回 E2ESession（message_id = 卡片消息 id；降级 / 原生路径时为空串）。
        """
        from hermes_lark_streaming.composer.classify import classify_send

        chat = self._resolve_chat(use_open_id, chat_id)
        meta: dict[str, Any] = {"expect_edits": True}
        result = await self._composer.handle_send(chat, content, None, meta, classify_send(content, meta))
        session = E2ESession(chat_id=chat, use_open_id=use_open_id, last_result=result)
        if result is not None and getattr(result, "message_id", ""):
            session.message_id = str(result.message_id)
            self._sessions[session.message_id] = session
            st = self._composer.state_by_message(session.message_id)
            if st is not None:
                session.card_id = st.card_id
        await self._pace()
        return session

    async def send_segment(self, session: E2ESession, text: str) -> None:
        """hermes 分段发送语义：后续 CH_STREAM send 帧 → 引擎以 "\\n\\n" 追加段落."""
        from hermes_lark_streaming.composer.classify import classify_send

        meta: dict[str, Any] = {"expect_edits": True}
        session.last_result = await self._composer.handle_send(
            session.chat_id, text, None, meta, classify_send(text, meta),
        )
        await self._pace()

    async def feed_answer(self, session: E2ESession, text: str) -> None:
        """answer 流式更新（hermes edit 全量语义：text 为当前全量文本）."""
        from hermes_lark_streaming.composer.classify import classify_edit

        session.last_result = await self._composer.handle_edit(
            session.chat_id, session.message_id, text, False, classify_edit(text, False),
        )
        await self._pace()

    async def feed_reasoning(self, session: E2ESession | str, text: str) -> None:
        """思考帧："💬 " + text（_interim_send → CH_PROGRESS；建卡前进 pending 暂存）.

        建卡前可传 chat_id 字符串或 pending_session() 占位句柄。
        """
        from hermes_lark_streaming.composer.classify import classify_send

        chat, target = self._chat_of(session)
        content = "💬 " + text
        meta: dict[str, Any] = {"_interim_send": True}
        result = await self._composer.handle_send(chat, content, None, meta, classify_send(content, meta))
        if target is not None:
            target.last_result = result
        await self._pace()

    async def feed_tool_update(
        self, session: E2ESession | str, tool_name: str, detail: str = "",
    ) -> None:
        """工具进度帧：🔧 {tool}："{detail}"（hermes progress 模板同款）.

        建卡前可传 chat_id 字符串或 pending_session() 占位句柄。
        """
        from hermes_lark_streaming.composer.classify import classify_send

        chat, target = self._chat_of(session)
        content = f'🔧 {tool_name}："{detail}"'
        meta: dict[str, Any] = {"_interim_send": True}
        result = await self._composer.handle_send(chat, content, None, meta, classify_send(content, meta))
        if target is not None:
            target.last_result = result
        await self._pace()

    async def complete(
        self, session: E2ESession, answer: str = "", *, error_message: str = "",
    ) -> None:
        """turn 定稿（edit finalize=True → stream_element 终值 + close_streaming）.

        V2 引擎无错误卡片语义：error_message 仅留痕，不改变定稿行为。
        answer 为空时以 answer 缓冲区当前内容定稿。
        """
        from hermes_lark_streaming.composer.classify import classify_edit

        if error_message:
            _logger.info("e2e: complete(error_message=%r) — V2 引擎按普通定稿处理", error_message)
        session.last_result = await self._composer.handle_edit(
            session.chat_id, session.message_id, answer, True, classify_edit(answer, True),
        )
        await self._pace()

    async def send_raw(
        self, content: str, metadata: dict[str, Any] | None, chat_id: str = "",
    ) -> Any:
        """直接驱动 composer.handle_send（plain / interim 帧断言用；原生路径返回 None）."""
        from hermes_lark_streaming.composer.classify import classify_send

        chat = self._resolve_chat(False, chat_id)
        return await self._composer.handle_send(chat, content, None, metadata, classify_send(content, metadata))

    def pending_session(self, use_open_id: bool = False, chat_id: str = "") -> E2ESession:
        """建卡前占位句柄（progress 帧将进 engine 的 pending_status 暂存）."""
        return E2ESession(chat_id=self._resolve_chat(use_open_id, chat_id), use_open_id=use_open_id)

    def _chat_of(self, session: E2ESession | str) -> tuple[str, E2ESession | None]:
        """兼容建卡前场景：传 chat_id 字符串（无句柄）或 session 均可."""
        if isinstance(session, str):
            return session, None
        return session.chat_id, session

    # ── 检视助手 ────────────────────────────────────────────────────────
    def get_state(self, session: E2ESession) -> Any:
        """engine 的 CardTurnState（按 message_id 反查；含 answer_text / status_lines / finalized）."""
        if self._composer is None or not session.message_id:
            return None
        return self._composer.state_by_message(session.message_id)

    def get_card(self, session: E2ESession) -> MockCardState | None:
        """mock：内存卡片状态；real：None（真实飞书无法回查卡片内容）."""
        if self._use_real_feishu or not session.card_id:
            return None
        return self.mock_server.get_card(session.card_id)

    def get_answer_text(self, session: E2ESession) -> str:
        """answer 当前内容.

        mock：读 mock 卡片的 answer 元素（验证 stream_element 真值）；
        real：读 engine 状态缓冲（真实飞书唯一可用来源）。
        """
        if not self._use_real_feishu:
            card = self.get_card(session)
            if card is None:
                return ""
            from hermes_lark_streaming.cardkit.elements import ANSWER_ELEMENT_ID

            return str(card.elements.get(ANSWER_ELEMENT_ID, {}).get("content", "") or "")
        st = self.get_state(session)
        return str(st.answer_text or "") if st is not None else ""

    def get_status_lines(self, session: E2ESession) -> list[str]:
        """engine 状态面板行（工具 / 思考 / interim；两种模式同源）."""
        st = self.get_state(session)
        return list(st.status_lines) if st is not None else []

    def get_call_count(self, operation: str) -> int:
        """统计 mock API 调用次数（real 模式恒 0：不拦截真实调用）."""
        if self._use_real_feishu:
            return 0
        return sum(1 for c in self.mock_server.call_log if c["op"] == operation)

    def get_total_api_calls(self) -> int:
        """mock 模式下全部 API 调用次数."""
        if self._use_real_feishu:
            return 0
        return len(self.mock_server.call_log)

    # ── 常用断言 ────────────────────────────────────────────────────────
    def assert_card_created(self, session: E2ESession) -> None:
        """断言卡片已建立（handle_send 消费了首帧并在 mock 注册）."""
        assert session.message_id, "卡片未创建（handle_send 未返回 message_id）"
        if not self._use_real_feishu:
            assert self.mock_server.get_card(session.card_id) is not None, \
                f"卡片 {session.card_id} 未在 mock 服务注册"

    async def assert_card_sealed(self, session: E2ESession) -> None:
        """断言卡片已定稿：engine finalized + mock streaming_mode 关闭 + summary 非空."""
        st = self.get_state(session)
        assert st is not None and st.finalized, "engine 状态未 finalized"
        if not self._use_real_feishu:
            card = self.get_card(session)
            assert card is not None, "卡片未找到"
            assert not card.streaming_mode, \
                f"卡片 {session.card_id} streaming_mode 未关闭"
            assert card.summary, "定稿 summary 为空"
