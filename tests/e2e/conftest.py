"""E2E test fixtures — single runner, auto mock/real switching.

v1.1.0: The E2ETestRunner automatically uses real Feishu API when
FEISHU_E2E_APP_ID + FEISHU_E2E_APP_SECRET + FEISHU_E2E_CHAT_ID +
FEISHU_E2E_OPEN_ID are all set. Otherwise uses mock server.

v1.1.1: chat_id 和 open_id 都必填（分别测群聊和私聊场景）。
v1.1.1: 真飞书模式下测试间加 1 秒延迟，避免触发飞书 API 频率限制
        （CardKit 流式模式豁免 QPS，但 create/send/close 计入 1000/分 & 50/秒）。

v1.3.6: runner fixture 改为 module 级——同一测试文件的所有测试方法共享
        1 个 runner（1 条 anchor 消息），减少真飞书模式群聊消息数 ~85%。
        每个测试方法通过 autouse fixture 清理 controller sessions 保证隔离。

Test code is identical in both modes — only the underlying client differs.
"""

from __future__ import annotations

import asyncio
import os
import pytest

from .framework import E2ETestRunner


def _has_real_feishu_creds() -> bool:
    """Check if real Feishu credentials are available.

    v1.1.1: 4 variables required: app_id, app_secret, chat_id, open_id.
    chat_id 和 open_id 都需要（分别测群聊和私聊）。
    """
    return bool(
        os.environ.get("FEISHU_E2E_APP_ID")
        and os.environ.get("FEISHU_E2E_APP_SECRET")
        and os.environ.get("FEISHU_E2E_CHAT_ID")
        and os.environ.get("FEISHU_E2E_OPEN_ID")
    )


# ── Module-level runner — 1 anchor per file, not per test ──

@pytest.fixture(scope="module")
async def _module_runner():
    """v1.3.6: module 级 runner——同一文件所有测试共享 1 个 runner（1 条 anchor）。

    真飞书模式下每个 anchor 是一条群聊文本消息，改为 module 级后：
    - test_e2e_full.py: 12 anchor → 1 anchor
    - test_e2e_clarify.py: 5 anchor → 1 anchor
    - test_e2e_header.py: 4 anchor → 1 anchor
    总共从 ~21 条群聊消息减少到 3 条。
    """
    r = E2ETestRunner()
    await r.setup()
    yield r
    await r.teardown()


@pytest.fixture
async def runner(_module_runner):
    """每个测试方法拿到 module 级 runner，但先清理 controller sessions 保证隔离。

    v1.3.6: 清理前一个测试残留的 session，避免 concurrency seal 误触发。
    mock_server 也 reset（mock 模式清理调用记录）。
    """
    # 清理前一个测试的残留状态
    try:
        _module_runner.controller._sess_clear()
    except Exception:
        pass
    _module_runner.mock_server.reset()
    _module_runner._sessions.clear()
    _module_runner._real_card_states.clear()
    yield _module_runner


@pytest.fixture(autouse=True)
async def _rate_limit_guard():
    """v1.1.1: 真飞书模式下测试间加延迟，避免触发飞书 API 频率限制.

    飞书 CardKit API 限制：
    - API 级：1000 次/分 & 50 次/秒（流式模式豁免）
    - 单卡片级：10 次/秒
    - create/send/close 计入配额

    v1.1.1: 延迟从 1 秒加到 2 秒，确保不触发限流。
    mock 模式不需要延迟。
    """
    yield
    if _has_real_feishu_creds():
        await asyncio.sleep(2.0)


# ── Pytest configuration ──

def pytest_configure(config):
    """Register custom markers and log mode."""
    config.addinivalue_line(
        "markers",
        "real_feishu: test runs against real Feishu API (auto-detected from env vars)",
    )
    if _has_real_feishu_creds():
        config._hermes_e2e_mode = "real"
    else:
        config._hermes_e2e_mode = "mock"


def pytest_report_header(config):
    """Add e2e mode to pytest header output."""
    mode = getattr(config, "_hermes_e2e_mode", "unknown")
    return [f"hermes-lark-streaming e2e mode: {mode}"]
