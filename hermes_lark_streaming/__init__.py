"""hermes-lark-streaming — Feishu/Lark CardKit v2.0 streaming cards for Hermes Agent."""

__version__ = "0.8.6"

from .plugin import register

__all__ = ["register", "__version__"]
