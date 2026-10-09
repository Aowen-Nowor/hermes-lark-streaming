"""End-to-end test framework for hermes-lark-streaming (V2).

V2.0.0: 插件出站为纯 platform 零钩子架构，E2E 直接驱动 FrameComposer
（与 adapter.py 出站语义层同款接线），不经过任何 controller / monkey-patch。

Architecture:
  - MockFeishuServer: 内存模拟 Feishu CardKit + IM API（方法签名与
    FeishuClient 完全兼容，可直接作为 composer 的 client）
  - E2ETestRunner: 编排 建卡 → 流式 → 状态面板 → 两步定稿 全流程，
    帧驱动复用 classify_send / classify_edit 真实输出
  - mock / real 双模式：环境变量自动切换，测试代码完全一致

Usage:
  from tests.e2e.framework import E2ETestRunner

  async def test_simple_answer(runner):   # runner 由 conftest 提供
      session = await runner.start_message("hello")
      await runner.feed_answer(session, "Hello world!")
      await runner.complete(session, answer="Hello world!")
      assert "Hello world!" in runner.get_answer_text(session)
"""
