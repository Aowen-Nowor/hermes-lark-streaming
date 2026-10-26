"""真飞书 E2E 验收脚本 — FrameComposer 直驱真实 FeishuClient.

与 adapter.py 完全同款的接线（_CardFactory + FrameComposer + FeishuClient），
帧驱动复用 classify_send / classify_edit 的真实输出，把 V2 帧重组引擎的
全部通道打到真实飞书 API 上（CardKit create / stream_element / update /
close_streaming / 澄清卡），在真实聊天里可视化验证：

    connect  — tenant_access_token 获取 + 直发文本（连通性锚点；同时登记为
               reply 场景的引用锚点，--scenario connect,reply 连跑零额外噪音）
    answer   — 流式卡片：建卡 → 打字机流式更新 → 两步定稿
    progress — 工具/思考状态面板：建卡前暂存并入 + 建卡后增量更新 + 去重
    interim  — 建卡前 interim 暂存 / 建卡后 interim 状态行
    full     — 仿真实 turn 全时序（progress → 建卡 → 面板 → 流式 → 定稿；
               含 v2.0.22 连续 terminal 裸围栏块 → 独立步骤行归属）
    burst    — 并发帧 burst → 单卡片（v2.0.11 per-chat 串行化真飞书验证）
    native   — 官方 native 帧序列 → 单卡全回合（v2.0.13 send_stream_frame 语义）
    clarify  — 澄清交互卡片（select 分支，真实可点）
    plain    — 原生路径（无标记 send → 原生文本，非卡片）
    reply    — 卡片回复引用触发消息（v2.0.22）：om_ 锚点 → reply 形态发卡
               （群聊必验；私聊经 FEISHU_E2E_DM_CHAT_ID 双验证）+ 伪锚点
               降级直发（内容不丢）

引用锚点语义（reply 场景）：飞书 reply 的锚点本来就是一条独立聊天消息
（真实流程 = 用户的提问），卡片以回复关系挂引用线，锚点内容不进卡片体。
锚点消息文案已自解释；单独跑 reply 场景时脚本会自发一条锚点并标注。

用法：
    export FEISHU_E2E_APP_ID=cli_xxx
    export FEISHU_E2E_APP_SECRET=xxx
    export FEISHU_E2E_CHAT_ID=oc_xxx          # 群聊（机器人须在群内）
    # 可选：FEISHU_E2E_DM_CHAT_ID=oc_xxx      # 私聊 P2P 会话 id（reply 场景私聊验证）
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

from hermes_lark_streaming.composer.classify import (
    classify_edit,
    classify_send,
)
from hermes_lark_streaming.composer.engine import FrameComposer
from hermes_lark_streaming.config.reader import Config
from hermes_lark_streaming.feishu.client import FeishuClient, FeishuClientConfig
from hermes_lark_streaming.plugin.adapter import _CardFactory

_V = getattr(sys.modules["hermes_lark_streaming"], "__version__", "unknown")

STREAM_INTERVAL_SEC = 0.4   # 单卡 stream_element 限频保护（10 次/秒）
SCENARIO_GAP_SEC = 2.0      # 场景间隔（create/close 计入 1000 次/分）

results: list[tuple[str, str, str]] = []
artifacts: list[str] = []
ANCHORS: dict[str, str] = {}  # 跨场景锚点登记（connect → reply）


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
    msg_id = await client.send_text_to_chat(
        chat_id, f"🚀 feishu-streaming v{_V} E2E · connect 连通性测试（本消息可被 reply 场景引用）")
    dt = time.monotonic() - t0
    ok = bool(msg_id)
    check("send_text_to_chat 返回 message_id", ok, f"om_dt={dt:.1f}s id={msg_id}")
    if ok:
        artifacts.append(f"connect 文本锚点: {msg_id}")
        ANCHORS["connect"] = msg_id


async def scenario_answer(composer: FrameComposer, client: FeishuClient, chat_id: str) -> None:
    print("== 场景 answer：流式卡片全周期 ==")
    chunks = ["你好，这是 ", f"**v{_V} 真飞书 E2E**", " 的流式答案。",
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
        # v2.0.22 裸围栏块（hermes 连续 terminal 调用省略 header 的真实产物）：
        # 应归属 terminal 渲染为独立步骤行，而非无头灰字行（Task 29 用户困惑修复）
        ("progress", '```\necho "=== E2E FULL 裸块归属 ===" && date "+%H:%M:%S"\n```', {"_interim_send": True}),
        ("stream", "全部检查完成。", {"expect_edits": True}),
        ("stream", "全部检查完成。\n\n**系统正常**，负载 0.12，运行 22 小时。", {"expect_edits": True}),
        ("progress", "💬 工具跑完了，正在汇总结果…", {"_interim_send": True}),
        ("progress", "⚡ notify：\"推送结果卡片\"", {"_interim_send": True}),
        ("final", f"全部检查完成。\n\n**系统正常**，负载 0.12，运行 22 小时。\n\n— feishu-streaming v{_V} full-scenario E2E", None),
    ]
    r = await adapter_send(composer, chat_id, seq[0][1], seq[0][2])
    check("① thinking 行（建卡前暂存）", r is not None)
    r = await adapter_send(composer, chat_id, seq[1][1], seq[1][2])
    check("② pending 行（暂存）", r is not None)
    r = await adapter_send(composer, chat_id, seq[2][1], seq[2][2])
    check("③ preview 取代 pending（暂存合并）", r is not None)
    r = await adapter_send(composer, chat_id, seq[3][1], seq[3][2])
    check("④ 裸围栏块暂存（v2.0.22 独立步骤行）", r is not None)
    r1 = await adapter_send(composer, chat_id, seq[4][1], seq[4][2])
    check("⑤ answer 首帧建卡（带面板）", bool(r1 and r1.success), f"msg={getattr(r1, 'message_id', None)}")
    msg_id = str(getattr(r1, "message_id", "") or "")
    if msg_id:
        artifacts.append(f"full 卡片消息: {msg_id}")
    st = composer.state_by_message(msg_id) if msg_id else None
    check("⑥ 首卡面板 3 行（thinking + terminal + 裸块独立，无 pending 残留）",
          bool(st and len(st.status_lines) == 3 and any("E2E FULL" in ln for ln in st.status_lines)
               and not any(ln.endswith("...") for ln in st.status_lines)),
          f"lines={list(st.status_lines) if st else None}")
    await asyncio.sleep(STREAM_INTERVAL_SEC)
    r = await adapter_edit(composer, chat_id, msg_id, seq[5][1], finalize=False)
    check("⑦ answer 流式更新", bool(r and r.success))
    await asyncio.sleep(STREAM_INTERVAL_SEC)
    r = await adapter_send(composer, chat_id, seq[6][1], seq[6][2])
    st = composer.state_by_message(msg_id)
    check("⑧ thinking 第 2 轮入面板", bool(r and r.success and st and len(st.status_lines) == 4))
    await asyncio.sleep(STREAM_INTERVAL_SEC)
    r = await adapter_send(composer, chat_id, seq[7][1], seq[7][2])
    st = composer.state_by_message(msg_id)
    check("⑨ notify 工具行入面板", bool(r and r.success and st and len(st.status_lines) == 5))
    await asyncio.sleep(STREAM_INTERVAL_SEC)
    r = await adapter_edit(composer, chat_id, msg_id, seq[8][1], finalize=True)
    check("⑩ 定稿（页脚 + 关流式）", bool(r and r.success))


async def scenario_burst(composer: FrameComposer, client: FeishuClient, chat_id: str) -> None:
    """v2.0.11：并发帧 burst —— per-chat 串行化锁的真飞书验证.

    复现 2026-10-12 14:30 生产事故的帧形态（工具完成后思考中继/工具行/
    answer 首帧多 lane 齐发）：全部帧 asyncio.gather 真并发投递。无锁时建卡
    窗口（真飞书 create+send ≈0.5-1s）内每帧各建一张卡（一轮 3 卡事故）；
    有锁时恒 1 张，全部行并入同一张卡。
    """
    print("== 场景 burst：并发帧 → 单卡片（v2.0.11 串行化）==")
    frames = [
        ("progress", "💬 burst 场景：并发思考行一", {"_interim_send": True}),
        ("progress", "🖥️ terminal...", {"_interim_send": True}),
        ("progress", '🖥️ terminal："echo burst"', {"_interim_send": True}),
        ("progress", "💬 burst 场景：并发思考行二", {"_interim_send": True}),
        ("stream", "burst 场景并发答案第一段。", {"expect_edits": True}),
        ("stream", "burst 场景并发答案第二段。", {"expect_edits": True}),
    ]
    results = await asyncio.gather(
        *(adapter_send(composer, chat_id, content, meta) for _k, content, meta in frames)
    )
    stream_ids = {str(getattr(r, "message_id", "") or "") for r in results[4:]}
    stream_ids.discard("")
    check("并发 answer 帧落在同一张卡（无重复建卡）", len(stream_ids) == 1, f"ids={stream_ids}")
    msg_id = next(iter(stream_ids)) if stream_ids else ""
    if msg_id:
        artifacts.append(f"burst 卡片消息: {msg_id}")
    st = composer.state_by_message(msg_id) if msg_id else None
    check("answer 缓冲两段保序并存",
          bool(st and "第一段" in st.answer_text and "第二段" in st.answer_text),
          f"answer={getattr(st, 'answer_text', None)!r:.80}")
    check("面板行并入同一卡（思考×2 + 工具 preview）",
          bool(st and len(st.status_lines) >= 3),
          f"lines={list(getattr(st, 'status_lines', []))}")
    if msg_id:
        r = await adapter_edit(
            composer, chat_id, msg_id,
            "burst 场景并发答案第一段。\n\nburst 场景并发答案第二段。",
            finalize=True,
        )
        check("burst 卡定稿", bool(r and r.success))


async def scenario_native(composer: FrameComposer, client: FeishuClient, chat_id: str) -> None:
    """v2.0.13：native streaming 帧序列 —— 单卡全回合的真飞书验证.

    模拟官方 native consumer 的真实调用序列（与 gateway/
    stream_consumer_transport.py 帧原语逐帧对齐，且全部经
    composer.handle_stream_frame —— adapter.send_stream_frame 的直接委托体）：
    seed("") → 内容快照 → interim 复合帧（答案+分隔符+工具行）→ 工具后续快照
    → finalize。官方 native 语义下工具边界不封卡（transport:389 吞
    segment-break finalize）→ 全程恰好 1 张卡；复合帧工具行拆进状态面板，
    正文不被工具行污染。
    """
    print("== 场景 native：官方 native 帧序列 → 单卡全回合（v2.0.13）==")
    chat = chat_id  # native 序列用同一 chat
    # ① 种子帧：官方 _start_transports/_native_push 首帧 text=""
    ok = await composer.handle_stream_frame(chat, "", finalize=False)
    check("① 种子帧建卡骨架", ok is True)
    st = composer.active_state(chat)
    card_id_1 = st.card_id if st else ""
    check("② 种子帧后无 answer 内容", bool(st and st.answer_text == ""))
    await asyncio.sleep(STREAM_INTERVAL_SEC)
    # ② 内容快照（cumulative：全量而非追加）
    ok = await composer.handle_stream_frame(chat, "native 场景答案开始。", finalize=False)
    check("③ 内容快照写入", ok is True and composer.active_state(chat).answer_text == "native 场景答案开始。")
    await asyncio.sleep(STREAM_INTERVAL_SEC)
    # ③ interim 复合帧（官方 _compose_frame_content 形态）
    ok = await composer.handle_stream_frame(
        chat, f"native 场景答案开始。\n\n---\n🔍 Searching the web", finalize=False)
    st = composer.active_state(chat)
    check("④ 复合帧：正文不被工具行污染",
          ok is True and st is not None and st.answer_text == "native 场景答案开始。")
    check("⑤ 复合帧：工具行进面板",
          bool(st and st.status_lines == ["🔍 Searching the web"]),
          f"lines={list(st.status_lines) if st else None}")
    await asyncio.sleep(STREAM_INTERVAL_SEC)
    # ④ 工具后续快照（真实 delta 已清 overlay，纯正文）
    ok = await composer.handle_stream_frame(
        chat, "native 场景答案开始。检索完成，单卡全回合。", finalize=False)
    st = composer.active_state(chat)
    check("⑥ 工具边界后仍同卡（单卡全回合）",
          ok is True and st is not None and st.card_id == card_id_1)
    check("⑦ 正文快照已更新", bool(st and st.answer_text.endswith("单卡全回合。")))
    msg_id = st.card_message_id if st else ""
    if msg_id:
        artifacts.append(f"native 卡片消息: {msg_id}")
    await asyncio.sleep(STREAM_INTERVAL_SEC)
    # ⑤ 终帧（got_done → finalize=True）
    ok = await composer.handle_stream_frame(
        chat, f"native 场景答案开始。检索完成，单卡全回合。\n\n— feishu-streaming v{_V} native-scenario E2E", finalize=True)
    st2 = composer._states.get(chat)
    check("⑧ 终帧定稿", ok is True and st2 is not None and st2.finalized)
    check("⑨ 全程单卡（card_id 不变）", bool(st2 and st2.card_id == card_id_1))


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


async def scenario_oneshot(client: FeishuClient, chat_id: str) -> None:
    print("== 场景 oneshot：cron 投递一次性定稿卡（v2.0.14）==")
    from hermes_lark_streaming.cardkit import build_cron_card, build_gateway_card
    from hermes_lark_streaming.cardkit.special import parse_cron_delivery

    # 官方 _deliver_result 包装格式（cron/scheduler_delivery.py:2025-2031）
    wrapped = (
        "Cronjob Response: e2e-oneshot-demo\n"
        "(job_id: e2e0nest014)\n"
        "-------------\n"
        "\n"
        "#### 一次性定稿卡验证\n\n"
        "- 官方包装头解析（任务名 + job_id）\n"
        "- **markdown 粗体渲染**\n"
        "- 灰色 job_id 溯源页脚\n\n"
        "本卡即 cron 定时任务投递的落地形态（此前为 msg_type=post 富文本）。\n"
    )
    title, job_id, body = parse_cron_delivery(wrapped)
    check("包装头解析（任务名）", title == "e2e-oneshot-demo", f"title={title!r}")
    check("包装头解析（job_id）", job_id == "e2e0nest014", f"job_id={job_id!r}")
    check("包装头剥离", "Cronjob Response:" not in body and "(job_id:" not in body)
    card = build_cron_card(body, title=title, job_id=job_id)
    elements = card["body"]["elements"]
    check(
        "cron 卡构建（标题行 + job_id 页脚）",
        elements[0]["tag"] == "div"
        and "icon" not in elements[0]  # v2.0.16：头部无图标
        and "job_id: e2e0nest014" in elements[-1]["text"]["content"],
    )
    msg_id = await client.send_card_to_chat(chat_id, card)
    check("cron 一次性卡直发成功", bool(msg_id), f"msg={msg_id}")
    if msg_id:
        artifacts.append(f"cron 一次性卡消息: {msg_id}")

    gw_card = build_gateway_card(
        "✅ oneshot 场景：回合外 final（notify 无活跃卡）与 ≥200 字 plain "
        "同走本静态卡通道（build_gateway_card）。\n\n第二张：gateway 静态卡变体。"
    )
    msg2 = await client.send_card_to_chat(chat_id, gw_card)
    check("gateway 静态卡直发成功", bool(msg2), f"msg={msg2}")
    if msg2:
        artifacts.append(f"gateway 静态卡消息: {msg2}")


async def scenario_reply(composer: FrameComposer, client: FeishuClient, chat_id: str) -> None:
    """v2.0.22：卡片回复引用触发消息 —— 群聊/私聊双场景 + 降级红线.

    锚点策略：connect 场景登记的文本消息优先复用（--scenario connect,reply
    连跑零额外噪音）；单独跑时自发一条自解释锚点。飞书 reply 语义下锚点
    本体是独立消息（真实流程 = 用户提问），卡片以回复关系挂引用线。
    """
    from hermes_lark_streaming.composer.classify import CH_STREAM, ClassifyResult

    print("== 场景 reply：卡片引用触发消息（v2.0.22，群聊/私聊）==")
    anchor = ANCHORS.pop("connect", "")
    if anchor:
        check("① 复用 connect 场景锚点", True, f"om={anchor[:28]}")
    else:
        anchor = await client.send_text_to_chat(
            chat_id, "（E2E 锚点 · 模拟用户提问）卡片将回复引用本消息——飞书 reply 语义下锚点是独立消息，引用线挂在本消息上，锚点内容不进卡片体。")
        check("① 锚点消息已发出（模拟用户提问）", bool(anchor), f"om={anchor[:28]}")
    if not anchor:
        return
    cls = ClassifyResult(CH_STREAM, kind="answer", raw_content="内容", meta={})
    r = await composer.handle_send(
        chat_id, "reply 场景：这张卡应以引用形态回应上面的锚点消息。", anchor,
        {"expect_edits": True}, cls)
    ok = bool(r and r.success and getattr(r, "message_id", None))
    check("② 群聊引用卡片已发出（reply 形态）", ok, f"msg={getattr(r, 'message_id', None)}")
    if ok:
        artifacts.append(f"reply 群聊卡片: {getattr(r, 'message_id', '')}（引用 {anchor[:28]}）")

    # 私聊双场景（可选）：P2P 会话同样以 om_ 锚点引用（v2.0.22 需求：私聊+群聊都生效）
    dm = _env("FEISHU_E2E_DM_CHAT_ID")
    if dm:
        dm_anchor = await client.send_text_to_chat(
            dm, "（E2E 锚点 · 模拟用户提问 · 私聊）卡片将回复引用本消息。")
        check("③ 私聊锚点已发出", bool(dm_anchor), f"om={dm_anchor[:28]}")
        r2 = await composer.handle_send(
            dm, "reply 场景（私聊）：这张卡应以引用形态回应你的锚点消息。", dm_anchor,
            {"expect_edits": True}, cls)
        ok2 = bool(r2 and r2.success and getattr(r2, "message_id", None))
        check("④ 私聊引用卡片已发出（reply 形态）", ok2, f"msg={getattr(r2, 'message_id', None)}")
        if ok2:
            artifacts.append(f"reply 私聊卡片: {getattr(r2, 'message_id', '')}")
    else:
        check("③ 私聊验证跳过（未配置 FEISHU_E2E_DM_CHAT_ID）", True, "SKIP — 不计失败")

    # 降级红线：伪锚点 reply 必败（99992354）→ 直发内容不丢。
    # 必须先清 composer 状态——② 已为本 chat 建了活跃卡，不清会让 send 帧
    # 并入活跃卡（返回旧 message_id），根本走不到 reply→fallback 分支
    # （s170-B 用独立 composer 才测到目标分支的教训）。
    composer.clear()
    r3 = await composer.handle_send(
        chat_id, "reply 场景：伪锚点降级直发的卡片（内容不丢验证）。", "om_e2e_nonexistent_anchor",
        {"expect_edits": True}, cls)
    check("⑤ 伪锚点降级直发成功（新消息，内容不丢）",
          bool(r3 and r3.success and getattr(r3, "message_id", None)
               and str(getattr(r3, "message_id", "")) not in {anchor, str(getattr(r, "message_id", "") or "")}),
          f"msg={getattr(r3, 'message_id', None)}")
    if getattr(r3, "message_id", None):
        artifacts.append(f"reply 降级直发卡: {getattr(r3, 'message_id', '')}")


# ── 主流程 ──────────────────────────────────────────────────────────────────
SCENARIOS = ("connect", "answer", "progress", "interim", "full", "burst", "native", "clarify", "plain", "oneshot", "reply")


async def run(selected: list[str]) -> int:
    app_id, app_secret, chat_id = _require_env()
    client = _build_client(app_id, app_secret)
    composer, _factory = _build_composer(client)

    print(f"feishu-streaming v{_V} 真飞书 E2E")
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
            elif name == "burst":
                await scenario_burst(composer, client, chat_id)
            elif name == "native":
                await scenario_native(composer, client, chat_id)
            elif name == "clarify":
                await scenario_clarify(composer, client, chat_id)
            elif name == "plain":
                await scenario_plain(client, chat_id)
            elif name == "oneshot":
                await scenario_oneshot(client, chat_id)
            elif name == "reply":
                await scenario_reply(composer, client, chat_id)
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
    parser = argparse.ArgumentParser(description=f"feishu-streaming v{_V} 真飞书 E2E")
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
