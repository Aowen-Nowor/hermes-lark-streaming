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
    """保护合法 Markdown 强调结构，转义所有剩余 *。

    飞书 Markdown 解析器比 CommonMark 更激进——会把 2*4000+4*3000
    中的 *4000+4* 配对为斜体，导致乘号消失、数字拼合。

    解决思路：不是猜"哪个 * 是乘号"，而是反过来——先保护合法
    Markdown 结构（粗体、斜体、代码），再转义一切剩余 *。
    这样逻辑是 100% 严密的，不需要概率判断。

    判断"合法斜体"的关键规则：
      开头 * 前面是 行首/空白/标点/CJK字符 → 合法斜体
      开头 * 前面是 ASCII字母/数字/下划线   → 不合法，必须转义

    逻辑基础：
      CJK 字符是自然语言 → 后面跟 * 只能是排版（斜体）
      ASCII 字母/数字是形式语言 → 后面跟 * 只能是运算符
      两者区别是语言的本质差异，不是概率。

    算法：
    1. 提取代码块/行内代码 → 保护（代码内 * 是字面量）
    2. 提取粗体 **...** → 保护（粗体永远是排版意图）
    3. 提取合法斜体 *...* → 保护（开头*不在ASCII字母/数字/下划线后）
    4. 转义所有剩余 *（这些不可能是合法 Markdown，飞书会误配对）
    5. 还原保护区域

    v1.3.0 fix: defensive cleanup of null bytes. The placeholder pattern
    ``\\x00P{i}P\\x00`` uses null bytes as delimiters. If the INPUT text
    already contains null bytes (e.g. the AI reproduced the pattern from
    source code, or an encoding glitch introduced them), the restoration
    regex could match AI-generated placeholders and raise IndexError, or
    the ``if _protected`` guard could skip restoration leaving our own
    placeholders leaked. Fix: strip null bytes from input AND output.
    """
    # v1.3.0 fix: strip any pre-existing null bytes from the input.
    # Null bytes are never legitimate in markdown text — they are either
    # encoding artifacts or leaked placeholders from a previous call.
    # Stripping them here prevents the restoration regex from matching
    # spurious placeholder patterns and raising IndexError.
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

    # Step 3: 保护合法斜体 *...*
    # 开头 * 合法条件：前面不是 ASCII 字母/数字/下划线
    # 这样 CJK 字符后的 * 会被保护（中文斜体的唯一写法），
    # 而 ASCII 字母/数字后的 * 不会被保护（是运算符）。
    text = _RE_VALID_ITALIC.sub(_save, text)

    # Step 4: 转义剩余 *（飞书可能误配对的）
    # * 后面跟非空白、非 * 的字符时，飞书会尝试配对，必须转义。
    # * 后面跟空格或行尾时，飞书不会配对，安全不转义（如列表 * 项目）。
    text = _RE_UNPAIRED_ASTERISK.sub(r'\\*', text)

    # Step 5: 还原保护区域
    # v1.3.5 fix: 逆向遍历（高索引→低索引）以确保嵌套占位符正确恢复。
    # 当粗体包裹行内代码时，外层（粗体）索引高于内层（行内代码）。
    # re.sub 从左到右匹配并跳过替换文本中的内容，导致内层占位符泄漏。
    # 改用 str.replace 逆向遍历：先恢复外层（含内层占位符），
    # 后恢复内层——str.replace 扫描全串，不会跳过替换内容中的匹配。
    if _protected:
        for i in range(len(_protected) - 1, -1, -1):
            text = text.replace(f'\x00P{i}P\x00', _protected[i])

    # v1.3.0 fix: final safety net — strip any remaining null bytes.
    # This catches: (a) spurious placeholder patterns we didn't create,
    # (b) any null bytes that survived the restoration, (c) encoding artifacts.
    # Null bytes render as boxes (□) in Feishu and must never reach the API.
    if '\x00' in text:
        text = text.replace('\x00', '')

    return text


def optimize_markdown_style(text: str) -> str:
    """优化流式 Markdown 以适配飞书 CardKit 渲染.

    1. 提取代码块用占位符保护
    2. 标题降级: H1 -> H4, H2-H6 -> H5
    3. 还原代码块
    4. 压缩多余空行
    5. 剥离无效图片 key（非 img_xxx 格式）
    """
    # Early return: short texts without markdown structure don't need
    # complex regex processing.  Skip only when no headings, code blocks,
    # images, or excessive blank lines are present.
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
