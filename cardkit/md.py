"""Markdown 文本处理 — 标题降级、表格降级、图片 key 剥离、长文本分块."""

from __future__ import annotations

import logging
import re

_logger = logging.getLogger("hermes_lark_streaming")

_MAX_CARD_TABLES = 20  # 流式卡片：20表降级阈值（流式增量内容，飞书宽松执行）
_MAX_CRON_TABLES = 5   # 静态卡片：5表降级阈值（飞书 Card 2.0 单卡硬限）
_MAX_CHUNK_CHARS = 2400

# ── Pre-compiled regex patterns (P2-01: avoid recompilation on every call) ──
_RE_FENCED_CODE = re.compile(r'```[\s\S]*?```')
_RE_INLINE_CODE = re.compile(r'`[^`]+`')
_RE_BOLD = re.compile(r'\*{2,3}(?!\s)((?:(?!\*{2,3}).)+?)(?<!\s)\*{2,3}', re.DOTALL)
_RE_VALID_ITALIC = re.compile(r'(?<![a-zA-Z0-9_])\*(?!\s)((?:(?!\*).)+?)(?<!\s)\*', re.DOTALL)
_RE_UNPAIRED_ASTERISK = re.compile(r'(?<!\\)\*(?=[^\s*])')
_RE_TABLE_ROW = re.compile(r"\|.+\|\n\|[-:| ]+\|[\s\S]*?(?=\n\n|\n(?!\|)|$)")
_RE_IMAGE_REF = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
_RE_CODE_BLOCK_EXTRACT = re.compile(r"(^|\n)(`{3,})([^\n]*)\n[\s\S]*?\n\2(?=\n|$)")
_RE_H1_TO_H3 = re.compile(r"^#{1,3} ", re.MULTILINE)
_RE_HEADING_DEMOTE = re.compile(r"^#{2,6} (.+)$", re.MULTILINE)
_RE_H1_DEMOTE = re.compile(r"^# (.+)$", re.MULTILINE)
_RE_MULTI_NEWLINE = re.compile(r"\n{3,}")
_RE_SHORT_MD_CHECK = re.compile(r'^#{1,6} |\n#{1,6} |```|!\[|\n{3,}')
# v1.3.0: placeholder pattern for restoring protected code/bold/italic blocks
_RE_PROTECTED_PLACEHOLDER = re.compile(r'\x00P(\d+)P\x00')

__all__ = [
    "_MAX_CRON_TABLES",
    "_downgrade_tables",
    "_find_tables_outside_code_blocks",
    "_split_long_text",
    "_strip_invalid_image_keys",
    "escape_markdown_asterisks",
    "optimize_markdown_style",
]

def _find_tables_outside_code_blocks(text: str) -> list[tuple[int, int, str]]:
    """查找代码块外的 markdown 表格，返回 [(start, end, raw), ...]."""
    code_ranges: list[tuple[int, int]] = []
    for m in _RE_FENCED_CODE.finditer(text):
        code_ranges.append((m.start(), m.end()))

    def _in_code(idx: int) -> bool:
        return any(s <= idx < e for s, e in code_ranges)

    results: list[tuple[int, int, str]] = []
    for m in _RE_TABLE_ROW.finditer(text):
        if not _in_code(m.start()):
            results.append((m.start(), m.end(), m.group(0)))
    return results

def _downgrade_tables(text: str, limit: int = _MAX_CARD_TABLES) -> str:
    """超限表格降级为代码块（保留内容可见但飞书不渲染为表格元素）."""
    # Early return: no tables possible without pipe characters
    if '|' not in text:
        return text
    matches = _find_tables_outside_code_blocks(text)
    if len(matches) <= limit:
        return text
    result = text
    for start, end, raw in reversed(matches[limit:]):
        replacement = f"```\n{raw}\n```"
        result = result[:start] + replacement + result[end:]
    return result

def _strip_invalid_image_keys(text: str) -> str:
    """移除非 img_ 前缀的图片引用."""
    if "![" not in text:
        return text

    def _replace(m: re.Match) -> str:
        return m.group(0) if m.group(2).startswith("img_") else ""

    return _RE_IMAGE_REF.sub(_replace, text)

def escape_markdown_asterisks(text: str) -> str:
    """飞书 Markdown 解析器比 CommonMark 更激进——会把 2*4000+4*3000"""
    if '\x00' in text:
        text = text.replace('\x00', '')

    if '*' not in text:
        return text

    _protected: list[str] = []

    def _save(m: re.Match) -> str:
        _protected.append(m.group(0))
        return f'\x00P{len(_protected) - 1}P\x00'

    # Step 1: 保护代码区域
    text = _RE_FENCED_CODE.sub(_save, text)
    text = _RE_INLINE_CODE.sub(_save, text)

    # Step 2: 保护粗体 **...** 和 ***...***
    text = _RE_BOLD.sub(_save, text)

    text = _RE_VALID_ITALIC.sub(_save, text)

    # Step 4: 转义剩余 *（飞书可能误配对的）
    text = _RE_UNPAIRED_ASTERISK.sub(r'\\*', text)

    if _protected:
        for i in range(len(_protected) - 1, -1, -1):
            text = text.replace(f'\x00P{i}P\x00', _protected[i])

    # Null bytes render as boxes (□) in Feishu and must never reach the API.
    if '\x00' in text:
        text = text.replace('\x00', '')

    return text

def optimize_markdown_style(text: str) -> str:
    """1. 提取代码块用占位符保护"""
    if len(text) < 100 and not _RE_SHORT_MD_CHECK.search(text):
        return text
    try:
        # 1. 提取代码块
        mark = "___CB_"
        code_blocks: list[str] = []

        def _extract(m: re.Match) -> str:
            prefix = m.group(1) or ""
            block = m.group(0)[len(prefix) :]
            idx = len(code_blocks)
            code_blocks.append(block)
            return f"{prefix}{mark}{idx}___"

        r = _RE_CODE_BLOCK_EXTRACT.sub(_extract, text)

        # 2. 标题降级（仅当存在 H1-H3 时）
        if _RE_H1_TO_H3.search(text):
            r = _RE_HEADING_DEMOTE.sub(r'##### \1', r)
            r = _RE_H1_DEMOTE.sub(r'#### \1', r)

        # 3. 还原代码块
        for i, block in enumerate(code_blocks):
            r = r.replace(f"{mark}{i}___", block)

        # 4. 压缩多余空行
        r = _RE_MULTI_NEWLINE.sub("\n\n", r)

        # 5. 剥离无效图片 key
        r = _strip_invalid_image_keys(r)

        return r
    except Exception:
        _logger.debug("optimize_markdown_style failed", exc_info=True)
        return text

def _split_long_text(text: str, limit: int = _MAX_CHUNK_CHARS) -> list[str]:
    """将超长文本按段落/换行拆分为多个不超过 limit 字符的块."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        cut = text.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks
