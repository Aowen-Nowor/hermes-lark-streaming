"""Compatibility probes must run the entire regression suite in mock E2E mode."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PROBE_PATH = _REPO_ROOT / "scripts" / "compat_probe.py"
_REAL_ENV = (
    "FEISHU_E2E_REAL",
    "FEISHU_E2E_APP_ID",
    "FEISHU_E2E_APP_SECRET",
    "FEISHU_E2E_CHAT_ID",
    "FEISHU_E2E_OPEN_ID",
    "FEISHU_E2E_BASE_URL",
)


def _load_probe():
    spec = importlib.util.spec_from_file_location("compat_probe_under_test", _PROBE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_force_mock_e2e_removes_every_real_mode_variable() -> None:
    probe = _load_probe()
    force_mock = getattr(probe, "_force_mock_e2e", None)
    assert callable(force_mock), "compat_probe must expose its mock-environment sanitizer"

    env = {name: f"value-for-{name}" for name in _REAL_ENV}
    env["UNRELATED"] = "keep"
    sanitized = force_mock(env)

    assert sanitized is env
    assert sanitized["UNRELATED"] == "keep"
    for name in _REAL_ENV:
        assert name not in sanitized


def test_main_sanitizes_process_environment_before_any_probe_step(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    probe = _load_probe()
    for name in _REAL_ENV:
        monkeypatch.setenv(name, f"value-for-{name}")
    monkeypatch.setattr(
        sys,
        "argv",
        ["compat_probe.py", "--hermes-dir", str(tmp_path / "missing-hermes")],
    )

    assert probe.main() == 1
    for name in _REAL_ENV:
        assert name not in os.environ


def test_surface_checklist_excludes_plugin_client_methods() -> None:
    """S4 清单不得把插件自身 FeishuClient 方法误列为基类必备。

    send_stream_frame / send_card_to_chat / reply_card_by_id /
    cardkit_* 是插件自己的接口，不是内置 FeishuAdapter 基类方法。
    旧清单导致 0.23.2+ 每次探针误报 S4 失败。
    """
    probe = _load_probe()
    checklist = set(probe.SURFACE_CHECKLIST)

    for name in (
        "send_stream_frame",
        "send_card_to_chat",
        "reply_card_by_id",
        "cardkit_create",
        "cardkit_update",
        "cardkit_stream_element",
    ):
        assert name not in checklist, f"{name} 是插件自身方法，不应列入基类清单"

    for name in (
        "send",
        "edit_message",
        "send_clarify",
        "delete_message",
        "handle_message",
        "_handle_message_with_guards",
        "set_message_handler",
        "_CARD_ACTION_ROUTES",
    ):
        assert name in checklist, f"{name} 是插件运行时依赖的基类方法"
