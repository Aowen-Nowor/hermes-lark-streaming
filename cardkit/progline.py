"""CardKit v2.0.6 — hermes gateway 进度行解析（叶子模块）.

把 gateway progress 模板产物拆解为结构化行，供两处消费：

- ``composer/classify.py``：CH_PROGRESS 通道判定与 (kind, tool) 提取
- ``cardkit/cards.py``：``_render_status_children`` 把行渲染成 V1 风格面板子元素
- ``composer/engine.py``：``split_progress_segments`` 把 accumulate 快照拆成逻辑行

v2.0.5 前这段逻辑只在 classify.py——cards.py 需要同一套解析时出现循环导入
（cardkit ← composer ← cardkit），故下沉到本叶子模块：只依赖 stdlib。

v2.0.6 真实帧格式对齐（E2E ≠ 正式对话根因修复）：
E2E 此前模拟的 ``🖥️ terminal："..."`` 是合成格式；hermes v0.21.x 真实
progress 模板（``agent/display.py`` + ``gateway/run_turn_runner.py`` +
``locales/*.yaml gateway.progress``）实为：

1. terminal 代码块（feishu 支持 code block 时）::

       💻 terminal
       ```
       echo "=== 时间 ==="; date "+%Y-%m-%d %H:%M:%S"
       ```

   连续 terminal 调用会省略重复 header（裸围栏块）；多行命令截断为
   首行 + `` ...``；同文本重发带 ``(×N)`` 去重后缀。
2. 中文动词行（display.friendly_tool_labels 默认开）::

       🔍 搜索网页 · feishu cardkit     （search 类连接符 " · "）
       📖 读取文件 /tmp/status.txt      （默认连接符 " "）
       📚 列出技能                      （无预览动词）

3. 既有合成格式继续兼容：``{emoji} {tool}...``（pending）、
   ``{emoji} {tool}："{preview}"``（preview）、``💬 {text}``（thinking）、
   ``{emoji} {tool}``（裸 head）。

动词表来自 hermes locales ``display.verb.*``（zh + en 双语），用于把动词行
反查回工具名（图标/标题渲染需要工具名）。匹配永不抛异常。
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
    "match_verb_line",
    "parse_progress_line",
    "preview_text",
    "split_progress_segments",
    "strip_dedup_suffix",
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
# 裸工具头行：{emoji} {tool}（tool_head 模板；单发时按 pending 渲染）
_TOOL_HEAD_RE = re.compile(r"^(\S+)\s+([A-Za-z_][\w.\-]*)$")
# 网关同文本去重后缀：run_turn_runner 重复进度行会追加 " (×N)"
_DEDUP_SUFFIX_RE = re.compile(r"\s*\(×\d+\)\s*$")
# 网关命令截断尾：多行命令取首行后追加 " ..." / 截断追加 "..."
_TRUNC_TAIL_RE = re.compile(r"\s*\.{3}$")
# emoji 行拆头：{emoji} {rest}（emoji 为首个空白前 token）
_EMOJI_LEAD_RE = re.compile(r"^(\S+)\s+(.+)$")

# ── 动词行反查表（hermes locales display.verb.*，zh + en）───────────────────
# 同一动词文本多工具冲突时取更常见工具（en "Reading"：read_file 优先 web_extract）。
_VERB_TO_TOOL: tuple[tuple[str, str], ...] = (
    # zh（locales/zh.yaml display.verb）
    ("搜索网页", "web_search"),
    ("读取网页", "web_extract"),
    ("浏览", "browser_navigate"),
    ("点击", "browser_click"),
    ("输入", "browser_type"),
    ("读取文件", "read_file"),
    ("写入文件", "write_file"),
    ("编辑", "patch"),
    ("搜索文件", "search_files"),
    ("运行命令", "terminal"),
    ("运行代码", "execute_code"),
    ("生成图片", "image_generate"),
    ("生成视频", "video_generate"),
    ("生成语音", "text_to_speech"),
    ("查看图片", "vision_analyze"),
    ("搜索历史会话", "session_search"),
    ("阅读技能", "skill_view"),
    ("列出技能", "skills_list"),
    ("更新技能", "skill_manage"),
    ("委派任务", "delegate_task"),
    ("安排定时任务", "cronjob_manage"),
    ("提问", "clarify"),
    ("更新记忆", "memory"),
    ("更新任务", "todo_list"),
    # en（locales/en.yaml display.verb）
    ("Searching the web", "web_search"),
    ("Searching files", "search_files"),
    ("Searching past sessions", "session_search"),
    ("Reading skill", "skill_view"),
    ("Reading", "read_file"),
    ("Writing", "write_file"),
    ("Browsing", "browser_navigate"),
    ("Clicking", "browser_click"),
    ("Typing", "browser_type"),
    ("Editing", "patch"),
    ("Running code", "execute_code"),
    ("Running", "terminal"),
    ("Generating image", "image_generate"),
    ("Generating video", "video_generate"),
    ("Generating speech", "text_to_speech"),
    ("Looking at the image", "vision_analyze"),
    ("Listing skills", "skills_list"),
    ("Updating skill", "skill_manage"),
    ("Updating memory", "memory"),
    ("Updating tasks", "todo_list"),
    ("Delegating", "delegate_task"),
    ("Scheduling", "cronjob_manage"),
    ("Asking", "clarify"),
)
# 长动词优先（"运行代码" 先于 "运行"、"Reading skill" 先于 "Reading"）
_VERBS_SORTED: tuple[tuple[str, str], ...] = tuple(
    sorted(_VERB_TO_TOOL, key=lambda pair: len(pair[0]), reverse=True)
)
# 动词-预览连接符（locales display.verb_connector：search 类 " · "/" for "，默认 " "）
_VERB_CONNECTORS: tuple[str, ...] = (" · ", " for ", " ")


def strip_dedup_suffix(text: str) -> str:
    """剥掉网关去重后缀 `` (×N)``（run_turn_runner 同文本重发标记）."""
    return _DEDUP_SUFFIX_RE.sub("", (text or "").strip()).strip()


def _split_fence_segment(text: str) -> tuple[str, str]:
    """多行段 → (header, 命令正文). 无围栏返回 (原文, "")；裸围栏 header 为空."""
    lines = text.splitlines()
    start = -1
    for i, ln in enumerate(lines):
        if ln.strip().startswith("```"):
            start = i
            break
    if start < 0:
        return text.strip(), ""
    end = -1
    for i in range(len(lines) - 1, start, -1):
        if lines[i].strip().startswith("```"):
            end = i
            break
    header = "\n".join(lines[:start]).strip()
    body = "\n".join(lines[start + 1: end if end > start else len(lines)])
    return header, body.strip()


def _parse_single_line(text: str) -> tuple[str, str, str]:
    """单行进度文本 → (kind, tool, clean). 多格式按确定性顺序尝试."""
    m = _TOOL_PENDING_RE.match(text)
    if m:
        return "tool", m.group(2), text
    m = _TOOL_PREVIEW_RE.match(text)
    if m:
        return "tool", m.group(2), text
    verb = match_verb_line(text)
    if verb is not None:
        return "tool", verb[0], text
    m = _TOOL_HEAD_RE.match(text)
    if m:
        return "tool", m.group(2), text
    if _EMOJI_RE.match(text):
        return "tool", "", text
    return "unknown", "", text


def parse_progress_line(content: str) -> tuple[str, str, str]:
    """把 progress 文本拆成 (kind, tool_name, clean_text).

    kind: "thinking" | "tool" | "unknown"（unknown 时 clean_text 为原文）
    v2.0.6：支持多行段（tool_head + 围栏代码块，header 可缺省于连续调用）、
    中文/英文动词行、去重后缀 ``(×N)``。
    永不抛异常；解析不出结构时返回原文。
    """
    try:
        text = strip_dedup_suffix(content)
        if text.startswith(_THINKING_PREFIX):
            return "thinking", "", text[len(_THINKING_PREFIX):].strip()
        if "```" in text:
            header, _body = _split_fence_segment(text)
            if header:
                kind, tool, _clean = _parse_single_line(header.splitlines()[0].strip())
                return ("tool" if kind == "unknown" else kind, tool, text)
            return "tool", "", text
        return _parse_single_line(text)
    except Exception:  # 解析纯装饰，绝不影响渲染（BLE 未启用，noqa 会触发 RUF100）
        _logger.debug("parse_progress_line failed for %r", (content or "")[:50], exc_info=True)
        return "unknown", "", (content or "").strip()


def match_verb_line(text: str) -> tuple[str, str] | None:
    """中文/英文动词行 → (tool_name, preview)；非动词行返回 None.

    格式：``{emoji} {verb}{connector}{preview}``（连接符 " · " / " for " / " "；
    无预览动词只有 ``{emoji} {verb}``）。动词前缀命中但连接符不匹配时返回
    None（如 "编辑器 ..." 不是 patch 动词行）。永不抛异常。
    """
    try:
        t = strip_dedup_suffix(text)
        if not t or not _EMOJI_RE.match(t):
            return None
        m = _EMOJI_LEAD_RE.match(t)
        if not m:
            return None
        rest = m.group(2).strip()
        for verb, tool in _VERBS_SORTED:
            if not rest.startswith(verb):
                continue
            tail = rest[len(verb):]
            if not tail:
                return (tool, "")  # 无预览动词（列出技能 / Listing skills）
            for conn in _VERB_CONNECTORS:
                if tail.startswith(conn):
                    return (tool, tail[len(conn):].strip())
            return None  # 动词前缀但无连接符 → 普通文本
        return None
    except Exception:  # noqa: BLE001
        return None


def preview_text(text: str) -> str:
    """工具进度行 → 命令/参数摘要；非工具行返回空串。永不抛异常。

    v2.0.6 覆盖：围栏代码块（首行命令，剥截断尾）、动词行（连接符后正文）、
    既有引号预览（``{tool}：\"...\"``）。
    """
    try:
        t = strip_dedup_suffix(text)
        if not t:
            return ""
        if "```" in t:
            _header, body = _split_fence_segment(t)
            return _TRUNC_TAIL_RE.sub("", body.splitlines()[0].strip()) if body else ""
        m = _TOOL_PREVIEW_RE.match(t)
        if m:
            preview = (m.group(3) or "").strip()
            # legacy 模板把预览包在引号里（`："{preview}"`）→ 剥一对包裹引号；
            # 只剥成对包裹（命令自身的尾部引号如 date "+%H:%M:%S" 不受影响）
            if len(preview) >= 2 and preview[0] in "\"“”" and preview[-1] in "\"“”":
                preview = preview[1:-1].strip()
            return _TRUNC_TAIL_RE.sub("", preview)
        verb = match_verb_line(t)
        if verb is not None:
            return _TRUNC_TAIL_RE.sub("", verb[1])
        return ""
    except Exception:  # 解析纯装饰，绝不影响渲染（BLE 未启用，noqa 会触发 RUF100）
        _logger.debug("preview_text parse failed for %r", (text or "")[:50], exc_info=True)
        return ""


def split_progress_segments(raw: str) -> list[str]:
    """accumulate 快照 → 逻辑段列表（围栏块与 header 保持一体）.

    gateway accumulate 模式每个 tick 重发"全部进度行"（``"\\n"`` 连接，
    行本身可能是多行代码块）。按 ``str.splitlines`` 直拆会把代码块拆散
    （``` 独立成行 → 面板渲染破碎）。本函数保证：

    - 围栏块（```...```）整体成段，且**向前吸附紧邻的单行 header**
      （``💻 terminal`` + 块 → 一个段，合并/渲染语义完整；💬 思考行
      不被吸附——思考文本是面板独立步骤，不能变回代码块 header）
    - 无 header 的裸围栏块（连续 terminal 调用）独立成段
    - 未闭合围栏按到文本末尾处理（部分帧也渲染，不丢内容）
    - 普通行逐行成段；空行丢弃

    v2.0.8 多行思考吸附（用户验收 2026-10-11「思考后面格式不对」根因）：
    reasoning_feed / 网关中继的 ``💬 {text}`` 思考文本自带换行（推理正文/
    新闻草稿列表），按行直拆会把首行后的内容打碎成独立 raw 行（只有首行
    保留思考样式）。本函数把 💬 行与其后的**非 emoji 非围栏连续行**吸附为
    一个逻辑段（思考块是多行逻辑单元），直到下一个 emoji 行/围栏/结尾。
    """
    if not raw or not raw.strip():
        return []
    segments: list[str] = []
    buf: list[str] = []
    in_fence = False
    in_thinking = False
    for line in raw.splitlines():
        stripped = line.strip()
        if in_fence:
            buf.append(stripped)
            if stripped.startswith("```"):
                in_fence = False
                in_thinking = False
                segments.append("\n".join(buf))
                buf = []
            continue
        if stripped.startswith("```"):
            in_fence = True
            in_thinking = False
            # 向前吸附紧邻单行工具头（emoji 开头：tool_head/pending/preview/动词行），
            # 块与头一体；普通文本行与 💬 思考行不吸附（思考块独立成段）
            if segments:
                prev = segments[-1]
                if ("\n" not in prev and not prev.startswith("```")
                        and not prev.startswith(_THINKING_PREFIX) and _EMOJI_RE.match(prev)):
                    buf.append(segments.pop())
            buf.append(stripped)
            continue
        if buf:
            # 未闭合围栏中遇到的非围栏行不应出现（防御）：先落盘已开段
            segments.append("\n".join(buf))
            buf = []
        if not stripped:
            continue  # 空行丢弃（思考段内段落分隔坍缩，正文语义不变）
        if stripped.startswith(_THINKING_PREFIX):
            # 思考行开启/延续思考块：同一快照内的同块续行不再新开段
            if in_thinking and segments:
                segments[-1] = f"{segments[-1]}\n{stripped}"
            else:
                segments.append(stripped)
                in_thinking = True
            continue
        if in_thinking and not _EMOJI_RE.match(stripped):
            # 思考块续行（非 emoji 普通文本）→ 吸附进当前思考段
            segments[-1] = f"{segments[-1]}\n{stripped}"
            continue
        in_thinking = False
        segments.append(stripped)
    if buf:
        segments.append("\n".join(buf))
    return segments
