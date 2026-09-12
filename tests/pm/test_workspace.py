"""pm.workspace: the generated uv-workspace root for plugin deps.

The workspace root is a pm-GENERATED project (never the committed
pyproject.toml — sealed installs are read-only and member lists are
machine-specific). Its pyproject = core's pyproject verbatim +
``[tool.uv.workspace] members`` pointing at each snapshotted plugin.
``uv lock`` unions core + plugin deps into ONE lock; conflict = loud refusal.
"""

from __future__ import annotations

import os
import subprocess
import json
import shutil
import sys
from pathlib import Path

import pytest

import pm.workspace as ws
from pm.environment import managed_environment


@pytest.fixture(autouse=True)
def isolated_machine_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))


@pytest.fixture
def layout(tmp_path, monkeypatch):
    """A fake install: core repo with pyproject, plugin dirs, store."""
    from tests.pm.test_workspace_build_inputs import _wheel

    wheels = tmp_path / "wheels"
    wheels.mkdir()
    _wheel(wheels, "httpx", "0.28.1")
    _wheel(wheels, "rich", "13.9.4")
    uv = shutil.which("uv")
    assert uv
    monkeypatch.setattr("pm._uv._toolchain", lambda **kwargs: (Path(uv), Path(sys.executable)))
    core = tmp_path / "core"
    core.mkdir()
    (core / "pyproject.toml").write_text(
        "[project]\n"
        'name = "hermes-agent"\n'
        'version = "0.1.0"\n'
        'requires-python = ">=3.11"\n'
        'dependencies = ["httpx==0.28.1"]\n'
        '[tool.uv]\npackage=false\nno-index=true\n'
        f'find-links=[{json.dumps(wheels.as_posix())}]\n',
        encoding="utf-8",
    )
    plugins = tmp_path / "home" / "plugins"
    plug_a = plugins / "plug-a"
    plug_a.mkdir(parents=True)
    (plug_a / "plugin.yaml").write_text("name: plug-a\n", encoding="utf-8")
    (plug_a / "pyproject.toml").write_text(
        "[project]\nname = \"plug-a\"\nversion = \"0.1.0\"\n"
        'requires-python = ">=3.11"\ndependencies = ["rich==13.9.4"]\n',
        encoding="utf-8",
    )
    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setattr(ws.paths, "repo_root", lambda: core)
    monkeypatch.setattr(ws.paths, "store_root", lambda: store)
    return tmp_path, core, plug_a, store


def test_build_writes_core_pyproject_verbatim(layout):
    tmp, core, plug_a, _ = layout
    root = tmp / "workspace"
    ws.lock_and_sync([plug_a], [], root=root, source=core, seed_lock=None,
                     environment=managed_environment(tmp / "env"))
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    core_text = (core / "pyproject.toml").read_text(encoding="utf-8")
    # core's project table is carried verbatim (name, deps, requires-python)
    assert 'name = "hermes-agent"' in text
    assert 'dependencies = ["httpx==0.28.1"]' in text
    assert 'requires-python = ">=3.11"' in text
    # nothing else was invented
    for line in core_text.strip().splitlines():
        assert line in text


def test_members_keep_their_source_with_the_generation(layout):
    import tomllib

    tmp, core, plug_a, _ = layout
    root = tmp / "workspace"
    ws.lock_and_sync([plug_a], [], root=root, source=core, seed_lock=None,
                     environment=managed_environment(tmp / "env"))
    document = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    [relative] = document["tool"]["uv"]["workspace"]["members"]
    copied = root / relative / "pyproject.toml"
    assert copied.resolve().is_relative_to(root.resolve())
    before = copied.read_bytes()
    assert before == (plug_a / "pyproject.toml").read_bytes()
    (plug_a / "pyproject.toml").write_bytes(b"changed after publication")
    assert copied.read_bytes() == before


def test_preparation_refuses_existing_workspace_without_mutating_it(layout):
    from pm.package import InstallError

    tmp, core, plug_a, _ = layout
    root = tmp / "workspace"
    environment = managed_environment(tmp / "env")
    kwargs = dict(root=root, source=core, seed_lock=None,
                  environment=environment)
    ws.lock_and_sync([plug_a], [], **kwargs)
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    (plug_a / "pyproject.toml").write_text('changed after publication')
    with pytest.raises(InstallError, match="fresh"):
        ws.lock_and_sync([], [], **kwargs)
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


def test_missing_explicit_seed_cannot_silently_resolve_new_versions(layout):
    tmp, core, plug_a, _ = layout
    with pytest.raises(FileNotFoundError):
        ws.lock_and_sync([plug_a], [], root=tmp / "workspace", source=core,
                         seed_lock=tmp / "missing.lock", environment=managed_environment(tmp / "env"))
    assert not (tmp / "env").exists()


def test_zero_plugins_still_builds_a_root_with_no_members(layout):
    tmp, core, _, _ = layout
    root = tmp / "workspace"
    ws.lock_and_sync([], [], root=root, source=core, seed_lock=None,
                     environment=managed_environment(tmp / "env"))
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert 'name = "hermes-agent"' in text
    assert "[tool.uv.workspace]" not in text or "members = []" in text


def test_member_stamp_hash_changes_with_plugin_set(layout):
    _, _, plug_a, _ = layout
    stamp_empty = ws.members_stamp([])
    stamp_a = ws.members_stamp([plug_a])
    stamp_b = ws.members_stamp([plug_a, plug_a])  # dedupes to same set
    assert stamp_empty != stamp_a
    assert stamp_a == stamp_b


def test_member_stamp_changes_when_pyproject_content_changes(tmp_path):
    """Task 4 contract: a pulled plugin with changed pins must move the
    stamp — path-only hashing left dep bumps invisible to the venv sync."""
    plug = tmp_path / "plug"
    plug.mkdir()
    (plug / "pyproject.toml").write_text(
        'dependencies = ["pkg==1.0.0"]\n', encoding="utf-8"
    )
    before = ws.members_stamp([plug])
    # the plugin update: same dir, new pins
    (plug / "pyproject.toml").write_text(
        'dependencies = ["pkg==2.0.0"]\n', encoding="utf-8"
    )
    after = ws.members_stamp([plug])
    assert before != after
    # no pyproject at all: still hashable (the dir identity carries it)
    bare = tmp_path / "bare"
    bare.mkdir()
    assert ws.members_stamp([bare]) != before


def test_member_stamp_missing_pyproject_does_not_crash(tmp_path):
    """A member dir whose pyproject vanished mid-scan hashes on path only."""
    plug = tmp_path / "ghost"
    plug.mkdir()
    (plug / "pyproject.toml").write_text("x\n", encoding="utf-8")
    first = ws.members_stamp([plug])
    (plug / "pyproject.toml").unlink()
    second = ws.members_stamp([plug])
    assert first != second  # content term dropped out, stamp moved


def test_enabled_member_dirs_finds_enabled_dep_plugins(tmp_path, monkeypatch):
    plugins = tmp_path / "plugins"
    plugins.mkdir(parents=True)

    # modern plugin: pyproject.toml
    modern = plugins / "modern-plug"
    modern.mkdir()
    (modern / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

    # legacy plugin: pip_dependencies in plugin.yaml, no pyproject
    legacy = plugins / "legacy-plug"
    legacy.mkdir()
    (legacy / "plugin.yaml").write_text(
        "name: legacy-plug\npip_dependencies:\n  - \"requests>=2\"\n",
        encoding="utf-8",
    )

    # dep-less plugin: neither — not a member even when enabled
    plain = plugins / "plain-plug"
    plain.mkdir()
    (plain / "plugin.yaml").write_text("name: plain-plug\n", encoding="utf-8")

    # dep-carrying but NOT-ENABLED plugin — must not join the union
    orphan = plugins / "orphan-plug"
    orphan.mkdir()
    (orphan / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

    # enabled order = enable recency (legacy enabled first/older, modern
    # newest LAST) — discovery preserves the configured order.
    monkeypatch.setattr(
        "pm.plugins_state.enabled_plugins_ordered",
        lambda **kwargs: {plugins: ["legacy-plug", "modern-plug", "plain-plug"]},
    )
    found = ws.enabled_member_dirs()
    names = [p.name for p in found]
    assert names == ["legacy-plug", "modern-plug"]
    assert "orphan-plug" not in names


def test_enabled_member_dirs_empty_when_nothing_enabled(tmp_path, monkeypatch):
    plugins = tmp_path / "plugins"
    plugins.mkdir(parents=True)
    member = plugins / "member"
    member.mkdir()
    (member / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

    monkeypatch.setattr(
        "pm.plugins_state.enabled_plugins_ordered", lambda **kwargs: {}
    )
    assert ws.enabled_member_dirs() == []


def test_enabled_member_dirs_ignores_non_profile_entries(tmp_path, monkeypatch):
    import pm.plugins_state as pstate

    home = tmp_path / "home"
    profiles = home / "profiles"
    profiles.mkdir(parents=True)
    member = profiles / "work" / "plugins" / "member"
    member.mkdir(parents=True)
    (member / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (profiles / "work" / "config.yaml").write_text(
        "plugins:\n  enabled: [member]\n", encoding="utf-8",
    )
    (profiles / "README.txt").write_text("not a profile", encoding="utf-8")
    monkeypatch.setattr("hermes_cli.runtime_paths.dependency_home_root", lambda: home)

    assert pstate._all_homes() == [home, profiles / "work"]
    assert ws.enabled_member_dirs() == [member]


# --- classified failures + staging surface (FINAL-RUNTIME-CONTRACT) ---


def test_classify_resolver_conflict_is_resolutionconflict():
    from pm.package import InstallError
    from pm.workspace import ResolutionConflict, classify_uv_failure

    err = classify_uv_failure(
        "lock", 1,
        "  x No solution found for `hermes-agent>=0.1.0` because only the "
        "following versions are available:\n",
    )
    assert isinstance(err, ResolutionConflict)
    assert isinstance(err, InstallError)
    assert "no solution found" in str(err).lower()


def test_classify_network_failure_stays_generic():
    from pm.workspace import ResolutionConflict, classify_uv_failure

    err = classify_uv_failure("lock", 1, "error: Failed to fetch https://pypi.org (timed out)")
    assert not isinstance(err, ResolutionConflict)


def test_sync_failure_is_never_a_conflict(layout, monkeypatch):
    from pm.package import InstallError

    tmp, core, _, _ = layout
    environment = managed_environment(tmp / "candidate")
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kwargs:
                        subprocess.CompletedProcess(cmd, 1, "", "Failed to download wheel"))
    with pytest.raises(InstallError) as excinfo:
        ws.lock_and_sync([], [], root=tmp / "workspace", source=core,
                         seed_lock=None, environment=environment, frozen=True)
    assert not isinstance(excinfo.value, ws.ResolutionConflict)


def test_staging_root_and_env_are_honored_without_live_mutation(layout, monkeypatch):
    tmp, core, _, _ = layout
    staging = tmp / "staging-ws"
    monkeypatch.setenv("PM_WORKSPACE_TEST_SENTINEL", "live")
    environment = managed_environment(tmp / "staging-venv", env={
        "PATH": "/staged/bin", "PM_WORKSPACE_TEST_SENTINEL": "staged",
    })
    # The prepared environment is authoritative; workspace never discovers tools.
    monkeypatch.setattr(shutil, "which", lambda *args, **kwargs: pytest.fail("PATH discovery"))
    seen = []
    def run(cmd, **kwargs):
        seen.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(subprocess, "run", run)
    ws.lock_and_sync([], [], root=staging, source=core, seed_lock=None, environment=environment)
    assert [cmd[1] for cmd, _ in seen] == ["lock", "sync"]
    for cmd, kwargs in seen:
        assert Path(cmd[0]) == environment.uv
        assert Path(kwargs["cwd"]) == staging
        assert kwargs["env"]["PM_WORKSPACE_TEST_SENTINEL"] == "staged"
        assert kwargs["env"]["UV_CACHE_DIR"] == str(environment.cache)
        assert kwargs["env"]["UV_PROJECT_ENVIRONMENT"] == str(environment.destination)
        assert kwargs["env"]["UV_PYTHON"] == str(environment.python)
    assert os.environ["PM_WORKSPACE_TEST_SENTINEL"] == "live"
