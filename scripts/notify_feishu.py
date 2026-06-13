"""飞书通知脚本 — 由 GitHub Actions workflow 调用，环境变量传入参数。"""

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.request
import xml.etree.ElementTree as ET

# ── 从 plugin.yaml 读取版本号 ──

PLUGIN_VERSION = "unknown"
try:
    _script_dir = os.path.dirname(os.path.abspath(__file__))
    _plugin_yaml = os.path.join(_script_dir, "..", "plugin.yaml")
    with open(_plugin_yaml, "r") as _f:
        for _line in _f:
            if _line.strip().startswith("version:"):
                PLUGIN_VERSION = _line.split(":", 1)[1].strip().strip('"').strip("'")
                break
except Exception:
    pass

# ── 从环境变量读取配置 ──

FEISHU_WEBHOOK = os.environ["FEISHU_WEBHOOK"]
FEISHU_SECRET = os.environ["FEISHU_SECRET"]
TEST_EXIT_CODE = os.environ.get("TEST_EXIT_CODE", "")
REPO = os.environ.get("REPO", "")
RUN_ID = os.environ.get("RUN_ID", "")
SERVER_URL = os.environ.get("SERVER_URL", "https://github.com")
TRIGGER = os.environ.get("TRIGGER", "")
FORCE = os.environ.get("FORCE", "false")

run_url = f"{SERVER_URL}/{REPO}/actions/runs/{RUN_ID}"
trigger_label = "⏰ 定时同步" if TRIGGER == "schedule" else "👆 手动触发"
if FORCE == "true":
    trigger_label += "（强制测试）"


# ── 飞书签名 ──

timestamp = str(int(time.time()))
string_to_sign = f"{timestamp}\n{FEISHU_SECRET}"
hmac_code = hmac.new(
    string_to_sign.encode("utf-8"),
    digestmod=hashlib.sha256,
).digest()
sign = base64.b64encode(hmac_code).decode("utf-8")


# ── 解析 JUnit XML，按文件聚合 ──

failed_summary = []  # 只收集失败的文件
total_tests = 0
total_failures = 0
total_errors = 0
total_skipped = 0

try:
    tree = ET.parse("test_report.xml")
    root = tree.getroot()

    file_map = {}
    for tc in root.iter("testcase"):
        cn = tc.get("classname", "unknown")
        parts = cn.split(".")
        if len(parts) >= 2:
            fname = parts[0] + "/" + parts[1] + ".py"
        else:
            fname = cn.replace(".", "/") + ".py"

        if fname not in file_map:
            file_map[fname] = {"total": 0, "fail": 0, "error": 0, "skip": 0}
        file_map[fname]["total"] += 1
        if tc.find("failure") is not None:
            file_map[fname]["fail"] += 1
        if tc.find("error") is not None:
            file_map[fname]["error"] += 1
        if tc.find("skipped") is not None:
            file_map[fname]["skip"] += 1

    for fname, counts in sorted(file_map.items()):
        total_tests += counts["total"]
        total_failures += counts["fail"]
        total_errors += counts["error"]
        total_skipped += counts["skip"]

        if counts["fail"] + counts["error"] > 0:
            detail = f"{counts['total']} ran, {counts['fail']} failed"
            if counts["error"] > 0:
                detail += f", {counts['error']} errors"
            failed_summary.append(f"❌ `{fname}` — {detail}")

except Exception as e:
    failed_summary = [f"⚠️ 无法解析测试报告: {e}"]


# ── 状态判定 ──

passed = TEST_EXIT_CODE == "0"
status_icon = "✅" if passed else "❌"
status_text = "PASSED" if passed else "FAILED"
color = "turquoise" if passed else "red"
summary_line = f"{total_tests} tests, {total_failures} failed, {total_skipped} skipped"


# ── Actions 日志：打印完整测试输出 ──

print("=" * 60)
print("📋 完整测试输出:")
print("=" * 60)
try:
    with open("test_output.txt", "r") as f:
        print(f.read())
except Exception:
    print("无法读取 test_output.txt")

if not passed:
    print("\n" + "=" * 60)
    print("❌ 失败用例详情:")
    print("=" * 60)
    try:
        tree = ET.parse("test_report.xml")
        root = tree.getroot()
        for tc in root.iter("testcase"):
            failure = tc.find("failure")
            if failure is not None:
                name = tc.get("name", "unknown")
                msg = failure.get("message") or ""
                text = (failure.text or "")[:500]
                print(f"\n--- {name} ---")
                print(f"Message: {msg}")
                print(text)
    except Exception as e:
        print(f"无法解析失败详情: {e}")

print("\n" + "=" * 60)
print("🔄 变更文件:")
print("=" * 60)
try:
    with open("changed_files.txt", "r") as f:
        print(f.read())
except Exception:
    print("无变更文件记录")

print("\n" + "=" * 60)
print("📝 提交日志:")
print("=" * 60)
try:
    with open("commit_log.txt", "r") as f:
        print(f.read())
except Exception:
    print("无提交日志")


# ── 读取提交日志 ──

commit_lines = []
try:
    with open("commit_log.txt", "r") as f:
        for line in f:
            line = line.strip()
            if line:
                commit_lines.append(line)
except Exception:
    pass

commit_summary = ""
if commit_lines:
    # 飞书卡片单条消息过长会被截断，限制每条 80 字符
    items = [f"> `{c[:80]}`" for c in commit_lines]
    commit_summary = "\n".join(items)

# ── 构建飞书卡片 ──

elements = [
    {
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": (
                f"**版本**: `v{PLUGIN_VERSION}`\n"
                f"**同步**: ✅ 已推送\n"
                f"**触发**: {trigger_label}\n"
                f"**仓库**: {REPO} `master`\n"
                f"**测试汇总**: {status_icon} {status_text} — {summary_line}"
            ),
        },
    },
]

# 提交说明区块
if commit_summary:
    elements.append({"tag": "hr"})
    elements.append({
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": f"**📝 提交说明**:\n{commit_summary}",
        },
    })

# 只有失败时才展示失败文件列表
if failed_summary:
    elements.append({"tag": "hr"})
    elements.append({
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": "**❌ 失败脚本**:\n" + "\n".join(failed_summary),
        },
    })

elements.append({"tag": "hr"})
elements.append({
    "tag": "action",
    "actions": [{
        "tag": "button",
        "text": {"tag": "plain_text", "content": "🔗 查看 Actions 详情"},
        "url": run_url,
        "type": "primary",
    }],
})

payload = {
    "timestamp": timestamp,
    "sign": sign,
    "msg_type": "interactive",
    "card": {
        "header": {
            "title": {
                "tag": "plain_text",
                "content": "飞书流式卡片插件·动态",
            },
            "template": color,
        },
        "elements": elements,
    },
}


# ── 发送请求 ──

data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
req = urllib.request.Request(
    FEISHU_WEBHOOK,
    data=data,
    headers={"Content-Type": "application/json"},
)
try:
    with urllib.request.urlopen(req) as resp:
        print(f"✅ Feishu notified: {resp.read().decode()}")
except Exception as e:
    print(f"❌ Failed to notify Feishu: {e}")