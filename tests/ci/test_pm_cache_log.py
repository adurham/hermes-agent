"""PM cache diagnostics describe the action's actual selected branches."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("hit,save", [("true", "true"), ("false", "true"), ("", "false")])
def test_cache_log_reports_selected_restore_and_save_policy(tmp_path, hit, save, capsys):
    from scripts.ci.pm_cache_log import log_caches
    steps: dict[str, dict] = {"prepare": {"outputs": {"store": str(tmp_path / "tools")}},
             "install": {"outputs": {"uv-cache-path": str(tmp_path / "uv"),
                                     "npm-cache-path": str(tmp_path / "npm")}}}
    inputs = {}
    for name in ["tools", "python", "node"]:
        inputs[f"save-{name}-cache"] = save
        selected = f"{name}-cache" + ("-restore" if save == "false" else "")
        steps[selected] = {"outcome": "success", "outputs": {"cache-hit": hit}}
    log_caches(inputs, steps, "fixture-ref")
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 3
    for family, line in zip(["tools", "uv", "npm"], lines):
        assert f"[setup-pm cache] {family}:" in line
        assert f"cache-hit={hit!r}" in line
        assert ("save=on-success" if save == "true" else "save=disabled") in line
        assert str(tmp_path / family) in line


def test_cache_log_does_not_call_a_skipped_restore_a_miss(tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts/ci/pm_cache_log.py"
    result = subprocess.run(
        [sys.executable, "-S", str(script)],
        env={**os.environ, "_PM_CACHE_INPUTS": "{}", "_PM_CACHE_STEPS": json.dumps({
            "tools-cache": {"outcome": "skipped", "outputs": {}},
            "python-cache-restore": {"outcome": "failure", "outputs": {}},
        })},
        capture_output=True, text=True, encoding="utf-8", timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert "tools: restore=skipped" in lines[0]
    assert "uv: restore=failure" in lines[1]
    assert "npm: restore=skipped" in lines[2]
