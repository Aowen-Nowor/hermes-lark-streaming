"""Hermes compatibility adapter — isolates all Hermes internal interface access.

This is the ONLY file that should import from Hermes internals.
When Hermes upgrades, only this file needs to be updated.

Version detection (Task 3.3) allows the adapter to select the correct
import strategy for different Hermes versions.
"""

from __future__ import annotations
import importlib
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any, Optional

_logger = logging.getLogger("hermes_lark_streaming")


class HermesCompat:
    """Encapsulates all Hermes internal module access.
    
    Usage:
        compat = HermesCompat()
        if compat.gateway_runner_class:
            # patch GatewayRunner methods
        if compat.feishu_adapter_class:
            # patch FeishuAdapter methods
    """
    
    def __init__(self):
        self._detect_version()
        self._resolve_modules()
    
    def _detect_version(self) -> None:
        """Detect Hermes version from various sources."""
        self.hermes_version: str = "unknown"
        
        # Try importlib.metadata
        try:
            from importlib.metadata import version
            self.hermes_version = version("hermes-agent")
        except Exception:
            pass
        
        # Try __version__ attribute
        if self.hermes_version == "unknown":
            try:
                import hermes_cli
                self.hermes_version = getattr(hermes_cli, "__version__", "unknown")
            except Exception:
                pass
        
        _logger.info("HLS: Hermes version detected: %s", self.hermes_version)
    
    def _resolve_modules(self) -> None:
        """Resolve all Hermes internal modules, recording what's available."""
        self.gateway_runner_class: Any | None = None
        self.aiagent_class: Any | None = None
        self.feishu_adapter_class: Any | None = None
        self.cron_scheduler_module: Any | None = None
        self.conversation_loop_module: Any | None = None
        self.conversation_loop_func: Any | None = None
        self.run_agent_module: Any | None = None
        
        # GatewayRunner
        try:
            from gateway.run import GatewayRunner
            self.gateway_runner_class = GatewayRunner
        except (ImportError, AttributeError):
            _logger.debug("HLS: GatewayRunner not available yet")
        
        # AIAgent
        try:
            from run_agent import AIAgent
            self.aiagent_class = AIAgent
            self.run_agent_module = sys.modules.get("run_agent")
        except (ImportError, AttributeError):
            _logger.debug("HLS: AIAgent not available yet")
        
        # FeishuAdapter — 抽取到 _resolve_feishu_adapter()，
        # 便于 resolve_feishu_adapter_class_fresh() 复用（v1.4.0: fix deferred loading patch miss）
        self.feishu_adapter_class = self._resolve_feishu_adapter()
        
        # Cron scheduler
        for mod_name in ("cron.scheduler", "gateway.cron.scheduler"):
            try:
                mod = importlib.import_module(mod_name)
                if hasattr(mod, "_deliver_result"):
                    self.cron_scheduler_module = mod
                    break
            except ImportError:
                continue
        
        # Conversation loop (with namespace collision workaround)
        self._resolve_conversation_loop()
    
    def _resolve_feishu_adapter(self) -> Any | None:
        """Resolve FeishuAdapter class through the gateway's namespace.
        
        CRITICAL: The gateway loads feishu adapter via hermes_plugins namespace
        (importlib creates a separate module object). Patching gateway.platforms.feishu
        won't affect the gateway's instance. We MUST find the class through the
        same namespace the gateway uses.
        
        Verified on Hermes v0.17.0 (2026-06-24):
          - gateway.platforms.feishu → FAILED (No module named)
          - hermes_plugins.feishu_platform.adapter → OK (gateway runtime creates this)
          - plugins.platforms.feishu.adapter → OK (source path, always available)
        The gateway's adapter instance is created from hermes_plugins.feishu_platform.adapter,
        so we must patch THAT class, not a different import path.
        
        v1.4.0: 抽取为独立方法，便于 resolve_feishu_adapter_class_fresh() 复用以
        在 deferred loading 完成后重新解析真身 class（hermes v0.17.0+ bundled
        platform deferred loading 修复，详见 patching/__init__.py 的
        _schedule_direct_patch 注释）。
        """
        # 顺序很关键：真身（hermes_plugins.feishu_platform.adapter）优先，确保
        # 如果 deferred loader 已触发，我们能拿到 gateway 实际使用的 class object。
        _feishu_import_paths = [
            "hermes_plugins.feishu_platform.adapter",  # Hermes v0.17+ (gateway runtime 真身)
            "plugins.platforms.feishu.adapter",        # Source path (替身，always available)
            "gateway.platforms.feishu",                # Legacy path (Hermes < v0.17)
        ]
        for _mod_path in _feishu_import_paths:
            try:
                mod = importlib.import_module(_mod_path)
                if hasattr(mod, "FeishuAdapter"):
                    cls = getattr(mod, "FeishuAdapter")
                    _logger.debug("HLS: FeishuAdapter resolved via %s", _mod_path)
                    return cls
            except (ImportError, AttributeError):
                # v1.4.0: 真身尚未加载（hermes v0.17.0+ bundled platform
                # deferred loading），fallback 到替身；将在 _schedule_direct_patch
                # 延迟重打阶段拿到真身后重新 patch（详见 patching/__init__.py）
                if _mod_path == "hermes_plugins.feishu_platform.adapter":
                    _logger.debug(
                        "HLS: %s not yet loaded (deferred loading), "
                        "trying fallback paths; will re-patch in deferred stage",
                        _mod_path,
                    )
                continue
        _logger.debug("HLS: FeishuAdapter not available via any import path")
        return None
    
    def resolve_feishu_adapter_class_fresh(self) -> Any | None:
        """Re-resolve FeishuAdapter class without reusing cached state.
        
        v1.4.0: 新增方法，供 _schedule_direct_patch 延迟重打阶段调用。
        每次 invoke 都重新跑 _resolve_feishu_adapter 解析逻辑（不复用
        self.feishu_adapter_class 缓存），返回当前 sys.modules 里能拿到的
        最新 class。
        
        用途：hermes v0.17.0+ bundled platform deferred loading 场景下，
        apply_patches() 启动早期只能拿到替身（plugins.platforms.feishu.adapter），
        2s/8s 后 deferred loader 触发真身（hermes_plugins.feishu_platform.adapter）
        加载进 sys.modules，此时调用本方法可拿到真身 class 重新 patch。
        """
        return self._resolve_feishu_adapter()
    
    def _resolve_conversation_loop(self) -> None:
        """Resolve agent.conversation_loop, handling Apple Silicon namespace collision."""
        # Strategy 1: sys.modules cache
        cl_mod = sys.modules.get("agent.conversation_loop")
        if cl_mod is not None:
            func = getattr(cl_mod, "run_conversation", None)
            if func is not None:
                self.conversation_loop_module = cl_mod
                self.conversation_loop_func = func
                _logger.debug("HLS: conversation_loop resolved via sys.modules")
                return
        
        # Strategy 2: Anchor-based discovery
        for anchor_name in ("gateway.run", "run_agent"):
            anchor = sys.modules.get(anchor_name)
            if anchor is None:
                try:
                    anchor = importlib.import_module(anchor_name)
                except ImportError:
                    continue
            anchor_file = getattr(anchor, "__file__", None)
            if not anchor_file:
                continue
            repo_root = Path(anchor_file).resolve().parent
            if anchor_name == "gateway.run":
                repo_root = repo_root.parent
            cl_file = repo_root / "agent" / "conversation_loop.py"
            if not cl_file.is_file():
                continue
            spec = importlib.util.spec_from_file_location("agent.conversation_loop", str(cl_file))
            if spec is None or spec.loader is None:
                continue
            try:
                mod = importlib.util.module_from_spec(spec)
                sys.modules["agent.conversation_loop"] = mod
                spec.loader.exec_module(mod)
                func = getattr(mod, "run_conversation", None)
                if func is not None:
                    self.conversation_loop_module = mod
                    self.conversation_loop_func = func
                    _logger.debug("HLS: conversation_loop resolved via anchor %s", anchor_name)
                    return
            except Exception as e:
                _logger.debug("HLS: anchor-based load failed: %s", e)
        
        # Strategy 3: Standard import
        try:
            from agent.conversation_loop import run_conversation as _func
            import agent.conversation_loop as _mod
            self.conversation_loop_module = _mod
            self.conversation_loop_func = _func
        except (ImportError, AttributeError):
            pass
    
    @property
    def has_gateway_runner(self) -> bool:
        return self.gateway_runner_class is not None
    
    @property
    def has_aiagent(self) -> bool:
        return self.aiagent_class is not None
    
    @property
    def has_feishu_adapter(self) -> bool:
        return self.feishu_adapter_class is not None
    
    @property
    def has_cron_scheduler(self) -> bool:
        return self.cron_scheduler_module is not None
    
    @property
    def has_conversation_loop(self) -> bool:
        return self.conversation_loop_func is not None
    
    def get_layout_report(self) -> dict[str, bool]:
        """Return a dict of what's available — for doctor command and logging."""
        return {
            "has_gateway_runner": self.has_gateway_runner,
            "has_aiagent": self.has_aiagent,
            "has_feishu_adapter": self.has_feishu_adapter,
            "has_cron_scheduler": self.has_cron_scheduler,
            "has_conversation_loop": self.has_conversation_loop,
            "hermes_version": self.hermes_version,
        }
