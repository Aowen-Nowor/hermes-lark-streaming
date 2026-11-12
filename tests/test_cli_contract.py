"""CLI 诊断退出码与跨平台解释器发现契约。"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_lark_streaming import __main__ as cli
from scripts import smoke_v2


def _cfg(*, app_id: str = "", app_secret: str = ""):
    return SimpleNamespace(
        env_app_id=app_id,
        env_app_secret=app_secret,
        feishu_app_id=app_id,
        feishu_app_secret=app_secret,
        print_strategy="delay",
        print_step=4,
        streaming_panel_expanded=False,
        footer_fields=[],
        footer_show_label=False,
        native_streaming=False,
        oneshot_cards=True,
        card_duration_sec=600,
    )


def test_verify_returns_failure_when_credentials_or_hermes_missing() -> None:
    with patch("hermes_lark_streaming.config.Config", return_value=_cfg()), \
            patch("importlib.util.find_spec", return_value=None):
        assert cli._cmd_verify() == 1


def test_status_requires_both_app_id_and_secret() -> None:
    with patch("hermes_lark_streaming.config.Config", return_value=_cfg(app_id="cli_test")):
        assert cli._cmd_status() == 1


def test_cleanup_returns_failure_on_write_error() -> None:
    with patch("hermes_lark_streaming.plugin.cleanup_streaming_config", side_effect=OSError("disk full")):
        assert cli._cmd_cleanup() == 1


def test_doctor_returns_failure_when_credentials_missing(tmp_path: Path) -> None:
    with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}, clear=False), \
            patch("hermes_lark_streaming.config.Config", return_value=_cfg()), \
            patch("importlib.metadata.version", side_effect=ModuleNotFoundError("lark-oapi")), \
            patch("hermes_lark_streaming.__main__._find_hermes_python", return_value=None):
        assert cli._cmd_doctor() == 1


def test_find_hermes_python_supports_windows_generation(tmp_path: Path) -> None:
    python = tmp_path / "installs" / "install-id" / "environments" / "hash" / "venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}), \
            patch("pathlib.Path.home", return_value=tmp_path / "no-home"):
        assert cli._find_hermes_python() == str(python)


def test_smoke_with_hermes_requires_explicit_path() -> None:
    with patch.object(sys, "argv", ["smoke_v2.py", "--with-hermes"]), \
            patch.object(smoke_v2, "level1"), \
            patch.object(smoke_v2, "level3"):
        try:
            smoke_v2.main()
        except SystemExit as exc:
            assert exc.code == 2
        else:
            raise AssertionError("--with-hermes without a path must fail argument parsing")
