"""Compatibility accessors for Hermes APIs without a public plugin seam.

Keep private Hermes imports in this module. Runtime callers retain their original
failure handling; this module does not add delivery fallbacks or alter semantics.
"""

from __future__ import annotations

from typing import Any, Callable


def resolve_gateway_model() -> Any:
    """Return Hermes' current gateway model resolver result."""
    from gateway.run import _resolve_gateway_model

    return _resolve_gateway_model()


def build_session_key(source: Any) -> Any:
    """Build a session key using the Hermes session implementation."""
    from gateway.session import build_session_key as _build_session_key

    return _build_session_key(source)


def env_cred_class() -> type[Any]:
    """Return Hermes' environment-to-platform credential bridge class."""
    from gateway.platform_env import EnvCred

    return EnvCred


def safe_schedule_threadsafe(
    coroutine: Any,
    loop: Any,
    **kwargs: Any,
) -> Any:
    """Schedule a coroutine through Hermes' thread-safe async helper."""
    from agent.async_utils import safe_schedule_threadsafe as _safe_schedule_threadsafe

    return _safe_schedule_threadsafe(coroutine, loop, **kwargs)
