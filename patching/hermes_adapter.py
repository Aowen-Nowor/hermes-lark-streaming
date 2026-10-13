"""Hermes compatibility adapter — isolates all Hermes internal interface access."""

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

    v1.8.3 (P0) deadlock fix: ALL module resolution here is non-blocking —
    ``sys.modules`` cache reads only, zero ``importlib.import_module`` /
    import statements. Rationale (issue #18, PR #20, two independent
    production reports): hermes runs each plugin's import + register() on a
    dedicated "plugin-load:" deadline worker thread while the gateway main
    thread may still be executing a module import that holds that module's
    import lock. ANY blocking import here of a module in the main thread's
    in-flight import graph = circular wait = watchdog kill (300s).
    Anything not yet loaded is resolved later by the deferred-targets poller
    in patching/__init__.py (2s interval, 60s budget), which likewise only
    reads sys.modules and never imports.
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
        
        # GatewayRunner — v1.8.3 (P0): sys.modules cache only, never import
        # (deadlock fix, see class docstring). gateway.run is ALWAYS in
        # sys.modules by the time the gateway needs GatewayRunner — the
        # deferred-targets poller re-reads it 2s later if we return None.
        _gw_mod = sys.modules.get("gateway.run")
        if _gw_mod is not None:
            _cls = getattr(_gw_mod, "GatewayRunner", None)
            if _cls is not None:
                self.gateway_runner_class = _cls
        if self.gateway_runner_class is None:
            _logger.debug("HLS: GatewayRunner not available yet")

        # AIAgent — v1.8.3 (P0): sys.modules cache only, never import.
        # hermes imports run_agent LAZILY (first agent turn), so a None here
        # is normal at register time — the deferred poller re-checks.
        _ra_mod = sys.modules.get("run_agent")
        if _ra_mod is not None:
            _cls = getattr(_ra_mod, "AIAgent", None)
            if _cls is not None:
                self.aiagent_class = _cls
                self.run_agent_module = _ra_mod
        if self.aiagent_class is None:
            _logger.debug("HLS: AIAgent not available yet")
        
        # FeishuAdapter — 抽取到 _resolve_feishu_adapter()，
        # 便于 resolve_feishu_adapter_class_fresh() 复用（v1.4.0: fix deferred loading patch miss）
        self.feishu_adapter_class = self._resolve_feishu_adapter()
        
        # Cron scheduler — v1.8.3 (P0): sys.modules cache only, never import.
        for _mod_name in ("cron.scheduler", "gateway.cron.scheduler"):
            _mod = sys.modules.get(_mod_name)
            if _mod is not None and hasattr(_mod, "_deliver_result"):
                self.cron_scheduler_module = _mod
                break
        
        # Conversation loop (with namespace collision workaround)
        self._resolve_conversation_loop()
    
    def _resolve_feishu_adapter(self) -> Any | None:
        """Resolve FeishuAdapter class through the gateway's namespace."""
        # 顺序很关键：真身（hermes_plugins.feishu_platform.adapter）优先，确保
        # 如果 deferred loader 已触发，我们能拿到 gateway 实际使用的 class object。
        _feishu_import_paths = [
            "hermes_plugins.feishu_platform.adapter",  # Hermes v0.17+ (gateway runtime 真身)
            "plugins.platforms.feishu.adapter",        # Source path (替身，always available)
            "gateway.platforms.feishu",                # Legacy path (Hermes < v0.17)
        ]
        # v1.8.3 (P0): sys.modules cache only, never import (deadlock fix).
        # The gateway imports the real adapter (hermes_plugins.feishu_platform.adapter)
        # when it builds the platform — until then the create_adapter hook plus
        # the deferred-targets poller cover patching.
        for _mod_path in _feishu_import_paths:
            mod = sys.modules.get(_mod_path)
            if mod is None:
                continue  # not loaded yet — deferred poller will retry
            cls = getattr(mod, "FeishuAdapter", None)
            if cls is not None:
                _logger.debug("HLS: FeishuAdapter resolved via %s", _mod_path)
                return cls
        _logger.debug("HLS: FeishuAdapter not available via any import path")
        return None
    
    def resolve_feishu_adapter_class_fresh(self) -> Any | None:
        """Re-resolve FeishuAdapter class without reusing cached state."""
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
        # v1.8.3 (P0): anchors must ALREADY be loaded — importing them here can
        # deadlock against the gateway main thread's in-flight import (issue #18).
        # Unloaded anchors are skipped; the deferred-targets poller re-runs this
        # whole resolution once startup completes.
        for anchor_name in ("gateway.run", "run_agent"):
            anchor = sys.modules.get(anchor_name)
            if anchor is None:
                continue  # non-blocking: skip unloaded anchors
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
        
        # Strategy 3 — v1.8.3 (P0): sys.modules cache only, never import
        # (deadlock fix, same rationale as Strategy 2).
        _mod = sys.modules.get("agent.conversation_loop")
        if _mod is not None:
            _func = getattr(_mod, "run_conversation", None)
            if _func is not None:
                self.conversation_loop_module = _mod
                self.conversation_loop_func = _func
    
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
