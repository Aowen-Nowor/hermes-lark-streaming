"""Commands: help, status, monitor, monitor reset, config reload. Bypasses Hermes AI"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

_logger = logging.getLogger("hermes_lark_streaming")

# Pitfall: Feishu API calls run in real OS threads; concurrent increments
_metrics_lock = threading.Lock()

# Hold strong refs to fire-and-forget tasks (prevent mid-execution GC).
_aowen_pending_tasks: set = set()

_metrics: dict[str, Any] = {
    "cards_created": 0,
    "cards_completed": 0,
    "cards_failed": 0,
    "cards_aborted": 0,
    "api_calls": 0,
    "api_errors": 0,
    "stream_element_calls": 0,
    "stream_element_failures": 0,
    "batch_update_calls": 0,
    "active_sessions": 0,
    "started_at": time.time(),
}

_error_codes: dict[int, int] = {}  # error_code → count

def record_card_created() -> None:
    with _metrics_lock:
        _metrics["cards_created"] += 1

def record_card_completed() -> None:
    with _metrics_lock:
        _metrics["cards_completed"] += 1

def record_card_failed() -> None:
    with _metrics_lock:
        _metrics["cards_failed"] += 1

def record_card_aborted() -> None:
    with _metrics_lock:
        _metrics["cards_aborted"] += 1

def record_api_call(operation: str) -> None:
    with _metrics_lock:
        _metrics["api_calls"] += 1
        if operation == "cardkit_stream_element":
            _metrics["stream_element_calls"] += 1
        elif operation == "cardkit_batch_update":
            _metrics["batch_update_calls"] += 1

def record_api_error(code: int, operation: str = "") -> None:
    with _metrics_lock:
        _metrics["api_errors"] += 1
        _error_codes[code] = _error_codes.get(code, 0) + 1
        if operation == "cardkit_stream_element":
            _metrics["stream_element_failures"] += 1

def set_active_sessions(count: int) -> None:
    with _metrics_lock:
        _metrics["active_sessions"] = count

def get_metrics() -> dict[str, Any]:
    """Get current metrics snapshot."""
    with _metrics_lock:
        uptime = time.time() - _metrics["started_at"]
        return {
            **_metrics,
            "uptime_seconds": round(uptime, 1),
            "uptime_human": _format_uptime(uptime),
            "error_codes": dict(_error_codes),
        }

def _format_uptime(seconds: float) -> str:
    """Format uptime as human-readable string."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m {int(seconds % 60)}s"
    hours = int(minutes // 60)
    return f"{hours}h {minutes % 60}m"

def _get_version() -> str:
    """Get plugin version from plugin.yaml."""
    try:
        from pathlib import Path
        yaml_path = Path(__file__).resolve().parent.parent / "plugin.yaml"
        if yaml_path.exists():
            for line in yaml_path.read_text(encoding="utf-8").splitlines():
                if line.startswith("version:"):
                    return line.split(":", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return "unknown"

# Color: green=success, orange=warning, red=error, blue=info, grey=neutral.
_ICON_TOKENS: dict[str, str] = {
    "info": "info_outlined",
    "success": "resolve_filled",
    "warning": "time_outlined",
    "locked": "lock_outlined",
    "agent": "robot-add_outlined",
    "collapse": "down-small-ccm_outlined",
}

# v2 font color names.
_COLOR_MAP: dict[str, str | None] = {
    "default": None,
    "grey": "grey",
    "blue": "blue",
    "green": "green",
    "orange": "orange-300",
    "red": "red",
    "turquoise": "turquoise",
}

def _icon_div(icon_key: str, content: str, *, icon_color: str = "grey", text_size: str = "normal", text_color: str | None = None, icon_size: str = "16px 16px") -> dict:
    """Build div with standard_icon + lark_md text."""
    token = _ICON_TOKENS.get(icon_key, "info_outlined")
    text: dict = {"tag": "lark_md", "content": content, "text_size": text_size}
    if text_color:
        text["text_color"] = text_color
    return {
        "tag": "div",
        "icon": {
            "tag": "standard_icon",
            "token": token,
            "size": icon_size,
            "color": _COLOR_MAP.get(icon_color, "grey"),
        },
        "text": text,
    }

def _metric_block(label: str, value: Any, *, icon_key: str = "info", color: str = "default") -> dict:
    """Build metric block: icon + grey label + bold colored value."""
    font_color = _COLOR_MAP.get(color)
    if font_color:
        value_md = f"<font color='{font_color}'>**{value}**</font>"
    else:
        value_md = f"**{value}**"
    token = _ICON_TOKENS.get(icon_key, "info_outlined")
    return {
        "tag": "div",
        "icon": {
            "tag": "standard_icon",
            "token": token,
            "size": "20px 20px",
            "color": font_color or "grey",
        },
        "text": {
            "tag": "lark_md",
            "content": f"<font color='grey'>{label}</font>\n{value_md}",
        },
    }

def _two_col(left: dict, right: dict) -> dict:
    """Build 2-column responsive row (stacks on mobile)."""
    return {
        "tag": "column_set",
        "flex_mode": "stretch",
        "columns": [
            {"tag": "column", "width": "weighted", "weight": 1, "elements": [left]},
            {"tag": "column", "width": "weighted", "weight": 1, "elements": [right]},
        ],
    }

def _three_col(c1: dict, c2: dict, c3: dict) -> dict:
    """Build 3-column responsive row (stacks on mobile)."""
    return {
        "tag": "column_set",
        "flex_mode": "stretch",
        "columns": [
            {"tag": "column", "width": "weighted", "weight": 1, "elements": [c1]},
            {"tag": "column", "width": "weighted", "weight": 1, "elements": [c2]},
            {"tag": "column", "width": "weighted", "weight": 1, "elements": [c3]},
        ],
    }

def _section_title(text: str, *, color: str = "grey") -> dict:
    """Build section title — bold colored small text."""
    return {
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": f"**{text}**",
            "text_color": _COLOR_MAP.get(color, "grey") or "grey",
            "text_size": "notation",
        },
    }

def _fold(title: str, elements: list[dict], *, expanded: bool = False, border_color: str = "grey") -> dict:
    """Build collapsible panel."""
    return {
        "tag": "collapsible_panel",
        "expanded": expanded,
        "header": {
            "title": {
                "tag": "plain_text",
                "content": title,
                "text_color": "grey",
                "text_size": "notation",
            },
            "vertical_align": "center",
            "icon": {
                "tag": "standard_icon",
                "token": "down-small-ccm_outlined",
                "size": "16px 16px",
                "color": "grey",
            },
            "icon_position": "right",
            "icon_expanded_angle": -180,
        },
        "border": {"color": border_color, "corner_radius": "5px"},
        "vertical_spacing": "4px",
        "padding": "8px 8px 8px 8px",
        "elements": elements,
    }

def _footer_note(content: str) -> dict:
    """Build footer note — small grey text."""
    return {
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": content,
            "text_size": "notation",
            "text_color": "grey",
        },
    }

# ── Card builders ──

def build_help_card() -> dict[str, Any]:
    """Build help card — query (read-only) and action (state-changing) commands."""
    version = _get_version()

    query_cmds = [
        ("help", "显示本帮助信息", "info"),
        ("status", "查看插件状态与当前配置", "info"),
        ("monitor", "查看监控面板（卡片数、API 调用等）", "info"),
    ]
    action_cmds = [
        ("monitor reset", "重置监控统计计数器", "warning"),
        ("config reload", "修改 config.yaml 后重新加载配置", "warning"),
    ]

    elements: list[dict] = [
        _icon_div("info", f"**hermes-lark-streaming** v{version}",
                  icon_color="blue", icon_size="20px 20px"),
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "所有命令以 `/aowen` 开头，不经过 Hermes AI，直接由插件处理。无参数的 `/aowen` 同 `/aowen help`。",
                "text_size": "notation",
                "text_color": "grey",
            },
        },
        {"tag": "hr"},
        _section_title("查询类命令（只读，安全）", color="blue"),
    ]

    for cmd, desc, icon_key in query_cmds:
        elements.append(_icon_div(
            icon_key, f"`/aowen {cmd}` — {desc}", icon_color="blue",
        ))

    elements.append({"tag": "hr"})
    elements.append(_section_title("操作类命令（会修改状态）", color="orange"))

    for cmd, desc, icon_key in action_cmds:
        elements.append(_icon_div(
            icon_key, f"`/aowen {cmd}` — {desc}", icon_color="orange",
        ))

    elements.append({"tag": "hr"})
    elements.append(_footer_note("发送 `/aowen <命令名>` 使用对应功能。"))

    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": "插件命令帮助"}, "template": "blue"},
        "body": {"elements": elements},
    }

def build_status_card() -> dict[str, Any]:
    """Build status card — 3 metrics, patch status, collapsible config."""
    try:
        from ..controller import get_controller
        from ..patching import _patch_status
        from ..config import Config

        ctrl = get_controller()
        cfg = Config()

        ctrl_ready = ctrl.enabled and ctrl._client_ok()
        creds_status = ("已就绪", "success") if ctrl_ready else ("未就绪", "error")
        active_count = ctrl._sess_active_count()

        from .. import __version__ as plugin_version

        # ── Top: 3 key metrics ──
        version_block = _metric_block("版本", f"v{plugin_version}", icon_key="info", color="blue")
        creds_block = _metric_block(
            "飞书客户端", creds_status[0],
            icon_key="success" if ctrl_ready else "locked",
            color=creds_status[1],
        )
        active_block = _metric_block(
            "活跃会话", active_count, icon_key="agent",
            color="orange" if active_count > 0 else "default",
        )

        # ── Patch status section ──
        patch_elements: list[dict] = []
        if _patch_status:
            for key, val in _patch_status.items():
                if key in ("version", "hermes_layout"):
                    continue
                if val in ("✓", "applied"):
                    patch_elements.append(_icon_div(
                        "success", f"`{key}` · {val}",
                        icon_color="green", text_size="notation",
                    ))
                elif "pending" in str(val):
                    patch_elements.append(_icon_div(
                        "warning", f"`{key}` · {val}",
                        icon_color="orange", text_size="notation",
                    ))
                else:
                    patch_elements.append(_icon_div(
                        "locked", f"`{key}` · {val}",
                        icon_color="red", text_size="notation",
                    ))
        else:
            patch_elements.append(_icon_div(
                "warning", "补丁状态不可用",
                icon_color="orange", text_size="notation",
            ))

        # ── Config section (collapsible, grouped by category) ──
        has_creds = bool(cfg.feishu_app_id or cfg.env_app_id)

        streaming_cfg = [
            f"`enabled`: `{cfg.enabled}`",
            f"`linear`: `{cfg.linear}`",
            f"`flush_interval_ms`: `{cfg.flush_interval_ms}`",
            f"`card_ttl_sec`: `{cfg.card_duration_sec}`",
            f"`print_strategy`: `{cfg.print_strategy}`",
            f"`print_step`: `{cfg.print_step}`",
        ]
        card_cfg = [
            f"`gateway_cards`: `{cfg.gateway_cards}`",
            f"`panel_expanded`: `{cfg.panel_expanded}`",
            f"`streaming_panel_expanded`: `{cfg.streaming_panel_expanded}`",
            f"`show_reasoning`: `{cfg.show_reasoning}`",
        ]
        limit_cfg = [
            f"`max_tool_steps`: `{cfg.max_tool_steps}`",
            f"`max_reasoning_rounds`: `{cfg.max_reasoning_rounds}`",
            f"`footer_show_label`: `{cfg.footer_show_label}`",
        ]
        creds_cfg = [
            f"`feishu_credentials`: `{'已配置' if has_creds else '未配置'}`",
        ]

        config_elements: list[dict] = [
            _section_title("流式控制", color="blue"),
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(streaming_cfg), "text_size": "notation"}},
            _section_title("卡片行为", color="blue"),
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(card_cfg), "text_size": "notation"}},
            _section_title("数量限制", color="blue"),
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(limit_cfg), "text_size": "notation"}},
            _section_title("凭证", color="blue"),
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(creds_cfg), "text_size": "notation"}},
        ]

        return {
            "schema": "2.0",
            "config": {"update_multi": True},
            "header": {
                "title": {"tag": "plain_text", "content": "插件状态"},
                "template": "green" if ctrl_ready else "red",
            },
            "body": {
                "elements": [
                    _three_col(version_block, creds_block, active_block),
                    {"tag": "hr"},
                    _section_title("补丁应用状态", color="grey"),
                    *patch_elements,
                    {"tag": "hr"},
                    _fold("当前配置（点击展开）", config_elements, expanded=False),
                    {"tag": "hr"},
                    _footer_note("修改 config.yaml 后发送 `/aowen config reload` 生效。"),
                ],
            },
        }
    except Exception:
        _logger.error("HLS: build_status_card error", exc_info=True)
        return {
            "schema": "2.0",
            "config": {"update_multi": True},
            "header": {"title": {"tag": "plain_text", "content": "插件状态"}, "template": "red"},
            "body": {"elements": [
                _icon_div("locked", "状态卡片构建失败，请查看日志", icon_color="red"),
            ]},
        }

def build_monitor_card() -> dict[str, Any]:
    """Build monitor card — top summary, lifecycle, API calls, error codes."""
    m = get_metrics()
    version = _get_version()

    api_err_color = "error" if m["api_errors"] > 0 else "default"
    stream_fail_color = "error" if m["stream_element_failures"] > 0 else "default"
    failed_color = "error" if m["cards_failed"] > 0 else "default"

    elements: list[dict] = [
        _three_col(
            _metric_block("版本", f"v{version}", icon_key="info", color="blue"),
            _metric_block("运行时间", m["uptime_human"], icon_key="warning", color="default"),
            _metric_block("活跃会话", m["active_sessions"], icon_key="agent",
                          color="orange" if m["active_sessions"] > 0 else "default"),
        ),
        {"tag": "hr"},
        _section_title("卡片生命周期", color="blue"),
        _two_col(
            _metric_block("创建", m["cards_created"], icon_key="info", color="default"),
            _metric_block("已完成", m["cards_completed"], icon_key="success", color="green"),
        ),
        _two_col(
            _metric_block("失败", m["cards_failed"], icon_key="locked", color=failed_color),
            _metric_block("已停止", m["cards_aborted"], icon_key="warning", color="default"),
        ),
        {"tag": "hr"},
        _section_title("API 调用", color="blue"),
        _two_col(
            _metric_block("总调用", m["api_calls"], icon_key="info", color="default"),
            _metric_block("错误", m["api_errors"], icon_key="locked", color=api_err_color),
        ),
        _two_col(
            _metric_block("流式调用", m["stream_element_calls"], icon_key="info", color="default"),
            _metric_block("流式失败", m["stream_element_failures"], icon_key="locked", color=stream_fail_color),
        ),
        _two_col(
            _metric_block("批量更新", m["batch_update_calls"], icon_key="info", color="default"),
            _metric_block("活跃会话", m["active_sessions"], icon_key="agent", color="orange" if m["active_sessions"] > 0 else "default"),
        ),
    ]

    if m["error_codes"]:
        err_elements: list[dict] = [_section_title("错误码分布", color="orange")]
        total_err = sum(m["error_codes"].values()) or 1
        for code, count in sorted(m["error_codes"].items()):
            pct = count * 100 // total_err
            err_elements.append(_icon_div(
                "locked",
                f"`{code}` · {count} 次 · {pct}%",
                icon_color="red", text_size="notation",
            ))
        elements.append({"tag": "hr"})
        elements.extend(err_elements)

    elements.append({"tag": "hr"})
    elements.append(_footer_note(
        f"更新时间: {time.strftime('%Y-%m-%d %H:%M:%S')} · "
        f"发送 `/aowen monitor` 刷新 · `/aowen monitor reset` 重置"
    ))

    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": "插件监控面板"}, "template": "blue"},
        "body": {"elements": elements},
    }

def build_reset_card() -> dict[str, Any]:
    """Build reset confirmation card."""
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": "统计已重置"},
            "template": "green",
        },
        "body": {
            "elements": [
                _icon_div("success", "**所有计数器已清零，运行时间重新计时。**",
                          icon_color="green", icon_size="20px 20px"),
                {"tag": "hr"},
                _footer_note("现在发送 `/aowen monitor` 查看重置后的数据。"),
            ],
        },
    }

def _build_unknown_command_card(subcommand: str) -> dict:
    """Build unknown command card."""
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": "未知命令"},
            "template": "orange",
        },
        "body": {
            "elements": [
                _icon_div("warning", f"未知命令: `/aowen {subcommand}`",
                          icon_color="orange", icon_size="20px 20px"),
                {"tag": "hr"},
                _footer_note("发送 `/aowen help` 查看可用命令列表。"),
            ],
        },
    }

def build_interrupt_hint_card() -> dict[str, Any]:
    """Build hint card shown when /aowen sent during active agent run."""
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": "AI 正在回复中"},
            "template": "orange",
        },
        "body": {
            "elements": [
                _icon_div(
                    "warning",
                    "**AI 正在回复上一条消息，无法处理 /aowen 命令。**",
                    icon_color="orange", icon_size="20px 20px",
                ),
                {"tag": "hr"},
                _icon_div(
                    "info",
                    "请等待当前回复完成，或发送 `/stop` 中断后再使用 `/aowen` 命令。",
                    icon_color="blue",
                ),
                {"tag": "hr"},
                _footer_note("当前命令已被忽略，未发送给 AI。"),
            ],
        },
    }

def _handle_config_reload() -> dict:
    """Handle /aowen config reload."""
    try:
        from ..config import Config
        cfg = Config()
        cfg.reload()
        _logger.info("HLS: config reloaded via /aowen config reload")
        return {
            "schema": "2.0",
            "config": {"update_multi": True},
            "header": {
                "title": {"tag": "plain_text", "content": "配置已重新加载"},
                "template": "green",
            },
            "body": {
                "elements": [
                    _icon_div("success", "**配置已重新加载，新配置已立即生效。**",
                              icon_color="green", icon_size="20px 20px"),
                    {"tag": "hr"},
                    _footer_note(f"加载时间: {time.strftime('%Y-%m-%d %H:%M:%S')}"),
                ],
            },
        }
    except Exception as e:
        _logger.error("HLS: config reload failed", exc_info=True)
        err_text = str(e)[:500]
        return {
            "schema": "2.0",
            "config": {"update_multi": True},
            "header": {
                "title": {"tag": "plain_text", "content": "配置加载失败"},
                "template": "red",
            },
            "body": {
                "elements": [
                    _icon_div("locked", "**配置加载失败，插件继续使用旧配置。**",
                              icon_color="red", icon_size="20px 20px"),
                    {"tag": "hr"},
                    _fold("错误详情（点击展开）", [
                        {"tag": "div", "text": {"tag": "lark_md",
                          "content": f"```\n{err_text}\n```", "text_size": "notation"}},
                    ], expanded=False, border_color="red"),
                ],
            },
        }

def handle_pre_gateway_dispatch(event: Any, gateway: Any = None, **kwargs) -> dict | None:
    """Handle /aowen commands."""
    try:
        text = getattr(event, "text", "") or ""
        text_stripped = text.strip()

        if not text_stripped.lower().startswith("/aowen"):
            return None

        source = getattr(event, "source", None)
        platform = getattr(getattr(source, "platform", None), "value", "")
        if platform != "feishu":
            return None

        chat_id = getattr(source, "chat_id", "") if source else ""
        if not chat_id:
            _logger.warning("HLS: /aowen command but no chat_id")
            return None

        # Parse subcommand: /aowen <sub> [arg]
        parts = text_stripped.split(None, 2)
        subcommand = parts[1].strip().lower() if len(parts) > 1 else "help"
        sub_arg = parts[2].strip().lower() if len(parts) > 2 else ""

        _logger.info("HLS: /aowen %s %s command detected, chat=%s", subcommand, sub_arg, chat_id[:12])

        if subcommand == "help" or subcommand == "":
            _send_card_async(chat_id, build_help_card(), "help")
            return _skip("/aowen help handled")

        elif subcommand == "status":
            _send_card_async(chat_id, build_status_card(), "status")
            return _skip("/aowen status handled")

        elif subcommand == "config" and sub_arg == "reload":
            card = _handle_config_reload()
            _send_card_async(chat_id, card, "config_reload")
            return _skip("/aowen config reload handled")

        elif subcommand == "monitor":
            if sub_arg == "reset":
                _do_reset()
                _send_card_async(chat_id, build_reset_card(), "reset")
                return _skip("/aowen monitor reset handled")
            else:
                _send_card_async(chat_id, build_monitor_card(), "monitor")
                return _skip("/aowen monitor handled")

        else:
            _send_card_async(chat_id, _build_unknown_command_card(subcommand), "unknown")
            return _skip(f"/aowen {subcommand} unknown")

    except Exception:
        # Pitfall: must return skip to block dispatch, else /aowen foo reaches LLM.
        _logger.exception("HLS: /aowen handler error — suppressing agent dispatch")
        return _skip("/aowen handler error suppressed")

def _do_reset() -> None:
    """Reset metrics counters."""
    global _metrics, _error_codes

    with _metrics_lock:
        old_created = _metrics["cards_created"]
        old_errors = _metrics["api_errors"]

        _metrics = {
            "cards_created": 0,
            "cards_completed": 0,
            "cards_failed": 0,
            "cards_aborted": 0,
            "api_calls": 0,
            "api_errors": 0,
            "stream_element_calls": 0,
            "stream_element_failures": 0,
            "batch_update_calls": 0,
            "active_sessions": _metrics["active_sessions"],
            "started_at": time.time(),
        }
        _error_codes.clear()

    _logger.info("HLS: metrics reset (was: created=%d, errors=%d)", old_created, old_errors)

def _send_card_async(chat_id: str, card: dict, cmd_name: str) -> None:
    """Send card fire-and-forget via FeishuClient. Lazy-inits if not yet ready."""
    import asyncio
    from ..controller import get_controller

    ctrl = get_controller()
    if not ctrl.enabled:
        _logger.warning("HLS: /aowen %s but controller not enabled", cmd_name)
        return

    # Pitfall: get_event_loop() raises RuntimeError in Python 3.14 when no loop running.
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _logger.warning("HLS: /aowen %s but no running event loop", cmd_name)
        return

    if not loop.is_running():
        _logger.warning("HLS: /aowen %s but event loop not running", cmd_name)
        return

    async def _init_and_send():
        try:
            if not ctrl._client_ok():
                await ctrl._ensure_init()
            if not ctrl._client_ok():
                _logger.warning("HLS: /aowen %s but controller init failed", cmd_name)
                return
            await ctrl._client.send_card_to_chat(chat_id, card)
            _logger.info("HLS: %s card sent to chat=%s", cmd_name, chat_id[:12])
        except Exception:
            _logger.error("HLS: failed to send %s card", cmd_name, exc_info=True)

    # Hold strong ref to prevent mid-execution GC.
    task = loop.create_task(_init_and_send())
    _aowen_pending_tasks.add(task)
    task.add_done_callback(_aowen_pending_tasks.discard)

def _skip(reason: str) -> dict:
    return {"action": "skip", "reason": reason}

__all__ = [
    "record_card_created",
    "record_card_completed",
    "record_card_failed",
    "record_card_aborted",
    "record_api_call",
    "record_api_error",
    "set_active_sessions",
    "get_metrics",
    "build_monitor_card",
    "build_status_card",
    "build_help_card",
    "build_reset_card",
    "build_interrupt_hint_card",
    "handle_pre_gateway_dispatch",
]
