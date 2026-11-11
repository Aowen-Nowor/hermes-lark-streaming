#!/usr/bin/env python3
"""hermes-agent 新版本兼容性探测（hermes-release-watch 工作流的结论来源）.

用户验收语义（2026-11-11 指令）：工作流检测到 hermes-agent 新版本后，
自动拉取新版源码与本插件最新代码做真实测试，推送「插件是否兼容」的
最终结论——而不是通知人工去做哪些测试。

五步探测（每步产出可读明细，任何一步失败都给出失配点后立即定性）：

  S1  hermes 源码导入    PYTHONPATH 前置新版源码，import 内置飞书适配器
                         （失败 = 依赖/结构级失配，明细含 ImportError 链）
  S2  插件绑定           import 插件 adapter，验证 _builtin 已绑定真基类
                         （未绑定 = 继承面断裂，插件将 fail-fast 拒启）
  S3  继承面 fail-fast   _rebuild_class_with_real_base + 实例化触发
                         _probe_override_surface（send/edit_message/
                         send_clarify 缺失即 RuntimeError——插件第一红线）
  S4  官方表面清单       插件运行时依赖的基类方法逐项核对（超出
                         fail-fast 三方法之外的内生依赖面）
  S5  回归测试           插件 pytest 全套在「hermes 源码于 sys.path」的
                         真实环境下运行（= 新版 hermes 场景的插件回归）

退出码：0 = 兼容（全部通过）；1 = 存在失配（--json 落盘供工作流读取）。
本脚本只读不写：不改插件状态、不连飞书、不注册任何运行时钩子。
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import subprocess
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace

PLUGIN_ROOT = Path(__file__).resolve().parent.parent

# S4：插件运行时内生依赖的官方表面（生产代码逐一引用过的方法/属性）。
# 依据：plugin/adapter.py + composer/* 的 super()/builtin_attr 引用清单；
# 任何一项缺失都意味着该 hermes 版本下插件部分能力失灵。
SURFACE_CHECKLIST = (
    "send",
    "edit_message",
    "send_clarify",
    "send_stream_frame",
    "delete_message",
    "handle_message",
    "_handle_message_with_guards",
    "set_message_handler",
    "_CARD_ACTION_ROUTES",
    "send_card_to_chat",
    "reply_card_by_id",
    "cardkit_create",
    "cardkit_update",
    "cardkit_stream_element",
)

_HERMES_VERSION_ATTRS = ("__version__", "VERSION", "version")


def _hermes_version() -> str:
    for attr in _HERMES_VERSION_ATTRS:
        try:
            import gateway.config  # noqa: F401 — 版本探测宿主

            mod = sys.modules.get("gateway.config")
            val = getattr(mod, attr, None)
            if isinstance(val, str) and val.strip():
                return val.strip()
        except Exception:  # noqa: BLE001 — 版本装饰信息，尽力而为
            pass
    # hermes_cli 是版本事实源（桌面版/CLI 同源）
    for mod_name in ("hermes_cli", "hermes"):
        try:
            mod = importlib.import_module(mod_name)
            for attr in _HERMES_VERSION_ATTRS:
                val = getattr(mod, attr, None)
                if isinstance(val, str) and val.strip():
                    return val.strip()
        except Exception:  # noqa: BLE001
            continue
    return "unknown"


def _register_plugin_package() -> None:
    """复刻 hermes _load_directory_module 的目录插件加载（tests/conftest 同款）."""
    init_file = PLUGIN_ROOT / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "hermes_lark_streaming", str(init_file),
        submodule_search_locations=[str(PLUGIN_ROOT)])
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create module spec for {init_file}")
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "hermes_lark_streaming"
    module.__path__ = [str(PLUGIN_ROOT)]
    sys.modules["hermes_lark_streaming"] = module
    spec.loader.exec_module(module)


def _step(name: str) -> None:
    print(f"\n=== {name} ===")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-dir", required=True, help="hermes-agent 源码目录（新版 checkout）")
    parser.add_argument("--skip-tests", action="store_true", help="跳过 S5 回归测试（调试用）")
    parser.add_argument("--json", dest="json_path", help="结论 JSON 落盘路径（工作流读取）")
    args = parser.parse_args()

    hermes_dir = Path(args.hermes_dir).resolve()
    report: dict = {
        "verdict": "pass",
        "steps": [],
        "surface_missing": [],
        "hermes_version": "unknown",
        "tests_passed": None,
        "tests_failed": None,
    }

    def _record(step: str, ok: bool, detail: str) -> bool:
        report["steps"].append({"step": step, "ok": ok, "detail": detail[:1500]})
        print(f"[{'PASS' if ok else 'FAIL'}] {step}: {detail}")
        return ok

    if not hermes_dir.exists():
        print(f"hermes-agent 源码目录不存在: {hermes_dir}", file=sys.stderr)
        return 1

    # S1：hermes 源码导入（PYTHONPATH 前置——工作流已设，此处双保险）
    _step("S1 hermes 源码导入")
    hermes_path = str(hermes_dir)
    if hermes_path not in sys.path:
        sys.path.insert(0, hermes_path)
    try:
        builtin = importlib.import_module("plugins.platforms.feishu.adapter")
        report["hermes_version"] = _hermes_version()
        _record("S1 hermes 源码导入", True,
                f"plugins.platforms.feishu.adapter OK（hermes {report['hermes_version']}）")
    except Exception as exc:  # noqa: BLE001 — 失配即结论
        detail = f"{type(exc).__name__}: {exc}\n" + traceback.format_exc(limit=3)
        _record("S1 hermes 源码导入", False, detail)
        report["verdict"] = "fail"
        _emit(report, args.json_path)
        return 1

    # S2：插件包导入 + builtin 绑定验证
    _step("S2 插件绑定")
    try:
        _register_plugin_package()
        plugin_adapter = importlib.import_module("hermes_lark_streaming.plugin.adapter")
    except Exception as exc:  # noqa: BLE001
        detail = f"{type(exc).__name__}: {exc}\n" + traceback.format_exc(limit=3)
        _record("S2 插件绑定", False, detail)
        report["verdict"] = "fail"
        _emit(report, args.json_path)
        return 1
    if plugin_adapter._builtin is None:
        detail = (
            "插件导入成功但内置基类未绑定（_builtin=None）——继承面断裂，"
            f"原始导入错误: {plugin_adapter._BUILTIN_IMPORT_ERROR}"
        )
        _record("S2 插件绑定", False, detail)
        report["verdict"] = "fail"
        _emit(report, args.json_path)
        return 1
    _record("S2 插件绑定", True, "hermes_lark_streaming.plugin.adapter OK，内置基类已绑定")

    # S3：类重建 + 实例化触发继承面 fail-fast 探测
    _step("S3 继承面 fail-fast（类重建 + 实例化探测）")
    try:
        adapter_cls = plugin_adapter._rebuild_class_with_real_base()
        # 生产同构：实例化 __init__ 内部先跑 _probe_override_surface，
        # send/edit_message/send_clarify 任一缺失 → RuntimeError（fail-fast 红线）。
        config = SimpleNamespace(extra={}, enabled=True)
        instance = adapter_cls(config)
        if instance.__class__.__bases__[0] is plugin_adapter._MissingBuiltin:
            raise RuntimeError("类重建后仍挂在占位基类上——继承面换基失败")
        _record("S3 继承面 fail-fast", True,
                f"{adapter_cls.__name__} 实例化成功，基类={instance.__class__.__bases__[0].__module__}.{instance.__class__.__bases__[0].__name__}")
    except Exception as exc:  # noqa: BLE001
        detail = f"{type(exc).__name__}: {exc}\n" + traceback.format_exc(limit=3)
        _record("S3 继承面 fail-fast", False, detail)
        report["verdict"] = "fail"
        _emit(report, args.json_path)
        return 1

    # S4：官方表面清单逐项核对
    _step("S4 官方表面清单")
    base = adapter_cls.__bases__[0]
    missing = [name for name in SURFACE_CHECKLIST if not hasattr(base, name)]
    report["surface_missing"] = missing
    if missing:
        _record("S4 官方表面清单", False, f"缺失 {len(missing)} 项: {', '.join(missing)}")
        report["verdict"] = "fail"
        _emit(report, args.json_path)
        return 1
    _record("S4 官方表面清单", True, f"{len(SURFACE_CHECKLIST)} 项表面全部存在")

    # S5：插件回归测试（hermes 源码在 sys.path 的新版环境）
    _step("S5 插件回归测试")
    if args.skip_tests:
        _record("S5 插件回归测试", True, "--skip-tests 跳过")
    else:
        env = dict(os.environ)
        env["PYTHONPATH"] = hermes_path + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-q", "-p", "no:cacheprovider"],
            cwd=str(PLUGIN_ROOT), env=env, capture_output=True, text=True, timeout=1200,
        )
        tail = "\n".join((proc.stdout or "").strip().splitlines()[-8:])
        if proc.returncode != 0:
            _record("S5 插件回归测试", False, f"exit={proc.returncode}\n{tail}")
            report["verdict"] = "fail"
            report["tests_stdout_tail"] = tail
            _emit(report, args.json_path)
            return 1
        # pytest -q 末行形如 "N passed, M failed"
        import re
        m = re.search(r"(\d+) passed", proc.stdout or "")
        failed = re.search(r"(\d+) failed", proc.stdout or "")
        report["tests_passed"] = int(m.group(1)) if m else None
        report["tests_failed"] = int(failed.group(1)) if failed else 0
        _record("S5 插件回归测试", True, proc.stdout.strip().splitlines()[-1] if proc.stdout else "exit=0")

    print(f"\n=== 结论: {report['verdict'].upper()} ===")
    _emit(report, args.json_path)
    return 0 if report["verdict"] == "pass" else 1


def _emit(report: dict, json_path: str | None) -> None:
    if json_path:
        Path(json_path).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
