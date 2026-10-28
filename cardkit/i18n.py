"""飞书卡片 i18n — 中英双语文本映射."""

from __future__ import annotations

__all__ = [
    "_LOCALES",
    "_T",
    "_i18n",
    "_t",
]

_LOCALES = ["zh_cn", "en_us"]

_T: dict[str, tuple[str, str]] = {
    "status_completed": ("Completed", "已完成"),
    "status_error": ("Error", "出错"),
    "status_stopped": ("Stopped", "已停止"),
    "elapsed": ("Elapsed {}", "耗时 {}"),
    "context": ("Context {}", "上下文 {}"),
    "processing": ("Processing...", "处理中..."),
    "api_calls": ("API", "API"),
    "history_offset": ("Offset", "偏移量"),
    "compression_exhausted": ("⚠ Context Full", "⚠ 上下文已满"),
    "cache": ("Cache {}", "缓存 {}"),
    # ── Context loading hint (first card only, removed on first token) ──
    "loading_context": ("Loading context...", "正在加载上下文..."),
    # ── Clarify interactive card (three-state: pending / submitted / confirmed) ──
    "clarify_select_placeholder": ("Quick select...", "快速选择..."),
    "clarify_input_placeholder": ("Type your answer...", "请输入你的回答..."),
    "clarify_selected": ("Selected: {}", "已选择: {}"),
    "clarify_submitted": ("Submitted, awaiting confirmation...", "已提交，等待确认..."),
    "clarify_retry": ("Retry submission", "重试提交"),
    "clarify_confirmed": ("Confirmed", "已确认"),
    # v2.0.21：过期/重启后点击澄清卡的回调提示（clarify._expired_response）
    "clarify_expired": ("This clarify card has expired or the session was restarted. Please ask again.",
                        "该澄清卡片已过期或会话已重启，请重新发起提问。"),
    "cost_estimated": ("${} (est.)", "${} (估算)"),
    "cost_actual": ("${} (actual)", "${} (实报)"),
    "cost_included": ("Free", "免费"),
    # ── Unified panel i18n ──
    "agent_process": ("agent loop", "agent loop"),
    "rounds": ("{} rounds", "{} 轮"),
    "tools_count": ("{} tools", "{} 个工具"),
    "round_n": ("Round {}", "第 {} 轮"),
    # v2.0.26：clarify 暂停行（engine.suspend_for_clarify → 面板状态行）。
    # 行语言取 en（与面板既有 hermes 进度行「🔍 Searching...」等一致——
    # status_lines 是单语文本通道，卡片级 i18n 仅标题/页脚元素支持）。
    # emoji ⏸ 与 hermes 边界占位（💬 Waiting for your choice...）刻意区分：
    # 面板行语法（classify._is_progress_frame_text）把 💬 行归思考块，⏸ 不冲突。
    "clarify_wait": ("⏸ Waiting for your choice...", "⏸ 等待你的选择…"),
}

def _i18n(en: str, zh: str) -> dict[str, str]:
    return {"zh_cn": zh, "en_us": en}

def _t(key: str) -> dict[str, str]:
    """简写: _t("processing") → _i18n(*_T["processing"])。"""
    return _i18n(*_T[key])
