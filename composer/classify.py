"""V2 帧分类器 — 把 hermes 出站帧分类为卡片可消费的通道.

输入契约（全部为 hermes v0.21.x 官方 metadata / 参数，无任何私有 hack）：

- ``metadata["expect_edits"]=True``  → answer 流式帧（后续会被 edit_message 更新）
- ``metadata["_interim_send"]=True`` → mid-turn 状态帧（interim 播报 / progress 气泡首条）
- ``metadata["notify"]=True``        → turn 定稿帧（原生流式 final 语义）
- ``edit_message(..., finalize=True)`` → 定稿编辑
- progress 文本模板（hermes locales，稳定 i18n key 渲染产物）:
    thinking 前缀 ``💬 {text}``；工具行 ``{emoji} {tool}...`` / ``{emoji} {tool}："{preview}"``
    v2.0.6 补真实模板（``agent/display.py`` + ``run_turn_runner``）：
    tool_head + 围栏代码块（terminal）、中文/英文动词行（``💻 运行命令 ...``）、
    裸围栏块（连续 terminal 调用省略 header）、去重后缀 ``(×N)``

设计红线：分类器**永不抛异常**——任何解析失败都降级为最保守的通道（interim / plain），
消息内容永不丢失（最坏情况走原生文本路径）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

# v2.0.5：进度行解析下沉到 cardkit.progline 叶子模块（cards.py 渲染面板时需要
# 同一套解析，原地对拷会漂移；从 classify 导入又会 cardkit←composer 循环）。
# re-export 既有公开名（_THINKING_PREFIX / parse_progress_line）；
# _EMOJI_RE 供 _is_progress_text 启发式使用。
from ..cardkit.progline import (
    _EMOJI_RE,
    _THINKING_PREFIX,
    parse_progress_line,
)  # noqa: F401 — re-export 供既有导入方使用

_logger = logging.getLogger("hermes_lark_streaming.composer")

# ── 通道常量 ────────────────────────────────────────────────────────────────
CH_STREAM = "stream"      # answer 流式帧（send 首条 / edit 后续）
CH_PROGRESS = "progress"  # 工具进度行 / thinking 气泡（progress lane）
CH_INTERIM = "interim"    # interim 播报（非工具非思考的中途文本）
CH_FINAL = "final"        # turn 定稿
CH_PLAIN = "plain"        # 普通消息（cron / 后台 / 无标记发送）→ 原生路径


@dataclass
class ClassifyResult:
    """一帧的分类结论。"""

    channel: str
    kind: str = "text"          # 细分：answer / thinking / tool / advisory / plain
    tool_name: str = ""         # kind=tool 时尽力提取
    thinking_text: str = ""     # kind=thinking 时剥离前缀后的原文
    raw_content: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


def _strip_internal_meta(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """剥离 Gateway 内部标记（官方注释：adapters strip it before the wire）。"""
    if not metadata:
        return {}
    meta = dict(metadata)
    meta.pop("_interim_send", None)
    return meta


def _is_progress_text(content: str) -> bool:
    """启发式：progress lane 文本 = thinking 气泡 或 emoji 开头的短工具行.

    v2.0.6：裸围栏代码块（`` ``` `` 开头，连续 terminal 调用省略 header 的
    真实形态）也判为 progress；上限放宽到 600（块含命令正文比单行预览长）。"""
    text = (content or "").strip()
    if not text:
        return False
    if text.startswith(_THINKING_PREFIX):
        return True
    if text.startswith("```"):
        return len(text) <= 600  # 命令块远短于 600；长文本不可能是进度块
    if len(text) > 300:  # 工具行都很短；长文本不可能是工具行
        return False
    return bool(_EMOJI_RE.match(text))


# v2.0.1 公开别名：适配器层上下文升格规则需要复用进度启发式
# （edit-transport 平台的进度气泡首条是裸 send，无任何标记，需在活跃卡片期
# 结合启发式升格 CH_PROGRESS；详见 adapter.send 注释）。
is_progress_text = _is_progress_text


def classify_send(
    content: str,
    metadata: dict[str, Any] | None,
) -> ClassifyResult:
    """分类一个 send() 帧。"""
    try:
        meta = metadata or {}
        interim = bool(meta.get("_interim_send"))
        expect_edits = bool(meta.get("expect_edits"))
        notify = bool(meta.get("notify"))

        # 1) answer 流首帧：显式 expect_edits 且不是内部状态帧
        if expect_edits and not interim:
            return ClassifyResult(CH_STREAM, kind="answer", raw_content=content, meta=_strip_internal_meta(meta))

        # 2) mid-turn 状态帧（progress 气泡首条 / interim 播报共用 _interim_send 标记）
        if interim:
            if _is_progress_text(content):
                kind, tool, clean = parse_progress_line(content)
                return ClassifyResult(CH_PROGRESS, kind=kind, tool_name=tool, thinking_text=clean,
                                      raw_content=content, meta=_strip_internal_meta(meta))
            return ClassifyResult(CH_INTERIM, kind="advisory", raw_content=content, meta=_strip_internal_meta(meta))

        # 3) 定稿帧
        if notify:
            return ClassifyResult(CH_FINAL, kind="final", raw_content=content, meta=_strip_internal_meta(meta))

        # 4) 兜底：无任何标记 → 原生路径（cron / 后台 / 外部投递）
        return ClassifyResult(CH_PLAIN, kind="plain", raw_content=content, meta=_strip_internal_meta(meta))
    except Exception:
        _logger.debug("classify_send fallback to plain", exc_info=True)
        return ClassifyResult(CH_PLAIN, kind="plain", raw_content=content or "")


def classify_edit(
    content: str,
    finalize: bool,
) -> ClassifyResult:
    """分类一个 edit_message() 帧。

    edit 无 metadata 参数 → 通道归属由 engine 的 message_id 注册表决定，
    这里只判定 finalize 语义。
    """
    return ClassifyResult(
        CH_FINAL if finalize else CH_STREAM,
        kind="final" if finalize else "answer",
        raw_content=content or "",
    )
