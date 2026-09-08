"""The CI bootstrap reads PM's pins without importing installed dependencies."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from pm.lock import Lockfile
from pm.paths import lockfile_path
from pm.store import current_target


@pytest.mark.parametrize("toolchain,names", [
    ("python", {"python", "uv"}),
    ("node", {"node", "npm"}),
    ("all", {"python", "uv", "node", "npm"}),
])
def test_stdlib_bootstrap_exports_the_pm_lock(toolchain, names, tmp_path):
    root = Path(__file__).resolve().parents[2]
    output = tmp_path / "output"
    envfile = tmp_path / "environment"
    home = tmp_path / "runner state"
    env = {**os.environ, "GITHUB_OUTPUT": str(output), "GITHUB_ENV": str(envfile)}
    result = subprocess.run(
        [sys.executable, "-S", str(root / "scripts/ci/setup_toolchain.py"),
         "prepare", "--toolchain", toolchain, "--home", str(home)],
        cwd=tmp_path, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    values = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8-sig").splitlines())
    lock = Lockfile(lockfile_path())
    assert json.loads(values["packages"]) == sorted(names)
    assert values["target"] == current_target()
    for name in names:
        expected = lock.version(name)
        assert values[f"{name}-version"] == (expected.partition("+")[0] if name == "python" else expected)
    exported = dict(line.split("=", 1) for line in envfile.read_text(encoding="utf-8-sig").splitlines())
    assert Path(exported["HERMES_HOME"]) == home
    assert Path(exported["HERMES_RUNTIME_DIR"]).is_relative_to(home)
    assert not Path(exported["HERMES_RUNTIME_DIR"]).exists(), "prepare must not provision before cache restore"


@pytest.mark.parametrize("extras", ['"dev"', '{}', '[1]', '["dev\\nHERMES_HOME=bad"]', '["--all"]'])
def test_invalid_extras_do_not_export_or_install(extras, tmp_path):
    root = Path(__file__).resolve().parents[2]
    output = tmp_path / "output"
    home = tmp_path / "state"
    result = subprocess.run(
        [sys.executable, "-S", str(root / "scripts/ci/setup_toolchain.py"), "prepare",
         "--home", str(home), "--extras", extras],
        cwd=tmp_path, env={**os.environ, "GITHUB_OUTPUT": str(output)},
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode != 0
    assert "extras must be a JSON array of extra names" in result.stderr
    assert not output.exists()
    assert not home.exists()
