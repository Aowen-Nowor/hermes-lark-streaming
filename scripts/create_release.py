#!/usr/bin/env python3
"""Gitee Go 流水线发版脚本 —— 调用 Gitee API 创建 Release（自动创建 tag）.

在工作流中由 .workflow/release-pipeline.yml 调用。所有变量通过环境
变量传入（shell 自己展开，不依赖 release@gitee 插件的参数模板引擎）：

必需环境变量（在 Gitee Go 流水线参数设置 中配置）：
    OWNER  仓库 owner（如 Aowen-Nowor）
    TOKEN  个人访问令牌（需 projects、releases 权限）

    注意：变量名不能以 GITEE_ 或 GO_ 开头（Gitee 系统保留前缀），
    所以用 OWNER/TOKEN 而不是 GITEE_OWNER/GITEE_TOKEN。

系统变量（Gitee Go 自动注入，无需配置）：
    GITEE_COMMIT  当前 commit SHA（用作 target_commitish）
    GITEE_REPO    仓库全名（owner/repo 格式，如 Aowen-Nowor/hermes-lark-streaming）

可选环境变量：
    REPO         仓库名（默认 hermes-lark-streaming）
    TARGET_BRANCH Release 的 target_commitish（默认 github_sync；但优先用 GITEE_COMMIT）

工作流设计（v1.1.0 最终方案）：
  - 不用 release@gitee 插件（其参数不支持运行时变量）
  - 不用 git tag/git push（CI 环境无 git 凭据，push 会失败）
  - 只调 Gitee API 创建 Release：
    * tag_name = v{version}
    * target_commitish = GITEE_COMMIT（当前 commit）
    * Gitee API 会在创建 Release 时自动创建对应的 tag（如果不存在）

版本号从 plugin.yaml 提取（grep + sed，不依赖 PyYAML）。
Release body 只放完整更新日志链接（v1.7.0 勘误：此前描述与 build_release_body 实现矛盾——不再把 CHANGELOG.md 全文塞进 body，用户点击链接查看）。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_YAML = REPO_ROOT / "plugin.yaml"

GITEE_API_BASE = "https://gitee.com/api/v5/repos"


def extract_version() -> str:
    """从 plugin.yaml 提取 version 字段，兼容单引号/双引号/无引号三种写法."""
    if not PLUGIN_YAML.exists():
        print(f"ERROR: {PLUGIN_YAML} 不存在", file=sys.stderr)
        sys.exit(1)
    for line in PLUGIN_YAML.read_text(encoding="utf-8").splitlines():
        if line.startswith("version:"):
            value = line.split(":", 1)[1].strip()
            value = value.strip('"').strip("'")
            return value
    print("ERROR: plugin.yaml 中未找到 version: 字段", file=sys.stderr)
    sys.exit(1)


def ensure_env() -> tuple[str, str, str, str]:
    """校验并返回 (owner, token, repo, target_commitish).

    变量名用 OWNER/TOKEN（不能以 GITEE_/GO_ 开头，Gitee 系统保留前缀）。
    target_commitish 优先用 GITEE_COMMIT 系统变量（当前 commit SHA），
    这样 Gitee API 创建 Release 时会基于该 commit 自动创建 tag。
    """
    owner = os.environ.get("OWNER", "").strip()
    token = os.environ.get("TOKEN", "").strip()
    repo = os.environ.get("REPO", "hermes-lark-streaming").strip()
    # 优先用 GITEE_COMMIT（当前 commit SHA），其次 TARGET_BRANCH 环境变量，默认 github_sync
    target_commitish = (
        os.environ.get("GITEE_COMMIT", "").strip()
        or os.environ.get("TARGET_BRANCH", "").strip()
        or "github_sync"
    )

    if not owner:
        print(
            "ERROR: 环境变量 OWNER 未配置\n"
            "请在 Gitee Go 流水线设置 → 参数设置 中配置 OWNER=Aowen-Nowor\n"
            "注意：变量名不能以 GITEE_ 或 GO_ 开头（系统保留前缀）",
            file=sys.stderr,
        )
        sys.exit(1)
    if not token:
        print(
            "ERROR: 环境变量 TOKEN 未配置\n"
            "请在 Gitee Go 流水线设置 → 参数设置 中配置 TOKEN=<个人访问令牌>\n"
            "注意：变量名不能以 GITEE_ 或 GO_ 开头（系统保留前缀）",
            file=sys.stderr,
        )
        sys.exit(1)
    return owner, token, repo, target_commitish


def build_release_body(tag: str, owner: str, repo: str) -> str:
    """构造 Release 描述：只放完整更新日志链接（用户点击可跳转）.

    不再把 CHANGELOG.md 全部内容塞进 Release body（太长）。
    用户点击链接即可查看完整更新日志。
    """
    changelog_url = f"https://gitee.com/{owner}/{repo}/raw/github_sync/docs/CHANGELOG.md"
    return f"完整更新日志：{changelog_url}"


def create_gitee_release(
    *,
    owner: str,
    token: str,
    repo: str,
    tag: str,
    body: str,
    target_commitish: str,
) -> None:
    """调用 Gitee Open API 创建 Release.

    如果 tag 不存在，Gitee API 会基于 target_commitish 自动创建 tag。
    如果 Release 已存在，视为成功（幂等）。

    API 文档: https://gitee.com/api/v5/swagger
    """
    url = f"{GITEE_API_BASE}/{owner}/{repo}/releases"
    payload = {
        "access_token": token,
        "tag_name": tag,
        "name": tag,
        "body": body,
        "prerelease": False,
        "target_commitish": target_commitish,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json;charset=UTF-8"},
        method="POST",
    )

    print("ℹ️  调用 Gitee API 创建 Release...")
    print(f"   URL: {url}")
    print(f"   tag: {tag}")
    print(f"   name: {tag}")
    print(f"   target_commitish: {target_commitish[:12]}...")

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp_body = resp.read().decode("utf-8")
            status = resp.status
            print(f"✅ API 响应码: {status}")
            try:
                resp_data = json.loads(resp_body)
                release_url = resp_data.get("html_url", "")
                if release_url:
                    print("✅ Release 创建成功！")
                    print(f"   URL: {release_url}")
            except json.JSONDecodeError:
                print("✅ Release 创建成功（响应非 JSON）")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        # 已存在的 Release 视为成功（幂等）
        # 注意：不能用模糊的 "exist" 匹配——401 Unauthorized 的错误信息
        # "Access token does not exist" 也包含 "exist"，会被误判。
        # 只认明确的"已存在"提示（Gitee API 返回 400 + 明确文案）。
        is_already_exist = (
            "已存在" in err_body
            or "already exist" in err_body.lower()
            or "release already" in err_body.lower()
        )
        if is_already_exist:
            print(f"ℹ️  Release {tag} 已存在，跳过创建")
        else:
            print(f"❌ Release 创建失败，HTTP {e.code}", file=sys.stderr)
            print(f"   响应: {err_body[:2000]}", file=sys.stderr)
            sys.exit(1)
    except Exception as e:
        print(f"❌ 调用 API 异常: {e}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    # 1. 提取版本号
    version = extract_version()
    tag = f"v{version}"
    print(f"✅ 检测到版本号: {version}  →  tag: {tag}")

    # 2. 校验环境变量
    owner, token, repo, target_commitish = ensure_env()
    print(f"✅ 仓库: {owner}/{repo}")
    print(f"✅ target_commitish: {target_commitish}")

    # 3. 构造 Release 描述
    body = build_release_body(tag, owner, repo)

    # 4. 调用 Gitee API 创建 Release（API 会自动创建 tag，无需 git push）
    create_gitee_release(
        owner=owner, token=token, repo=repo,
        tag=tag, body=body, target_commitish=target_commitish,
    )

    print("✅ 发版流水线完成")


if __name__ == "__main__":
    main()
