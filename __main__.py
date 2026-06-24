"""CLI 入口: python -m hermes_lark_streaming [status|verify|cleanup].

兼容非标准安装路径：当 hermes_lark_streaming 不在默认 sys.path 时，
自动搜索常见安装路径并加入 sys.path。

启动方式
--------
1. **pip 安装后**::

       python -m hermes_lark_streaming status

2. **目录插件（非 pip 安装）**::

       # 目录名可能是 hermes-lark-streaming (hyphens)，
       # 此时 -m 方式不可用，需直接运行 __main__.py：
       $HERMES_PYTHON ~/.hermes/plugins/hermes-lark-streaming/__main__.py status

3. **设置 PYTHONPATH 后**::

       PYTHONPATH=~/.hermes/plugins $HERMES_PYTHON -m hermes_lark_streaming status

原理：``python -m hermes_lark_streaming`` 要求包目录名与 Python 包名
一致（``hermes_lark_streaming``，下划线）。Hermes 插件目录使用
``hermes-lark-streaming``（连字符），导致 ``-m`` 无法找到包。
直接运行 ``__main__.py`` 可绕过此限制——脚本会自注册包到
``sys.modules``，使后续的相对导入正常工作。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

# ── 包目录（本文件所在目录） ──
_HERE = Path(__file__).resolve().parent


def _bootstrap_package() -> None:
    """当 hermes_lark_streaming 不可导入时，手动注册到 sys.modules。

    场景 1: ``python -m hermes_lark_streaming`` 但目录名不匹配
            （hermes-lark-streaming vs hermes_lark_streaming）。
            此时 Python 报 "No module named" 错误，__main__.py
            根本不会执行——需改用直接运行 __main__.py 方式。

    场景 2: 直接运行 ``python /path/to/__main__.py``。此时
            ``__name__ == "__main__"``，包未注册到 sys.modules，
            相对导入 ``from .config import Config`` 会失败。
            此函数用 importlib 注册包，使相对导入可用。

    场景 3: pip 安装后 ``python -m hermes_lark_streaming``。
            包已在 sys.path 中，无需处理。
    """
    try:
        import hermes_lark_streaming  # noqa: F401
        return  # 已可导入，无需处理
    except ImportError:
        pass

    # ── 策略 1: 将父目录加入 sys.path ──
    # 如果父目录下有 hermes_lark_streaming/ 子目录（pip 安装后的结构），
    # 这就足够了。
    parent = _HERE.parent
    if str(parent) not in sys.path:
        sys.path.insert(0, str(parent))

    try:
        import hermes_lark_streaming  # noqa: F401
        return
    except ImportError:
        pass

    # ── 策略 2: 搜索常见安装路径 ──
    hermes_home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))

    search_paths: list[Path] = [
        # 1. HERMES_HOME/plugins/（可能有 hermes_lark_streaming/ 子目录）
        hermes_home / "plugins",
        # 2. HERMES_HOME 下的 site-packages（lib/python*/site-packages）
        *hermes_home.glob("lib/python*/site-packages"),
        # 3. hermes-agent 下的 site-packages
        *Path("/opt/hermes-agent").glob("lib/python*/site-packages"),
        *Path("/usr/local/hermes-agent").glob("lib/python*/site-packages"),
        *Path(str(Path.home() / "hermes-agent")).glob("lib/python*/site-packages"),
        # 4. 当前 Python 的 site-packages
        *Path(sys.prefix).glob("lib/python*/site-packages"),
    ]

    for p in search_paths:
        if p.exists() and str(p) not in sys.path:
            sys.path.insert(0, str(p))
            try:
                import hermes_lark_streaming  # noqa: F401
                return  # 找到了，停止搜索
            except ImportError:
                continue

    # ── 策略 3: 手动注册当前目录为 hermes_lark_streaming 包 ──
    # 当插件目录名是 hermes-lark-streaming（连字符）时，即使把
    # 父目录加入 sys.path，Python 也找不到（目录名 ≠ 包名）。
    # 使用 importlib.util 手动注册。
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
                # 注册失败，回滚
                sys.modules.pop("hermes_lark_streaming", None)

    # ── 所有策略失败 ──
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
    """Auto-detect the Hermes Agent Python interpreter path.

    Searches common installation locations in order:
    1. ~/.hermes/hermes-agent/venv/bin/python3 (Hermes Desktop)
    2. /usr/local/lib/hermes-agent/venv/bin/python3 (CLI/server install)
    3. /opt/hermes-agent/venv/bin/python3 (alternative install)
    4. The python3 in PATH (fallback)
    """
    candidates = [
        Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3",
        Path("/usr/local/lib/hermes-agent/venv/bin/python3"),
        Path("/opt/hermes-agent/venv/bin/python3"),
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    # Fallback: system python3
    import shutil
    if shutil.which("python3"):
        return shutil.which("python3")
    return None


def _cmd_python() -> int:
    """Print the detected Hermes Python path."""
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

    # After bootstrap, set __package__ so that any relative imports in
    # this module (or in code called from here) can resolve correctly.
    # When running ``python /path/to/__main__.py`` directly, Python
    # leaves __package__ as None, causing "attempted relative import
    # with no known parent package" errors.
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
    """Remove plugin-injected config entries from config.yaml.

    Run this after ``hermes plugins uninstall hermes-lark-streaming``
    to clean up the ``hermes_lark_streaming`` config section and ``plugins.enabled`` entry.
    """
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
    """Full diagnostic check — version, config, credentials, patch status, logs.

    This is the primary troubleshooting command. Run it when:
    - Cards are not appearing
    - After a Hermes upgrade to check patch compatibility
    - To collect info for a bug report

    Exit codes:
      0 — all checks passed
      1 — one or more checks failed (details printed above)
      2 — cannot import plugin (installation broken)
    """
    import os
    import sys

    print("=" * 60)
    print("  hermes-lark-streaming doctor")
    print("=" * 60)
    print()

    # ── 1. Plugin version ──
    try:
        from hermes_lark_streaming import __version__
        print(f"[1/6] Plugin version:    {__version__}")
    except ImportError as e:
        print(f"[1/6] Plugin version:    IMPORT FAILED — {e}")
        print("      The plugin is not installed correctly.")
        return 2

    # ── 2. Python & Hermes path ──
    print(f"[2/6] Python:             {sys.version.split()[0]}")
    hermes_home = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
    print(f"      HERMES_HOME:        {hermes_home}")
    hermes_python = _find_hermes_python()
    print(f"      Hermes Python:      {hermes_python or '(not found)'}")

    # ── 3. Config ──
    print()
    try:
        from hermes_lark_streaming.config import Config
        cfg = Config()
        print(f"[3/6] Configuration:")
        print(f"      enabled:            {cfg.enabled}")
        print(f"      linear:             {cfg.linear}")
        print(f"      gateway_cards:      {cfg.gateway_cards}")
        print(f"      flush_interval_ms:  {cfg.flush_interval_ms}")
        print(f"      card_ttl_sec:       {cfg.card_duration_sec}")
        print(f"      print_strategy:     {cfg.print_strategy}")
        print(f"      panel_expanded:     {cfg.panel_expanded}")
        print(f"      streaming_panel_expanded: {cfg.streaming_panel_expanded}")
        print(f"      max_tool_steps:     {cfg.max_tool_steps}")
        print(f"      max_reasoning_rounds: {cfg.max_reasoning_rounds}")
        print(f"      footer_fields:      {cfg.footer_fields}")
        print(f"      footer_show_label:  {cfg.footer_show_label}")
    except Exception as e:
        print(f"[3/6] Configuration:     FAILED — {e}")
        return 1

    # ── 4. Feishu credentials ──
    print()
    has_env_creds = bool(os.environ.get("FEISHU_APP_ID") and os.environ.get("FEISHU_APP_SECRET"))
    has_cfg_creds = bool(getattr(cfg, "feishu_app_id", None) and getattr(cfg, "feishu_app_secret", None))
    env_file = Path(hermes_home) / ".env"
    has_env_file = env_file.exists()
    print(f"[4/6] Feishu credentials:")
    print(f"      env vars:           {'configured' if has_env_creds else 'MISSING'}")
    print(f"      config.yaml:        {'configured' if has_cfg_creds else 'not set'}")
    print(f"      ~/.hermes/.env:     {'exists' if has_env_file else 'not found'}")
    if not (has_env_creds or has_cfg_creds):
        print("      ⚠ WARNING: No Feishu credentials found. Cards will NOT work.")
        print("        Set FEISHU_APP_ID and FEISHU_APP_SECRET environment variables,")
        print("        or add them to ~/.hermes/.env, or configure in config.yaml.")

    # ── 5. Patch status ──
    print()
    try:
        from hermes_lark_streaming.patching import _patch_status
        if _patch_status:
            print(f"[5/6] Patch status (from last gateway start):")
            for key, val in _patch_status.items():
                if key in ("version", "hermes_layout"):
                    continue
                status_icon = "✓" if val == "✓" or val == "applied" else ("⚠" if "pending" in str(val) else "✗")
                print(f"      {status_icon} {key}: {val}")
            layout = _patch_status.get("hermes_layout", {})
            print(f"      Hermes layout: {layout}")
        else:
            print(f"[5/6] Patch status:      (not available — gateway not started or patches not applied)")
            print("      Run this command from within the Hermes gateway process,")
            print("      or check agent.log for 'HLS: patch summary' line.")
    except Exception as e:
        print(f"[5/6] Patch status:      FAILED — {e}")

    # ── 6. Log file ──
    print()
    log_dir = Path(hermes_home) / "logs"
    agent_log = log_dir / "agent.log"
    print(f"[6/6] Logs:")
    print(f"      Log directory:      {log_dir}")
    print(f"      agent.log:          {'exists' if agent_log.exists() else 'not found'}")
    if agent_log.exists():
        try:
            size = agent_log.stat().st_size
            print(f"      agent.log size:     {size:,} bytes")
        except Exception:
            pass  # stat() failed, non-critical
    print()
    print("To check plugin logs:")
    print(f"  grep 'HLS:' {agent_log} | tail -100")
    print()
    print("To check for errors:")
    print(f"  grep -E 'ERROR|WARNING' {agent_log} | tail -50")

    print()
    print("=" * 60)
    print("  Doctor check complete.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
