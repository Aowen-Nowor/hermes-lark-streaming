"""v2.0.23：真飞书 E2E 用例现代化 —— 防回归锁定.

背景（用户验收反馈）：
1. e2e_v2.py 版本水印硬编码（v2.0.0/v2.0.5/v2.0.13）散落多场景 —— 用例观感
   老旧；本轮改为 plugin.yaml 单一事实源动态插值（_V）。
2. reply 引用（v2.0.22）只存在于一次性探针 s170，未进正式 E2E 场景表。
3. full 场景 progress 帧全是带头行，未覆盖 v2.0.22 裸围栏块 → 独立步骤行
   归属（Task 29 用户 Terminal 困惑的修复面）。
4. 锚点消息（s170「这条消息将被卡片引用」）文案不自解释，群内观感困惑；
   reply 场景现复用 connect 锚点（连跑零噪音）+ 自解释文案 + 私聊双验证。

本文件不跑真飞书（真飞书验收走 scripts/e2e_v2.py 本体），只锁定脚本结构
防未来回归：场景注册表完整性 / 版本水印动态化 / 裸块帧存在 / reply 三路径。
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = REPO_ROOT / "scripts" / "e2e_v2.py"


def _load_script():
    """加载 scripts/e2e_v2.py 为模块（conftest 已注册包别名，脚本内引导跳过）."""
    spec = importlib.util.spec_from_file_location("e2e_v2_under_test", str(_SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("e2e_v2_under_test", mod)
    spec.loader.exec_module(mod)
    return mod


def test_scenarios_registry_complete() -> None:
    """场景注册表：11 场景含 reply，且每个场景都有可调用实现（分发表不漏）."""
    mod = _load_script()
    expected = (
        "connect", "answer", "progress", "interim", "full", "burst",
        "native", "clarify", "plain", "oneshot", "reply",
    )
    assert mod.SCENARIOS == expected
    for name in expected:
        fn = getattr(mod, f"scenario_{name}", None)
        assert callable(fn), f"场景 {name} 缺少 scenario_{name} 实现"


def test_version_watermark_dynamic() -> None:
    """版本水印动态化：运行时文案经 _V 插值，旧硬编码水印（v2.0.x）归零.

    发往飞书的四处水印（connect/answer/full/native + argparse 描述）必须
    引用 _V；源码不再出现任何 'v2.0.<n> 真飞书' / '-scenario E2E' 字面量。
    docstring 中的历史版本号引用（如 'v2.0.11 per-chat 串行化'）是变更
    说明不是水印，不在此断言范围。
    """
    src = _SCRIPT.read_text(encoding="utf-8")
    # ① 旧硬编码水印清零（docstring 标题与正文四处运行时文案）
    for legacy in (
        '"""V2.0.0 真飞书 E2E 验收脚本',
        '"**V2.0.0 真飞书 E2E**"',
        "v2.0.5 full-scenario E2E",
        "v2.0.13 native-scenario E2E",
        '"feishu-streaming v2.0.0 E2E · connect',
    ):
        assert legacy not in src, f"旧版本水印残留: {legacy!r}"
    # ② 运行时文案全部经 _V 插值
    assert '_V = getattr(sys.modules["hermes_lark_streaming"], "__version__"' in src
    assert src.count('v{_V}') >= 4, "动态版本插值点不足（connect/answer/full/native）"
    # ③ _V 确实取自 plugin.yaml（运行时值与 manifest 一致）
    mod = _load_script()
    import re
    manifest = (REPO_ROOT / "plugin.yaml").read_text(encoding="utf-8")
    m = re.search(r'^version:\s*"([^"]+)"', manifest, re.M)
    assert m and mod._V == m.group(1)


def test_connect_registers_anchor_and_reply_reuses() -> None:
    """锚点登记：connect 成功后写 ANCHORS['connect']，reply 场景优先复用."""
    src = _SCRIPT.read_text(encoding="utf-8")
    assert 'ANCHORS["connect"] = msg_id' in src, "connect 场景未登记锚点"
    assert 'ANCHORS.pop("connect", "")' in src, "reply 场景未复用 connect 锚点"
    # 锚点文案自解释（s170「这条消息将被卡片引用」困惑的直接整改）
    assert "锚点内容不进卡片体" in src


def test_full_scenario_has_bare_block_frame() -> None:
    """full 场景覆盖 v2.0.22 裸围栏块帧：独立步骤行归属 + 面板行数断言."""
    mod = _load_script()
    src = inspect.getsource(mod.scenario_full)
    # 裸围栏块帧（hermes 连续 terminal 调用省略 header 的真实产物）
    assert '```\\necho "=== E2E FULL' in src, "full 场景缺裸围栏块帧"
    assert "裸围栏块暂存" in src
    # 面板行数断言随裸块帧同步（3 行首卡 → 4 → 5）
    assert "len(st.status_lines) == 3" in src
    assert "len(st.status_lines) == 4" in src
    assert "len(st.status_lines) == 5" in src


def test_reply_scenario_covers_group_dm_and_fallback() -> None:
    """reply 场景三路径：群聊引用（必验）/ 私聊引用（DM 变量可选）/ 伪锚点降级."""
    mod = _load_script()
    src = inspect.getsource(mod.scenario_reply)
    # 群聊引用（om_ 锚点 → handle_send reply_to）
    assert "群聊引用卡片已发出" in src
    # 私聊双验证走独立环境变量（P2P 会话同为 oc_ id）
    assert 'FEISHU_E2E_DM_CHAT_ID' in src
    assert "私聊引用卡片已发出" in src
    assert "私聊验证跳过" in src  # 未配置时 SKIP 不计失败
    # 降级红线（伪锚点 → 直发内容不丢）；且 ⑤ 前必须清 composer 状态——
    # 否则 send 帧并入 ② 的活跃卡，根本走不到 reply→fallback 分支
    # （首轮真飞书实测教训：未清状态时 ⑤ 返回 ② 的 message_id，路径落空）
    assert "伪锚点降级直发成功" in src
    clear_at = src.find("composer.clear()")
    fallback_at = src.find("om_e2e_nonexistent_anchor")
    assert 0 <= clear_at < fallback_at, "伪锚点用例前未清 composer 状态（会并入活跃卡而非降级直发）"
    # 用 composer.handle_send 真实入口（reply_to 位置参数与 adapter 同款）
    assert "handle_send(" in src
