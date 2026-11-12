"""Regression contracts for the repository's CI workflow control flow."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"


def _step(workflow_name: str, job_name: str, step_name: str) -> dict:
    data = yaml.safe_load((_WORKFLOWS / workflow_name).read_text(encoding="utf-8"))
    steps = data["jobs"][job_name]["steps"]
    return next(step for step in steps if step.get("name") == step_name)


def _embedded_probe_renderer(run: str) -> str:
    marker = "python3 - <<'PY'\n"
    assert marker in run, "probe renderer must be directly executable for regression tests"
    return run.split(marker, 1)[1].split("\nPY", 1)[0]


def test_release_probe_captures_exit_and_only_advances_on_explicit_verdict() -> None:
    probe = _step("hermes-release-watch.yml", "compat", "Run compat probe (S1-S5)")
    run = probe["run"]
    assert "set +e" in run
    assert "PROBE_EXIT=${PIPESTATUS[0]}" in run
    assert 'exit "$PROBE_EXIT"' in run
    assert "verdict=" in run
    assert "summary<<" in run

    notify = _step("hermes-release-watch.yml", "compat", "Notify Feishu (兼容性结论)")
    assert notify["env"]["COMPAT_SUMMARY"] == "${{ steps.probe.outputs.summary }}"

    for name in ("Save seen release (仅明确结论)", "Persist to cache"):
        condition = _step("hermes-release-watch.yml", "compat", name)["if"]
        assert "always()" in condition
        assert "steps.probe.outputs.verdict == 'pass'" in condition
        assert "steps.probe.outputs.verdict == 'fail'" in condition


@pytest.mark.parametrize(
    ("report", "probe_exit", "expected_verdict"),
    [
        (None, "2", "unknown"),
        ({"verdict": "pass", "steps": [{"step": "S1", "ok": True, "detail": "import ok"}]}, "0", "pass"),
        ({"verdict": "fail", "steps": [{"step": "S5", "ok": False, "detail": "2 failed"}]}, "1", "fail"),
        ({"verdict": "pass", "steps": []}, "2", "unknown"),
    ],
)
def test_release_probe_renderer_always_emits_outputs(
    tmp_path: Path,
    report: dict | None,
    probe_exit: str,
    expected_verdict: str,
) -> None:
    probe = _step("hermes-release-watch.yml", "compat", "Run compat probe (S1-S5)")
    script = _embedded_probe_renderer(probe["run"])
    if report is not None:
        (tmp_path / "compat_report.json").write_text(json.dumps(report), encoding="utf-8")
    output = tmp_path / "github_output.txt"
    output.touch()
    env = dict(os.environ, GITHUB_OUTPUT=str(output), PROBE_EXIT=probe_exit)

    completed = subprocess.run(
        [sys.executable, "-"],
        input=script,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    rendered = output.read_text(encoding="utf-8")
    assert f"verdict={expected_verdict}" in rendered
    assert "summary<<" in rendered
    assert f"probe exit={probe_exit}" in rendered


def test_sync_tests_gitee_tip_before_mirror_and_preserves_no_change_sync() -> None:
    workflow = yaml.safe_load(
        (_WORKFLOWS / "sync-from-gitee.yml").read_text(encoding="utf-8")
    )
    names = [step.get("name") for step in workflow["jobs"]["sync"]["steps"]]
    detect = names.index("Detect changes")
    assert "Checkout Gitee tip for test & release" in names
    checkout = names.index("Checkout Gitee tip for test & release")
    tests = names.index("Run full test suite (unit + e2e mock)")
    mirror = names.index("Mirror branches and tags (Gitee is source of truth)")
    assert detect < checkout < tests < mirror

    checkout_run = _step(
        "sync-from-gitee.yml", "sync", "Checkout Gitee tip for test & release"
    )["run"]
    assert "git checkout --detach" in checkout_run
    assert "steps.detect.outputs.new_sha" in checkout_run

    mirror_condition = _step(
        "sync-from-gitee.yml", "sync", "Mirror branches and tags (Gitee is source of truth)"
    )["if"]
    assert "steps.run_tests.outputs.exit_code == '0'" in mirror_condition
    assert "steps.detect.outputs.has_changes != 'true'" in mirror_condition
    assert "inputs.force_tests != true" in mirror_condition


def test_sync_records_real_pytest_exit_code() -> None:
    run = _step("sync-from-gitee.yml", "sync", "Run full test suite (unit + e2e mock)")["run"]
    assert "set +e" in run
    assert "TEST_EXIT=${PIPESTATUS[0]}" in run
    assert 'echo "exit_code=$TEST_EXIT"' in run
    assert 'exit "$TEST_EXIT"' in run


def test_existing_release_target_must_match_tested_gitee_tip() -> None:
    run = _step("sync-from-gitee.yml", "sync", "Check if GitHub Release already exists")["run"]
    assert "--json targetCommitish" in run
    assert 'NEW="${{ steps.detect.outputs.new_sha }}"' in run
    assert '"$TARGET" != "$NEW"' in run
    assert "exit 1" in run


def test_gitee_remote_has_no_embedded_token() -> None:
    step = _step("sync-from-gitee.yml", "sync", "Add Gitee remote and fetch")
    run = step["run"]
    remote_lines = [line.strip() for line in run.splitlines() if "git remote add gitee" in line]
    assert len(remote_lines) == 1
    assert "https://gitee.com/" in remote_lines[0]
    assert "GITEE_TOKEN" not in remote_lines[0]
    assert "http.extraHeader" in run
    assert "${GITEE_BASIC}" in run
    assert step["env"]["GITEE_TOKEN"] == "${{ secrets.GITEE_TOKEN }}"


def test_all_github_actions_are_pinned_to_full_commit_sha() -> None:
    import re

    for workflow in _WORKFLOWS.glob("*.yml"):
        text = workflow.read_text(encoding="utf-8")
        uses = re.findall(r"uses:\s*([^\s]+)@([^\s]+)", text)
        assert uses, workflow.name
        for action, ref in uses:
            if action.startswith(("actions/", "softprops/")):
                assert re.fullmatch(r"[0-9a-f]{40}", ref), f"{workflow.name}: {action}@{ref}"
