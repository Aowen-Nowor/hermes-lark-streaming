"""CardKit v2.0 — Card assemblers: streaming, complete, and linear cards."""

from __future__ import annotations

from typing import Any

from .elements import (
    ANSWER_ELEMENT_ID,
    UNIFIED_PANEL_ELEMENT_ID,
    _aux_text_size,
    _build_reasoning_body_detail,
    _build_reasoning_round_title,
    _build_tool_step_detail,
    _build_tool_step_title,
    _build_unified_panel_placeholder,
    _card_text_style_config,
    _count_tag_objects,
    _loading_element,
    _streaming_element,
)
from .i18n import _LOCALES, _T, _t
from .progline import (
    _THINKING_PREFIX,
    parse_progress_line,
    preview_text,
    strip_dedup_suffix,
)
from .tool_icons import resolve_tool_icon, split_notice_lead, tool_display_title

__all__ = [
    '_enforce_card_element_limit',
    'build_streaming_card_v2',
]

# Feishu Card 2.0 element limit — every JSON object with a ``tag`` key
# counts toward this limit at all nesting levels.
_FEISHU_ELEMENT_LIMIT = 200

_ELEMENT_LIMIT_MARGIN = 5

# ── v2.0.5 状态面板 V1(1.8.1) 同款渲染 ────────────────────────────────────────
# gateway progress 模板行 → V1 unified panel 子元素：
#   💬 思考行 → 分组为推理轮次：robot-add 图标 + 绿色粗体「第 N 轮」+ 缩进灰字正文
#   工具行   → official standard_icon + 彩色粗体工具标题（pending=橙 running /
#              已执行=绿 success）+ 缩进灰字命令摘要（仅已执行行）
#   其余     → 原样 markdown（降级路径）
# 标题/图标解析见 tool_icons.py（V1 state/tooluse.py 移植）。


# v2.0.8：单条思考正文显示上限（V1 _REASONING_DISPLAY_LIMIT 同款）
_REASONING_DISPLAY_LIMIT = 2000


def _truncate_reasoning_body(text: str) -> str:
    """思考正文截断（V1 elements._truncate_reasoning 同语义）."""
    text = (text or "").strip()
    if len(text) <= _REASONING_DISPLAY_LIMIT:
        return text
    return text[:_REASONING_DISPLAY_LIMIT] + f"……（已截断，共 {len(text)} 字）"


def _round_elapsed_ms(lines: list[str], line_times: list[float]) -> dict[int, float]:
    """思考轮次耗时表：{轮次首个 💬 行的 index: elapsed_ms}（v2.0.8）.

    V1 语义：轮次在下一个非思考行（工具/通知）到达时定稿，耗时 =
    定稿时间 - 轮次首行到达时间；仍在进行中的末轮以最后一行时间为准。
    line_times 缺失/不对齐时返回空表（标题省略 elapsed，不伪造）。
    """
    out: dict[int, float] = {}
    try:
        if not line_times or len(line_times) < len(lines):
            return out
        i = 0
        while i < len(lines):
            if not lines[i].startswith(_THINKING_PREFIX):
                i += 1
                continue
            start_i = i
            while i < len(lines) and lines[i].startswith(_THINKING_PREFIX):
                i += 1
            end_i = i - 1  # 思考连续段最后一行
            # 定稿时刻：下一个非思考行到达时间；无则用末行（进行中轮次）
            end_t = line_times[i] if i < len(lines) else line_times[end_i]
            elapsed = (end_t - line_times[start_i]) * 1000.0
            if elapsed > 0:
                out[start_i] = elapsed
    except Exception:  # noqa: BLE001 — 装饰性数据，永不抛
        return {}
    return out


def _last_thinking_group(lines: list[str]) -> tuple[int, bool]:
    """最后一组连续 💬 思考行的 ``(起始下标, 是否延伸到末尾)``；无思考组 ``(-1, False)``.

    v2.0.16 三色状态的组态判定：延伸到末尾 = 流式期该轮尚未被后续行
    （工具/通报/下一轮）封口 → 进行中（橙）；延伸被截断 = 已封口 → 绿。
    """
    start = -1
    prev = False
    last_is_thinking = False
    for i, raw in enumerate(lines):
        text = strip_dedup_suffix(raw or "")
        if not text.strip():
            continue
        kind = parse_progress_line(text)[0]
        if kind == "thinking":
            if not prev:
                start = i
            prev = True
        else:
            prev = False
        last_is_thinking = kind == "thinking"
    return start, bool(start >= 0 and last_is_thinking)


def _render_status_children(
    lines: list[str],
    durations: dict[str, str] | None = None,
    line_times: list[float] | None = None,
    *,
    turn_state: str = "finalized",
) -> list[dict[str, Any]]:
    """status_lines → V1 风格面板子元素（解析失败行降级为原始 markdown）.

    v2.0.5 对齐 V1(1.8.1)：
    - 连续 💬 行分组为一轮推理（第 N 轮标题绿色 + robot-add_outlined；V1 的
      橙色仅用于流式中未定稿轮次，V2 的 💬 行到达即完整，恒绿不伪造状态）
    - 工具行标题 humanize/描述符化（terminal→Terminal、grep→Search text）
    - pending 行（尾部 ``...``）橙色 running；已执行行绿色 success
    - 预览文本取引号内命令摘要渲染为缩进灰字（不再是含引号原文）

    v2.0.8（用户验收：耗时缺失/思考格式破碎）：
    - 工具行标题追加单工具耗时「Terminal (2.3 s)」（V1 state/tooluse.py 同款，
      数据源 = 官方 post_tool_call 钩子 duration_ms，非伪造）
    - 思考轮次标题追加「第 N 轮 · X.Xs」（V1 elements 同款，耗时 = 行时间戳
      到下一工具行到达；line_times 缺失时省略不伪造）
    - 思考正文整段渲染（多行吸附由 progline.split_progress_segments 保证），
      超过 2000 字截断（V1 _REASONING_DISPLAY_LIMIT 同款）

    v2.0.12（用户验收：思考正文 markdown 未渲染）：
    - 思考正文 plain_text → lark_md（_build_reasoning_body_detail：逐行
      <font color='grey'> 包裹保持灰色缩进视觉，markdown 语义生效）；
      命令摘要保持 plain_text（命令是代码非 markdown 语义）

    v2.0.16（用户验收：轮次标题恒绿，V1 是绿/橙/红三色）：
    - ``turn_state="streaming"``（流式期）：最后一组未被封口的 💬 思考轮
      → 橙（orange-300，V1 current_reasoning 语义）；其余组 → 绿。
    - ``turn_state="finalized"``（默认，定稿收口）：全部绿（已完成）。
    - 红（失败）不实施：V1 ``failed=True`` 全库零调用，hermes 工具进度行
      仅启动时发射（run_turn_runner.py:_progress_build_message）无失败形态
      ——帧流无失败数据源，不伪造；失败语义由 ⚠️ 橙色警告行承载。
    """
    children: list[dict[str, Any]] = []
    round_no = 0
    prev_thinking = False
    streaming = turn_state == "streaming"
    last_group_start, open_at_end = _last_thinking_group(lines)
    elapsed_by_line = _round_elapsed_ms(lines, list(line_times or []))
    for idx, line in enumerate(lines):
        text = strip_dedup_suffix(line)
        if not text:
            continue
        kind, tool, clean = parse_progress_line(text)
        if kind == "thinking":
            if not prev_thinking:
                round_no += 1
                # v2.0.16 三色：流式期最后一组未封口思考轮 → 橙（V1 对齐），
                # 其余组恒绿（定稿轮）；finalize 重渲染收口为全绿。
                children.append(
                    _build_reasoning_round_title(
                        round_no, elapsed_by_line.get(idx, 0.0),
                        finalized=not (streaming and open_at_end and idx == last_group_start))
                )
            prev_thinking = True
            body = clean or text[len(_THINKING_PREFIX):].strip() or text
            # v2.0.12：思考正文走 lark_md 渲染（**bold** 等 markdown 语义生效），
            # 命令摘要仍走 plain_text（命令非 markdown 语义，见 _render 注释）。
            detail = _build_reasoning_body_detail(_truncate_reasoning_body(body))
            if detail is not None:
                children.append(detail)
            continue
        prev_thinking = False
        if kind == "tool":
            pending = text.endswith("...")
            if tool:
                title = tool_display_title(tool)
                label = (durations or {}).get(strip_dedup_suffix(text))
                if label and not pending:  # V1：已执行才带耗时（running 行无耗时可计）
                    title = f"{title} ({label})"
                children.append(_build_tool_step_title(
                    {"title": title,
                     "status": "running" if pending else "success",
                     "icon": resolve_tool_icon(tool)},
                ))
            if not pending:
                preview = preview_text(text)  # v2.0.6：包裹引号已在 preview_text 内剥除，不再误伤命令尾部引号
                if preview:
                    detail = _build_tool_step_detail({"detail": preview})
                    if detail is not None:
                        children.append(detail)
            if tool or preview_text(text):
                continue
        # v2.0.10：未识别通报行（限流/警告/后台通知等 emoji 开头且解析不出
        # 工具结构的行）→ 官方图标 + 统一 notation 样式；emoji 从正文剥除。
        # 其余真未知行维持裸 markdown 兑底（可能含用户 markdown 语义）。
        notice = split_notice_lead(text)
        if notice is not None:
            children.append(_build_notice_row(notice[0], notice[1]))
            continue
        children.append({"tag": "markdown", "content": text})
    return children or [{"tag": "markdown", "content": " "}]  # 飞书面板拒绝空 elements


def _build_notice_row(icon_token: str, body: str) -> dict[str, Any]:
    """通报行：官方 standard_icon + notation 正文（v2.0.10；v2.0.16 警告族橙色）.

    与工具标题行同构（div + standard_icon + notation 字号），正文用
    plain_text（与思考/预览详情同字体体系，无 markdown 注入面），
    保证未识别通报行与面板其余行视觉一致——用户验收要求“面板一律
    飞书官方图标 + 统一样式”（限流提示行 ⏱️ 裸 markdown 事故 2026-10-12）。

    v2.0.16：警告族（⚠️/🚨 → warning_outlined）图标与正文橙色——用户验收
    “警告类不能用灰色”（Model fallback 行 2026-10-13 截图）；其余通报行
    保持灰色。text_color 仅 plain_text 支持（v2.0.12 取证），orange 为
    V1 错误面板生产原语（elements._build_error_panel）。
    """
    warning = icon_token == "warning_outlined"
    color = "orange" if warning else "grey"
    return {
        "tag": "div",
        "icon": {
            "tag": "standard_icon",
            "token": icon_token,
            "color": color,
        },
        "text": {
            "tag": "plain_text",
            "content": body,
            "text_color": color,
            "text_size": _aux_text_size(),
        },
    }


def _apply_panel_title(panel: dict[str, Any], title: str) -> None:
    """覆写 collapsible_panel header 标题（V1 风格："agent loop · N 个工具"）."""
    if not title:
        return
    header = panel.get("header") or {}
    title_el = header.get("title") or {}
    title_el["content"] = title
    title_el["i18n_content"] = {"zh_cn": title, "en_us": title}
    header["title"] = title_el
    panel["header"] = header


def _enforce_card_element_limit(
    card: dict[str, Any],
    *,
    panel_element_id: str = UNIFIED_PANEL_ELEMENT_ID,
) -> dict[str, Any]:
    """Counts **all** tag objects in the card (including nested ones)."""
    threshold = _FEISHU_ELEMENT_LIMIT - _ELEMENT_LIMIT_MARGIN
    total = _count_tag_objects(card)
    if total <= threshold:
        return card

    # ── Find the unified panel element in card body ──
    body = card.get("body", {})
    elements = body.get("elements", [])
    panel = None
    for elem in elements:
        if elem.get("element_id") == panel_element_id and elem.get("tag") == "collapsible_panel":
            panel = elem
            break

    if panel is None:
        # No panel found — nothing to trim (answer/footer must not be trimmed)
        return card

    children: list[dict] = panel.get("elements", [])

    # ── Check if a collapse hint already exists ──
    hint_idx = None
    for i, child in enumerate(children):
        if isinstance(child.get("content"), str) and "已折叠" in child["content"]:
            hint_idx = i
            break
    _HINT_TEMPLATE = {"tag": "markdown", "content": "⚡ 还有 0 项已折叠", "text_size": _aux_text_size()}
    _HINT_TAG_COUNT = _count_tag_objects(_HINT_TEMPLATE)  # typically 1
    if hint_idx is None:
        total += _HINT_TAG_COUNT  # Reserve exact space for the new collapse hint

    # ── Trim oldest items from panel children until under threshold ──
    trimmed_count = 0
    while total > threshold and len(children) > 1:
        # Skip the collapse hint (first child if it ends with "已折叠")
        first_content = children[0].get("content", "")
        remove_idx = 1 if isinstance(first_content, str) and first_content.endswith("已折叠") else 0
        removed = children.pop(remove_idx)
        total -= _count_tag_objects([removed])
        trimmed_count += 1

    if trimmed_count > 0:
        # Update or add collapse hint
        # Re-find hint_idx (may have shifted due to removals)
        hint_idx = None
        for i, child in enumerate(children):
            if isinstance(child.get("content"), str) and "已折叠" in child["content"]:
                hint_idx = i
                break
        if hint_idx is not None:
            # Parse existing trimmed count from hint, then add new trimmed count
            # e.g. "⚡ 还有 5 项已折叠" → existing_count=5, trimmed_count=3 → "⚡ 还有 8 项已折叠"
            old_hint = children[hint_idx]["content"]
            # Extract the number before "项" — simple string parsing, no regex needed
            existing_count = 0
            _idx = old_hint.find("项")
            if _idx > 0:
                # Walk backwards skipping whitespace, then collect digits
                _end = _idx
                while _end > 0 and old_hint[_end - 1] == ' ':
                    _end -= 1
                _start = _end
                while _start > 0 and old_hint[_start - 1].isdigit():
                    _start -= 1
                if _start < _end:
                    existing_count = int(old_hint[_start:_end])
            total_trimmed = existing_count + trimmed_count
            children[hint_idx]["content"] = f"⚡ 还有 {total_trimmed} 项已折叠"
        else:
            children.insert(0, {
                "tag": "markdown",
                "content": f"⚡ 还有 {trimmed_count} 项已折叠",
                "text_size": _aux_text_size(),
            })

    # Update panel children in the card
    panel["elements"] = children
    return card

def build_streaming_card_v2(
    *,
    tool_steps: list[dict] | None = None,
    elapsed_ms: float = 0,
    show_tool_use: bool = True,
    show_reasoning: bool = False,
    show_streaming_element: bool = True,
    streaming_panel_expanded: bool = True,
    print_strategy: str = "delay",
    print_step: int = 4,
    include_unified_panel: bool = True,
    include_loading_hint: bool = False,  # v2.0.4: 已废弃，保留签名兼容（不再渲染）
    include_answer_element: bool = True,
    status_lines: list[str] | None = None,
    answer_text: str = "",
    panel_title: str = "",
    status_durations: dict[str, str] | None = None,  # v2.0.8：工具行耗时表
    status_times: list[float] | None = None,         # v2.0.8：行到达时间戳
    turn_state: str = "streaming",                   # v2.0.16：轮次三色（流式末轮橙/定稿全绿）
) -> dict[str, Any]:
    """Card lifecycle (v1.0.2+):

    ``status_lines``：V2 帧重组引擎的工具/思考/interim 状态行，
    v2.0.3 起按 V1 插件风格渲染（图标 + 彩色工具名 + 缩进灰色摘要）。
    ``answer_text``：已流出的 answer 缓冲——cardkit_update 全量更新时
    回填 answer 元素，防止工具帧刷新面板把已流内容重置为空。
    ``panel_title``：面板动态标题（V1: "agent loop · N 个工具"；空 → 静态 "agent loop"）。
    ``status_durations``：单工具耗时表（行 → "2.3 s"）；``status_times``：
    行到达时间戳（思考轮次耗时）。均 v2.0.8，缺失时对应装饰省略。
    ``turn_state``（v2.0.16）："streaming"（默认，本函数即流式卡工厂——末组
    未封口思考轮橙色）| "finalized"（定稿收口全绿，finalize 重渲染专用）。
    """
    elements: list[dict] = []

    # ── Unified panel (linear mode — single panel for reasoning+tools) ──
    # v2.0.24（用户反馈：无思考/工具调用时出现空 "agent loop" 折叠面板）：
    # 面板懒渲染——仅 status_lines 非空时才随卡片渲染。纯 answer 回合建卡
    # 不再挂空壳面板（v2.0.13 的「定稿时删空壳」从主清理路径退居防御性
    # 保险：正常路径不再产生空面板）。首个状态行到达时由 composer._patch_status
    # 全量更新（cardkit_update = card_json 全量替换）补插面板——真飞书 s172
    # 探针实证「更新时新增元素」合法（含流式卡建卡时未声明的元素）。
    if include_unified_panel and status_lines:
        panel = _build_unified_panel_placeholder(expanded=streaming_panel_expanded)
        _apply_panel_title(panel, panel_title)
        panel["elements"] = _render_status_children(
            status_lines, durations=status_durations, line_times=status_times,
            turn_state=turn_state)
        elements.append(panel)

    # ── Streaming answer element ──
    if show_streaming_element and include_answer_element:
        elements.append(_streaming_element(content=answer_text or "", element_id=ANSWER_ELEMENT_ID))

    # ── Loading hint: v2.0.4 移除（用户确认走平台后无需"正在加载上下文..."占位；
    #    v2.0.3 部署时曾从服务器热补丁回退，本版起仓库内永久移除）。
    #    spinner 保留作为封卡 insert_before 锚点 + 首卡 loading 指示。
    # ── Loading spinner ──
    elements.append(_loading_element())

    card: dict[str, Any] = {
        "schema": "2.0",
        "config": {
            "streaming_mode": True,
            "streaming_config": {
                "print_frequency_ms": {"default": 70},
                "print_step": {"default": print_step},
                "print_strategy": print_strategy,
            },
            # v2.0.20：自定义字号完全 opt-in——card_style.text_size 已配置时
            # 注入 config.style（s157 探针定案 CardKit 接受），未配置零注入。
            **_card_text_style_config(),
            "locales": _LOCALES,
            "summary": {
                "content": _T["processing"][0],
                "i18n_content": _t("processing"),
            },
        },
        "body": {"elements": elements},
    }
    # v2.0.9 P2-5：200 元素上限安全网接线（此前函数定义+导出但从未调用）。
    # 正常状态 ≤12 行 ≈ 30 元素，远低于阈值；裁剪仅在异常膨胀时生效
    # （裁最旧面板子项 + 「⚡ 还有 N 项已折叠」提示），answer/页脚不裁。
    return _enforce_card_element_limit(card)
