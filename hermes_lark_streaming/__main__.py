"""CLI 入口: python -m hermes_lark_streaming [status|verify|cleanup]。"""

from __future__ import annotations

import sys


def main() -> int:
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

    print(f"Unknown command: {cmd}")
    _print_usage()
    return 1


def _print_usage() -> None:
    print("Usage: $(dirname $(readlink -f $(which hermes)))/python -m hermes_lark_streaming <command>")
    print()
    print("Commands:")
    print("  status     Show current configuration and credentials status")
    print("  verify     Verify environment compatibility")
    print("  cleanup    Remove plugin-injected config from config.yaml (run after uninstall)")
    print()
    print("Note: This plugin uses runtime monkey patching (no file modification).")
    print("      Install/uninstall via: hermes plugins install/uninstall")


def _cmd_status() -> int:
    from .config import Config

    cfg = Config()
    print(f"Config streaming.enabled: {cfg.enabled}")
    print(f"Config streaming.linear: {cfg.linear}")
    print(f"Feishu credentials: {'configured' if (cfg.env_app_id or cfg.feishu_app_id) else 'MISSING'}")
    print()
    print("Plugin uses runtime monkey patching — no source files are modified.")
    print("Install/uninstall via: hermes plugins install/uninstall")
    return 0


def _cmd_verify() -> int:
    from .config import Config

    cfg = Config()
    print(f"Config streaming.enabled: {cfg.enabled}")
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

    return 0


def _cmd_cleanup() -> int:
    """Remove plugin-injected config entries from config.yaml.

    Run this after ``hermes plugins uninstall hermes-lark-streaming``
    to clean up the ``streaming`` config section and ``plugins.enabled`` entry.
    """
    from .plugin import _cleanup_config

    _cleanup_config()
    print("Cleanup complete. Next steps:")
    print("  1. hermes plugins uninstall hermes-lark-streaming")
    print("  2. hermes gateway restart")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
