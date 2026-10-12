"""V2.0.0 冒烟验收脚本（可重复执行）.

用法：
    python3 scripts/smoke_v2.py                 # Level 1（无外部依赖）
    python3 scripts/smoke_v2.py --with-sdk      # Level 2（需 lark-oapi）
    python3 scripts/smoke_v2.py --with-hermes [HERMES_ROOT]   # Level 3（需 hermes 源码树）

Level 3 会把 hermes 源码树加入 sys.path，真实执行：
    - 内置 FeishuAdapter 导入
    - FeishuStreamingAdapter 继承 + 继承面探测
    - FrameComposer 帧分类与路由决策（不发起网络）
    - clarify / aowen 模块导入
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(("PASS" if ok else "FAIL", name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def level1() -> None:
    print("== Level 1: 包结构 / plugin.yaml / 帧分类器 ==")
    # plugin.yaml
    import yaml

    manifest = yaml.safe_load((REPO_ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    check("plugin.yaml kind=platform", manifest.get("kind") == "platform")
    check("plugin.yaml name", manifest.get("name") == "feishu-streaming-platform")
    check("plugin.yaml version=2.0.0", manifest.get("version") == "2.0.0")
    # 平台名推导（hermes loader 规则：strip trailing '-platform'）
    name = manifest["name"]
    platform_name = name.removesuffix("-platform")
    check("平台名推导 feishu-streaming", platform_name == "feishu-streaming")

    # 帧分类器（直接以源码方式加载，避免包别名依赖）
    sys.path.insert(0, str(REPO_ROOT.parent))
    mod = _load_pkg("classify")
    cls_send = mod.classify_send
    # 样本 1：answer 流首帧
    r = cls_send("Hello", {"expect_edits": True})
    check("流帧分类", r.channel == "stream" and r.kind == "answer", f"channel={r.channel}")
    check("内部标记剥离", "_interim_send" not in r.meta)
    # 样本 2：thinking 气泡（progress lane）
    r = cls_send("💬 正在推理这个问题的边界…", {"_interim_send": True})
    check("thinking 分类", r.channel == "progress" and r.kind == "thinking", f"kind={r.kind}")
    check("thinking 文本剥离", r.thinking_text == "正在推理这个问题的边界…")
    # 样本 3：工具行
    r = cls_send("🔧 bash：\"ls -la\"", {"_interim_send": True})
    check("工具行分类", r.channel == "progress" and r.kind == "tool" and r.tool_name == "bash", f"tool={r.tool_name}")
    # 样本 4：interim 播报
    r = cls_send("这是一个中途说明文本，没有 emoji 前缀。", {"_interim_send": True})
    check("interim 分类", r.channel == "interim", f"channel={r.channel}")
    # 样本 5：定稿帧
    r = cls_send("最终回答", {"notify": True})
    check("final 分类", r.channel == "final")
    # 样本 6：普通消息
    r = cls_send("cron 投递内容", None)
    check("plain 分类", r.channel == "plain")
    # 样本 7：异常输入不抛
    r = cls_send(None, {"expect_edits": "yes"})
    check("防御性分类（None content）", r is not None)


def _load_pkg(sub: str):
    """以独立包名加载 composer 子模块（规避相对导入链，Level 1 专用）."""
    import importlib.util

    name = f"_smoke_pkg_{sub}"
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "composer" / f"{sub}.py",
    )
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def _install_package_alias() -> None:
    """把 repo root 以 ``hermes_lark_streaming`` 包名注册（目录名是连字符）."""
    import importlib.util

    if "hermes_lark_streaming" in sys.modules:
        return
    init_py = REPO_ROOT / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "hermes_lark_streaming", init_py,
        submodule_search_locations=[str(REPO_ROOT)],
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["hermes_lark_streaming"] = pkg
    spec.loader.exec_module(pkg)


def level2() -> None:
    print("== Level 2: 卡片构建（需 lark-oapi）==")
    sys.path.insert(0, str(REPO_ROOT.parent))
    _install_package_alias()
    import hermes_lark_streaming  # noqa: F401 — 触发 __init__（版本读取）
    from hermes_lark_streaming.cardkit.cards import build_streaming_card_v2

    card = build_streaming_card_v2()
    check("流式卡片构建", card.get("schema") == "2.0" and card["config"].get("streaming_mode") is True)
    from hermes_lark_streaming.cardkit.special import build_clarify_card

    c = build_clarify_card(question="选一个", choices=["a", "b"], clarify_id="smoke")
    check("澄清卡片构建", bool(c.get("body", {}).get("elements")))
    from hermes_lark_streaming.feishu.client import FeishuClientConfig

    cfg = FeishuClientConfig(app_id="cli_x", app_secret="s")
    check("FeishuClientConfig", cfg.base_url.endswith("/open-apis"))


def level3(hermes_root: str) -> None:
    print("== Level 3: 真实 hermes 导入链 / 继承面 / 注册协议 ==")
    sys.path.insert(0, str(hermes_root))
    sys.path.insert(0, str(REPO_ROOT.parent))
    _install_package_alias()
    import hermes_lark_streaming as pkg

    check("包导入 + 版本", pkg.__version__ == "2.0.0", pkg.__version__)
    from hermes_lark_streaming.plugin import adapter as A

    # 惰性导入：register 前基类为占位，显式触发真实导入
    A._get_builtin()
    check("内置 FeishuAdapter 导入", A._get_builtin().FeishuAdapter is not None,
          type(A._get_builtin().FeishuAdapter).__module__)
    # 注册协议（mock ctx，只验证调用面，不真注册）——register 会用真实基类重建类
    calls: dict[str, tuple] = {}

    class _Ctx:
        def register_platform(self, *a, **kw):
            calls["platform"] = (a, kw)

        def register_hook(self, *a, **kw):
            calls.setdefault("hooks", []).append(a)

    A.register(_Ctx())
    kw = calls.get("platform", ((), {}))[1]
    check("register_platform 调用", kw.get("name") == "feishu-streaming"
          and kw.get("adapter_factory") is A.FeishuStreamingAdapter)
    check("依赖检查 fn 复用内置", kw.get("check_fn") is A._get_builtin().feishu_deps_present)
    check("/aowen 官方 hook 注册", any(h[0] == "pre_gateway_dispatch" for h in calls.get("hooks", [])))
    # 继承链（register 重建后验证）
    from gateway.platforms.base import BasePlatformAdapter  # type: ignore

    check("继承链完整", issubclass(A.FeishuStreamingAdapter, A._get_builtin().FeishuAdapter)
          and issubclass(A._get_builtin().FeishuAdapter, BasePlatformAdapter))
    # composer 引擎导入
    from hermes_lark_streaming.composer import FrameComposer

    check("FrameComposer 导入", FrameComposer is not None)
    # clarify / aowen / config / plugin 模块导入
    from hermes_lark_streaming.plugin import clarify  # noqa: F401

    check("clarify 模块导入", True)
    from hermes_lark_streaming.aowen import (  # noqa: F401
        build_status_card,
        handle_pre_gateway_dispatch,
    )

    check("aowen 模块导入（零 controller 依赖）", True)
    from hermes_lark_streaming.plugin import (  # noqa: F401
        check_lark_oapi_floor,
        ensure_streaming_config,
    )

    check("plugin 配置模块导入", True)
    # 重写点存在（重建后的类上）
    check("重写点存在（send/edit/send_clarify）",
          all(hasattr(A.FeishuStreamingAdapter, n) for n in ("send", "edit_message", "send_clarify")))
    # 零补丁验证：内置适配器的 send 未被替换（V2 走子类重写，不动内置类）
    import plugins.platforms.feishu.adapter as _builtin_mod  # type: ignore

    check("内置 send 未被 patch",
          _builtin_mod.FeishuAdapter.send.__module__ != "hermes_lark_streaming.plugin.adapter")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-sdk", action="store_true")
    ap.add_argument("--with-hermes", nargs="?", const="/home/z/my-project/audit/hermes-agent")
    args = ap.parse_args()

    level1()
    if args.with_sdk:
        try:
            level2()
        except Exception as exc:  # noqa: BLE001 — 冒烟顶层兜底
            check("Level 2 执行", False, repr(exc))
    if args.with_hermes:
        try:
            level3(args.with_hermes)
        except Exception as exc:  # noqa: BLE001 — 冒烟顶层兜底
            check("Level 3 执行", False, repr(exc))

    failed = [r for r in results if r[0] == "FAIL"]
    print(f"\n== 冒烟结果：{len(results) - len(failed)}/{len(results)} 通过 ==")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
