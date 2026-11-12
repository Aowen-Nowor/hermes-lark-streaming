"""E2E mode selection must never send real messages without explicit opt-in."""

from __future__ import annotations

import pytest

from .framework import E2ETestRunner, has_real_feishu_creds

_REAL_ENV = (
    "FEISHU_E2E_REAL",
    "FEISHU_E2E_APP_ID",
    "FEISHU_E2E_APP_SECRET",
    "FEISHU_E2E_CHAT_ID",
    "FEISHU_E2E_OPEN_ID",
)
_CREDS = {
    "FEISHU_E2E_APP_ID": "cli_test_app",
    "FEISHU_E2E_APP_SECRET": "test-secret",
    "FEISHU_E2E_CHAT_ID": "oc_test_chat",
}


def _set_mode_env(monkeypatch: pytest.MonkeyPatch, values: dict[str, str]) -> None:
    for name in _REAL_ENV:
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


@pytest.mark.parametrize(
    ("values", "expected_real"),
    [
        ({}, False),
        (_CREDS, False),
        ({"FEISHU_E2E_REAL": "1"}, False),
        ({**_CREDS, "FEISHU_E2E_REAL": "0"}, False),
        ({**_CREDS, "FEISHU_E2E_REAL": "true"}, False),
        ({**_CREDS, "FEISHU_E2E_REAL": "yes"}, False),
        ({**_CREDS, "FEISHU_E2E_REAL": "1"}, True),
        (
            {
                "FEISHU_E2E_REAL": "1",
                "FEISHU_E2E_APP_ID": _CREDS["FEISHU_E2E_APP_ID"],
                "FEISHU_E2E_APP_SECRET": _CREDS["FEISHU_E2E_APP_SECRET"],
            },
            False,
        ),
    ],
)
def test_real_mode_requires_explicit_flag_and_complete_credentials(
    monkeypatch: pytest.MonkeyPatch,
    values: dict[str, str],
    expected_real: bool,
) -> None:
    _set_mode_env(monkeypatch, values)
    assert has_real_feishu_creds() is expected_real


def test_runner_defaults_to_mock_even_when_credentials_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_mode_env(monkeypatch, _CREDS)
    runner = E2ETestRunner()
    assert runner.is_real_mode is False


def test_scenario_script_requires_explicit_real_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import e2e_v2

    _set_mode_env(monkeypatch, _CREDS)
    with pytest.raises(SystemExit) as exc:
        e2e_v2._require_env()
    assert exc.value.code == 2

    monkeypatch.setenv("FEISHU_E2E_REAL", "1")
    assert e2e_v2._require_env() == (
        _CREDS["FEISHU_E2E_APP_ID"],
        _CREDS["FEISHU_E2E_APP_SECRET"],
        _CREDS["FEISHU_E2E_CHAT_ID"],
    )


def test_scenario_script_rejects_non_one_real_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import e2e_v2

    _set_mode_env(monkeypatch, {**_CREDS, "FEISHU_E2E_REAL": "true"})
    with pytest.raises(SystemExit) as exc:
        e2e_v2._require_env()
    assert exc.value.code == 2
