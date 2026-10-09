"""E2E tests for Clarify interactive card.

历史回归（v1.3.0 生产 bug）：LLM 把 choices 传成 dict-repr 字符串
``"{'id': 1, 'path': '/mnt/nas/backup1'}"``，未归一化时 lark_md 会把
``{'`` 渲染成 ``{id':1)`` 乱码。cardkit 构建器内部 normalize + escape
（defense in depth），测试同时覆盖卡片构建与 clarify.send_clarify_card
正式发送路径。

V2 说明：V1 的 TestClarifyDispatchPatch（断言 monkey-patch 包装安装）
随 patching/ 层一并退役 —— V2 的 clarify 走 FeishuStreamingAdapter
重写 send_clarify 后的正式路径（clarify.py，内部 try/except 导入
tools/gateway，无 hermes 环境安全）。这里直接驱动
clarify.send_clarify_card(adapter=轻量 stub, client=runner.client, ...)。
"""

from __future__ import annotations

import pytest
from hermes_lark_streaming.cardkit import (
    build_clarify_card,
    build_clarify_confirmed_card,
    build_clarify_submitted_card,
)

# 生产 bug 日志中的原始 choices（dict-repr 字符串）
_RAW_DICT_REPR_CHOICES = [
    "{'id': 1, 'path': '/mnt/nas/backup1'}",
    "{'id': 2, 'path': '/mnt/nas/backup2'}",
    "{'id': 3, 'path': '/mnt/nas/backup3'}",
    "{'id': 4, 'path': '/mnt/nas/backup4'}",
]


class _StubAdapter:
    """轻量 adapter stub — send_clarify_card 只把 adapter 当 opaque 参数."""


def _last_card_of(runner, op: str) -> dict | None:
    """mock 模式：取最近一次指定 op 调用携带的卡片 JSON（real 模式无记录）."""
    if runner.is_real_mode:
        return None
    calls = [c for c in runner.mock_server.call_log if c["op"] == op]
    return calls[-1].get("card") if calls else None


class TestClarifyCardNormalization:
    """卡片构建器层：dict-repr / 特殊字符归一化与转义（沿用 V1 覆盖）."""

    async def test_clarify_card_with_dict_repr_choices(self, runner):
        """dict-repr choices 必须被归一化为可读路径，而非乱码."""
        card = build_clarify_card(
            question="确认 NAS 访问路径，避免我朝错误方向配置",
            choices=_RAW_DICT_REPR_CHOICES,
            clarify_id="e2e_test_clarify_001",
        )

        elements = card["body"]["elements"]

        # markdown 选项列表：应含可读路径，不应含 dict-repr 乱码
        md_el = next(e for e in elements if e.get("tag") == "markdown")
        md_content = md_el["content"]
        assert "/mnt/nas/backup1" in md_content
        assert "/mnt/nas/backup2" in md_content
        assert "/mnt/nas/backup3" in md_content
        assert "/mnt/nas/backup4" in md_content

        # 不应含未转义的 { （bug 触发条件；\{ 形式安全）
        unescaped = md_content.replace("\\{", "").replace("\\}", "")
        assert "{" not in unescaped

        # select_static 下拉用 plain_text（无需转义）
        select_el = next(e for e in elements if e.get("tag") == "select_static")
        for opt in select_el["options"]:
            assert "/mnt/nas/backup" in opt["text"]["content"]

        # 发送验证（mock 记录卡片 / real 验证 API 接受）
        msg_id = await runner.client.send_card_to_chat(runner.chat_id, card)
        assert msg_id

    async def test_clarify_card_with_special_chars_in_question(self, runner):
        """question 含花括号必须转义（v1.3.0 Round 2 修复）."""
        card = build_clarify_card(
            question="Confirm {'id': 1} configuration?",
            choices=["Yes", "No"],
            clarify_id="e2e_test_clarify_002",
        )

        elements = card["body"]["elements"]
        content = elements[0]["text"]["content"]
        assert "\\{" in content
        unescaped = content.replace("\\{", "").replace("\\}", "")
        assert "{" not in unescaped

        msg_id = await runner.client.send_card_to_chat(runner.chat_id, card)
        assert msg_id

    async def test_clarify_submitted_card_escaped(self, runner):
        """提交态卡片：选中文本特殊字符转义（CallBackCard 响应体，不走 IM API）."""
        card = build_clarify_submitted_card(
            question="Which option?",
            selected="/mnt/nas/backup[1]",
            clarify_id="e2e_test_clarify_003",
        )

        elements = card["body"]["elements"]
        content = elements[1]["text"]["content"]
        assert "\\[" in content
        assert "\\]" in content

    async def test_clarify_confirmed_card_escaped(self, runner):
        """确认态卡片：特殊字符转义."""
        card = build_clarify_confirmed_card(
            question="Which option?",
            selected="path/to/{config}",
        )

        elements = card["body"]["elements"]
        content = elements[1]["text"]["content"]
        assert "\\{" in content
        assert "\\}" in content

        msg_id = await runner.client.send_card_to_chat(runner.chat_id, card)
        assert msg_id

    async def test_clarify_card_normal_choices_unchanged(self, runner):
        """正常字符串 choices 不产生转义副作用."""
        card = build_clarify_card(
            question="Which deployment target?",
            choices=["staging", "production"],
            clarify_id="e2e_test_clarify_004",
        )

        elements = card["body"]["elements"]
        md_el = next(e for e in elements if e.get("tag") == "markdown")
        md_content = md_el["content"]
        assert "staging" in md_content
        assert "production" in md_content
        assert "\\" not in md_content

        msg_id = await runner.client.send_card_to_chat(runner.chat_id, card)
        assert msg_id


class TestClarifySendPath:
    """V2 正式发送路径：clarify.send_clarify_card（adapter 重写 send_clarify 的实现）.

    V1 的 patching 安装断言已随 patching/ 层退役；本组测试验证
    send_clarify_card 的行为契约：卡片发送 / 注册表登记 / reply 分支 /
    归一化，且在无 hermes 环境安全（内部 try/except 导入）。
    """

    def _cleanup_clarify(self, clarify_mod, clarify_id: str) -> None:
        clarify_mod._cleanup(clarify_id)

    async def test_send_clarify_card_direct_send(self, runner):
        from hermes_lark_streaming import clarify as clarify_mod

        cid = "e2e_clarify_direct_001"
        try:
            result = await clarify_mod.send_clarify_card(
                adapter=_StubAdapter(), client=runner.client, chat_id=runner.chat_id,
                question="E2E：选择一个部署目标",
                choices=["staging", "production"],
                clarify_id=cid,
            )
            # 有 hermes 环境 → SendResult(success)；无 hermes → None（卡片已发送）
            assert result is None or bool(getattr(result, "success", False))

            if not runner.is_real_mode:
                assert runner.get_call_count("send_card_to_chat") == 1
                card = _last_card_of(runner, "send_card_to_chat")
                assert card is not None and "staging" in str(card)

            # 注册表登记（三态流程的服务端记忆）
            assert clarify_mod._clarify_questions.get(cid) == "E2E：选择一个部署目标"
            assert clarify_mod._clarify_choices.get(cid) == ["staging", "production"]
            assert clarify_mod._clarify_card_msg_ids.get(cid)
        finally:
            self._cleanup_clarify(clarify_mod, cid)

    async def test_send_clarify_card_reply_branch(self, runner):
        """metadata 带 reply_to → 走 reply_card 分支（回复锚点消息）."""
        from hermes_lark_streaming import clarify as clarify_mod

        cid = "e2e_clarify_reply_001"
        try:
            result = await clarify_mod.send_clarify_card(
                adapter=_StubAdapter(), client=runner.client, chat_id=runner.chat_id,
                question="E2E：回复式澄清",
                choices=["A", "B"],
                clarify_id=cid,
                metadata={"reply_to": "om_anchor_001"},
            )
            assert result is None or bool(getattr(result, "success", False))

            if not runner.is_real_mode:
                assert runner.get_call_count("reply_card") == 1
                assert runner.get_call_count("send_card_to_chat") == 0
            assert clarify_mod._clarify_card_msg_ids.get(cid)
        finally:
            self._cleanup_clarify(clarify_mod, cid)

    async def test_send_clarify_card_normalizes_dict_repr_choices(self, runner):
        """生产 bug 场景走完整发送路径：dict-repr choices 归一化后入卡."""
        from hermes_lark_streaming import clarify as clarify_mod

        cid = "e2e_clarify_normalize_001"
        try:
            result = await clarify_mod.send_clarify_card(
                adapter=_StubAdapter(), client=runner.client, chat_id=runner.chat_id,
                question="确认 NAS 访问路径，避免我朝错误方向配置",
                choices=_RAW_DICT_REPR_CHOICES,
                clarify_id=cid,
            )
            assert result is None or bool(getattr(result, "success", False))

            if not runner.is_real_mode:
                card = _last_card_of(runner, "send_card_to_chat")
                assert card is not None, "卡片未发送"
                md = next(e for e in card["body"]["elements"] if e.get("tag") == "markdown")
                assert "/mnt/nas/backup1" in md["content"]
                unescaped = md["content"].replace("\\{", "").replace("\\}", "")
                assert "{" not in unescaped, "发送卡片仍含未转义花括号（bug 回归）"

            assert clarify_mod._clarify_choices.get(cid) == [
                "/mnt/nas/backup1", "/mnt/nas/backup2", "/mnt/nas/backup3", "/mnt/nas/backup4",
            ]
        finally:
            self._cleanup_clarify(clarify_mod, cid)

    async def test_send_clarify_card_without_choices(self, runner):
        """自由输入型澄清（choices=None）→ 卡片仅含问题 + 输入框."""
        from hermes_lark_streaming import clarify as clarify_mod

        cid = "e2e_clarify_input_001"
        try:
            result = await clarify_mod.send_clarify_card(
                adapter=_StubAdapter(), client=runner.client, chat_id=runner.chat_id,
                question="请描述目标路径",
                choices=None,
                clarify_id=cid,
            )
            assert result is None or bool(getattr(result, "success", False))

            if not runner.is_real_mode:
                card = _last_card_of(runner, "send_card_to_chat")
                assert card is not None
                assert "请描述目标路径" in str(card)
                tags = {e.get("tag") for e in card["body"]["elements"]}
                assert "input" in tags, f"自由输入澄清卡应含 input 元素: {tags}"

            assert clarify_mod._clarify_questions.get(cid) == "请描述目标路径"
        finally:
            self._cleanup_clarify(clarify_mod, cid)


@pytest.mark.real_feishu
class TestClarifyRealModeMarker:
    """占位标记类：real_feishu marker 演示（conftest 注册），保持 marker 语义存活."""

    async def test_marker_only_smoke(self, runner):
        assert runner.client is not None
