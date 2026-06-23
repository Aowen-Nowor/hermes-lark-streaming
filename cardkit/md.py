"""Markdown 文本处理 — 标题降级、表格降级、图片 key 剥离、长文本分块."""

from __future__ import annotations

import logging
import re

_logger = logging.getLogger("hermes_lark_streaming")

_MAX_CARD_TABLES = 20  # 流式卡片：20表降级阈值（流式增量内容，飞书宽松执行）
_MAX_CRON_TABLES = 5   # 静态卡片：5表降级阈值（飞书 Card 2.0 单卡硬限）
_MAX_CHUNK_CHARS = 2400

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
    for m in re.finditer(r"```[\s\S]*?```", text):
        code_ranges.append((m.start(), m.end()))

    def _in_code(idx: int) -> bool:
        return any(s <= idx < e for s, e in code_ranges)

    results: list[tuple[int, int, str]] = []
    for m in re.finditer(r"\|.+\|\n\|[-:| ]+\|[\s\S]*?(?=\n\n|\n(?!\|)|$)", text):
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

    return re.sub(r"!\[([^\]]*)\]\(([^)\s]+)\)", _replace, text)


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
    """
    if '*' not in text:
        return text

    _protected: list[str] = []

    def _save(m: re.Match) -> str:
        _protected.append(m.group(0))
        return f'\x00P{len(_protected) - 1}P\x00'

    # Step 1: 保护代码区域
    text = re.sub(r'```[\s\S]*?```', _save, text)
    text = re.sub(r'`[^`]+`', _save, text)

    # Step 2: 保护粗体 **...** 和 ***...***
    text = re.sub(
        r'\*{2,3}(?!\s)((?:(?!\*{2,3}).)+?)(?<!\s)\*{2,3}',
        _save, text, flags=re.DOTALL,
    )

    # Step 3: 保护合法斜体 *...*
    # 开头 * 合法条件：前面不是 ASCII 字母/数字/下划线
    # 这样 CJK 字符后的 * 会被保护（中文斜体的唯一写法），
    # 而 ASCII 字母/数字后的 * 不会被保护（是运算符）。
    text = re.sub(
        r'(?<![a-zA-Z0-9_])\*(?!\s)((?:(?!\*).)+?)(?<!\s)\*',
        _save, text, flags=re.DOTALL,
    )

    # Step 4: 转义剩余 *（飞书可能误配对的）
    # * 后面跟非空白、非 * 的字符时，飞书会尝试配对，必须转义。
    # * 后面跟空格或行尾时，飞书不会配对，安全不转义（如列表 * 项目）。
    text = re.sub(r'(?<!\\)\*(?=[^\s*])', r'\\*', text)

    # Step 5: 还原保护区域
    for i, block in enumerate(_protected):
        text = text.replace(f'\x00P{i}P\x00', block)

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
    if len(text) < 100 and not re.search(r'^#{1,6} |\n#{1,6} |```|!\[|\n{3,}', text):
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

        r = re.sub(r"(^|\n)(`{3,})([^\n]*)\n[\s\S]*?\n\2(?=\n|$)", _extract, text)

        # 2. 标题降级（仅当存在 H1-H3 时）
        if re.search(r"^#{1,3} ", text, re.MULTILINE):
            r = re.sub(r"^#{2,6} (.+)$", r"##### \1", r, flags=re.MULTILINE)
            r = re.sub(r"^# (.+)$", r"#### \1", r, flags=re.MULTILINE)

        # 3. 还原代码块
        for i, block in enumerate(code_blocks):
            r = r.replace(f"{mark}{i}___", block)

        # 4. 压缩多余空行
        r = re.sub(r"\n{3,}", "\n\n", r)

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
