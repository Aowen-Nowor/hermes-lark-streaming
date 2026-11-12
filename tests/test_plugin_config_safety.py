"""插件注册与显式配置迁移的安全边界。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch
import os

import yaml


def test_prewarm_never_injects_or_rewrites_config() -> None:
    from hermes_lark_streaming.plugin.adapter import _prewarm_config

    with patch("hermes_lark_streaming.plugin.check_lark_oapi_floor"), \
            patch("hermes_lark_streaming.plugin.ensure_streaming_config") as ensure:
        _prewarm_config()
    ensure.assert_not_called()


def test_explicit_migration_preserves_existing_text_and_is_atomic(tmp_path: Path) -> None:
    from hermes_lark_streaming.plugin import ensure_streaming_config

    config_path = tmp_path / "config.yaml"
    original = "# keep this comment\nmodel:\n  default: test\n"
    config_path.write_text(original, encoding="utf-8")
    with patch("hermes_lark_streaming.plugin._get_hermes_config_path", return_value=config_path), \
            patch("os.replace", wraps=os.replace) as replace:
        ensure_streaming_config()
        ensure_streaming_config()

    text = config_path.read_text(encoding="utf-8")
    assert text.startswith(original)
    assert text.count("feishu_streaming:") == 1
    assert yaml.safe_load(text)["feishu_streaming"]["print_step"] == 4
    replace.assert_called_once()
    assert not list(tmp_path.glob("config.yaml.*.feishu-streaming-platform"))


def test_legacy_migration_preserves_comments(tmp_path: Path) -> None:
    from hermes_lark_streaming.plugin import ensure_streaming_config

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "# retain me\nhermes_lark_streaming:\n  print_step: 7\nmodel:\n  default: test\n",
        encoding="utf-8",
    )
    with patch("hermes_lark_streaming.plugin._get_hermes_config_path", return_value=config_path):
        ensure_streaming_config()

    text = config_path.read_text(encoding="utf-8")
    assert text.startswith("# retain me\n")
    assert "hermes_lark_streaming:" not in text
    assert yaml.safe_load(text)["feishu_streaming"]["print_step"] == 7


def test_cleanup_removes_only_plugin_sections(tmp_path: Path) -> None:
    from hermes_lark_streaming.plugin import cleanup_streaming_config

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "# retain me\nfeishu_streaming:\n  print_step: 4\n"
        "hermes_lark_streaming:\n  print_step: 7\nmodel:\n  default: test\n",
        encoding="utf-8",
    )
    with patch("hermes_lark_streaming.plugin._get_hermes_config_path", return_value=config_path):
        cleanup_streaming_config()

    text = config_path.read_text(encoding="utf-8")
    assert text == "# retain me\nmodel:\n  default: test\n"
