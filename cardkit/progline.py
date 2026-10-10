"""CardKit v2.0.5 — hermes gateway 进度行解析（叶子模块）.

把 gateway progress 模板产物拆解为结构化行，供两处消费：

- ``composer/classify.py``：CH_PROGRESS 通道判定与 (kind, tool) 提取
- ``cardkit/cards.py``：``_render_status_children`` 把行渲染成 V1 风格面板子元素

v2.0.5 前这段逻辑只在 classify.py——cards.py 需要同一套解析时出现循环导入
（cardkit ← composer ← cardkit），故下沉到本叶子模块：只依赖 stdlib，
classify 与 cards 都从这里导入。正则语义与 v2.0.4 classify.py 完全一致
（``_TOOL_PREVIEW_RE`` 追加捕获组 3 提取引号内预览文本，匹配行为不变）。

gateway 模板（locales/zh.yaml gateway.progress）：
    thinking_prefix: "💬 {text}"
    tool_head:       "{emoji} {tool}"
    tool_pending:    "{emoji} {tool}..."
    tool_preview:    '{emoji} {tool}："{preview}"'
"""

from __future__ import annotations

import logging
import re

_logger = logging.getLogger("hermes_lark_streaming.cardkit.progline")

__all__ = [
    "_EMOJI_RE",
    "_THINKING_PREFIX",
    "_TOOL_PENDING_RE",
    "_TOOL_PREVIEW_RE",
    "parse_progress_line",
    "preview_text",
]

_THINKING_PREFIX = "\U0001f4ac"  # 💬

# 工具行首 emoji 检测（hermes tool_head/tool_pending/tool_preview 均以 emoji 开头）
_EMOJI_RE = re.compile(
    "^[\U0001f300-\U0001faff\u2600-\u27bf\U0001f1e6-\U0001f1ff\u2b00-\u2bff\u23e9-\u23fa]"
)
# 工具预览行：{emoji} {tool}："..." / {emoji} {tool}("...")（组 3 = 引号内预览）
_TOOL_PREVIEW_RE = re.compile(r"^(\S+)\s+([A-Za-z_][\w.\-]*)\s*[：（(](.*)$")
# 工具待运行行：{emoji} {tool}...（组 2 = 工具名）
_TOOL_PENDING_RE = re.compile(r"^(\S+)\s+([A-Za-z_][\w.\-]*)\.\.\.$")


def parse_progress_line(content: str) -> tuple[str, str, str]:
    """把 progress 文本拆成 (kind, tool_name, clean_text).

    kind: "thinking" | "tool" | "unknown"（unknown 时 clean_text 为原文）
    永不抛异常；解析不出结构时返回原文。
    """
    text = (content or "").strip()
    if text.startswith(_THINKING_PREFIX):
        return "thinking", "", text[len(_THINKING_PREFIX):].strip()
    m = _TOOL_PENDING_RE.match(text)
    if m:
        return "tool", m.group(2), text
    m = _TOOL_PREVIEW_RE.match(text)
    if m:
        return "tool", m.group(2), text
    if _EMOJI_RE.match(text):
        return "tool", "", text
    return "unknown", "", text


def preview_text(text: str) -> str:
    """工具预览行 → 引号内命令摘要；非预览行返回空串。永不抛异常。"""
    try:
        m = _TOOL_PREVIEW_RE.match((text or "").strip())
        if m:
            return (m.group(3) or "").strip()
    except Exception:  # noqa: BLE001 — 解析纯装饰，绝不影响渲染
        _logger.debug("preview_text parse failed for %r", (text or "")[:50], exc_info=True)
    return ""
