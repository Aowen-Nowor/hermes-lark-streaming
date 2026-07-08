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
    "processing_prefix": ("💭 Processing...", "💭 处理中..."),
    "thought": ("Thought", "思考"),
    "thinking_panel": ("Thinking", "思考中"),
    "thought_for": ("Thought for {}", "思考了 {}"),
    "done": ("Done.", "完成。"),
    "api_calls": ("API", "API"),
    "history_offset": ("Offset", "偏移量"),
    "error_panel": ("Error", "错误信息"),
    "interrupt_panel": ("Interrupted", "中断信息"),
    "compression_exhausted": ("⚠ Context Full", "⚠ 上下文已满"),
    "cache": ("Cache {}", "缓存 {}"),
    "bg_review_panel": ("Review", "审查"),
    "partial_continues": ("Continues in next message", "内容未完，继续在下一条消息"),
    # ── Context loading hint (first card only, removed on first token) ──
    "loading_context": ("Loading context...", "正在加载上下文..."),
    # ── Clarify interactive card (three-state: pending / submitted / confirmed) ──
    "clarify_select_placeholder": ("Quick select...", "快速选择..."),
    "clarify_input_placeholder": ("Type your answer...", "请输入你的回答..."),
    "clarify_selected": ("Selected: {}", "已选择: {}"),
    "clarify_submitted": ("Submitted, awaiting confirmation...", "已提交，等待确认..."),
    "clarify_retry": ("Retry submission", "重试提交"),
    "clarify_confirmed": ("Confirmed", "已确认"),
    "cost_estimated": ("${} (est.)", "${} (估算)"),
    "cost_actual": ("${} (actual)", "${} (实报)"),
    "cost_included": ("Free", "免费"),
    # ── Unified panel i18n ──
    "agent_process": ("agent loop", "agent loop"),
    "rounds": ("{} rounds", "{} 轮"),
    "tools_count": ("{} tools", "{} 个工具"),
    "round_n": ("Round {}", "第 {} 轮"),
}

def _i18n(en: str, zh: str) -> dict[str, str]:
    return {"zh_cn": zh, "en_us": en}

def _t(key: str) -> dict[str, str]:
    """简写: _t("processing") → _i18n(*_T["processing"])。"""
    return _i18n(*_T[key])
