"""V2 生产回归：适配器类必须在导入期绑定真实基类（零参 super 身份一致）.

背景（2026-10-10 云服务器网关实测暴露，两轮事故）：
① 旧实现惰性到 register() 才用 ``type()`` 换基——借用的方法函数对象带旧类
   ``__class__`` cell → 首次实例化 ``super()`` 抛
   ``TypeError: obj ... is not an instance or subtype of type``；
② 换 ``__bases__`` 原地赋值——CPython 3.12+ 直接
   ``TypeError: __bases__ assignment: ... deallocator differs`` → register()
   崩溃 → 插件加载失败 → 网关零平台。

唯一稳妥实现：模块导入期即 try 绑定真实基类（hermes 进程内此刻必可导入），
类身份唯一、cell 一致、rebuild 退化为 no-op。本测试在**子进程**中预置
builtin 替身（模拟 hermes 进程内导入时序），断言：
1. 导入期类即绑定真实基类（而非占位类）
2. rebuild 为 no-op：类对象身份不变
3. 方法 ``__class__`` cell 指向同一个类（零参 super 前提）
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

_SCRIPT = r"""
import importlib.util, os, sys, types

sys.path.insert(0, __REPO_ROOT__)

# 预置 builtin 替身：模拟 hermes 进程内「plugins.* 已可导入」的时序
# （网关/CLI/插件加载器内 import 我们的模块时，hermes 自身模块树就在 sys.path）
class RealStubBase:
    def __init__(self, config):
        self.config = config

_stub_pkg = types.ModuleType("plugins")
_stub_platforms = types.ModuleType("plugins.platforms")
_stub_feishu = types.ModuleType("plugins.platforms.feishu")
_stub_adapter = types.ModuleType("plugins.platforms.feishu.adapter")
_stub_adapter.FeishuAdapter = RealStubBase
_stub_pkg.platforms = _stub_platforms
_stub_platforms.feishu = _stub_feishu
_stub_feishu.adapter = _stub_adapter
for name, mod in [("plugins", _stub_pkg), ("plugins.platforms", _stub_platforms),
                  ("plugins.platforms.feishu", _stub_feishu),
                  ("plugins.platforms.feishu.adapter", _stub_adapter)]:
    sys.modules[name] = mod

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

# 1) 导入期绑定：类基座必须已是真实基类（生产主路径）
assert A.FeishuStreamingAdapter.__bases__ == (RealStubBase,), (
    f"import-time binding failed, bases={A.FeishuStreamingAdapter.__bases__!r}")

before_id = id(A.FeishuStreamingAdapter)

# 2) rebuild 必须是 no-op（真实基类已绑定，不再换基/重建）
cls = A._rebuild_class_with_real_base()
assert id(cls) == before_id, "rebuild should be a no-op after import-time binding"
assert cls is A.FeishuStreamingAdapter

# 3) __class__ cell 指向同一个类对象（零参 super 前提）
cells = [c.cell_contents for c in (cls.__init__.__closure__ or [])]
class_cells = [c for c in cells if isinstance(c, type)]
assert class_cells and any(c is cls for c in class_cells), (
    "__class__ cell does not reference the class — zero-arg super() would "
    f"raise TypeError at instantiation; cells={class_cells!r}")

print("EAGERBIND_OK")
""".replace("__REPO_ROOT__", repr(str(_REPO_ROOT)))


def test_import_time_base_binding_preserves_class_identity() -> None:
    """导入期绑定真实基类 + rebuild no-op + cell 一致（生产 super()/注册双事故回归）。"""
    proc = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, (
        f"eager-binding regression failed\nstdout: {proc.stdout}\nstderr: {proc.stderr}")
    assert "EAGERBIND_OK" in proc.stdout
