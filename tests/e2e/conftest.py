"""E2E test fixtures — module 级 runner + real 模式限速 guard + 模式标注.

V2.0.0: runner 驱动 FrameComposer（controller / session 清理已随 V1 退役），
测试间隔离改为 composer.clear() + mock_server.reset() —— engine 状态注册表
按 chat_id 索引，残留会让下一个测试的 turn 复用上一张卡片的状态对象。

module 级 runner（沿用 v1.3.6 思路）：同一文件所有测试共享 1 个 runner，
真飞书模式下 setup 只做 1 次连通性自检（V2 卡片直发聊天，无 anchor 消息）。

Test code is identical in both modes — only the underlying client differs.
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from .framework import E2ETestRunner, has_real_feishu_creds

# ── Module-level runner — setup 一次，全文件共享 ────────────────────────────

@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def _module_runner():
    """module 级 runner — 真飞书模式下仅 setup 时发 1 条连通性自检文本."""
    runner = E2ETestRunner()
    await runner.setup()
    yield runner
    await runner.teardown()


@pytest.fixture
def runner(_module_runner):
    """每个测试拿到共享 runner；测试前后清空 composer 状态注册表保证隔离."""
    _module_runner.reset()
    yield _module_runner
    _module_runner.reset()


@pytest.fixture(autouse=True)
async def _rate_limit_guard():
    """真飞书模式下测试间加 2 秒延迟，避免触发飞书 API 频率限制.

    飞书 CardKit API 限制：1000 次/分 & 50 次/秒（流式模式豁免）、
    单卡片级 10 次/秒（帧间限速由 runner._pace 承担）；create/send/close
    计入配额。mock 模式不需要延迟。
    """
    yield
    if has_real_feishu_creds():
        await asyncio.sleep(2.0)


# ── Pytest configuration ────────────────────────────────────────────────────

def pytest_configure(config):
    """Register custom markers and log mode."""
    config.addinivalue_line(
        "markers",
        "real_feishu: test runs against real Feishu API (auto-detected from env vars)",
    )
    config._hermes_e2e_mode = "real" if has_real_feishu_creds() else "mock"


def pytest_report_header(config):
    """Add e2e mode to pytest header output."""
    mode = getattr(config, "_hermes_e2e_mode", "unknown")
    return [f"hermes-lark-streaming e2e mode: {mode}"]
