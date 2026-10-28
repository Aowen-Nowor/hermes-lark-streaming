"""FrameComposer — V2 帧重组引擎（纯 platform，零钩子）.

模块：
- classify: 出站帧分类（metadata / progress 模板识别）
- engine:   CardTurnState 状态机 + CardKit 驱动
- finalize: 定稿/折叠/收尾
"""

from .classify import (
    CH_FINAL,
    CH_INTERIM,
    CH_PLAIN,
    CH_PROGRESS,
    CH_STREAM,
    ClassifyResult,
    classify_edit,
    classify_send,
)
from .engine import CardTurnState, FrameComposer

__all__ = [
    "CH_FINAL",
    "CH_INTERIM",
    "CH_PLAIN",
    "CH_PROGRESS",
    "CH_STREAM",
    "CardTurnState",
    "ClassifyResult",
    "FrameComposer",
    "classify_edit",
    "classify_send",
]
