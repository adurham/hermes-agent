"""Regression coverage for the retirement snapshot fixes.

Pins the retained-link inventory contract (plugins/skills links stay live and
rechecked, never followed), empty-directory preservation during relocation,
and fail-closed behavior for links into the removal footprint.
"""
import json
import os
import socket
import sqlite3
from pathlib import Path

import pytest

from hermes_cli.backup_migration import (
    LINK_INVENTORY,
    retained_link,
    snapshot_migration_home,
    verify_retained_links,
)


def _make_minimal_home(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text("model:\n  provider: openrouter\n")
    with sqlite3.connect(root / "state.db") as conn:
        conn.execute("CREATE TABLE witness(value)")
        conn.execute("INSERT INTO witness VALUES ('kept')")
    (root / "profiles" / "default").mkdir(parents=True)
    (root / "profiles" / "default" / "config.yaml").write_text("profile: default\n")


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable in test environment: {exc}")


class TestRetainedLinkInventory:
    def test_classifies_plugins_and_skills_links_only(self, tmp_path):
        payload = tmp_path / "payload"
        link = payload / "profiles" / "default" / "plugins" / "mnemosyne-wrapper" / "runtime"
        link.parent.mkdir(parents=True)
        target = payload / "shared-runtime"
        target.mkdir()
        _symlink_or_skip(link, target)

        row = retained_link(link, link.relative_to(payload), ())
        assert row["path"] == "profiles/default/plugins/mnemosyne-wrapper/runtime"
        assert row["target"] == str(target)
        assert row["resolved"] == str(target.resolve())

    def test_rejects_links_outside_plugins_and_skills(self, tmp_path):
        link = tmp_path / "config.yaml.link"
        target = tmp_path / "config.yaml.target"
        target.write_text("x")
        _symlink_or_skip(link, target)
        with pytest.raises(ValueError, match="cannot be a link"):
            retained_link(link, Path("config.yaml.link"), ())

    def test_rejects_links_resolving_into_the_removal_footprint(self, tmp_path):
        home = tmp_path / "home"
        removal = tmp_path / "preview-app"
        (removal / "inner").mkdir(parents=True)
        link = home / "plugins" / "runtime"
        link.parent.mkdir(parents=True)
        _symlink_or_skip(link, removal / "inner")
        with pytest.raises(ValueError, match="removal footprint"):
            retained_link(link, Path("plugins/runtime"), (removal,))

    def test_verify_detects_a_link_that_changed_after_snapshot(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        target = tmp_path / "runtime-target"
        target.mkdir()
        link = home / "plugins" / "runtime"
        link.parent.mkdir(parents=True)
        _symlink_or_skip(link, target)
        snapshot = tmp_path / "snapshot"
        snapshot.mkdir()
        drifted = {
            "path": "plugins/runtime",
            "target": str(tmp_path / "elsewhere"),
            "resolved": str(target.resolve()),
        }
        (snapshot / LINK_INVENTORY).write_text(json.dumps([drifted]))
        with pytest.raises(ValueError, match="changed during migration"):
            verify_retained_links(home, snapshot, ())


class TestSnapshotMigrationHome:
    def test_preserves_empty_plugin_and_skill_directories(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        snapshot = tmp_path / "snapshot"
        _make_minimal_home(home)
        (home / "profiles" / "default" / "plugins" / "empty-plugin").mkdir(parents=True)
        (home / "skills" / "empty-skill").mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(home))
        snapshot_migration_home(home, snapshot)
        assert (snapshot / "profiles" / "default" / "plugins" / "empty-plugin").is_dir()
        assert (snapshot / "skills" / "empty-skill").is_dir()

    def test_rejects_relocation_links_without_explicit_preservation(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        snapshot = tmp_path / "snapshot"
        _make_minimal_home(home)
        target = tmp_path / "runtime-target"
        target.mkdir()
        link = home / "plugins" / "runtime-link"
        link.parent.mkdir(parents=True)
        _symlink_or_skip(link, target)
        monkeypatch.setenv("HERMES_HOME", str(home))
        with pytest.raises(ValueError, match="explicit external preservation"):
            snapshot_migration_home(home, snapshot)

    def test_rejects_links_into_removal_footprint_even_with_roots(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        snapshot = tmp_path / "snapshot"
        _make_minimal_home(home)
        removal = home / "preview-home"
        (removal / "inner").mkdir(parents=True)
        link = home / "plugins" / "runtime"
        link.parent.mkdir(parents=True)
        _symlink_or_skip(link, removal / "inner")
        monkeypatch.setenv("HERMES_HOME", str(home))
        with pytest.raises(ValueError, match="removal footprint"):
            snapshot_migration_home(home, snapshot, retained_removal_roots=(removal,))

    def test_retains_plugins_and_skills_links_with_verified_inventory(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        snapshot = tmp_path / "snapshot"
        _make_minimal_home(home)
        removal = home / "preview-home"
        removal.mkdir()
        runtime_target = tmp_path / "runtime-target"
        runtime_target.mkdir()
        skill_target = tmp_path / "skill-target"
        skill_target.mkdir()
        runtime_link = home / "plugins" / "runtime-link"
        runtime_link.parent.mkdir(parents=True)
        _symlink_or_skip(runtime_link, runtime_target)
        skill_link = home / "skills" / "shared-skill"
        skill_link.parent.mkdir(parents=True)
        _symlink_or_skip(skill_link, skill_target)
        monkeypatch.setenv("HERMES_HOME", str(home))
        snapshot_migration_home(home, snapshot, retained_removal_roots=(removal,))
        inventory = json.loads((snapshot / LINK_INVENTORY).read_text())
        assert {row["path"] for row in inventory} == {
            "plugins/runtime-link",
            "skills/shared-skill",
        }
        # Links stay live in the source home; the snapshot never contains copies.
        assert runtime_link.is_symlink()
        assert not (snapshot / "plugins" / "runtime-link").exists()
        verify_retained_links(home, snapshot, (removal,))

    @pytest.mark.platforms("linux")
    def test_rejects_unix_domain_sockets(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        snapshot = tmp_path / "snapshot"
        _make_minimal_home(home)
        monkeypatch.setenv("HERMES_HOME", str(home))
        # AF_UNIX paths cap at ~108 bytes; bind by relative name from inside ``home``.
        monkeypatch.chdir(home)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as gateway_socket:
            gateway_socket.bind("runtime.sock")
            with pytest.raises(ValueError, match="special file"):
                snapshot_migration_home(home, snapshot)
