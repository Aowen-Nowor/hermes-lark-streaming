"""飞书通知脚本 — 由 GitHub Actions workflow 调用，环境变量传入参数。

v1.1.1: 卡片美化 + 支持多个测试报告 + 变更文件列表。
- 读取 unit_test_report.xml + e2e_report.xml 汇总测试结果
- 读取 changed_files.txt 显示变更文件（最多 10 条）
- 卡片用 column_set/div/hr/icon 美化，颜色语义化
"""

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
    import sys, traceback
    traceback.print_exc(file=sys.stderr)

# ── 从环境变量读取配置 ──

# v1.7.0 (R5-08): friendly validation — raw os.environ[...] raised a bare
# KeyError with no hint about WHICH variable is missing or where to set it.
_missing_env = [
    _name for _name in ("FEISHU_WEBHOOK", "FEISHU_SECRET")
    if not os.environ.get(_name, "").strip()
]
if _missing_env:
    raise SystemExit(
        f"[notify_feishu] missing required env var(s): {', '.join(_missing_env)}. "
        "Set them in the workflow/CI secrets before calling this script."
    )
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


# ── 解析多个测试报告 ──

failed_summary = []
skipped_details = []
total_tests = 0
total_failures = 0
total_errors = 0
total_skipped = 0

# v1.1.1: 支持多个报告文件
_report_files = [
    ("单元测试", "unit_test_report.xml"),
    ("E2E 测试", "e2e_report.xml"),
]

for _report_label, _report_file in _report_files:
    if not os.path.exists(_report_file):
        continue
    try:
        tree = ET.parse(_report_file)
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
                skip_el = tc.find("skipped")
                skip_msg = skip_el.get("message", "") if skip_el is not None else ""
                tc_name = tc.get("name", "unknown")
                short_cn = cn.split(".")[-1] if "." in cn else cn
                skipped_details.append(f"  • `{short_cn}::{tc_name}` — {skip_msg[:80]}")

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
        failed_summary.append(f"⚠️ 无法解析 {_report_file}: {e}")


# ── 状态判定 ──

passed = TEST_EXIT_CODE == "0"
status_icon = "✅" if passed else "❌"
status_text = "通过" if passed else "失败"
header_color = "turquoise" if passed else "red"

# v1.1.1: 测试汇总含失败/错误(阻塞)/跳过
summary_parts = [f"{total_tests} 个测试"]
if total_failures > 0:
    summary_parts.append(f"{total_failures} 个失败")
if total_errors > 0:
    summary_parts.append(f"{total_errors} 个错误(阻塞)")
if total_skipped > 0:
    summary_parts.append(f"{total_skipped} 个跳过")
if not total_failures and not total_errors and not total_skipped:
    summary_parts.append("全部通过")
summary_line = " · ".join(summary_parts)


# ── 读取变更文件 ──

changed_files = []
try:
    with open("changed_files.txt", "r") as f:
        for line in f:
            line = line.strip()
            if line:
                changed_files.append(line)
except Exception:
    pass

# 最多 10 条
changed_files_display = changed_files[:10]
if len(changed_files) > 10:
    changed_files_display.append(f"... 共 {len(changed_files)} 个文件")


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

# Gitee MR 合并去重
import re as _re

def _strip_mr_prefix(msg: str) -> str:
    return _re.sub(r"^!\d+\s+", "", msg)

_seen_bare: dict[str, str] = {}
for line in commit_lines:
    bare = _strip_mr_prefix(line)
    if bare not in _seen_bare:
        _seen_bare[bare] = line
    else:
        existing = _seen_bare[bare]
        if _re.match(r"^!\d+\s+", line) and not _re.match(r"^!\d+\s+", existing):
            _seen_bare[bare] = line
commit_lines = list(_seen_bare.values())

commit_summary = ""
if commit_lines:
    items = [f"> `{c[:80]}`" for c in commit_lines]
    commit_summary = "\n".join(items)


# ── 构建飞书卡片（v1.1.1 美化版）──

def _metric_col(label: str, value: str, color: str = "default") -> dict:
    """构建一个指标列."""
    color_map = {"default": None, "green": "green", "red": "red", "orange": "orange-300"}
    font_color = color_map.get(color)
    if font_color:
        value_md = f"<font color='{font_color}'>**{value}**</font>"
    else:
        value_md = f"**{value}**"
    return {
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": f"<font color='grey'>{label}</font>\n{value_md}",
        },
    }


def _fold(title, elements_content, *, expanded=False, border_color="grey"):
    """构建一个折叠面板（collapsible_panel）."""
    return {
        "tag": "collapsible_panel",
        "expanded": expanded,
        "header": {
            "title": {"tag": "plain_text", "content": title, "text_color": "grey", "text_size": "notation"},
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
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": elements_content}},
        ],
    }


elements = [
    # ── 顶部：版本 + 状态（3 列）──
    {
        "tag": "column_set",
        "flex_mode": "stretch",
        "columns": [
            {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                _metric_col("版本", f"v{PLUGIN_VERSION}", "default"),
            ]},
            {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                _metric_col("同步", "✅ 已推送", "green"),
            ]},
            {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                _metric_col("触发", trigger_label.split("（")[0], "default"),
            ]},
        ],
    },
    {"tag": "hr"},
    # ── 测试汇总 ──
    {
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": f"**测试汇总**: {status_icon} {status_text} — {summary_line}",
        },
    },
]

# ── 变更文件（折叠面板）──
if changed_files_display:
    elements.append({"tag": "hr"})
    file_lines = "\n".join(f"  • `{f}`" for f in changed_files_display)
    elements.append(_fold(
        f"🔄 变更文件 ({len(changed_files)})",
        file_lines,
        expanded=len(changed_files) <= 3,  # ≤3 个默认展开
    ))

# ── 提交说明（折叠面板）──
if commit_summary:
    elements.append({"tag": "hr"})
    elements.append(_fold("📝 提交说明", commit_summary, expanded=False))

# ── 失败详情（折叠面板，默认展开）──
# v1.1.1: 分开失败(failure)和阻塞(error)
failure_items = [s for s in failed_summary if s.startswith("❌")]
error_items = [s for s in failed_summary if s.startswith("⚠️")]

if failure_items:
    elements.append({"tag": "hr"})
    elements.append(_fold(
        f"❌ 失败详情 ({total_failures})",
        "\n".join(failure_items),
        expanded=True,  # 失败详情默认展开
        border_color="red",
    ))

if error_items or total_errors > 0:
    elements.append({"tag": "hr"})
    elements.append(_fold(
        f"🚫 阻塞详情 ({total_errors})",
        "\n".join(error_items) if error_items else "  • 解析报告时出错",
        expanded=True,  # 阻塞详情默认展开
        border_color="red",
    ))

# ── 跳过的测试（折叠面板，有数据才展示）──
if skipped_details:
    elements.append({"tag": "hr"})
    display_skips = skipped_details[:10]
    if len(skipped_details) > 10:
        display_skips.append(f"  • ... 共 {len(skipped_details)} 条跳过")
    elements.append(_fold(
        f"⏭️ 跳过的测试 ({total_skipped})",
        "\n".join(display_skips),
        expanded=False,
    ))

elements.append({"tag": "hr"})
elements.append({
    "tag": "button",
    "text": {"tag": "plain_text", "content": "🔗 查看 Actions 详情"},
    "url": run_url,
    "type": "primary",
})

payload = {
    "timestamp": timestamp,
    "sign": sign,
    "msg_type": "interactive",
    "card": {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": "飞书流式卡片插件·动态",
            },
            "template": header_color,
        },
        "body": {"elements": elements},
    },
}


# ── Actions 日志 ──

print("=" * 60)
print("📋 测试输出:")
print("=" * 60)
for _label, _file in [("单元测试", "unit_test_output.txt"), ("E2E", "e2e_output.txt")]:
    try:
        with open(_file, "r") as f:
            content = f.read()
            if content:
                print(f"\n--- {_label} ---")
                print(content[-3000:])  # 最多 3000 字符
    except Exception:
        pass

print("\n" + "=" * 60)
print("🔄 变更文件:")
print("=" * 60)
for f in changed_files_display:
    print(f"  {f}")

print("\n" + "=" * 60)
print("📝 提交日志:")
print("=" * 60)
for c in commit_lines:
    print(f"  {c}")


# ── 发送请求 ──

data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
req = urllib.request.Request(
    FEISHU_WEBHOOK,
    data=data,
    headers={"Content-Type": "application/json"},
)
try:
    with urllib.request.urlopen(req) as resp:
        print(f"\n✅ Feishu notified: {resp.read().decode()}")
except Exception as e:
    print(f"\n❌ Failed to notify Feishu: {e}")
