"""Pytest configuration — register repo root as hermes_lark_streaming package.

The plugin code lives at the repo root (no nested hermes_lark_streaming/
subdirectory).  The repo directory is named ``hermes-lark-streaming`` (hyphens)
but the Python package is ``hermes_lark_streaming`` (underscores).  This
conftest bridges that gap by pre-registering the package in ``sys.modules``
before any test module is collected, mirroring what Hermes's
``_load_directory_module`` does at runtime.

Test-specific fixtures (event-loop cleanup, etc.) live below the
package-registration block.

``collect_ignore`` is no longer needed here because ``pyproject.toml`` sets
``testpaths = ["tests"]``, so pytest never collects root-level ``.py`` files.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _register_repo_as_package() -> None:
    """Ensure ``import hermes_lark_streaming`` resolves to the repo root.

    Uses ``importlib.util.spec_from_file_location`` with
    ``submodule_search_locations`` set to the repo root — identical to
    how Hermes's plugin loader discovers and loads directory plugins.
    """
    if "hermes_lark_streaming" in sys.modules:
        return

    init_file = _REPO_ROOT / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "hermes_lark_streaming",
        str(init_file),
        submodule_search_locations=[str(_REPO_ROOT)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create module spec for {init_file}")

    module = importlib.util.module_from_spec(spec)
    module.__package__ = "hermes_lark_streaming"
    module.__path__ = [str(_REPO_ROOT)]
    sys.modules["hermes_lark_streaming"] = module
    spec.loader.exec_module(module)


# Register eagerly so that test modules can ``import hermes_lark_streaming``
# at their top-level.
_register_repo_as_package()


# ──────────────────────────────────────────────────────────────────────
# Test-suite local fixtures.
#
# Event loop cleanup: tests that create ``asyncio.new_event_loop()`` via
# helpers (e.g. ``_make_session``) must register the loop in
# ``_loops_to_cleanup`` so the autouse fixture below can drain pending
# async generators and close the loop after each test — preventing
# ResourceWarning / "coroutine never awaited" warnings.
# ──────────────────────────────────────────────────────────────────────

import asyncio

import pytest


@pytest.fixture(autouse=True)
def _ensure_event_loop_for_sync_tests():
    """Ensure an event loop exists for sync tests (Python 3.11+ compatibility).

    pytest-asyncio's ``auto`` mode only injects event loops into ``async def``
    tests.  Sync tests that call code relying on ``asyncio.get_event_loop()``
    (e.g. ``controller._get_loop()``) need this fixture to ensure a loop is
    available — otherwise Python 3.11+ raises ``RuntimeError: There is no
    current event loop``.

    This fixture runs before every test and creates a new event loop if one
    doesn't exist.  It does NOT close the loop after the test (the loop may
    be cached by ``StreamCardController._loop`` for fire-and-forget tasks).
    Loop cleanup is handled by ``_cleanup_event_loops`` below.
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("loop closed")
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    yield


@pytest.fixture(autouse=True)
def _cleanup_event_loops():
    """Close any event loops that test helpers created but did not clean up."""
    yield
    # Import late: the list is populated at test execution time.
    try:
        from tests.test_controller import _loops_to_cleanup

        loops = list(_loops_to_cleanup)
        _loops_to_cleanup.clear()
    except ImportError:
        loops = []

    for loop in loops:
        if loop.is_closed() or loop.is_running():
            continue
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        finally:
            loop.close()


@pytest.fixture(autouse=True)
def _reset_config_singleton():
    """v1.2.0: 每个 test 重置 Config 单例，避免测试间状态泄漏.

    Config 改单例后，controller/patching/aowen 持有同一实例。
    测试若修改 _raw/_reload_cache 会影响后续测试。此 fixture 在
    每个 test 前清掉单例，保证隔离。同时重置 patching._config 缓存
    （它持有 Config 实例引用，需一并清避免持有旧单例）。
    """
    from hermes_lark_streaming.config.reader import Config
    Config._instance = None
    # v1.3.0 P1-03: patching._config global cache removed — Config singleton
    # reset above is sufficient for test isolation.
    yield
    Config._instance = None
