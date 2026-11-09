"""v2.0.33 回归测试 — 健壮性审计六连修（FIX-1/2/3/4/6/7）.

审计来源：健壮性六项审计（2026-11-09），每项均有生产日志/代码路径实证：

- FIX-1 网关停机收口：全链路无 teardown（rg 'shutdown|disconnect|unregister'
  零命中）；agent.log 2026-11-07 13:50:38 建卡与「Sent shutdown notification」
  同秒、随后 adapter 断连零 finalize → 远端卡 streaming 永挂（跑马灯）。
  composer.seal_all_on_shutdown + adapter.disconnect（基类停机面）双端修复。
- FIX-2 _pause_expire 吞定稿失败：_finalize 先置 finalized 再跑远端四步，
  中段异常被 blanket debug 吞掉 → 状态已 finalized、看门狗豁免、远端卡永挂
  流式态。补 _degrade + _seal_degraded_card（与 send/edit/stream 帧路径同语义）。
- FIX-3 handle_edit 无 finalized 守卫：定稿后 60s GC 冷却窗口内迟到的非定稿
  edit 对已关流式的卡发 cardkit_stream_element → 300309
  （feishu/client.py:107 CARDKIT_STREAMING_CLOSED）无处处理 → 整通道误降级。
  短路返回成功（定稿 edit 已携带全量终值，迟到帧按构造是重复内容）。
- FIX-4 _by_message 滞留旧键：_create_card 复用状态对象覆盖 card_message_id
  时不驱逐旧键 → 删除旧卡消息时 state_by_message(旧id) 解析到当前活跃状态，
  adapter.delete_message 无脑 forget(chat_id) 把进行中回合连根拔掉。
  两层修复：_create_card 先驱逐旧键（4a）+ delete_message 身份校验后才
  forget（4b，纵深防御）。
- FIX-6 GC 收口未定稿孤儿：idle>TTL 分支直接 pop，远端卡不收口（card_ttl_sec
  < watchdog_idle_sec 配置下看门狗永轮不到，GC 是唯一收口时机）。回收前置
  degraded+finalized 并异步 seal。
- FIX-7 看门狗重武装硬编码：call_later(_WATCHDOG_IDLE_SEC, ...) 无视
  Config.watchdog_idle_sec——自定义阈值 <300 的部署里收口延迟最高 5 倍于
  配置值。统一走 _watchdog_idle_sec()。

适配器层测试沿 test_clarify_delegation.py 的子进程模式（stub 基类预置
sys.modules，模拟 hermes 进程内导入时序）；composer 层沿 test_v226 的
fake client 直驱模式。
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from hermes_lark_streaming.composer import engine as _engine
from hermes_lark_streaming.composer.classify import CH_STREAM, ClassifyResult
from hermes_lark_streaming.composer.engine import CardTurnState, FrameComposer

_REPO_ROOT = Path(__file__).resolve().parent.parent

_LOGGER_NAME = "hermes_lark_streaming.composer"


# ---------------------------------------------------------------------------
# 测试夹具（对齐 test_v224_lazy_panel._FullClient / test_v208._settle 模式）
# ---------------------------------------------------------------------------


class _BaseFakeClient:
    """finalize 全链 fake：create/send/stream/update/batch/close/reopen 全补齐."""

    def __init__(self) -> None:
        self.cards: dict[str, dict] = {}
        self._n = 0
        self.batch_actions: list[list[dict]] = []
        self.close_calls = 0
        self.stream_calls = 0
        self.last_stream_content = ""
        self.reopen_calls = 0

    async def cardkit_create(self, card):
        self._n += 1
        cid = str(self._n)
        self.cards[cid] = {"card": card, "seq": 0}
        return cid

    async def send_card_by_id_to_chat(self, chat_id, card_id):
        return f"om_{card_id}"

    async def cardkit_stream_element(self, card_id, element_id, content, sequence=0):
        self.stream_calls += 1
        self.last_stream_content = content
        self.cards[card_id]["seq"] = sequence

    async def cardkit_update(self, card_id, card, sequence=0):
        self.cards[card_id]["card"] = card
        self.cards[card_id]["seq"] = sequence

    async def cardkit_batch_update(self, card_id, actions, sequence=0):
        self.batch_actions.append(actions)
        self.cards[card_id]["seq"] = sequence

    async def cardkit_close_streaming(self, card_id, sequence=0, *, summary=""):
        self.close_calls += 1
        self.cards[card_id]["seq"] = sequence

    async def cardkit_reopen_streaming(self, card_id, sequence=0):
        self.reopen_calls += 1
        self.cards[card_id]["seq"] = sequence


class _FailOnceStreamClient(_BaseFakeClient):
    """cardkit_stream_element 第 N 次调用抛 300309 型错误（其余调用成功）.

    模拟生产 300309（CARDKIT_STREAMING_CLOSED）：非 300317/200850，
    call_with_seq_recovery 不自愈、原样上抛——定稿四步第一刀（answer 终值
    patch）被击穿，正是 FIX-2 审计的异常形态。
    """

    def __init__(self, fail_on_call: int) -> None:
        super().__init__()
        self._fail_on_call = fail_on_call

    async def cardkit_stream_element(self, card_id, element_id, content, sequence=0):
        self.stream_calls += 1
        if self.stream_calls == self._fail_on_call:
            err = RuntimeError("cardkit_stream_element: streaming closed (300309)")
            err.code = 300309
            raise err
        self.last_stream_content = content
        self.cards[card_id]["seq"] = sequence


def _make_composer(
    client: _BaseFakeClient, *, suspend_sec: float = 0.0, config=None
) -> FrameComposer:
    from hermes_lark_streaming.cardkit.cards import build_streaming_card_v2

    return FrameComposer(
        get_client=lambda: client,
        build_card=lambda **kw: build_streaming_card_v2(**kw),
        build_final_card=lambda **kw: {},
        config=config,
        clarify_suspend_sec=suspend_sec,
    )


async def _settle() -> None:
    """让 fire-and-forget 任务（GC 孤儿收口）跑完（test_v208 同款）."""
    for _ in range(4):
        await asyncio.sleep(0)
    cur = asyncio.current_task()
    ts = [t for t in asyncio.all_tasks() if t is not cur and not t.done()]
    if ts:
        await asyncio.gather(*ts, return_exceptions=True)


def _edit_cls(text: str) -> ClassifyResult:
    return ClassifyResult(CH_STREAM, raw_content=text)


def _run_plugin_script(script: str, expect_token: str) -> None:
    """子进程运行适配器层回归脚本（test_clarify_delegation 同款）."""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        check=False, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, (
        f"plugin-script regression failed\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    assert expect_token in proc.stdout


# ---------------------------------------------------------------------------
# FIX-1：网关停机收口（composer.seal_all_on_shutdown + adapter.disconnect）
# ---------------------------------------------------------------------------


class TestFix1ShutdownSeal:
    async def test_seal_all_only_touches_live_cards(self, caplog):
        """活跃卡/暂停卡收口；已定稿/已降级/无卡占位一律跳过；返回收口数."""
        client = _BaseFakeClient()
        comp = _make_composer(client)
        loop = asyncio.get_running_loop()
        # A：活跃流式卡（顺带挂一个暂停定时器——收口须连带取消）
        await comp.handle_stream_frame("oc_sd_a", "回答中", finalize=False)
        st_a = comp._states["oc_sd_a"]
        comp._pause_timers["oc_sd_a"] = loop.call_later(999, lambda: None)
        # B：clarify 暂停卡——暂停卡同样是远端活卡，必须一并收口
        await comp.handle_stream_frame("oc_sd_b", "", finalize=False)
        await comp.suspend_for_clarify("oc_sd_b", "q")
        st_b = comp._states["oc_sd_b"]
        # C：已定稿卡（定稿时已关流式——不得重复收口）
        await comp.handle_stream_frame("oc_sd_c", "定稿", finalize=False)
        await comp.handle_stream_frame("oc_sd_c", "定稿", finalize=True)
        st_c = comp._states["oc_sd_c"]
        assert st_c.finalized
        close_before = client.close_calls  # C 的定稿已消耗一次 close
        # D：已降级卡（降级路径语义上已收口过——跳过）
        await comp.handle_stream_frame("oc_sd_d", "降级前", finalize=False)
        st_d = comp._states["oc_sd_d"]
        st_d.degraded = True
        st_d.finalized = True
        # E：无卡占位（无远端卡可收口）
        comp._states["oc_sd_e"] = CardTurnState(chat_id="oc_sd_e")

        with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
            sealed = await comp.seal_all_on_shutdown()

        assert sealed == 2
        assert client.close_calls == close_before + 2, "只有 A/B 两张活卡被关流式"
        assert st_a.finalized and st_a.degraded, "收口后必须置终态旗标停止消费"
        assert st_b.finalized and st_b.degraded
        assert "oc_sd_a" not in comp._pause_timers, "暂停定时器必须连带取消"
        assert st_c.degraded is False and st_d.finalized
        assert any(
            "shutdown seal closed 2 live card(s)" in r.message for r in caplog.records
        ), "停机取证日志行缺失"

    async def test_quiescent_composer_returns_zero(self, caplog):
        """无任何状态（含空注册表）：返回 0 且日志行照发（停机取证一致性）."""
        comp = _make_composer(_BaseFakeClient())
        with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
            assert await comp.seal_all_on_shutdown() == 0
        assert any(
            "shutdown seal closed 0 live card(s)" in r.message for r in caplog.records
        )

    def test_adapter_disconnect_seals_live_cards(self) -> None:
        """adapter.disconnect（基类停机面）后活跃卡收到 close_streaming.

        断言链：基类 disconnect 被调用（super 契约）→ composer 活跃卡收口
        （mock 收到 cardkit_close_streaming）→ 状态置终态 → 二次停机幂等。
        """
        script = r"""
import asyncio, importlib.util, os, sys, types

sys.path.insert(0, __REPO_ROOT__)

# 预置 builtin 替身：模拟 hermes 进程内「plugins.* 已可导入」的时序。
# 基类 disconnect 即生产 '[Feishu] Disconnected' 的停机面（gateway 停机序列
# 逐 adapter await——「✓ feishu-streaming disconnected」）。
class StubBase:
    def __init__(self, config):
        self.config = config
    async def disconnect(self):
        self.disconnect_calls = getattr(self, "disconnect_calls", 0) + 1

_stub_pkg = types.ModuleType("plugins")
_stub_platforms = types.ModuleType("plugins.platforms")
_stub_feishu = types.ModuleType("plugins.platforms.feishu")
_stub_adapter = types.ModuleType("plugins.platforms.feishu.adapter")
_stub_adapter.FeishuAdapter = StubBase
_stub_pkg.platforms = _stub_platforms
_stub_platforms.feishu = _stub_feishu
_stub_feishu.adapter = _stub_adapter
for name, mod in [("plugins", _stub_pkg), ("plugins.platforms", _stub_platforms),
                  ("plugins.platforms.feishu", _stub_feishu),
                  ("plugins.platforms.feishu.adapter", _stub_adapter)]:
    sys.modules[name] = mod

_spec_pkg = importlib.util.spec_from_file_location(
    "hermes_lark_streaming", os.path.join(__REPO_ROOT__, "__init__.py"),
    submodule_search_locations=[__REPO_ROOT__])
_pkg = importlib.util.module_from_spec(_spec_pkg)
_pkg.__package__ = "hermes_lark_streaming"
_pkg.__path__ = [__REPO_ROOT__]
sys.modules["hermes_lark_streaming"] = _pkg
_spec_pkg.loader.exec_module(_pkg)

import hermes_lark_streaming.plugin.adapter as A
from hermes_lark_streaming.cardkit.cards import build_streaming_card_v2
from hermes_lark_streaming.composer.engine import FrameComposer

_spec = importlib.util.spec_from_file_location(
    "mock_feishu", os.path.join(__REPO_ROOT__, "tests", "e2e", "mock_feishu.py"))
mock_mod = importlib.util.module_from_spec(_spec)
sys.modules["mock_feishu"] = mock_mod
_spec.loader.exec_module(mock_mod)


async def main():
    from hermes_lark_streaming.composer.classify import classify_send

    mock = mock_mod.MockFeishuServer()
    # 真卡片构建器：建卡 seed 需要 answer_content 元素骨架（空 dict 会 300313）
    comp = FrameComposer(
        get_client=lambda: mock,
        build_card=lambda **kw: build_streaming_card_v2(**kw),
        build_final_card=lambda **kw: {},
        config=None,
        clarify_suspend_sec=0.0,
    )
    inst = A.FeishuStreamingAdapter.__new__(A.FeishuStreamingAdapter)
    inst._composer = comp
    inst.disconnect_calls = 0

    # 经真实建卡路径播种活跃卡（mock：cardkit_create + send_card_by_id_to_chat）
    meta = {"expect_edits": True}
    res = await comp.handle_send("oc_sd", "hello", None, meta, classify_send("hello", meta))
    assert res is not None and res.success, res
    st = comp.state_by_message(res.message_id)
    assert st is not None and st.card_id, "活跃卡预期存在"

    await inst.disconnect()  # 网关停机面

    # 基类断连先行（super 契约）
    assert inst.disconnect_calls == 1, inst.disconnect_calls
    # 活跃卡被优雅收口：mock 收到关流式调用
    closes = [c for c in mock.call_log if c["op"] == "cardkit_close_streaming"]
    assert len(closes) == 1, closes
    assert st.degraded and st.finalized, (st.degraded, st.finalized)

    # 幂等：二次停机不再重复收口（状态均已终态）
    await inst.disconnect()
    closes = [c for c in mock.call_log if c["op"] == "cardkit_close_streaming"]
    assert len(closes) == 1, closes
    assert inst.disconnect_calls == 2

asyncio.run(main())
print("SHUTDOWN_SEAL_ADAPTER_OK")
""".replace("__REPO_ROOT__", repr(str(_REPO_ROOT)))
        _run_plugin_script(script, "SHUTDOWN_SEAL_ADAPTER_OK")

    def test_adapter_disconnect_survives_seal_failure(self) -> None:
        """seal_all_on_shutdown 意外上抛：停机序列绝不能被收口炸掉."""
        script = r"""
import asyncio, importlib.util, os, sys, types

sys.path.insert(0, __REPO_ROOT__)

class StubBase:
    def __init__(self, config):
        self.config = config
    async def disconnect(self):
        self.disconnect_calls = getattr(self, "disconnect_calls", 0) + 1

_stub_pkg = types.ModuleType("plugins")
_stub_platforms = types.ModuleType("plugins.platforms")
_stub_feishu = types.ModuleType("plugins.platforms.feishu")
_stub_adapter = types.ModuleType("plugins.platforms.feishu.adapter")
_stub_adapter.FeishuAdapter = StubBase
_stub_pkg.platforms = _stub_platforms
_stub_platforms.feishu = _stub_feishu
_stub_feishu.adapter = _stub_adapter
for name, mod in [("plugins", _stub_pkg), ("plugins.platforms", _stub_platforms),
                  ("plugins.platforms.feishu", _stub_feishu),
                  ("plugins.platforms.feishu.adapter", _stub_adapter)]:
    sys.modules[name] = mod

_spec_pkg = importlib.util.spec_from_file_location(
    "hermes_lark_streaming", os.path.join(__REPO_ROOT__, "__init__.py"),
    submodule_search_locations=[__REPO_ROOT__])
_pkg = importlib.util.module_from_spec(_spec_pkg)
_pkg.__package__ = "hermes_lark_streaming"
_pkg.__path__ = [__REPO_ROOT__]
sys.modules["hermes_lark_streaming"] = _pkg
_spec_pkg.loader.exec_module(_pkg)

import hermes_lark_streaming.plugin.adapter as A


class ExplodingComposer:
    async def seal_all_on_shutdown(self):
        raise RuntimeError("seal exploded")


async def main():
    inst = A.FeishuStreamingAdapter.__new__(A.FeishuStreamingAdapter)
    inst._composer = ExplodingComposer()
    inst.disconnect_calls = 0
    await inst.disconnect()  # 不得上抛——停机序列永不因收口失败而中断
    assert inst.disconnect_calls == 1

asyncio.run(main())
print("SHUTDOWN_SEAL_RESILIENT_OK")
""".replace("__REPO_ROOT__", repr(str(_REPO_ROOT)))
        _run_plugin_script(script, "SHUTDOWN_SEAL_RESILIENT_OK")


# ---------------------------------------------------------------------------
# FIX-2：_pause_expire 定稿失败 → degrade + seal（不再吞掉半定稿状态）
# ---------------------------------------------------------------------------


class TestFix2PauseExpireSeals:
    async def test_finalize_failure_degrades_and_seals(self):
        """_finalize 中段抛异常 → 降级旗标 + 远端卡补关流式（跑马灯防线）.

        序列：seed 帧打 stream#1 → finalize 帧进暂停窗口（无 API）→
        _pause_expire → _finalize 的 answer 终值 patch 撞 300309（stream#2
        失败，定稿已被置 finalized、看门狗豁免）→ 修复后的 except 走
        _degrade + _seal_degraded_card → seal 重发终值（stream#3）+ 关流式
        （close#1）。旧行为：close_calls == 0、通道未退役。
        """
        client = _FailOnceStreamClient(fail_on_call=2)
        comp = _make_composer(client, suspend_sec=1.2)
        chat = "oc_fix2"
        await comp.handle_stream_frame(chat, "正文", finalize=False, turn_id="t1")
        await comp.handle_stream_frame(chat, "正文定稿", finalize=True, turn_id="t1")
        st = comp._states[chat]
        assert st.pause_pending and not st.finalized

        await comp._pause_expire(chat)  # 定稿中段失败（fail_on_call=2）

        assert st.degraded, "定稿失败必须退役卡片通道（与帧路径同语义）"
        assert st.finalized, "失败半程的状态保持 finalized（本 turn 不再消费）"
        assert client.close_calls == 1, "必须补一次优雅收口（远端卡不得永挂流式态）"
        assert client.stream_calls == 3, "seed + 失败的终值 patch + seal 重发终值"

    async def test_pause_expire_success_path_untouched(self):
        """正常落定路径不受影响（守卫不误伤）：无降级、单次关流式."""
        client = _BaseFakeClient()
        comp = _make_composer(client, suspend_sec=1.2)
        chat = "oc_fix2ok"
        await comp.handle_stream_frame(chat, "正文", finalize=False, turn_id="t1")
        await comp.handle_stream_frame(chat, "正文定稿", finalize=True, turn_id="t1")
        await comp._pause_expire(chat)
        st = comp._states[chat]
        assert st.finalized and not st.degraded
        assert client.close_calls == 1


# ---------------------------------------------------------------------------
# FIX-3：定稿后迟到的非定稿 edit 短路吸收（300309 误降级根治）
# ---------------------------------------------------------------------------


class TestFix3LaggedEditGuard:
    async def test_lagged_nonfinalize_edit_after_finalize_short_circuits(self):
        """定稿后 60s 冷却窗口内的迟到非定稿帧：成功短路，零卡片 API、零降级."""
        client = _BaseFakeClient()
        comp = _make_composer(client)
        chat = "oc_fix3"
        await comp.handle_stream_frame(chat, "答案", finalize=False)
        st = comp._states[chat]
        mid = st.card_message_id
        await comp.handle_edit(chat, mid, "答案终值", True, _edit_cls("答案终值"))
        assert st.finalized
        stream_before = client.stream_calls
        content_before = client.last_stream_content

        result = await comp.handle_edit(
            chat, mid, "迟到的非定稿帧", False, _edit_cls("迟到的非定稿帧"))

        assert result is not None and result.success, "迟到重复帧按成功吸收"
        assert result.message_id == mid
        assert client.stream_calls == stream_before, "不得对已关流式的卡发任何 API"
        assert client.last_stream_content == content_before
        assert st.answer_text == "答案终值", "终值不被迟到重复帧覆盖"
        assert not st.degraded, "不得触发整通道误降级"

    async def test_live_edit_path_unaffected(self):
        """守卫不误伤活跃回合：定稿前的非定稿 edit 照常流式."""
        client = _BaseFakeClient()
        comp = _make_composer(client)
        chat = "oc_fix3live"
        await comp.handle_stream_frame(chat, "答案", finalize=False)
        st = comp._states[chat]
        stream_before = client.stream_calls
        result = await comp.handle_edit(
            chat, st.card_message_id, "答案更新", False, _edit_cls("答案更新"))
        assert result is not None and result.success
        assert client.stream_calls == stream_before + 1
        assert st.answer_text == "答案更新"
        assert not st.finalized and not st.degraded


# ---------------------------------------------------------------------------
# FIX-4：_by_message 滞留旧键（4a 驱逐 + 4b 身份校验双层）
# ---------------------------------------------------------------------------


class TestFix4StaleMessageKey:
    async def test_new_turn_evicts_old_message_key(self):
        """4a：状态对象复用建新卡时，旧 card_message_id 反查键必须驱逐."""
        client = _BaseFakeClient()
        comp = _make_composer(client)
        chat = "oc_fix4"
        await comp.handle_stream_frame(chat, "第一回合", finalize=False, turn_id="t1")
        st1 = comp._states[chat]
        m1 = st1.card_message_id
        await comp.handle_stream_frame(chat, "第一回合定稿", finalize=True, turn_id="t1")
        # 回合 2：复用状态对象 + 覆盖 card_message_id（既有设计）
        await comp.handle_stream_frame(chat, "", finalize=False, turn_id="t2")
        st2 = comp._states[chat]
        m2 = st2.card_message_id
        assert st2 is st1, "状态对象复用（既有设计，非本轮变更面）"
        assert m2 != m1, "新回合必须拿到新卡消息"
        assert m1 not in comp._by_message, "旧反查键必须被驱逐（FIX-4a）"
        assert comp.state_by_message(m1) is None, "旧卡消息不得再解析到当前活跃状态"
        assert comp.state_by_message(m2) is st2

    def test_adapter_delete_message_identity_check(self) -> None:
        """4b：反查键滞留时删除旧卡消息，活跃状态必须幸存（纵深防御）."""
        script = r"""
import asyncio, importlib.util, os, sys, types

sys.path.insert(0, __REPO_ROOT__)

class StubBase:
    def __init__(self, config):
        self.config = config
    async def delete_message(self, chat_id, message_id):
        self.deleted = getattr(self, "deleted", [])
        self.deleted.append((chat_id, message_id))
        return True

_stub_pkg = types.ModuleType("plugins")
_stub_platforms = types.ModuleType("plugins.platforms")
_stub_feishu = types.ModuleType("plugins.platforms.feishu")
_stub_adapter = types.ModuleType("plugins.platforms.feishu.adapter")
_stub_adapter.FeishuAdapter = StubBase
_stub_pkg.platforms = _stub_platforms
_stub_platforms.feishu = _stub_feishu
_stub_feishu.adapter = _stub_adapter
for name, mod in [("plugins", _stub_pkg), ("plugins.platforms", _stub_platforms),
                  ("plugins.platforms.feishu", _stub_feishu),
                  ("plugins.platforms.feishu.adapter", _stub_adapter)]:
    sys.modules[name] = mod

_spec_pkg = importlib.util.spec_from_file_location(
    "hermes_lark_streaming", os.path.join(__REPO_ROOT__, "__init__.py"),
    submodule_search_locations=[__REPO_ROOT__])
_pkg = importlib.util.module_from_spec(_spec_pkg)
_pkg.__package__ = "hermes_lark_streaming"
_pkg.__path__ = [__REPO_ROOT__]
sys.modules["hermes_lark_streaming"] = _pkg
_spec_pkg.loader.exec_module(_pkg)

import hermes_lark_streaming.plugin.adapter as A
from hermes_lark_streaming.cardkit.cards import build_streaming_card_v2
from hermes_lark_streaming.composer.engine import FrameComposer

_spec = importlib.util.spec_from_file_location(
    "mock_feishu", os.path.join(__REPO_ROOT__, "tests", "e2e", "mock_feishu.py"))
mock_mod = importlib.util.module_from_spec(_spec)
sys.modules["mock_feishu"] = mock_mod
_spec.loader.exec_module(mock_mod)


async def main():
    from hermes_lark_streaming.composer.classify import classify_send

    mock = mock_mod.MockFeishuServer()
    # 真卡片构建器：建卡 seed 需要 answer_content 元素骨架（空 dict 会 300313）
    comp = FrameComposer(
        get_client=lambda: mock,
        build_card=lambda **kw: build_streaming_card_v2(**kw),
        build_final_card=lambda **kw: {},
        config=None,
        clarify_suspend_sec=0.0,
    )
    inst = A.FeishuStreamingAdapter.__new__(A.FeishuStreamingAdapter)
    inst._composer = comp
    inst.deleted = []

    meta = {"expect_edits": True}
    res = await comp.handle_send("oc_dm", "hello", None, meta, classify_send("hello", meta))
    st = comp.state_by_message(res.message_id)
    assert st is not None and st.card_id

    # 滞留旧键（FIX-4a 修复前的历史形态/任意路径漏驱逐）：反查解析到活跃状态
    comp._by_message["om_stale_old_card"] = "oc_dm"

    # 删除旧卡消息：活跃状态必须幸存（远端删除照常执行）
    await inst.delete_message("oc_dm", "om_stale_old_card")
    assert comp._states.get("oc_dm") is st, "活跃状态被旧键误删（FIX-4b 回归）"
    assert inst.deleted == [("oc_dm", "om_stale_old_card")]

    # 身份匹配：删除当前卡消息 → forget 照常生效（原语义保持）
    ok = await inst.delete_message("oc_dm", st.card_message_id)
    assert ok is True
    assert comp._states.get("oc_dm") is None
    assert st.card_message_id not in comp._by_message

asyncio.run(main())
print("DELETE_IDENTITY_OK")
""".replace("__REPO_ROOT__", repr(str(_REPO_ROOT)))
        _run_plugin_script(script, "DELETE_IDENTITY_OK")


# ---------------------------------------------------------------------------
# FIX-6：GC 回收未定稿孤儿前先优雅收口远端卡
# ---------------------------------------------------------------------------


class TestFix6GcSealsOrphans:
    async def test_idle_orphan_sealed_then_evicted(self):
        """idle>TTL 的未定稿孤儿：置终态旗标 → seal 关流式 → 弹出注册表."""
        client = _BaseFakeClient()
        comp = _make_composer(client)
        chat = "oc_fix6"
        await comp.handle_stream_frame(chat, "孤儿正文", finalize=False)
        st = comp._states[chat]
        assert st.card_id
        st.last_activity -= 700.0  # > 默认 card_duration_sec 600

        comp._gc()

        assert comp._states.get(chat) is None, "状态必须照常回收（GC 卫生不回滚）"
        assert st.card_message_id not in comp._by_message
        assert st.degraded and st.finalized, "回收前必须置终态旗标停止消费"
        await _settle()  # fire-and-forget seal 任务跑完
        assert client.close_calls == 1, "远端卡必须先收口再回收（seal 失败才留日志）"

    async def test_finalized_cooldown_eviction_does_not_reseal(self):
        """已定稿 60s 冷却回收保持原样：不再发关流式（定稿时已关）."""
        client = _BaseFakeClient()
        comp = _make_composer(client)
        st = CardTurnState(chat_id="c1", card_id="card_x", card_message_id="om_x")
        st.finalized = True
        st.last_activity = time.monotonic() - 61
        comp._states["c1"] = st
        comp._by_message["om_x"] = "c1"

        comp._gc()

        assert "c1" not in comp._states and "om_x" not in comp._by_message
        assert client.close_calls == 0

    async def test_paused_state_still_exempt(self):
        """暂停卡继续豁免 idle 回收（clarify 等待可达 hermes 硬超时 3600s）."""
        client = _BaseFakeClient()
        comp = _make_composer(client)
        chat = "oc_fix6pause"
        await comp.handle_stream_frame(chat, "", finalize=False)
        await comp.suspend_for_clarify(chat, "q")
        st = comp._states[chat]
        st.last_activity -= 10_000.0

        comp._gc()

        assert comp._states.get(chat) is st, "暂停卡不得被 GC 回收"
        assert client.close_calls == 0


# ---------------------------------------------------------------------------
# FIX-7：看门狗武装/重武装周期走 Config.watchdog_idle_sec
# ---------------------------------------------------------------------------


class TestFix7WatchdogRearmConfig:
    async def test_arm_and_rearm_use_configured_idle(self, monkeypatch):
        """建卡首武装与 due 重武装的 call_later 延迟都取配置值（非模块常量）."""
        cfg = SimpleNamespace(watchdog_idle_sec=90, card_duration_sec=600)
        client = _BaseFakeClient()
        comp = _make_composer(client, config=cfg)
        chat = "oc_fix7"
        loop = asyncio.get_running_loop()
        captured: list[tuple[float, object, tuple]] = []
        original = loop.call_later

        def _spy(delay, callback, *args, **kwargs):
            captured.append((float(delay), callback, args))
            return original(delay, callback, *args, **kwargs)

        monkeypatch.setattr(loop, "call_later", _spy)
        await comp.handle_stream_frame(chat, "正文", finalize=False)  # 建卡 + 首武装
        comp._watchdog_due(chat)  # 回合仍活跃（last_activity 新鲜）→ 重武装
        await asyncio.sleep(0)

        assert _engine._WATCHDOG_IDLE_SEC != 90.0, "前置：配置值须异于模块常量"
        arms = [(d, a) for d, cb, a in captured if cb == comp._watchdog_due]
        assert len(arms) == 2, f"首武装 + 重武装各一次，实得 {arms}"
        assert all(d == 90.0 for d, _ in arms), f"武装周期必须用配置值 90s，实得 {arms}"
        assert all(a == (chat,) for _, a in arms)

    async def test_config_fallback_still_uses_default(self):
        """无配置（config=None）：回退模块常量（既有语义不回归）."""
        client = _BaseFakeClient()
        comp = _make_composer(client)
        assert comp._watchdog_idle_sec() == _engine._WATCHDOG_IDLE_SEC
