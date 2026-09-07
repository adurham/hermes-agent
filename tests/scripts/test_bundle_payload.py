"""Shared bundle operations use real filesystem and entrypoint contracts."""
from __future__ import annotations

import json
import os
import subprocess
import sys


import pytest

from scripts.bundles.payload import plant_surfaces, posix_launcher, project_entries, relativize_links, snapshot, write_manifest, stage_launchers
from scripts.bundles.desktop import release_version


def test_snapshot_and_manifest_are_shared_by_both_layouts(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", str(source)], check=True, capture_output=True)
    project = '[project]\nname="fixture"\nversion="1.2.3"\n[project.scripts]\ncustom="entry:run"\n'
    (source / "pyproject.toml").write_text(project, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-m", "fixture"], cwd=source, check=True, capture_output=True)
    (source / "untracked").write_text("must not ship")
    for repo_name, target in [("app", "linux-arm64-bionic"), ("hermes-agent", "win32-arm64")]:
        root = tmp_path / repo_name
        root.mkdir()
        snapshot(source, "HEAD", root / repo_name)
        manifest = write_manifest(root, target=target, repo=repo_name)
        assert project_entries(root / manifest["repo"]) == {"custom": "entry:run"}
        assert not (root / repo_name / "untracked").exists()
        assert not (root / repo_name / ".git").exists()
        assert json.loads((root / "manifest.json").read_text()) == manifest
    assert release_version(source, "v1.2.3") == "1.2.3"
    with pytest.raises(ValueError):
        release_version(source, "v1.2.4")


def test_surfaces_require_complete_outputs_and_replace_stale_files(tmp_path):
    source, repo = tmp_path / "build", tmp_path / "payload"
    tui = source / "ui-tui/dist"
    web = source / "hermes_cli/web_dist"
    tui.mkdir(parents=True)
    web.mkdir(parents=True)
    (tui / "entry.js").write_text("built tui")
    (web / "index.html").write_text("built web")
    plant_surfaces(repo, source)
    (repo / "hermes_cli/web_dist/stale").write_text("old")
    plant_surfaces(repo, source)
    assert not (repo / "hermes_cli/web_dist/stale").exists()
    assert (repo / "hermes_cli/tui_dist/entry.js").read_text() == "built tui"
    (web / "index.html").unlink()
    with pytest.raises(FileNotFoundError):
        plant_surfaces(repo, source)


def test_launcher_stage_reads_declared_entries_and_drops_stale_names(tmp_path, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace
    from pm.lock import Facts
    from pm.store import current_target

    root = tmp_path / "payload"
    repo, tools = root / "app", root / "tools"
    repo.mkdir(parents=True)
    tools.mkdir()
    interpreter = tools / "python/python.exe"
    interpreter.parent.mkdir()
    interpreter.write_bytes(b"fixture interpreter")
    (repo / "pyproject.toml").write_text('[project.scripts]\ncustom="entry:run"\n', encoding="utf-8")
    Facts(tools / "facts.json").record("python", "3.11.16", "python", {}, tools)
    manifest = write_manifest(root, target=current_target(), repo="app")
    (root / "bin").mkdir()
    (root / "bin/removed-entry").write_text("old")
    monkeypatch.setattr("pm.registry.get_package", lambda _: SimpleNamespace(binary=lambda *args: interpreter))
    calls = []

    def mint(argv, *, env, check):
        calls.append(json.loads(env["HERMES_MINT_SPECS"]))
        compile(Path(env["HERMES_MINT_WRAPPER"]).read_text(), "wrapper", "exec")
        (root / "bin/custom.exe").write_bytes(b"mint fixture")

    assert stage_launchers(root, manifest, run=mint) == ["custom"]
    assert not (root / "bin/removed-entry").exists()
    if current_target().startswith("win32"):
        assert calls == [[{"name": "custom", "module": "entry", "func": "run"}]]
    else:
        assert (root / "bin/custom").is_file()
    assert json.loads((root / "manifest.json").read_text())["launchers"] == ["custom"]


@pytest.mark.platforms("posix")
def test_relocation_preserves_sibling_and_framework_links(tmp_path):
    root = tmp_path / "payload"
    store = root / "tools/python/bin"
    venv = root / "venv/bin"
    store.mkdir(parents=True)
    venv.mkdir(parents=True)
    (store / "python3").write_text("interpreter")
    (venv / "python").symlink_to("/builder/tools/python/bin/python3")
    (venv / "python3").symlink_to("python")
    framework = root / "tools/framework"
    framework.symlink_to("python/bin/python3")
    assert relativize_links(root) == 1
    assert (venv / "python3").read_text() == "interpreter"
    assert os.readlink(venv / "python3") == "python"
    assert os.readlink(framework) == "python/bin/python3"
    assert relativize_links(root) == 0
    (venv / "bad").symlink_to("/usr/bin/python")
    with pytest.raises(ValueError, match="escapes payload"):
        relativize_links(root)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("target", ["linux-x64", "linux-arm64-bionic"])
def test_both_launchers_keep_active_home_and_call_declared_function(tmp_path, target):
    root = tmp_path / "payload with spaces"
    (root / "bin").mkdir(parents=True)
    (root / "app").mkdir()
    (root / "python").symlink_to(sys.executable)
    (root / "app/entry.py").write_text("import json,os,sys\ndef run():\n print(json.dumps([os.environ['HERMES_HOME'],sys.argv[1:]])); return 7\n")
    launcher = root / "bin/custom"
    launcher.write_text(posix_launcher("custom", "entry:run", python="python", repo="app", site="deps", target=target))
    result = subprocess.run(["sh", str(launcher), "two words", "$(nope)", ""], cwd=tmp_path,
                            env={**os.environ, "HERMES_HOME": str(tmp_path / "custom/profiles/memory")}, capture_output=True, text=True)
    assert result.returncode == 7, result.stderr
    assert json.loads(result.stdout) == [str(tmp_path / "custom/profiles/memory"), ["two words", "$(nope)", ""]]
