"""V2 生产回归：_rebuild_class_with_real_base 必须保持类对象身份.

背景（2026-10-10 云服务器网关实测暴露）：旧实现用 ``type()`` 重建类并借用原类
的方法函数对象，函数的 ``__class__`` cell 仍指向旧类（``_MissingBuiltin`` 基类
版本）→ 首次实例化 ``super().__init__()`` 抛
``TypeError: obj ... is not an instance or subtype of type``。

单元测试环境真实基类在导入期即绑定、rebuild 分支从不执行，因此该缺陷无法被
常规套件捕获——本测试在**子进程**中强制走 rebuild 分支（导入期 hermes 不在，
类绑定占位基类），断言：
1. 类对象身份不变（旧实现会创建第二个同名类对象 → 回归即失败）
2. ``__bases__`` 已切到真实基类
3. 方法函数的 ``__class__`` cell 指向同一个类对象（零参 super 前提）
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

_SCRIPT = r"""
import importlib.util, os, sys, types

sys.path.insert(0, __REPO_ROOT__)

# 包目录名含连字符（hermes-lark-streaming），不可直接 import —— 与 conftest 同款别名引导
_spec = importlib.util.spec_from_file_location(
    "hermes_lark_streaming", os.path.join(__REPO_ROOT__, "__init__.py"),
    submodule_search_locations=[__REPO_ROOT__])
_pkg = importlib.util.module_from_spec(_spec)
_pkg.__package__ = "hermes_lark_streaming"
_pkg.__path__ = [__REPO_ROOT__]
sys.modules["hermes_lark_streaming"] = _pkg
_spec.loader.exec_module(_pkg)

import hermes_lark_streaming.adapter as A

# 1) 导入期状态：类必须绑定占位基类（hermes 不在 → 生产网关同款前提）
assert A.FeishuStreamingAdapter.__bases__[0] is A._MissingBuiltin, (
    "precondition: class should bind _MissingBuiltin at import time")

before_id = id(A.FeishuStreamingAdapter)

# 2) 注入"真实基类"替身（不经 _import_builtin，模拟 hermes 就绪）
class RealStubBase:
    def __init__(self, config):
        self.config = config

stub = types.ModuleType("stub_builtin")
stub.FeishuAdapter = RealStubBase
A._builtin = stub  # _get_builtin() 的模块级缓存，非 None 即直返

# 3) 执行 rebuild（生产 register() 同款调用）
cls = A._rebuild_class_with_real_base()

# 4) 回归断言
assert id(cls) == before_id, (
    "class object identity changed — type() rebuild path is back")
assert cls is A.FeishuStreamingAdapter
assert cls.__bases__ == (RealStubBase,), f"unexpected bases: {cls.__bases__}"

cells = [c.cell_contents for c in (cls.__init__.__closure__ or [])]
class_cells = [c for c in cells if isinstance(c, type)]
assert class_cells and any(c is cls for c in class_cells), (
    "__class__ cell does not reference the (swapped) class — zero-arg super() "
    f"would raise TypeError at instantiation; cells={class_cells!r}")

print("BASESWAP_OK")
""".replace("__REPO_ROOT__", repr(str(_REPO_ROOT)))


def test_rebuild_with_real_base_preserves_class_identity() -> None:
    """rebuild 换基后：类身份/基类/__class__ cell 三者一致（生产 super() 崩溃回归）。"""
    proc = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, (
        f"rebuild regression failed\nstdout: {proc.stdout}\nstderr: {proc.stderr}")
    assert "BASESWAP_OK" in proc.stdout
