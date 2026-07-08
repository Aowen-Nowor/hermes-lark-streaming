"""兼容非标准安装路径：自动搜索常见安装路径并加入 sys.path。脚本自注册包到"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

# ── 包目录（本文件所在目录） ──
_HERE = Path(__file__).resolve().parent

def _bootstrap_package() -> None:
    """手动注册 hermes_lark_streaming 到 sys.modules (当不可导入时)."""
    try:
        import hermes_lark_streaming  # noqa: F401
        return  # 已可导入，无需处理
    except ImportError:
        pass

    # 策略 1: 父目录加入 sys.path.
    parent = _HERE.parent
    if str(parent) not in sys.path:
        sys.path.insert(0, str(parent))

    try:
        import hermes_lark_streaming  # noqa: F401
        return
    except ImportError:
        pass

    # 策略 2: 搜索常见安装路径.
    hermes_home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))

    search_paths: list[Path] = [
        hermes_home / "plugins",
        *hermes_home.glob("lib/python*/site-packages"),
        *Path("/opt/hermes-agent").glob("lib/python*/site-packages"),
        *Path("/usr/local/hermes-agent").glob("lib/python*/site-packages"),
        *Path(str(Path.home() / "hermes-agent")).glob("lib/python*/site-packages"),
        *Path(sys.prefix).glob("lib/python*/site-packages"),
    ]

    for p in search_paths:
        if p.exists() and str(p) not in sys.path:
            sys.path.insert(0, str(p))
            try:
                import hermes_lark_streaming  # noqa: F401
                return
            except ImportError:
                continue

    # 策略 3: 手动注册当前目录为 hermes_lark_streaming 包 (连字符目录名).
    init_file = _HERE / "__init__.py"
    if init_file.exists():
        spec = importlib.util.spec_from_file_location(
            "hermes_lark_streaming",
            str(init_file),
            submodule_search_locations=[str(_HERE)],
        )
        if spec is not None and spec.loader is not None:
            mod = importlib.util.module_from_spec(spec)
            sys.modules["hermes_lark_streaming"] = mod
            try:
                spec.loader.exec_module(mod)
                return
            except Exception:
                sys.modules.pop("hermes_lark_streaming", None)

    print(
        "Error: Cannot locate hermes_lark_streaming package.\n"
        "\n"
        "Possible fixes:\n"
        "  1. Install via pip:  pip install hermes-lark-streaming\n"
        "  2. Run directly:     $HERMES_PYTHON /path/to/hermes-lark-streaming/__main__.py status\n"
        "  3. Set PYTHONPATH:   PYTHONPATH=~/.hermes/plugins $HERMES_PYTHON -m hermes_lark_streaming status",
        file=sys.stderr,
    )

def _find_hermes_python() -> str | None:
    """Auto-detect Hermes Agent Python interpreter path."""
    candidates = [
        Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3",
        Path("/usr/local/lib/hermes-agent/venv/bin/python3"),
        Path("/opt/hermes-agent/venv/bin/python3"),
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    import shutil
    if shutil.which("python3"):
        return shutil.which("python3")
    return None

def _cmd_python() -> int:
    """Print detected Hermes Python path."""
    path = _find_hermes_python()
    if path:
        print(path)
        return 0
    print("Error: Cannot find Hermes Python interpreter.", file=sys.stderr)
    print("Please set HERMES_PYTHON manually:", file=sys.stderr)
    print("  export HERMES_PYTHON=/path/to/hermes-agent/venv/bin/python3", file=sys.stderr)
    return 1

def main() -> int:
    _bootstrap_package()

    # Set __package__ so relative imports work when running __main__.py directly.
    global __package__
    if __name__ == "__main__" and __package__ is None:
        __package__ = "hermes_lark_streaming"

    args = sys.argv[1:]
    if not args:
        _print_usage()
        return 0

    cmd = args[0]

    if cmd == "status":
        return _cmd_status()
    if cmd == "verify":
        return _cmd_verify()
    if cmd == "cleanup":
        return _cmd_cleanup()
    if cmd == "python":
        return _cmd_python()
    if cmd == "doctor":
        return _cmd_doctor()

    print(f"Unknown command: {cmd}")
    _print_usage()
    return 1

def _print_usage() -> None:
    print("Usage: python -m hermes_lark_streaming <command>")
    print("   or: python /path/to/hermes-lark-streaming/__main__.py <command>")
    print()
    print("Commands:")
    print("  status     Show current configuration and credentials status")
    print("  verify     Verify environment compatibility")
    print("  doctor     Full diagnostic: version, config, credentials, patch status, log path")
    print("  cleanup    Remove plugin-injected config from config.yaml (run after uninstall)")
    print("  python     Print the auto-detected Hermes Python interpreter path")
    print()
    print("Note: This plugin uses runtime monkey patching (no file modification).")
    print("      Install/uninstall via: hermes plugins install/uninstall")

def _cmd_status() -> int:
    try:
        from hermes_lark_streaming.config import Config

        cfg = Config()
        print(f"Config hermes_lark_streaming.enabled: {cfg.enabled}")
        print(f"Config hermes_lark_streaming.linear: {cfg.linear}")
        print(f"Feishu credentials: {'configured' if (cfg.env_app_id or cfg.feishu_app_id) else 'MISSING'}")
        print()
        print("Plugin uses runtime monkey patching — no source files are modified.")
        print("Install/uninstall via: hermes plugins install/uninstall")
    except ImportError as e:
        print(f"Error: Cannot import hermes_lark_streaming: {e}")
        print("Please ensure the plugin is installed correctly.")
        return 1
    return 0

def _cmd_verify() -> int:
    try:
        from hermes_lark_streaming.config import Config

        cfg = Config()
        print(f"Config hermes_lark_streaming.enabled: {cfg.enabled}")
        print(f"Feishu credentials: {'configured' if (cfg.env_app_id or cfg.feishu_app_id) else 'MISSING'}")

        # Verify that gateway modules are importable
        try:
            from gateway.run import GatewayRunner
            print("gateway.run.GatewayRunner: importable")
        except ImportError as e:
            print(f"gateway.run.GatewayRunner: NOT importable ({e})")

        try:
            from run_agent import AIAgent
            print("run_agent.AIAgent: importable")
        except ImportError as e:
            print(f"run_agent.AIAgent: NOT importable ({e})")
    except ImportError as e:
        print(f"Error: Cannot import hermes_lark_streaming: {e}")
        print("Please ensure the plugin is installed correctly.")
        return 1

    return 0

def _cmd_cleanup() -> int:
    """Remove plugin-injected config entries from config.yaml."""
    try:
        from hermes_lark_streaming.plugin import _cleanup_config

        _cleanup_config()
        print("Cleanup complete. Next steps:")
        print("  1. hermes plugins uninstall hermes-lark-streaming")
        print("  2. hermes gateway restart")
    except ImportError as e:
        print(f"Error: Cannot import hermes_lark_streaming: {e}")
        print("Please ensure the plugin is installed correctly.")
        return 1
    return 0

def _cmd_doctor() -> int:
    """Full diagnostic: version, config, credentials, patch status, logs."""
    import os, sys
    print("=" * 60)
    print("  hermes-lark-streaming doctor")
    print("=" * 60)
    print()

    try:
        from hermes_lark_streaming import __version__
        print(f"[1/6] Plugin version:    {__version__}")
    except ImportError as e:
        print(f"[1/6] Plugin version:    IMPORT FAILED — {e}")
        return 2

    print(f"[2/6] Python:             {sys.version.split()[0]}")
    hermes_home = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
    print(f"      HERMES_HOME:        {hermes_home}")
    hermes_python = _find_hermes_python()
    print(f"      Hermes Python:      {hermes_python or '(not found)'}")
    if hermes_python and hermes_python != sys.executable:
        print(f"      ⚠ Doctor Python differs from Hermes Python ({hermes_python})")

    print()
    try:
        from hermes_lark_streaming.config import Config
        cfg = Config()
        print("[3/6] Configuration:")
        for k in ("enabled", "linear", "gateway_cards", "flush_interval_ms",
                   "card_duration_sec", "print_strategy", "print_step",
                   "panel_expanded", "streaming_panel_expanded",
                   "max_tool_steps", "max_reasoning_rounds",
                   "footer_fields", "footer_show_label"):
            print(f"      {k:24}{getattr(cfg, k, '?')}")
    except Exception as e:
        print(f"[3/6] Configuration:     FAILED — {e}")
        return 1

    print()
    has_env = bool(os.environ.get("FEISHU_APP_ID") and os.environ.get("FEISHU_APP_SECRET"))
    has_cfg = bool(getattr(cfg, "feishu_app_id", None) and getattr(cfg, "feishu_app_secret", None))
    env_file = Path(hermes_home) / ".env"
    has_dotenv = False
    if env_file.exists():
        try:
            txt = env_file.read_text(encoding="utf-8", errors="replace")
            has_dotenv = "FEISHU_APP_ID=" in txt and "FEISHU_APP_SECRET=" in txt
        except Exception:
            pass
    print("[4/6] Feishu credentials:")
    print(f"      env vars:    {'configured' if has_env else 'not set'}")
    print(f"      config.yaml: {'configured' if has_cfg else 'not set'}")
    print(f"      ~/.hermes/.env: {'exists' if env_file.exists() else 'not found'}"
          + (f" (found)" if has_dotenv else ""))
    if not (has_env or has_cfg or has_dotenv):
        print("      ⚠ MISSING — cards will NOT work.")

    print()
    try:
        from hermes_lark_streaming.patching import _patch_status
        if _patch_status:
            print("[5/6] Patch status:")
            for key, val in _patch_status.items():
                if key in ("version", "hermes_layout"):
                    continue
                icon = "✓" if val in ("✓", "applied") else ("⚠" if "pending" in str(val) else "✗")
                print(f"      {icon} {key}: {val}")
            print(f"      Hermes layout: {_patch_status.get('hermes_layout', {})}")
        else:
            print("[5/6] Patch status: (not available — gateway not started)")
    except ModuleNotFoundError as e:
        print(f"[5/6] Patch status: (skipped — {e})")
    except Exception as e:
        print(f"[5/6] Patch status: FAILED — {e}")

    print()
    agent_log = Path(hermes_home) / "logs" / "agent.log"
    print(f"[6/6] Logs: {agent_log}"
          + (f" ({agent_log.stat().st_size:,} bytes)" if agent_log.exists() else " (not found)"))
    print(f"      grep 'HLS:' {agent_log} | tail -100")
    print()
    print("=" * 60)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
