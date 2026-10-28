#!/usr/bin/env python3
"""飞书通知统一脚本（v2.0.27 起，取代 V1 scripts/notify_feishu.py）。

由 GitHub Actions 工作流调用，stdlib only，四个子命令：

  github-event    GitHub PR/Issue 事件实时卡片（.github/workflows/feishu-notify.yml）
  gitee-digest    Gitee PR/Issue 增量摘要卡片（sync-from-gitee.yml 内嵌，--state-file 存水位）
  ci-report       同步+测试结果报告卡（sync-from-gitee.yml，取代 V1 notify_feishu.py；
                  修复其 junitxml `find(...) or find(...)` 真值依赖子元素数量的解析缺陷）
  hermes-release  hermes-agent 上游发新版提醒（hermes-release-watch.yml）

公共环境变量：
  FEISHU_WEBHOOK      飞书群自定义机器人 webhook（缺失时告警退出 0——通知组件不炸 CI）
  FEISHU_SIGN_SECRET  可选；机器人开启签名校验时必填

gitee-digest 专用：
  GITEE_OWNER / GITEE_REPO / GITEE_TOKEN   Gitee API 访问（仓库私有也需要）
  --state-file PATH                        增量水位文件（工作流经 actions/cache 持久化）
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

GITEE_API = "https://gitee.com/api/v5"

# ---------------------------------------------------------------------------
# 飞书发送
# ---------------------------------------------------------------------------


def _sign() -> dict:
    secret = os.environ.get("FEISHU_SIGN_SECRET", "").strip()
    if not secret:
        return {}
    ts = str(int(time.time()))
    digest = hmac.new(f"{ts}\n{secret}".encode("utf-8"), digestmod=hashlib.sha256).digest()
    return {"timestamp": ts, "sign": base64.b64encode(digest).decode("utf-8")}


def send_card(title: str, header_template: str, md_text: str, button: dict | None = None) -> None:
    """发送 Card 2.0 消息卡。缺失 webhook 或发送失败仅告警，绝不抛出（通知不炸 CI）。"""
    webhook = os.environ.get("FEISHU_WEBHOOK", "").strip()
    if not webhook:
        print("[feishu_notify] FEISHU_WEBHOOK 未配置，跳过飞书通知")
        return
    elements: list[dict] = [{"tag": "div", "text": {"tag": "lark_md", "content": md_text}}]
    if button:
        elements.append({"tag": "hr"})
        elements.append({"tag": "action", "actions": [button]})
    payload = {
        **_sign(),
        "msg_type": "interactive",
        "card": {
            "schema": "2.0",
            "config": {"update_multi": True},
            "header": {"title": {"tag": "plain_text", "content": title}, "template": header_template},
            "body": {"elements": elements},
        },
    }
    req = urllib.request.Request(
        webhook, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if body.get("code", 0) != 0:
            print(f"[feishu_notify] 飞书返回异常: {body}", file=sys.stderr)
        else:
            print("[feishu_notify] 飞书通知已发送")
    except Exception as exc:  # noqa: BLE001 — 通知失败不影响工作流
        print(f"[feishu_notify] 飞书通知发送失败: {exc}", file=sys.stderr)


def _btn(text: str, url: str) -> dict:
    return {
        "tag": "button", "text": {"tag": "plain_text", "content": text},
        "url": url, "type": "primary",
    }


# ---------------------------------------------------------------------------
# 子命令：github-event
# ---------------------------------------------------------------------------


def cmd_github_event() -> None:
    """GitHub PR/Issue 事件 → 实时卡片。"""
    path = os.environ.get("GITHUB_EVENT_PATH", "")
    name = os.environ.get("GITHUB_EVENT_NAME", "")
    if not path or not Path(path).exists():
        print("[feishu_notify] GITHUB_EVENT_PATH 缺失，跳过")
        return
    event = json.loads(Path(path).read_text(encoding="utf-8"))
    action = event.get("action", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    base = f"https://github.com/{repo}"

    if name == "issues":
        issue = event.get("issue", {})
        num, title = issue.get("number"), issue.get("title", "")
        user = issue.get("user", {}).get("login", "")
        url = issue.get("html_url") or f"{base}/issues/{num}"
        template = {"opened": "green", "reopened": "orange", "closed": "grey"}.get(action, "grey")
        title_line = f"🆕 GitHub Issue #{num}" if action == "opened" else f"GitHub Issue #{num}"
        body = f"**{title}**\n仓库 `{repo}` · 作者 {user} · 动作 **{action}**"
        send_card(title_line, template, body, _btn("🔗 查看 Issue", url))
    elif name == "pull_request":
        pr = event.get("pull_request", {})
        num, title = pr.get("number"), pr.get("title", "")
        user = pr.get("user", {}).get("login", "")
        url = pr.get("html_url") or f"{base}/pull/{num}"
        merged = pr.get("merged", False)
        if action == "closed" and merged:
            action_label, template = "closed (merged)", "violet"
        else:
            action_label = action
            template = {"opened": "green", "reopened": "orange", "closed": "grey"}.get(action, "grey")
        title_line = f"🆕 GitHub PR #{num}" if action == "opened" else f"GitHub PR #{num}"
        body = f"**{title}**\n仓库 `{repo}` · 作者 {user} · 动作 **{action_label}**"
        send_card(title_line, template, body, _btn("🔗 查看 PR", url))
    else:
        print(f"[feishu_notify] 未支持的事件类型 {name}，跳过")


# ---------------------------------------------------------------------------
# 子命令：gitee-digest
# ---------------------------------------------------------------------------


def _parse_ts(raw: str) -> datetime | None:
    """Gitee 时间解析（"2026-10-27T12:00:00 +0800" / ISO 8601 双形态容错）。"""
    raw = (raw or "").strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S %z"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _fetch_gitee(kind: str, owner: str, repo: str, token: str) -> list[dict]:
    query = f"state=all&direction=desc&per_page=30&access_token={token}"
    if kind == "pulls":
        query += "&sort=updated"
    url = f"{GITEE_API}/repos/{owner}/{repo}/{kind}?{query}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def cmd_gitee_digest() -> None:
    """Gitee PR/Issue 增量摘要：updated_at > 上次水位才播报，无更新静默。"""
    owner = os.environ.get("GITEE_OWNER", "").strip()
    repo = os.environ.get("GITEE_REPO", "").strip()
    token = os.environ.get("GITEE_TOKEN", "").strip()
    state_file = Path(sys.argv[sys.argv.index("--state-file") + 1]) if "--state-file" in sys.argv else Path(".gitee-digest-state.json")
    if not owner or not repo:
        print("[feishu_notify] GITEE_OWNER/GITEE_REPO 未配置，跳过 gitee-digest")
        return

    state: dict = {}
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            state = {}
    last_raw = state.get("last_check")
    last = _parse_ts(last_raw) if last_raw else None
    if last is None:  # 首跑基线：只回看 24h，防全量轰炸
        last = datetime.now(timezone.utc) - timedelta(hours=24)

    items: list[tuple[str, dict]] = []
    try:
        for kind, label in (("pulls", "PR"), ("issues", "Issue")):
            for entry in _fetch_gitee(kind, owner, repo, token):
                updated = _parse_ts(entry.get("updated_at", ""))
                if updated is not None and updated > last:
                    items.append((label, entry))
    except Exception as exc:  # noqa: BLE001
        print(f"[feishu_notify] Gitee API 查询失败: {exc}", file=sys.stderr)
        return

    if not items:
        print("[feishu_notify] Gitee 无新 PR/Issue 更新，静默")
        _write_state(state_file)
        return

    def _line(label: str, entry: dict) -> str:
        st = entry.get("state", "")
        return (
            f"- **{label} #{entry.get('number')}** {entry.get('title', '')}"
            f"\n  `{entry.get('user', {}).get('login', '')}` · {st} · <{entry.get('html_url', '')}>"
        )

    prs = [x for x in items if x[0] == "PR"]
    issues = [x for x in items if x[0] == "Issue"]
    parts: list[str] = [f"自上次检查（{last.astimezone(timezone(timedelta(hours=8))).strftime('%m-%d %H:%M')} 北京时间）以来的更新："]
    if prs:
        parts.append(f"**PR（{len(prs)}）**\n" + "\n".join(_line("PR", e) for _, e in prs[:10]))
        if len(prs) > 10:
            parts.append(f"…另有 {len(prs) - 10} 个 PR")
    if issues:
        parts.append(f"**Issue（{len(issues)}）**\n" + "\n".join(_line("Issue", e) for _, e in issues[:10]))
        if len(issues) > 10:
            parts.append(f"…另有 {len(issues) - 10} 个 Issue")
    send_card(f"🪩 Gitee 动态（{len(items)} 条更新）", "yellow", "\n\n".join(parts), _btn("🔗 打开 Gitee", f"https://gitee.com/{owner}/{repo}"))
    _write_state(state_file)


def _write_state(state_file: Path) -> None:
    state_file.write_text(
        json.dumps({"last_check": datetime.now(timezone.utc).isoformat()}), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# 子命令：ci-report
# ---------------------------------------------------------------------------


def _plugin_version() -> str:
    try:
        for line in (Path(__file__).resolve().parent.parent / "plugin.yaml").read_text(encoding="utf-8").splitlines():
            if line.startswith("version:"):
                return line.split(":", 1)[1].strip().strip('"').strip("'")
    except Exception:  # noqa: BLE001
        pass
    return "unknown"


def _summarize_junit(path: str) -> tuple[int, int, list[str]]:
    """返回 (total, failed, 失败行列表)。逐 testcase 显式 is not None 判定
    （V1 notify_feishu.py 的 `find(...) or find(...)` 在无子元素时真值歧义——已修复）。"""
    total, failed, lines = 0, 0, []
    if not Path(path).exists():
        return total, failed, lines
    try:
        root = ET.parse(path).getroot()
        for tc in root.iter("testcase"):
            total += 1
            fail = tc.find("failure")
            err = tc.find("error")
            if fail is None and err is None:
                continue
            failed += 1
            node = fail if fail is not None else err
            msg = (node.get("message") or "") if node is not None else ""
            lines.append(f"- `{tc.get('name', 'unknown')}`: {msg[:80]}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"- ⚠️ 无法解析 {path}: {exc}")
    return total, failed, lines


def cmd_ci_report() -> None:
    """同步+测试结果报告卡（取代 V1 notify_feishu.py）。"""
    exit_code = os.environ.get("TEST_EXIT_CODE", "1")
    repo = os.environ.get("REPO") or os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("RUN_ID", "")
    server = os.environ.get("SERVER_URL", "https://github.com")
    trigger = os.environ.get("TRIGGER", "")
    unit_t, unit_f, unit_l = _summarize_junit("unit_test_report.xml")
    e2e_t, e2e_f, e2e_l = _summarize_junit("e2e_report.xml")
    total, failed = unit_t + e2e_t, unit_f + e2e_f

    ok = exit_code == "0"
    template = "green" if ok else "red"
    status = "✅ 通过" if ok else "❌ 失败"
    summary = f"{total} 用例 · {failed} 失败" if total else "未运行"
    trigger_label = "⏰ 定时同步" if trigger == "schedule" else "👆 手动触发"

    body_lines = [
        f"**插件版本**: v{_plugin_version()}　**测试**: {status}（{summary}）　**触发**: {trigger_label}",
    ]
    changed: list[str] = []
    if Path("changed_files.txt").exists():
        changed = [x.strip() for x in Path("changed_files.txt").read_text(encoding="utf-8").splitlines() if x.strip()]
    if changed:
        shown = "\n".join(f"- `{f}`" for f in changed[:10])
        if len(changed) > 10:
            shown += f"\n- …共 {len(changed)} 个文件"
        body_lines.append(f"**变更文件**\n{shown}")
    failure_lines = unit_l + e2e_l
    if failure_lines:
        body_lines.append("**失败用例**\n" + "\n".join(failure_lines[:15]))
    button = _btn("🔗 查看 Actions", f"{server}/{repo}/actions/runs/{run_id}") if repo and run_id else None
    send_card("Gitee→GitHub 同步报告", template, "\n\n".join(body_lines), button)


# ---------------------------------------------------------------------------
# 子命令：hermes-release
# ---------------------------------------------------------------------------


def cmd_hermes_release() -> None:
    """hermes-agent 上游新版提醒（继承面兼容性需人工 E2E 验证）。"""
    latest = os.environ.get("HERMES_LATEST", "unknown")
    last = os.environ.get("HERMES_LAST", "none")
    body = (
        f"**hermes-agent** 发布新版本：**{latest}**（上次已记录：{last}）\n\n"
        "建议动作：\n"
        "1. 在测试服务器 checkout 新版本并重启网关；\n"
        "2. 跑 `scripts/e2e_v2.py --scenario full,reply` 真飞书验证；\n"
        "3. 插件继承面 fail-fast 探测会在启动日志明确报出失配点（若有）；\n"
        "4. 验证通过后再升级生产网关。"
    )
    send_card(f"⬆️ hermes-agent 新版本 {latest}", "orange", body, _btn("🔗 查看发布页", "https://github.com/NousResearch/hermes-agent/releases"))


# ---------------------------------------------------------------------------

COMMANDS = {
    "github-event": cmd_github_event,
    "gitee-digest": cmd_gitee_digest,
    "ci-report": cmd_ci_report,
    "hermes-release": cmd_hermes_release,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(f"用法: {sys.argv[0]} <{'|'.join(COMMANDS)}>", file=sys.stderr)
        sys.exit(2)
    COMMANDS[sys.argv[1]]()
