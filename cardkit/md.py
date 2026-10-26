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
_RE_TABLE_ROW = re.compile(r"\|.+\|\n\|[-:| ]+\|[\s\S]*?(?=\n\n|\n(?!\|)|$)")
_RE_IMAGE_REF = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
_RE_CODE_BLOCK_EXTRACT = re.compile(r"(^|\n)(`{3,})([^\n]*)\n[\s\S]*?\n\2(?=\n|$)")
_RE_H1_TO_H3 = re.compile(r"^#{1,3} ", re.MULTILINE)
_RE_HEADING_DEMOTE = re.compile(r"^#{2,6} (.+)$", re.MULTILINE)
_RE_H1_DEMOTE = re.compile(r"^# (.+)$", re.MULTILINE)
_RE_MULTI_NEWLINE = re.compile(r"\n{3,}")
_RE_SHORT_MD_CHECK = re.compile(r'^#{1,6} |\n#{1,6} |```|!\[|\n{3,}')
_RE_FENCE_LINE = re.compile(r"^\s*(`{3,})")  # 行首围栏标记（v2.0.21 围栏状态机）

__all__ = [
    "_MAX_CRON_TABLES",
    "_downgrade_tables",
    "_find_tables_outside_code_blocks",
    "_split_long_text",
    "_strip_invalid_image_keys",
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

def _demote_headings_outside_fences(text: str) -> str:
    """v2.0.21（审计 R5）：围栏感知的标题降级.

    原实现先提取**闭合**代码块（_RE_CODE_BLOCK_EXTRACT 要求闭合 ```），再对
    剩余文本跑 MULTILINE 标题降级——未闭合围栏（流式中段/模型输出奇数个
    ```）内的 ``# 注释``/``## 标题`` 行被误改写为 #####/####，代码显示变形。
    逐行状态机：行首 ``` 开栏/闭栏，围栏内行原样保留；围栏外行才降级
    （H1→####，H2-H6→#####，与原双正则语义逐行等价）。
    """
    lines = text.split("\n")
    out: list[str] = []
    fence_len = 0  # 0 = 不在围栏内；>0 = 当前围栏开栏标记的反引号数
    for ln in lines:
        m = _RE_FENCE_LINE.match(ln)
        if m:
            if not fence_len:
                fence_len = len(m.group(1))
            elif len(m.group(1)) >= fence_len:
                fence_len = 0  # 同长或更长的 ``` 闭栏
            out.append(ln)
            continue
        if fence_len:
            out.append(ln)  # 围栏内行原样（代码/注释不降级）
            continue
        if _RE_H1_DEMOTE.match(ln):
            ln = _RE_H1_DEMOTE.sub(r"#### \1", ln)
        else:
            ln = _RE_HEADING_DEMOTE.sub(r"##### \1", ln)
        out.append(ln)
    return "\n".join(out)


def _fence_balanced(prefix: str) -> bool:
    """v2.0.21（审计 R6）：prefix 的行首围栏标记是否成对（偶数 = 闭合）."""
    return len(_RE_FENCE_LINE.findall(prefix)) % 2 == 0


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

        # 2. 标题降级（仅当存在 H1-H3 时；v2.0.21 围栏状态机——未闭合围栏内
        # 的 # 行不再被误降级，闭合块已提取为占位符不受影响）
        if _RE_H1_TO_H3.search(text):
            r = _demote_headings_outside_fences(r)

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
    """将超长文本按段落/换行拆分为多个不超过 limit 字符的块.

    v2.0.21（审计 R6）：切点围栏感知——原实现可在围栏/表格中间切开，跨块
    不闭合 → 前后两块都渲染坏。现在候选切点（段落 > 换行 > 硬切）逐个检查
    围栏是否闭合，取第一个闭合的；三候选全部落在围栏内才硬切（极端罕见：
    单个未闭合围栏长度 > limit/2，与原行为一致不再恶化）。
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        candidates: list[int] = []
        cut = text.rfind("\n\n", 0, limit)
        if cut >= limit // 2:
            candidates.append(cut)
        cut = text.rfind("\n", 0, limit)
        if cut >= limit // 2:
            candidates.append(cut)
        chosen = limit  # 兑底硬切（与原行为一致）
        for cand in candidates:
            if _fence_balanced(text[:cand]):
                chosen = cand
                break
        chunks.append(text[:chosen])
        text = text[chosen:].lstrip("\n")
    return chunks
