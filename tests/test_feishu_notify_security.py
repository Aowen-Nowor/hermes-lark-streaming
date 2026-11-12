"""Gitee API failures must not leak access tokens into Actions logs."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from urllib.parse import quote_plus

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_NOTIFY_PATH = _REPO_ROOT / "scripts" / "feishu_notify.py"


def _load_notify():
    spec = importlib.util.spec_from_file_location("feishu_notify_under_test", _NOTIFY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("encode_token", [False, True])
def test_gitee_api_exception_is_redacted_before_logging(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    encode_token: bool,
) -> None:
    notify = _load_notify()
    token = "gitee/token+secret"
    exposed = quote_plus(token) if encode_token else token
    monkeypatch.setenv("GITEE_OWNER", "test-owner")
    monkeypatch.setenv("GITEE_REPO", "test-repo")
    monkeypatch.setenv("GITEE_TOKEN", token)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "feishu_notify.py",
            "gitee-digest",
            "--state-file",
            str(tmp_path / "state.json"),
        ],
    )

    def fail_api(*args: object) -> list[dict]:
        raise RuntimeError(
            "HTTP Error 401: https://gitee.com/api/v5/repos/test-owner/test-repo/pulls?"
            f"access_token={exposed}"
        )

    monkeypatch.setattr(notify, "_fetch_gitee", fail_api)
    notify.cmd_gitee_digest()

    stderr = capsys.readouterr().err
    assert "Gitee API 查询失败" in stderr
    assert token not in stderr
    assert exposed not in stderr
    assert "access_token=<redacted>" in stderr
