"""Reasoning prepend relocation + native composite fallback (edit-based fallback)."""
from __future__ import annotations

from hermes_lark_streaming.composer.engine import _extract_reasoning_block, _split_native_composite


def test_extract_zh_code_block():
    text = "💭 **推理：**\n```\n先查再问，拆任务\n```\n\n正文来了"
    clean, reasoning = _extract_reasoning_block(text)
    assert reasoning == "先查再问，拆任务"
    assert clean == "正文来了"


def test_extract_en_code_block():
    text = "💭 **Reasoning:**\n```\nworking it out\n```\n\nanswer here"
    clean, reasoning = _extract_reasoning_block(text)
    assert reasoning == "working it out"
    assert clean == "answer here"


def test_extract_missing_block_keeps_original():
    text = "正文第一行\n```\ncode\n```\n别的"
    clean, reasoning = _extract_reasoning_block(text)
    assert reasoning is None
    assert clean == text


def test_extract_non_reasoning_head_keeps_original():
    text = "💭 hello\n```\nnot reasoning\n```\n\nanswer"
    clean, reasoning = _extract_reasoning_block(text)
    assert reasoning is None
    assert clean == text


def test_split_fallback_when_strict_guard_fails():
    # 新工具/中文动词变异的尾行：严格语法会回滚为正文，兜底应拆出进面板
    composite = "先回答一半\n\n---\n🤓 询问 · 新工具X\n补充说明"
    answer, progress = _split_native_composite(composite)
    assert answer == "先回答一半"
    assert progress  # 非空即说明尾巴被拆出而非污染正文