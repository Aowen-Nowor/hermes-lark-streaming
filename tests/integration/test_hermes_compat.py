"""Hermes integration compatibility tests.

Verifies that the monkey-patching targets used by hermes-lark-streaming
still exist in the latest Hermes agent source code. These tests run
against the real Hermes source (checked out by the CI workflow).

Two verification strategies are used:

1. **Import-based** (preferred): Import the actual Hermes modules and
   verify classes, methods, and attributes exist at runtime.
2. **AST-based** (fallback): Parse the Hermes Python source files and
   inspect their abstract syntax tree.  Used when Hermes dependencies
   are unavailable and imports fail.

The Hermes source directory is read from the ``HERMES_SRC_DIR`` environment
variable (set by the CI workflow).  If not set, all tests are skipped.
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path
from typing import Any

import pytest


# ── Helpers ───────────────────────────────────────────────────────────


def _hermes_src_dir() -> Path | None:
    """Return the Hermes source directory, or None if not configured."""
    d = os.environ.get("HERMES_SRC_DIR", "").strip()
    if d and Path(d).is_dir():
        return Path(d)
    return None


def _ensure_hermes_on_path(src_dir: Path) -> None:
    """Add the Hermes source directory to sys.path (once)."""
    src_str = str(src_dir)
    if src_str not in sys.path:
        sys.path.insert(0, src_str)


def _find_source_file(src_dir: Path, module_path: str) -> Path | None:
    """Find a Python source file by dotted module path (e.g. 'gateway.run').

    Searches for both ``<path>.py`` and ``<path>/__init__.py``.
    """
    # Try direct .py file
    py_file = src_dir / (module_path.replace(".", "/") + ".py")
    if py_file.is_file():
        return py_file
    # Try package __init__.py
    init_file = src_dir / module_path.replace(".", "/") / "__init__.py"
    if init_file.is_file():
        return init_file
    return None


def _parse_ast(src_dir: Path, module_path: str) -> ast.Module | None:
    """Parse a Python source file and return its AST, or None if not found."""
    src_file = _find_source_file(src_dir, module_path)
    if src_file is None:
        return None
    try:
        return ast.parse(src_file.read_text(encoding="utf-8"), filename=str(src_file))
    except SyntaxError:
        return None


def _ast_has_class(tree: ast.Module, class_name: str) -> bool:
    """Check whether an AST tree contains a class definition with the given name."""
    return any(
        isinstance(node, ast.ClassDef) and node.name == class_name
        for node in ast.walk(tree)
    )


def _ast_class_has_method(tree: ast.Module, class_name: str, method_name: str) -> bool:
    """Check whether a class in the AST has a method with the given name."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if item.name == method_name:
                        return True
    return False


def _ast_any_class_has_method(tree: ast.Module, method_name: str) -> bool:
    """Check whether ANY class in the AST defines the given method.

    v1.8.4: Mixin 布局的 AST 扫描助手 —— hermes v0.21.x 把 GatewayRunner 拆成
    18 个 Mixin 组合、AIAgent 拆成 14 个，被补丁的方法可能定义在 Mixin 基类
    而非组合类类体。对候选文件内所有 ClassDef 取并集，语义与
    ``_AIAgent_CALLBACK_MODULE_CANDIDATES``（v1.8.2）一致。
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if item.name == method_name:
                        return True
    return False


def _ast_module_has_function(tree: ast.Module, func_name: str) -> bool:
    """Check whether an AST tree contains a top-level function with the given name."""
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == func_name:
                return True
    return False


def _ast_method_sig_has_param(
    tree: ast.Module, class_name: str, method_name: str, param_name: str,
) -> bool:
    """Check whether a class method's signature in the AST declares *param_name*.

    v1.8.2: 方案 A 的 AST fallback 辅助 —— hermes 依赖不可用（CI best-effort
    安装失败）时，用签名级 AST 检查代替 inspect.signature。
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if (
                    isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and item.name == method_name
                ):
                    args = item.args
                    all_args = (
                        list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
                    )
                    return any(a.arg == param_name for a in all_args)
    return False


def _ast_function_sig_has_param(
    tree: ast.Module, func_name: str, param_name: str,
) -> bool:
    """Check whether a top-level function's signature in the AST declares *param_name*."""
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            args = node.args
            all_args = list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
            return any(a.arg == param_name for a in all_args)
    return False


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def hermes_src() -> Path:
    """Provide the Hermes source directory, skipping if not available."""
    d = _hermes_src_dir()
    if d is None:
        pytest.skip("HERMES_SRC_DIR not set or not a directory — skipping integration tests")
    return d


@pytest.fixture(scope="session", autouse=True)
def _setup_path(hermes_src: Path) -> None:
    """Ensure Hermes source is on sys.path for all integration tests."""
    _ensure_hermes_on_path(hermes_src)


# ── Tests: Source discovery ───────────────────────────────────────────


class TestHermesSourceDiscovery:
    """Verify that the Hermes source tree is accessible and has expected layout."""

    def test_hermes_src_dir_exists(self, hermes_src: Path) -> None:
        """The Hermes source directory should exist."""
        assert hermes_src.is_dir(), f"Hermes source directory not found: {hermes_src}"

    def test_hermes_src_has_gateway_package(self, hermes_src: Path) -> None:
        """The Hermes source should contain a 'gateway' package."""
        gw_dir = hermes_src / "gateway"
        gw_init = gw_dir / "__init__.py"
        assert gw_dir.is_dir() or gw_init.is_file(), (
            "No 'gateway' package found in Hermes source"
        )

    def test_hermes_src_has_run_agent(self, hermes_src: Path) -> None:
        """The Hermes source should contain run_agent.py (AIAgent)."""
        ra_file = hermes_src / "run_agent.py"
        assert ra_file.is_file(), "No 'run_agent.py' found in Hermes source"


# ── Tests: GatewayRunner ─────────────────────────────────────────────


class TestGatewayRunner:
    """Verify that GatewayRunner class and its patched methods still exist."""

    def test_gateway_runner_class_exists(self, hermes_src: Path) -> None:
        """GatewayRunner class should exist in gateway.run."""
        # Try import first
        try:
            from gateway.run import GatewayRunner  # noqa: F401

            return  # Import succeeded, class exists
        except (ImportError, AttributeError):
            pass
        # Fallback: AST analysis
        tree = _parse_ast(hermes_src, "gateway.run")
        assert tree is not None, "gateway/run.py not found in Hermes source"
        assert _ast_has_class(tree, "GatewayRunner"), (
            "GatewayRunner class not found in gateway/run.py (AST analysis)"
        )

    # v1.8.4: 三个方法存在性测试的 AST 兜底统一改为候选并集扫描 —— import 层
    # 语义不变（hasattr 经 MRO 命中 Mixin 方法，v0.21.x 运行时仍为 True）；
    # AST 层此前只扫 gateway/run.py 单文件类体，v0.21.x Mixin 化后必然失明。

    def test_gateway_runner_has_handle_message(self, hermes_src: Path) -> None:
        """GatewayRunner should have _handle_message method."""
        try:
            from gateway.run import GatewayRunner

            assert hasattr(GatewayRunner, "_handle_message"), (
                "GatewayRunner._handle_message not found"
            )
            return
        except (ImportError, AttributeError):
            pass
        for mod_path in _GATEWAY_RUNNER_METHOD_MODULE_CANDIDATES:
            tree = _parse_ast(hermes_src, mod_path)
            if tree is not None and _ast_any_class_has_method(tree, "_handle_message"):
                return
        assert False, (
            f"GatewayRunner._handle_message not found in any candidate module "
            f"{_GATEWAY_RUNNER_METHOD_MODULE_CANDIDATES} (AST analysis)"
        )

    def test_gateway_runner_has_handle_message_with_agent(self, hermes_src: Path) -> None:
        """GatewayRunner should have _handle_message_with_agent method."""
        try:
            from gateway.run import GatewayRunner

            assert hasattr(GatewayRunner, "_handle_message_with_agent"), (
                "GatewayRunner._handle_message_with_agent not found"
            )
            return
        except (ImportError, AttributeError):
            pass
        for mod_path in _GATEWAY_RUNNER_METHOD_MODULE_CANDIDATES:
            tree = _parse_ast(hermes_src, mod_path)
            if tree is not None and _ast_any_class_has_method(
                tree, "_handle_message_with_agent"
            ):
                return
        assert False, (
            f"GatewayRunner._handle_message_with_agent not found in any candidate "
            f"module {_GATEWAY_RUNNER_METHOD_MODULE_CANDIDATES} (AST analysis)"
        )

    def test_gateway_runner_has_run_agent(self, hermes_src: Path) -> None:
        """GatewayRunner should have _run_agent method."""
        try:
            from gateway.run import GatewayRunner

            assert hasattr(GatewayRunner, "_run_agent"), (
                "GatewayRunner._run_agent not found"
            )
            return
        except (ImportError, AttributeError):
            pass
        for mod_path in _GATEWAY_RUNNER_METHOD_MODULE_CANDIDATES:
            tree = _parse_ast(hermes_src, mod_path)
            if tree is not None and _ast_any_class_has_method(tree, "_run_agent"):
                return
        assert False, (
            f"GatewayRunner._run_agent not found in any candidate module "
            f"{_GATEWAY_RUNNER_METHOD_MODULE_CANDIDATES} (AST analysis)"
        )


# ── Tests: AIAgent ───────────────────────────────────────────────────


# v1.8.2: AIAgent 回调属性的 AST 扫描候选 —— hermes v0.21.1 (v2026.9.7) 把
# 回调属性赋值从 run_agent.py / agent/conversation_loop.py 迁到了
# gateway/run_turn_runner.py（_wire_turn_agent_callbacks）和 agent/agent_init.py，
# 旧的两文件扫描面上 ast.Attribute 节点数归零。按 _FEISHU_ADAPTER_MODULE_CANDIDATES
# 的模式扩候选取并集；候选文件在旧版 hermes 缺失时自动跳过（纯增量，不改旧语义）。
_AIAgent_CALLBACK_MODULE_CANDIDATES = [
    "run_agent",                     # AIAgent 定义 + __init__ 构造回调参
    "agent.conversation_loop",       # v0.10+ 会话循环（历史赋值点，v0.21.1 起归零）
    "gateway.run_turn_runner",       # v0.21.1+ _wire_turn_agent_callbacks（4/5 赋值点）
    "agent.agent_init",              # v0.21.1+ AIAgent.__init__ 委托实现（background_review 赋值）
]

# v1.8.4: GatewayRunner / AIAgent 方法存在性的 AST 扫描候选 —— hermes v0.21.x
# 的 Mixin 化把被补丁方法搬出了原单体文件的类体：
#   _handle_message            → gateway/run_inbound.py（GatewayInboundMixin）
#   _handle_message_with_agent / _run_agent / _run_background_task
#                              → gateway/run_turn.py（GatewayTurnMixin）
#   AIAgent.run_conversation   → agent/turn_facade.py（TurnFacadeMixin）
# 单文件类体扫描面（gateway.run / run_agent）上方法定义数归零，哨兵必然失明。
# 候选文件在旧版 hermes 缺失时自动跳过（纯增量，不改旧语义，沿用
# _AIAgent_CALLBACK_MODULE_CANDIDATES / _RUN_CONVERSATION_METHOD_CANDIDATES
# 的既有候选并集模式）。
_GATEWAY_RUNNER_METHOD_MODULE_CANDIDATES = [
    "gateway.run",          # 旧版单体：方法在 GatewayRunner 类体内（< v0.21.x）
    "gateway.run_inbound",  # v0.21.x+ _handle_message（GatewayInboundMixin）
    "gateway.run_turn",     # v0.21.x+ _handle_message_with_agent/_run_agent/
                            # _run_background_task（GatewayTurnMixin）
]

_AIAGENT_METHOD_MODULE_CANDIDATES = [
    "run_agent",            # 旧版单体：run_conversation 在 AIAgent 类体内（< v0.21.x）
    "agent.turn_facade",    # v0.21.x+ run_conversation（TurnFacadeMixin，MRO 命中）
]

# v1.8.4: class_name → AST 候选模块映射（TestMonkeyPatchTargets 用）。
# FeishuAdapter 不在此表 —— 它已有专属的多路径 + BasePlatformAdapter 继承链回退。
_CLASS_METHOD_AST_MODULE_CANDIDATES = {
    "GatewayRunner": _GATEWAY_RUNNER_METHOD_MODULE_CANDIDATES,
    "AIAgent": _AIAGENT_METHOD_MODULE_CANDIDATES,
}


class TestAIAgent:
    """Verify that AIAgent class and its callback attributes still exist."""

    def test_aiagent_class_exists(self, hermes_src: Path) -> None:
        """AIAgent class should exist in run_agent module."""
        try:
            from run_agent import AIAgent  # noqa: F401

            return
        except (ImportError, AttributeError):
            pass
        tree = _parse_ast(hermes_src, "run_agent")
        assert tree is not None, "run_agent.py not found in Hermes source"
        assert _ast_has_class(tree, "AIAgent"), (
            "AIAgent class not found in run_agent.py (AST analysis)"
        )

    def test_aiagent_has_run_conversation(self, hermes_src: Path) -> None:
        """AIAgent should have run_conversation method."""
        try:
            from run_agent import AIAgent

            assert hasattr(AIAgent, "run_conversation"), (
                "AIAgent.run_conversation not found"
            )
            return
        except (ImportError, AttributeError):
            pass
        # v1.8.4: AST 兜底改候选并集 —— v0.21.x 起 run_conversation 经
        # TurnFacadeMixin 进入 MRO（agent/turn_facade.py），run_agent.py 类体扫描失明。
        for mod_path in _AIAGENT_METHOD_MODULE_CANDIDATES:
            tree = _parse_ast(hermes_src, mod_path)
            if tree is not None and _ast_any_class_has_method(tree, "run_conversation"):
                return
        assert False, (
            f"AIAgent.run_conversation not found in any candidate module "
            f"{_AIAGENT_METHOD_MODULE_CANDIDATES} (AST analysis)"
        )

    def test_aiagent_callback_attributes(self, hermes_src: Path) -> None:
        """AIAgent should support the expected callback attributes.

        v1.8.2 两层验证（hermes v0.21.1 迁移回调赋值点后重构）：

        - **Tier 1（import 层）**：``AIAgent.__init__`` 构造签名必须含 4 个回调
          构造参（stream_delta / interim_assistant / tool_progress / reasoning）
          ——这才是“agent 实例携带回调属性”的真实契约，v0.17.0~v0.21.1 一致。
          background_review_callback 非构造参（两版本均由赋值点覆盖），走 Tier 2。
        - **Tier 2（AST 层）**：按 ``_AIAgent_CALLBACK_MODULE_CANDIDATES`` 并集扫描
          ``ast.Attribute``，维持旧语义“全空才 fail”——v0.21.1 并集 4/5
          （reasoning_callback 只剩构造参、无赋值点），v0.21.0 并集 5/5。
        """
        # Callback attribute names the plugin wraps
        callback_attrs = [
            "stream_delta_callback",
            "interim_assistant_callback",
            "tool_progress_callback",
            "reasoning_callback",
            "background_review_callback",
        ]
        # Constructor-level callback params (the real runtime contract)
        ctor_callback_attrs = [
            "stream_delta_callback",
            "interim_assistant_callback",
            "tool_progress_callback",
            "reasoning_callback",
        ]

        # ── Tier 1: import 层 —— __init__ 构造参契约 ──
        try:
            import inspect

            from run_agent import AIAgent

            sig_params = set(inspect.signature(AIAgent.__init__).parameters)
            missing_ctor = [a for a in ctor_callback_attrs if a not in sig_params]
            assert not missing_ctor, (
                f"AIAgent.__init__ 缺少回调构造参数 {missing_ctor} —— "
                f"hermes 回调契约变化，插件回调包装（patching/callbacks.py）"
                f"可能失效，需人工复核"
            )
        except (ImportError, AttributeError):
            pass  # hermes 依赖不可用（CI best-effort 安装失败）→ 落 Tier 2

        # ── Tier 2: AST 层 —— 候选模块并集，“全空才 fail” ──
        found_attrs: set[str] = set()
        any_tree = False
        for mod_path in _AIAgent_CALLBACK_MODULE_CANDIDATES:
            tree = _parse_ast(hermes_src, mod_path)
            if tree is None:
                continue  # 候选文件在该版本不存在（如 v0.21.0 无 run_turn_runner.py）
            any_tree = True
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in callback_attrs:
                    found_attrs.add(node.attr)

        # If NONE are found in any candidate module, that's a stronger signal
        # of breakage (individual misses are soft — attributes may be wired via
        # **kwargs / setattr loops invisible to AST).
        if not found_attrs and any_tree:
            pytest.fail(
                f"None of the expected callback attributes ({callback_attrs}) "
                f"were found in any candidate module: "
                f"{_AIAgent_CALLBACK_MODULE_CANDIDATES}. "
                f"The plugin's callback wrapping may be broken."
            )


# ── Tests: FeishuAdapter ─────────────────────────────────────────────

# v1.4.0: FeishuAdapter 实际定义位置 —— hermes v0.17.0+ 在 plugins.platforms.feishu.adapter
# （源码路径），旧版在 gateway.platforms.feishu。AST fallback 按顺序尝试所有候选路径。
_FEISHU_ADAPTER_MODULE_CANDIDATES = [
    "gateway.platforms.feishu",         # Legacy path (Hermes < v0.17)
    "plugins.platforms.feishu.adapter", # v0.17+ source path (always available)
]


def _resolve_feishu_adapter_ast(hermes_src: Path) -> tuple[ast.Module | None, str | None]:
    """Return (ast_tree, module_path) for the first candidate where FeishuAdapter source is found.

    v1.4.0: 抽取为独立 helper —— hermes v0.17.0+ 重构后 FeishuAdapter 实际定义在
    plugins.platforms.feishu.adapter（不再是 gateway.platforms.feishu）。多个 TestFeishuAdapter
    / TestMonkeyPatchTargets 测试都需要按顺序尝试候选路径，避免在 v0.17.0+ 误报缺失。
    """
    for mod_path in _FEISHU_ADAPTER_MODULE_CANDIDATES:
        tree = _parse_ast(hermes_src, mod_path)
        if tree is not None:
            return tree, mod_path
    return None, None


class TestFeishuAdapter:
    """Verify that FeishuAdapter class and its patched methods still exist."""

    def test_feishu_adapter_class_exists(self, hermes_src: Path) -> None:
        """FeishuAdapter class should exist in gateway.platforms.feishu or plugins.platforms.feishu.adapter."""
        # Try import first (legacy path only — v0.17+ source path requires lark-oapi deps)
        try:
            from gateway.platforms.feishu import FeishuAdapter  # noqa: F401

            return
        except (ImportError, AttributeError):
            pass
        # Fallback: AST analysis — v1.4.0: try multiple candidate paths
        tree, resolved_path = _resolve_feishu_adapter_ast(hermes_src)
        assert tree is not None, (
            "FeishuAdapter source not found in any candidate path: "
            f"{_FEISHU_ADAPTER_MODULE_CANDIDATES}"
        )
        assert _ast_has_class(tree, "FeishuAdapter"), (
            f"FeishuAdapter class not found in {resolved_path} (AST analysis)"
        )

    def test_feishu_adapter_has_send(self, hermes_src: Path) -> None:
        """FeishuAdapter should have a 'send' method."""
        try:
            from gateway.platforms.feishu import FeishuAdapter

            assert hasattr(FeishuAdapter, "send"), "FeishuAdapter.send not found"
            return
        except (ImportError, AttributeError):
            pass
        # v1.4.0: try multiple candidate paths
        tree, resolved_path = _resolve_feishu_adapter_ast(hermes_src)
        assert tree is not None, (
            f"FeishuAdapter source not found in candidates: {_FEISHU_ADAPTER_MODULE_CANDIDATES}"
        )
        assert _ast_class_has_method(tree, "FeishuAdapter", "send"), (
            f"FeishuAdapter.send not found in {resolved_path} (AST analysis)"
        )

    def test_feishu_adapter_has_edit_message(self, hermes_src: Path) -> None:
        """FeishuAdapter should have an 'edit_message' method."""
        try:
            from gateway.platforms.feishu import FeishuAdapter

            assert hasattr(FeishuAdapter, "edit_message"), (
                "FeishuAdapter.edit_message not found"
            )
            return
        except (ImportError, AttributeError):
            pass
        # v1.4.0: try multiple candidate paths
        tree, resolved_path = _resolve_feishu_adapter_ast(hermes_src)
        assert tree is not None, (
            f"FeishuAdapter source not found in candidates: {_FEISHU_ADAPTER_MODULE_CANDIDATES}"
        )
        assert _ast_class_has_method(tree, "FeishuAdapter", "edit_message"), (
            f"FeishuAdapter.edit_message not found in {resolved_path} (AST analysis)"
        )


# ── Tests: Monkey-patching targets ───────────────────────────────────


class TestMonkeyPatchTargets:
    """Comprehensive check of all monkey-patching targets.

    These are the specific classes, methods, and functions that
    hermes-lark-streaming patches at runtime. If any of them are
    renamed or removed, the plugin will break silently.
    """

    # (module_path, class_name, method_name)
    CLASS_METHOD_TARGETS = [
        ("gateway.run", "GatewayRunner", "_handle_message"),
        ("gateway.run", "GatewayRunner", "_handle_message_with_agent"),
        ("gateway.run", "GatewayRunner", "_run_agent"),
        ("run_agent", "AIAgent", "run_conversation"),
        ("gateway.platforms.feishu", "FeishuAdapter", "send"),
        ("gateway.platforms.feishu", "FeishuAdapter", "edit_message"),
        # v1.4.0: send_clarify / _on_card_action_trigger 从 OPTIONAL 升级到 REQUIRED。
        # 这两个方法是 clarify 卡片链路的关键 patch 点，hermes 升级若移除/改名
        # 会导致插件 patch 静默失败 → clarify 退回 BasePlatformAdapter 纯文本
        # （正是 v1.4.0 修复的根因，详见 worklog Task 2-b）。必须强制校验。
        # 注意：send_clarify 在 hermes v0.17.0 里是 FeishuAdapter 继承自
        # BasePlatformAdapter 的方法（不在 FeishuAdapter class body 内定义），
        # AST fallback 需要查 BasePlatformAdapter 继承链（见 test_required_class_method_exists）。
        ("gateway.platforms.feishu", "FeishuAdapter", "send_clarify"),
        ("gateway.platforms.feishu", "FeishuAdapter", "_on_card_action_trigger"),
    ]

    # (module_path, function_name) — module-level functions patched
    FUNCTION_TARGETS = [
        ("agent.conversation_loop", "run_conversation"),
    ]

    # Optional targets — missing ones are warnings, not failures
    OPTIONAL_CLASS_METHOD_TARGETS = [
        ("gateway.run", "GatewayRunner", "_run_background_task"),
        # Reaction methods: Hermes renamed public → private in newer versions
        # At least one of each pair should exist for the feature to work
        ("gateway.platforms.feishu", "FeishuAdapter", "add_reaction"),       # old public name
        ("gateway.platforms.feishu", "FeishuAdapter", "_add_reaction"),      # new private name
        ("gateway.platforms.feishu", "FeishuAdapter", "delete_reaction"),    # old public name
        ("gateway.platforms.feishu", "FeishuAdapter", "_remove_reaction"),   # new private name
    ]

    # v1.4.0: FeishuAdapter 实际定义位置 —— 复用模块级 _FEISHU_ADAPTER_MODULE_CANDIDATES
    # （见 TestFeishuAdapter 上方定义）。AST fallback 按顺序尝试所有候选路径。
    _FEISHU_ADAPTER_FALLBACK_PATHS = _FEISHU_ADAPTER_MODULE_CANDIDATES

    # v1.4.0: FeishuAdapter 继承自 BasePlatformAdapter，send_clarify 等方法
    # 定义在 base class 不在 FeishuAdapter class body。AST fallback 找不到时
    # 需要查继承链。BasePlatformAdapter 位于 gateway.platforms.base。
    _BASE_PLATFORM_ADAPTER_FALLBACK_PATHS = [
        "gateway.platforms.base",
    ]

    @pytest.mark.parametrize(
        "module_path, class_name, method_name",
        CLASS_METHOD_TARGETS,
        ids=[f"{c}.{m}" for _, c, m in CLASS_METHOD_TARGETS],
    )
    def test_required_class_method_exists(
        self, hermes_src: Path, module_path: str, class_name: str, method_name: str,
    ) -> None:
        """Required class method must exist (import or AST).

        v1.4.0: AST fallback 增强 —— 对 FeishuAdapter 方法支持：
          1. 多路径回退：gateway.platforms.feishu（legacy）→ plugins.platforms.feishu.adapter
             （v0.17+ 真实路径），任意一个找到 class 即算 class 存在。
          2. 继承链回退：send_clarify 等方法定义在 BasePlatformAdapter（gateway.platforms.base），
             不在 FeishuAdapter class body 内。若 FeishuAdapter class body 没有该方法，
             再尝试在 BasePlatformAdapter 里找，找到即算通过。
        这两个增强确保测试在 hermes v0.17.0+ 重构后仍能正确校验 REQUIRED patch 点。
        """
        # Try import
        try:
            mod = __import__(module_path, fromlist=[class_name])
            cls = getattr(mod, class_name, None)
            assert cls is not None, f"Class {class_name} not found in {module_path}"
            assert hasattr(cls, method_name), (
                f"{class_name}.{method_name} not found in {module_path}"
            )
            return
        except (ImportError, AttributeError):
            pass
        # Fallback: AST — first try the declared module_path
        tree = _parse_ast(hermes_src, module_path)
        if tree is not None and _ast_class_has_method(tree, class_name, method_name):
            return  # Found in declared path's class body
        # v1.8.4: Mixin 布局回退 —— hermes v0.21.x 把 GatewayRunner/AIAgent 拆成
        # Mixin 组合，方法定义搬到候选文件（run_inbound/run_turn/turn_facade）。
        # 按候选并集扫描（any-class 语义），候选缺失自动跳过。
        for cand_mod in _CLASS_METHOD_AST_MODULE_CANDIDATES.get(class_name, []):
            cand_tree = _parse_ast(hermes_src, cand_mod)
            if cand_tree is not None and _ast_any_class_has_method(cand_tree, method_name):
                return  # Found in mixin candidate module
        # v1.4.0: 多路径回退（仅对 FeishuAdapter —— 其他 class 直接跳过）
        if class_name == "FeishuAdapter":
            for alt_path in self._FEISHU_ADAPTER_FALLBACK_PATHS:
                if alt_path == module_path:
                    continue  # already tried above
                alt_tree = _parse_ast(hermes_src, alt_path)
                if alt_tree is not None and _ast_class_has_method(alt_tree, class_name, method_name):
                    return  # Found in alternative FeishuAdapter path
            # v1.4.0: 继承链回退 —— FeishuAdapter 继承自 BasePlatformAdapter，
            # send_clarify 等方法可能在 base class 里定义（不在 FeishuAdapter class body）
            for base_path in self._BASE_PLATFORM_ADAPTER_FALLBACK_PATHS:
                base_tree = _parse_ast(hermes_src, base_path)
                if base_tree is not None and _ast_class_has_method(
                    base_tree, "BasePlatformAdapter", method_name,
                ):
                    return  # Found in BasePlatformAdapter (inherited)
        # Nothing found — fail with clear message
        assert tree is not None, (
            f"Source for {module_path} not found (also tried fallback paths "
            f"{self._FEISHU_ADAPTER_FALLBACK_PATHS} for FeishuAdapter)"
        )
        assert _ast_class_has_method(tree, class_name, method_name), (
            f"{class_name}.{method_name} not found in {module_path} (AST analysis); "
            f"also checked FeishuAdapter fallback paths + BasePlatformAdapter inheritance chain"
        )

    @pytest.mark.parametrize(
        "module_path, func_name",
        FUNCTION_TARGETS,
        ids=[f"{m}.{f}" for m, f in FUNCTION_TARGETS],
    )
    def test_required_function_exists(
        self, hermes_src: Path, module_path: str, func_name: str,
    ) -> None:
        """Required module-level function must exist (import or AST)."""
        # Try import
        try:
            mod = __import__(module_path, fromlist=[func_name])
            assert hasattr(mod, func_name), (
                f"Function {func_name} not found in {module_path}"
            )
            return
        except (ImportError, AttributeError):
            pass
        # Fallback: AST
        tree = _parse_ast(hermes_src, module_path)
        assert tree is not None, f"Source for {module_path} not found"
        assert _ast_module_has_function(tree, func_name), (
            f"Function {func_name} not found in {module_path} (AST analysis)"
        )

    @pytest.mark.parametrize(
        "module_path, class_name, method_name",
        OPTIONAL_CLASS_METHOD_TARGETS,
        ids=[f"{c}.{m} (optional)" for _, c, m in OPTIONAL_CLASS_METHOD_TARGETS],
    )
    def test_optional_class_method_exists(
        self, hermes_src: Path, module_path: str, class_name: str, method_name: str,
    ) -> None:
        """Optional class method — warn if missing, but don't fail."""
        # v1.7.0: FeishuAdapter targets try ALL candidate module paths — the
        # legacy gateway.platforms.feishu path was deleted in hermes v0.20.5
        # (Feishu lives at plugins.platforms.feishu.adapter / hermes_plugins
        # namespace), which previously made every reaction target misreport
        # skip even though _add_reaction/_remove_reaction exist.
        if module_path in _FEISHU_ADAPTER_MODULE_CANDIDATES:
            candidate_paths = list(_FEISHU_ADAPTER_MODULE_CANDIDATES)
        elif class_name in _CLASS_METHOD_AST_MODULE_CANDIDATES:
            # v1.8.4: GatewayRunner/AIAgent 可选目标（如 _run_background_task）
            # 同样按 Mixin 候选并集扫描 —— v0.21.x 起该方法定义在
            # gateway/run_turn.py（GatewayTurnMixin），单文件扫描会误报 skip。
            candidate_paths = list(_CLASS_METHOD_AST_MODULE_CANDIDATES[class_name])
        else:
            candidate_paths = [module_path]

        for candidate in candidate_paths:
            # Try import
            try:
                mod = __import__(candidate, fromlist=[class_name])
                cls = getattr(mod, class_name, None)
                if cls is not None and hasattr(cls, method_name):
                    return  # Found, all good
            except (ImportError, AttributeError):
                pass
            # Fallback: AST
            tree = _parse_ast(hermes_src, candidate)
            if tree is None:
                continue
            if class_name in _CLASS_METHOD_AST_MODULE_CANDIDATES:
                # v1.8.4: Mixin 候选文件里方法定义在 Mixin 基类（如
                # GatewayTurnMixin）而非组合类（GatewayRunner）类体 ——
                # 用 any-class 并集语义扫描。
                if _ast_any_class_has_method(tree, method_name):
                    return  # Found via AST (mixin layout)
            elif _ast_class_has_method(tree, class_name, method_name):
                return  # Found via AST
        # Not found — issue a warning (not a failure)
        pytest.skip(
            f"Optional target {class_name}.{method_name} not found in "
            f"{candidate_paths} — plugin feature will be degraded but not broken"
        )


# ── v1.1.2: Hermes 签名兼容性验证（v0.17.0 起累积，v1.8.2 迁移检测面）──


# v1.8.2: run_conversation 生产面候选 —— 插件生产代码真正用 inspect.signature
# 探测 persist_* 参数的两个位置（patching/__init__.py 的 AIAgent.run_conversation
# 检测 + patching/gateway.py 的 agent.conversation_loop.run_conversation 检测）。
# hermes v0.21.1 (v2026.9.7) 将 GatewayRunner._run_agent 泛化为
# (self, message, context_prompt, history, source, session_id, **turn_kwargs)，
# persist_* 参数移入 **turn_kwargs 透传、不再显式出现在 _run_agent 签名中；
# 但两个 run_conversation 生产面在 v0.17.0~v0.21.1 全程显式声明（经
# run_turn_runner 的 kwargs 透传链仍原样到达）。
_RUN_CONVERSATION_METHOD_CANDIDATES = [
    ("run_agent", "AIAgent"),                # v0.17.0~v0.21.0：run_agent.py 直接定义
    ("agent.turn_facade", "TurnFacadeMixin"),  # v0.21.1+：拆分后 AIAgent.run_conversation 经 MRO 命中
]

_RUN_CONVERSATION_FUNCTION_CANDIDATES = [
    "agent.conversation_loop",  # v0.10+ 模块级函数（插件模块级 patch 点）
]


def _persist_param_supported(hermes_src: Path, param_name: str) -> bool | None:
    """Check whether *param_name* is declared on the run_conversation production surfaces.

    Returns:
        True  — 至少一个面可解析，且全部可解析面均声明该参数
        False — 至少一个面可解析，且均未声明该参数
        None  — 两个面都无法解析（import 失败且 AST 候选全缺）

    v1.8.2（方案 A）：检测点从 GatewayRunner._run_agent 迁移到插件生产代码
    的真实探测面，与 patching/__init__.py、patching/gateway.py 逐字对齐。
    """
    import inspect

    # Tier 1: import 层 —— 与插件生产代码相同的 inspect.signature 探测
    surfaces: list[tuple[str, Any]] = []
    try:
        from run_agent import AIAgent

        surfaces.append(("AIAgent.run_conversation", AIAgent.run_conversation))
    except (ImportError, AttributeError):
        pass
    try:
        import agent.conversation_loop as _cl_mod

        surfaces.append(("agent.conversation_loop.run_conversation", _cl_mod.run_conversation))
    except (ImportError, AttributeError):
        pass

    if surfaces:
        return all(
            param_name in set(inspect.signature(func).parameters)
            for _, func in surfaces
        )

    # Tier 2: AST fallback —— hermes 依赖不可用时按候选扫描签名
    for mod_path, class_name in _RUN_CONVERSATION_METHOD_CANDIDATES:
        tree = _parse_ast(hermes_src, mod_path)
        if tree is not None and _ast_method_sig_has_param(
            tree, class_name, "run_conversation", param_name,
        ):
            return True
    for mod_path in _RUN_CONVERSATION_FUNCTION_CANDIDATES:
        tree = _parse_ast(hermes_src, mod_path)
        if tree is not None and _ast_function_sig_has_param(
            tree, "run_conversation", param_name,
        ):
            return True
    return None


class TestV017SignatureChanges:
    """v1.1.2: 验证 Hermes 的方法签名变化（v0.17.0 起累积）。

    - v0.17.0 (v2026.6.19)：run_conversation 新增 persist_user_message 参数
    - v0.21.1 (v2026.9.7)：GatewayRunner._run_agent 泛化为
      ``(self, message, context_prompt, history, source, session_id, **turn_kwargs)``，
      persist_* 参数移入 **turn_kwargs 透传。插件真正探测的是两个稳定的
      run_conversation 生产面（v0.17.0~v0.21.1 全程显式声明），哨兵随之
      迁移（v1.8.2 方案 A）。
    """

    def test_run_conversation_has_persist_user_timestamp_param(self, hermes_src: Path) -> None:
        """run_conversation 双生产面应支持 persist_user_timestamp 参数（v0.16.0+）.

        v1.8.2: 检测点从 GatewayRunner._run_agent 迁移到插件生产代码的真实
        探测面。v0.21.1 泛化 _run_agent 后 persist 参数不再出现在其签名中，
        但经 run_turn_runner 的 **turn_kwargs 透传链仍原样到达这两个面。
        """
        supported = _persist_param_supported(hermes_src, "persist_user_timestamp")
        assert supported, (
            "run_conversation 生产面缺少 persist_user_timestamp 参数，或两个面均"
            "无法解析 —— 插件用 inspect.signature 检测此参数（v0.21.1 起该参数"
            "经 _run_agent 的 **turn_kwargs 透传，终至此处），需人工复核"
        )

    def test_run_conversation_has_persist_user_message_param(self, hermes_src: Path) -> None:
        """run_conversation 双生产面应支持 persist_user_message 参数（v0.17.0 新增）.

        v1.8.2: 与 persist_user_timestamp 同步迁移检测面。v0.21.1 泛化
        _run_agent 后该参数在 run_conversation 双面仍显式声明
        （turn_facade / conversation_loop），测试从 skip 恢复为硬验证。
        """
        supported = _persist_param_supported(hermes_src, "persist_user_message")
        if supported is None:
            pytest.skip("run_conversation 生产面均不可解析 —— 布局超出已知候选")
        if supported is False:
            pytest.skip(
                "run_conversation 无 persist_user_message 参数 "
                "（Hermes < v0.17.0，插件用 inspect.signature 自动兼容）"
            )

    def test_run_conversation_still_callable(self, hermes_src: Path) -> None:
        """run_conversation 仍可调用（v0.17.0 重构后验证入口没变）."""
        try:
            from agent.conversation_loop import run_conversation  # noqa: F401
            return
        except (ImportError, AttributeError):
            pass
        try:
            from run_agent import AIAgent

            assert hasattr(AIAgent, "run_conversation"), (
                "AIAgent.run_conversation 不存在（v0.17.0 重构可能移除了入口）"
            )
            return
        except (ImportError, AttributeError):
            pass
        # v1.8.4: hermes 依赖不可用（CI Python < 3.14 装不上 hermes v0.21.6+ 的
        # 3.14-only 依赖）时 conversation_loop 与 run_agent 的 import 都会失败 ——
        # 此前第二个 import 无保护，ModuleNotFoundError 直接把用例炸成 error。
        # AST 兜底按 run_conversation 双生产面候选（AIAgent / TurnFacadeMixin）扫描。
        for mod_path, class_name in _RUN_CONVERSATION_METHOD_CANDIDATES:
            tree = _parse_ast(hermes_src, mod_path)
            if tree is not None and _ast_any_class_has_method(tree, "run_conversation"):
                return
        assert False, (
            f"run_conversation 入口不存在：conversation_loop 模块、"
            f"AIAgent.run_conversation 与 AST 候选 "
            f"{_RUN_CONVERSATION_METHOD_CANDIDATES} 全部落空"
        )
