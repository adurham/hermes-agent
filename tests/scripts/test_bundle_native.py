"""The native bundle pipeline publishes only after a real staged sync succeeds."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from scripts.bundles import native


def test_bundle_stages_git_tree_and_runs_native_children_before_manifest(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text('[project]\nname="fixture"\nversion="1.0.0"\nrequires-python=">=3.11"\n[tool.uv]\npackage=false\n', encoding="utf-8")
    uv = shutil.which("uv")
    assert uv, "native bundle test requires uv"
    env = {**os.environ, "UV_OFFLINE": "1", "UV_PYTHON_DOWNLOADS": "never", "UV_CACHE_DIR": str(tmp_path / "cache")}
    subprocess.run([uv, "lock", "--python", sys.executable], cwd=repo, env=env, check=True, capture_output=True)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-m", "fixture"], cwd=repo, check=True, capture_output=True)
    output = tmp_path / "payload"
    monkeypatch.setattr("pm.paths.repo_root", lambda: repo)
    monkeypatch.setattr(native, "_bundle_package_names", lambda: [])
    monkeypatch.setattr(native, "_install_names", lambda names: 0)
    monkeypatch.setattr(native, "_store", lambda: SimpleNamespace(root=output / "tools", entry=lambda _: Path(sys.executable).parent))
    monkeypatch.setattr(native, "_facts", lambda: SimpleNamespace(get=lambda _: {"entry": "python"}, entries_in_use=lambda: []))
    monkeypatch.setattr(native, "get_package", lambda _: SimpleNamespace(binary=lambda *args: Path(sys.executable)))
    monkeypatch.setattr(native, "pm_uv", lambda: (uv, dict(env)))
    monkeypatch.setattr(native, "_arch_guard", lambda store: [])
    monkeypatch.setattr("scripts.bundles.payload.relativize_links", lambda root: 0)
    monkeypatch.setattr("pm.features.installed_extras", lambda *args: [])
    monkeypatch.setattr("pm.packages.uv_cache_dir", lambda: tmp_path / "empty-cache")
    real_run = native._run_live
    calls = []

    def child(argv, *, cwd, env):
        calls.append(argv[1])
        assert not (output / "manifest.json").exists()
        return real_run(argv, cwd=cwd, env=env)

    monkeypatch.setattr(native, "_run_live", child)
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "original"))
    assert native.stage_native(SimpleNamespace(out=str(output), ref="HEAD")) == 0
    assert calls == ["venv", "sync"]
    assert (output / "hermes-agent/pyproject.toml").is_file()
    assert json.loads((output / "manifest.json").read_text())["repo"] == "hermes-agent"
    assert os.environ["HERMES_RUNTIME_DIR"] == str(tmp_path / "original")

    monkeypatch.setattr(native, "_run_live", lambda *a, **kw: (1, "injected failure"))
    assert native.stage_native(SimpleNamespace(out=str(output), ref="HEAD")) == 1
    assert not (output / "manifest.json").exists()
    assert os.environ["HERMES_RUNTIME_DIR"] == str(tmp_path / "original")
