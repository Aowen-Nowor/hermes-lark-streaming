"""V2.0.0 真飞书 E2E 验收脚本 — FrameComposer 直驱真实 FeishuClient.

与 adapter.py 完全同款的接线（_CardFactory + FrameComposer + FeishuClient），
帧驱动复用 classify_send / classify_edit 的真实输出，把 V2 帧重组引擎的
全部通道打到真实飞书 API 上（CardKit create / stream_element / update /
close_streaming / 澄清卡），在真实聊天里可视化验证：

    connect  — tenant_access_token 获取 + 直发文本（连通性锚点）
    answer   — 流式卡片：建卡 → 打字机流式更新 → 两步定稿
    progress — 工具/思考状态面板：建卡前暂存并入 + 建卡后增量更新 + 去重
    interim  — 建卡前 interim 暂存 / 建卡后 interim 状态行
    full     — 仿真实 turn 全时序（progress → 建卡 → 面板 → 流式 → 定稿）
    clarify  — 澄清交互卡片（select 分支，真实可点）
    plain    — 原生路径（无标记 send → 原生文本，非卡片）

用法：
    export FEISHU_E2E_APP_ID=cli_xxx
    export FEISHU_E2E_APP_SECRET=xxx
    export FEISHU_E2E_CHAT_ID=oc_xxx          # 群聊（机器人须在群内）
    # 可选：FEISHU_E2E_OPEN_ID=ou_xxx          # 未使用（V1 兼容占位）
    # 可选：FEISHU_E2E_BASE_URL=https://open.feishu.cn/open-apis

    python3 scripts/e2e_v2.py                     # 全场景
    python3 scripts/e2e_v2.py --scenario answer   # 单场景
    python3 scripts/e2e_v2.py --list              # 列出场景

速率自保护：流式更新间隔 ≥0.4s（单卡 10 次/秒限），场景间 2s。
退出码：全部通过 0；任一失败 1。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import os
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

# ── 包别名引导（与 tests/conftest.py 同款，独立于 pytest 运行）──────────────
if "hermes_lark_streaming" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "hermes_lark_streaming",
        str(REPO_ROOT / "__init__.py"),
        submodule_search_locations=[str(REPO_ROOT)],
    )
    if _spec is None or _spec.loader is None:  # pragma: no cover
        raise ImportError(f"Cannot create module spec for {REPO_ROOT / '__init__.py'}")
    _mod = importlib.util.module_from_spec(_spec)
    _mod.__package__ = "hermes_lark_streaming"
    _mod.__path__ = [str(REPO_ROOT)]
    sys.modules["hermes_lark_streaming"] = _mod
    _spec.loader.exec_module(_mod)

from hermes_lark_streaming.adapter import _CardFactory
from hermes_lark_streaming.composer.classify import (
    classify_edit,
    classify_send,
)
from hermes_lark_streaming.composer.engine import FrameComposer
from hermes_lark_streaming.config.reader import Config
from hermes_lark_streaming.feishu.client import FeishuClient, FeishuClientConfig

STREAM_INTERVAL_SEC = 0.4   # 单卡 stream_element 限频保护（10 次/秒）
SCENARIO_GAP_SEC = 2.0      # 场景间隔（create/close 计入 1000 次/分）

results: list[tuple[str, str, str]] = []
artifacts: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(("PASS" if ok else "FAIL", name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _require_env() -> tuple[str, str, str]:
    app_id = _env("FEISHU_E2E_APP_ID") or _env("FEISHU_APP_ID")
    app_secret = _env("FEISHU_E2E_APP_SECRET") or _env("FEISHU_APP_SECRET")
    chat_id = _env("FEISHU_E2E_CHAT_ID")
    missing = [k for k, v in (("APP_ID", app_id), ("APP_SECRET", app_secret), ("CHAT_ID", chat_id)) if not v]
    if missing:
        print("缺少环境变量：" + ", ".join(f"FEISHU_E2E_{k}" for k in missing))
        print("参见脚本头部用法说明。")
        sys.exit(2)
    return app_id, app_secret, chat_id


def _build_client(app_id: str, app_secret: str) -> FeishuClient:
    base_url = _env("FEISHU_E2E_BASE_URL") or "https://open.feishu.cn/open-apis"
    return FeishuClient(FeishuClientConfig(app_id=app_id, app_secret=app_secret, base_url=base_url))


def _build_composer(client: FeishuClient) -> tuple[FrameComposer, Any]:
    """与 adapter.FeishuStreamingAdapter.__init__ 完全同款的 composer 接线."""
    try:
        Config._instance = None
        cfg: Any = Config()
    except Exception:
        cfg = None
    factory = _CardFactory(cfg)
    composer = FrameComposer(
        get_client=lambda: client,
        build_card=factory.streaming,
        build_final_card=factory.final,
        config=cfg,
    )
    return composer, factory


# ── 帧驱动助手（等价于适配器出站语义层）─────────────────────────────────────
async def adapter_send(composer: FrameComposer, chat_id: str, content: str, metadata: dict[str, Any] | None) -> Any:
    cls = classify_send(content, metadata)
    if cls.channel == "plain":
        return None  # plain 由调用方走原生（脚本中不模拟原生发送）
    return await composer.handle_send(chat_id, content, None, metadata, cls)


async def adapter_edit(composer: FrameComposer, chat_id: str, message_id: str, content: str, *, finalize: bool) -> Any:
    return await composer.handle_edit(chat_id, message_id, content, finalize, classify_edit(content, finalize))


# ── 场景 ────────────────────────────────────────────────────────────────────
async def scenario_connect(client: FeishuClient, chat_id: str) -> None:
    print("== 场景 connect：tenant_access_token + 直发文本 ==")
    t0 = time.monotonic()
    msg_id = await client.send_text_to_chat(chat_id, "🚀 feishu-streaming v2.0.0 E2E · connect 连通性测试")
    dt = time.monotonic() - t0
    ok = bool(msg_id)
    check("send_text_to_chat 返回 message_id", ok, f"om_dt={dt:.1f}s id={msg_id}")
    if ok:
        artifacts.append(f"connect 文本锚点: {msg_id}")


async def scenario_answer(composer: FrameComposer, client: FeishuClient, chat_id: str) -> None:
    print("== 场景 answer：流式卡片全周期 ==")
    chunks = ["你好，这是 ", "**V2.0.0 真飞书 E2E**", " 的流式答案。",
              "\n\n第二段：打字机效果", " 由 `cardkit_stream_element`", " 驱动。"]
    full_text = "".join(chunks)

    r1 = await adapter_send(composer, chat_id, chunks[0], {"expect_edits": True})
    check("首帧建卡（send+expect_edits）", bool(r1 and r1.success), f"msg={getattr(r1, 'message_id', None)}")
    msg_id = str(r1.message_id or "")
    if msg_id:
        artifacts.append(f"answer 卡片消息: {msg_id}")

    acc = chunks[0]
    for chunk in chunks[1:]:
        await asyncio.sleep(STREAM_INTERVAL_SEC)
        acc += chunk
        r = await adapter_edit(composer, chat_id, msg_id, acc, finalize=False)
        check(f"流式更新（+{len(chunk)} 字）", bool(r and r.success), f"len={len(acc)}")

    r = await adapter_edit(composer, chat_id, msg_id, full_text, finalize=True)
    check("两步定稿（终值落盘 + close_streaming）", bool(r and r.success))
    st = composer.state_by_message(msg_id)
    check("turn 状态 finalized", bool(st and st.finalized))
    check("answer 终值完整", st is not None and st.answer_text == full_text, f"len={len(st.answer_text) if st else 0}")


async def scenario_progress(composer: FrameComposer, client: FeishuClient, chat_id: str) -> None:
    print("== 场景 progress：工具/思考状态面板 ==")
    # ① 建卡前的 progress（暂存 → 首卡并入）；帧格式 = hermes 真实模板产物
    #    （locales/zh.yaml gateway.progress：{emoji} {tool}："{preview}"）
    r = await adapter_send(composer, chat_id, "💬 正在拆解 E2E 场景的验证点…", {"_interim_send": True})
    check("建卡前 thinking 暂存（不抛错）", r is not None)
    r = await adapter_send(composer, chat_id, "🖥️ terminal：\"echo e2e-progress && date +%H:%M:%S\"", {"_interim_send": True})
    check("建卡前工具行暂存", r is not None)

    # ② 首条 answer 建卡（应带状态面板）
    r1 = await adapter_send(composer, chat_id, "面板场景首帧。", {"expect_edits": True})
    check("建卡成功", bool(r1 and r1.success), f"msg={getattr(r1, 'message_id', None)}")
    msg_id = str(r1.message_id or "")
    if msg_id:
        artifacts.append(f"progress 卡片消息: {msg_id}")
    st = composer.state_by_message(msg_id)
    check("暂存行并入 status_lines", bool(st and len(st.status_lines) == 2),
          f"lines={list(st.status_lines) if st else None}")

    # ③ 建卡后增量更新（去重 + 追加）
    await asyncio.sleep(STREAM_INTERVAL_SEC)
    await adapter_send(composer, chat_id, "🖥️ terminal：\"echo e2e-progress && date +%H:%M:%S\"", {"_interim_send": True})
    st2 = composer.state_by_message(msg_id)
    check("重复工具行去重", bool(st2 and len(st2.status_lines) == 2), f"lines={len(st2.status_lines) if st2 else 0}")

    await asyncio.sleep(STREAM_INTERVAL_SEC)
    r = await adapter_send(composer, chat_id, "🔍 web_search：\"feishu cardkit 流式\"", {"_interim_send": True})
    st3 = composer.state_by_message(msg_id)
    check("新工具行追加并 cardkit_update", bool(r and r.success and st3 and len(st3.status_lines) == 3))

    await asyncio.sleep(STREAM_INTERVAL_SEC)
    r = await adapter_send(composer, chat_id, "💬 思考：面板渲染验证中…", {"_interim_send": True})
    st4 = composer.state_by_message(msg_id)
    check("thinking 行追加", bool(r and r.success and st4 and len(st4.status_lines) == 4))

    # ④ 定稿（面板保留最后状态）
    await asyncio.sleep(STREAM_INTERVAL_SEC)
    r = await adapter_edit(composer, chat_id, msg_id, "面板场景定稿：以上 4 行应保留在状态面板中。", finalize=True)
    check("定稿成功", bool(r and r.success))


async def scenario_interim(composer: FrameComposer, client: FeishuClient, chat_id: str) -> None:
    print("== 场景 interim：中途播报 ==")
    # ① 建卡前 interim（无卡片 → 原生路径，脚本中仅验证不抛错且分类正确）
    cls = classify_send("这是一条无 emoji 的中途播报。", {"_interim_send": True})
    check("interim 分类正确", cls.channel == "interim" and cls.kind == "advisory", f"channel={cls.channel}")

    # ② 建卡后 interim → 状态行
    r1 = await adapter_send(composer, chat_id, "interim 场景首帧。", {"expect_edits": True})
    msg_id = str(getattr(r1, "message_id", "") or "")
    check("建卡成功", bool(r1 and r1.success), f"msg={msg_id}")
    if msg_id:
        artifacts.append(f"interim 卡片消息: {msg_id}")
    await asyncio.sleep(STREAM_INTERVAL_SEC)
    r = await adapter_send(composer, chat_id, "⏳ 正在等待外部资源（interim 播报）…", {"_interim_send": True})
    st = composer.state_by_message(msg_id)
    check("interim 并入状态面板", bool(r and r.success and st and any("interim" in ln for ln in st.status_lines)),
          f"lines={len(st.status_lines) if st else 0}")
    await asyncio.sleep(STREAM_INTERVAL_SEC)
    await adapter_edit(composer, chat_id, msg_id, "interim 场景定稿。", finalize=True)


async def scenario_full(composer: FrameComposer, client: FeishuClient, chat_id: str) -> None:
    print("== 场景 full：仿真实 turn 全时序 ==")
    seq = [
        # (kind, content, meta) —— 帧格式 = hermes 真实模板产物，含 pending→preview
        # 状态推进（v2.0.5 合并语义：同工具 pending 行被 preview 原地取代）
        ("progress", "💬 正在检查系统状态，先收集内核与运行时长…", {"_interim_send": True}),
        ("progress", "🖥️ terminal...", {"_interim_send": True}),
        ("progress", "🖥️ terminal：\"echo \"===[时间]==\"; date \"+%Y-%m-%d %H:%M:%S\"\"", {"_interim_send": True}),
        ("stream", "全部检查完成。", {"expect_edits": True}),
        ("stream", "全部检查完成。\n\n**系统正常**，负载 0.12，运行 22 小时。", {"expect_edits": True}),
        ("progress", "💬 工具跑完了，正在汇总结果…", {"_interim_send": True}),
        ("progress", "⚡ notify：\"推送结果卡片\"", {"_interim_send": True}),
        ("final", "全部检查完成。\n\n**系统正常**，负载 0.12，运行 22 小时。\n\n— feishu-streaming v2.0.5 full-scenario E2E", None),
    ]
    r = await adapter_send(composer, chat_id, seq[0][1], seq[0][2])
    check("① thinking 行（建卡前暂存）", r is not None)
    r = await adapter_send(composer, chat_id, seq[1][1], seq[1][2])
    check("② pending 行（暂存）", r is not None)
    r = await adapter_send(composer, chat_id, seq[2][1], seq[2][2])
    check("③ preview 取代 pending（暂存合并）", r is not None)
    r1 = await adapter_send(composer, chat_id, seq[3][1], seq[3][2])
    check("④ answer 首帧建卡（带面板）", bool(r1 and r1.success), f"msg={getattr(r1, 'message_id', None)}")
    msg_id = str(getattr(r1, "message_id", "") or "")
    if msg_id:
        artifacts.append(f"full 卡片消息: {msg_id}")
    st = composer.state_by_message(msg_id) if msg_id else None
    check("⑤ 首卡面板 2 行（无 pending 残留）",
          bool(st and len(st.status_lines) == 2 and not any(ln.endswith("...") for ln in st.status_lines)),
          f"lines={list(st.status_lines) if st else None}")
    await asyncio.sleep(STREAM_INTERVAL_SEC)
    r = await adapter_edit(composer, chat_id, msg_id, seq[4][1], finalize=False)
    check("⑥ answer 流式更新", bool(r and r.success))
    await asyncio.sleep(STREAM_INTERVAL_SEC)
    r = await adapter_send(composer, chat_id, seq[5][1], seq[5][2])
    st = composer.state_by_message(msg_id)
    check("⑦ thinking 第 2 轮入面板", bool(r and r.success and st and len(st.status_lines) == 3))
    await asyncio.sleep(STREAM_INTERVAL_SEC)
    r = await adapter_send(composer, chat_id, seq[6][1], seq[6][2])
    st = composer.state_by_message(msg_id)
    check("⑧ notify 工具行入面板", bool(r and r.success and st and len(st.status_lines) == 4))
    await asyncio.sleep(STREAM_INTERVAL_SEC)
    r = await adapter_edit(composer, chat_id, msg_id, seq[7][1], finalize=True)
    check("⑨ 定稿（页脚 + 关流式）", bool(r and r.success))


async def scenario_clarify(composer: FrameComposer, client: FeishuClient, chat_id: str) -> None:
    print("== 场景 clarify：澄清交互卡片 ==")
    from hermes_lark_streaming.cardkit import (
        build_clarify_card,
        normalize_clarify_choices,
    )

    choices = normalize_clarify_choices(["继续全量 E2E", "只跑冒烟", "先停一下"])
    card = build_clarify_card(question="E2E 场景全部跑完了，接下来做什么？", choices=choices, clarify_id="e2e-clarify-001")
    check("澄清卡片构建（交互元素非空）", bool(card))

    msg_id = await client.send_card_to_chat(chat_id, card)
    check("澄清卡片直发成功", bool(msg_id), f"msg={msg_id}")
    if msg_id:
        artifacts.append(f"clarify 卡片消息: {msg_id}")

    # 提交态卡片（点击后的回执样式）
    from hermes_lark_streaming.cardkit import build_clarify_submitted_card
    submitted = build_clarify_submitted_card(question="E2E 场景全部跑完了，接下来做什么？", selected="继续全量 E2E", clarify_id="e2e-clarify-001")
    check("提交态卡片构建", bool(submitted))


async def scenario_plain(client: FeishuClient, chat_id: str) -> None:
    print("== 场景 plain：原生路径 ==")
    cls = classify_send("普通文本（无任何 gateway 标记）", None)
    check("plain 分类（不进卡片通道）", cls.channel == "plain", f"channel={cls.channel}")
    msg_id = await client.send_text_to_chat(chat_id, "✉️ plain 场景：这条应是无卡片原生文本。")
    check("原生 send_text 成功", bool(msg_id), f"msg={msg_id}")
    if msg_id:
        artifacts.append(f"plain 文本消息: {msg_id}")


# ── 主流程 ──────────────────────────────────────────────────────────────────
SCENARIOS = ("connect", "answer", "progress", "interim", "full", "clarify", "plain")


async def run(selected: list[str]) -> int:
    app_id, app_secret, chat_id = _require_env()
    client = _build_client(app_id, app_secret)
    composer, _factory = _build_composer(client)

    print("feishu-streaming v2.0.0 真飞书 E2E")
    print(f"  app_id={app_id[:12]}…  chat_id={chat_id[:16]}…  场景={','.join(selected)}")
    print()

    for name in selected:
        try:
            if name == "connect":
                await scenario_connect(client, chat_id)
            elif name == "answer":
                await scenario_answer(composer, client, chat_id)
            elif name == "progress":
                await scenario_progress(composer, client, chat_id)
            elif name == "interim":
                await scenario_interim(composer, client, chat_id)
            elif name == "full":
                await scenario_full(composer, client, chat_id)
            elif name == "clarify":
                await scenario_clarify(composer, client, chat_id)
            elif name == "plain":
                await scenario_plain(client, chat_id)
            else:  # pragma: no cover
                check(f"未知场景 {name}", False)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            check(f"场景 {name} 异常", False, f"{type(exc).__name__}: {exc}")
        composer.clear()
        await asyncio.sleep(SCENARIO_GAP_SEC)

    failed = [r for r in results if r[0] == "FAIL"]
    print()
    print(f"== E2E 结果：{len(results) - len(failed)}/{len(results)} 通过 ==")
    if artifacts:
        print("\n飞书聊天内可查验的产物：")
        for a in artifacts:
            print(f"  - {a}")
    if failed:
        print("\n失败项：")
        for _, name, detail in failed:
            print(f"  - {name} {('— ' + detail) if detail else ''}")
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="feishu-streaming v2.0.0 真飞书 E2E")
    parser.add_argument("--scenario", default="all",
                        help=f"逗号分隔，可选: {','.join(SCENARIOS)} 或 all")
    parser.add_argument("--list", action="store_true", help="列出场景后退出")
    args = parser.parse_args()

    if args.list:
        print("可用场景:", ", ".join(SCENARIOS))
        return

    selected = list(SCENARIOS) if args.scenario == "all" else [s.strip() for s in args.scenario.split(",") if s.strip()]
    unknown = [s for s in selected if s not in SCENARIOS]
    if unknown:
        parser.error(f"未知场景: {unknown}")
    sys.exit(asyncio.run(run(selected)))


if __name__ == "__main__":
    main()
