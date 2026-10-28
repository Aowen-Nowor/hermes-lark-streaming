"""Tests for version metadata contracts.

Covers:
  - hermes_lark_streaming.__version__ matches the version in plugin.yaml
  - Fallback to "unknown" when plugin.yaml is missing
  - Fallback to "unknown" when plugin.yaml has no version: field
  - pyproject.toml dynamic-version wiring (v2.0.12: setup.py removed —
    setuptools resolves the dynamic version from __init__.__version__,
    whose single source of truth remains plugin.yaml)
  - requires-python aligned with hermes official (">=3.11,<3.15"; hermes
    pyproject comment: "we *only* support 3.14")
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import hermes_lark_streaming

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_PLUGIN_YAML = _REPO_ROOT / "plugin.yaml"


def _read_plugin_yaml_version() -> str:
    """Read version from plugin.yaml (single source of truth).

    This avoids hardcoding the version number in the test file,
    so the test never needs updating on version bumps.
    """
    for line in _PLUGIN_YAML.read_text(encoding="utf-8").splitlines():
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip().strip('"').strip("'")
    raise ValueError("No 'version:' field found in plugin.yaml")


def _is_plugin_yaml_path(p: Path) -> bool:
    """Return True if *p* points to a file named ``plugin.yaml``.

    This is used to scope global ``Path`` mocks so they only affect the
    plugin.yaml lookups performed by the modules under test.
    """
    return p.name == "plugin.yaml"


def _reload_hls():
    """Reload hermes_lark_streaming preserving conftest.py's registration.

    ``importlib.reload()`` fails when the module was loaded via
    ``spec_from_file_location`` (as conftest.py does) because
    ``module.__spec__`` may be None or incomplete, causing
    ``ModuleNotFoundError`` in Python 3.11+.  This function re-registers
    the module using the same approach as conftest.py.
    """
    init_file = _REPO_ROOT / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "hermes_lark_streaming",
        str(init_file),
        submodule_search_locations=[str(init_file.parent)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "hermes_lark_streaming"
    module.__path__ = [str(init_file.parent)]
    sys.modules["hermes_lark_streaming"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# __init__.py  –  hermes_lark_streaming.__version__
# ---------------------------------------------------------------------------


class TestInitVersion:
    """Tests for ``hermes_lark_streaming.__version__``."""

    def test_version_reads_correct_value(self) -> None:
        """__version__ should match the version defined in plugin.yaml."""
        expected = _read_plugin_yaml_version()
        assert hermes_lark_streaming.__version__ == expected

    def test_version_fallback_when_plugin_yaml_missing(self) -> None:
        """__version__ falls back to 'unknown' when plugin.yaml does not exist."""
        original_exists = Path.exists

        def mock_exists(self: Path) -> bool:
            if _is_plugin_yaml_path(self):
                return False
            return original_exists(self)

        try:
            with patch.object(Path, "exists", mock_exists):
                mod = _reload_hls()
            assert mod.__version__ == "unknown"
        finally:
            # Restore the module to its correct state regardless of test outcome
            _reload_hls()

    def test_version_fallback_when_no_version_field(self) -> None:
        """__version__ falls back to 'unknown' when plugin.yaml has no version: field."""
        original_exists = Path.exists
        original_read_text = Path.read_text

        def mock_exists(self: Path) -> bool:
            if _is_plugin_yaml_path(self):
                return True
            return original_exists(self)

        def mock_read_text(self: Path, *args: object, **kwargs: object) -> str:
            if _is_plugin_yaml_path(self):
                return "name: test\ndescription: no version here\n"
            return original_read_text(self, *args, **kwargs)

        try:
            with patch.object(Path, "exists", mock_exists), \
                 patch.object(Path, "read_text", mock_read_text):
                mod = _reload_hls()
            assert mod.__version__ == "unknown"
        finally:
            _reload_hls()


# ---------------------------------------------------------------------------
# pyproject.toml  –  dynamic version wiring & python floor (v2.0.12)
# ---------------------------------------------------------------------------
# setup.py was removed in v2.0.12 (repo-root hygiene: only __init__.py /
# __main__.py may live at root). The dynamic version moved into
# [tool.setuptools.dynamic] via attr resolution.


class TestPyprojectVersionContract:
    """pyproject.toml packaging metadata contracts."""

    def test_setup_py_removed(self) -> None:
        """setup.py no longer exists at repo root (root hygiene contract)."""
        assert not (_REPO_ROOT / "setup.py").exists()

    def test_dynamic_version_wired_to_plugin_yaml_source(self) -> None:
        """[tool.setuptools.dynamic] resolves version from the package attr,
        whose single source of truth is plugin.yaml."""
        text = _PYPROJECT.read_text(encoding="utf-8")
        assert "dynamic = [\"version\"]" in text
        assert 'version = {attr = "hermes_lark_streaming.__version__"}' in text

    def test_requires_python_matches_hermes_official(self) -> None:
        """requires-python is frozen to hermes official ">=3.11,<3.15".

        hermes-agent pyproject.toml declares the same range with the comment
        "we *only* support 3.14" (3.14 is the only supported runtime; the
        3.11 floor keeps the updater contract for old installs).
        """
        expected = 'requires-python = ">=3.11,<3.15"'
        assert expected in _PYPROJECT.read_text(encoding="utf-8")
