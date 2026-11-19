"""Small compatibility seams for Hermes private runtime APIs."""

from .hermes_api import (
    build_session_key,
    env_cred_class,
    resolve_gateway_model,
    safe_schedule_threadsafe,
)

__all__ = [
    "build_session_key",
    "env_cred_class",
    "resolve_gateway_model",
    "safe_schedule_threadsafe",
]
