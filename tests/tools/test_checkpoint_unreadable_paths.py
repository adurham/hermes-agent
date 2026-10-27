"""One unreadable path must not abort the whole checkpoint snapshot.

``git add -A`` stops with ``fatal: adding files failed`` on the first file it
cannot open, so a single root-owned key file used to leave a project with no
checkpoints at all. Staging now skips such paths, records them on the
snapshot, and still captures (and restores) everything else.
"""
from __future__ import annotations

import logging
import os
import sys

import pytest

import tools.checkpoint_manager as checkpoints

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="needs POSIX permissions enforced against a non-root user",
)


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoints, "CHECKPOINT_BASE", tmp_path / "checkpoints")
    work = tmp_path / "project"
    work.mkdir()
    (work / "keep.txt").write_text("original\n", encoding="utf-8")
    (work / "sub").mkdir()
    (work / "sub" / "nested.txt").write_text("nested original\n", encoding="utf-8")
    locked: list = []
    yield work, locked
    for path in locked:
        path.chmod(0o700 if path.is_dir() else 0o600)


def _lock(locked, path, mode=0o000):
    path.chmod(mode)
    locked.append(path)


def _assert_rest_restorable(manager, work, skipped_note):
    [entry] = manager.list_checkpoints(str(work))
    assert skipped_note in entry["reason"]
    (work / "keep.txt").write_text("clobbered\n", encoding="utf-8")
    (work / "sub" / "nested.txt").unlink()
    result = manager.restore(str(work), entry["hash"])
    assert result["success"], result
    assert (work / "keep.txt").read_text(encoding="utf-8") == "original\n"
    assert (work / "sub" / "nested.txt").read_text(encoding="utf-8") == "nested original\n"


def test_unreadable_file_is_skipped_and_the_rest_is_snapshotted(project, caplog):
    work, locked = project
    secret = work / "host.key"
    secret.write_text("private\n", encoding="utf-8")
    _lock(locked, secret)
    manager = checkpoints.CheckpointManager(enabled=True)

    with caplog.at_level(logging.WARNING, logger=checkpoints.__name__):
        assert manager.ensure_checkpoint(str(work), "baseline") is True

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("unreadable" in m and "host.key" in m for m in warnings), warnings
    _assert_rest_restorable(manager, work, "[unreadable paths not captured: host.key]")
    # The skipped file was left alone, not deleted or replaced.
    assert secret.exists() and (secret.stat().st_mode & 0o777) == 0


@pytest.mark.parametrize("mode", [0o000, 0o600], ids=["unlistable", "unsearchable"])
def test_unreadable_directory_is_skipped_and_the_rest_is_snapshotted(project, mode):
    work, locked = project
    run = work / "run"
    run.mkdir()
    (run / "state.txt").write_text("root state\n", encoding="utf-8")
    _lock(locked, run, mode)
    manager = checkpoints.CheckpointManager(enabled=True)

    assert manager.ensure_checkpoint(str(work), "baseline") is True

    store = checkpoints._store_path()
    [entry] = manager.list_checkpoints(str(work))
    tree = checkpoints._git_out(["ls-tree", "-r", "--name-only", entry["hash"]], store, str(work))
    assert "keep.txt" in tree.splitlines()
    assert not any(line.startswith("run/") for line in tree.splitlines())
    # 0o600 makes git abort; 0o000 is only a warning. Both are recorded.
    _assert_rest_restorable(manager, work, "[unreadable paths not captured: run]")


def test_previously_captured_path_that_becomes_unreadable_is_dropped(project):
    """A stale index entry for a now-unreadable path would otherwise ride
    along into every later snapshot and be checked out over it on restore."""
    work, locked = project
    secret = work / "host.key"
    secret.write_text("v1\n", encoding="utf-8")
    manager = checkpoints.CheckpointManager(enabled=True)
    assert manager.ensure_checkpoint(str(work), "first")

    _lock(locked, secret)
    (work / "keep.txt").write_text("changed\n", encoding="utf-8")
    manager.new_turn()
    assert manager.ensure_checkpoint(str(work), "second") is True

    store = checkpoints._store_path()
    latest = manager.list_checkpoints(str(work))[0]
    assert "[unreadable paths not captured: host.key]" in latest["reason"]
    tree = checkpoints._git_out(["ls-tree", "-r", "--name-only", latest["hash"]], store, str(work))
    assert "host.key" not in tree.splitlines()
    assert checkpoints._git_out(["show", f"{latest['hash']}:keep.txt"], store, str(work)) == "changed"


def test_genuine_git_failure_still_fails_the_snapshot(project):
    work, locked = project
    secret = work / "host.key"
    secret.write_text("private\n", encoding="utf-8")
    _lock(locked, secret)
    manager = checkpoints.CheckpointManager(enabled=True)
    store = checkpoints._store_path()
    assert checkpoints._init_store(store, str(work)) is None
    index = checkpoints._index_path(store, checkpoints._project_hash(str(work)))
    index.parent.mkdir(parents=True, exist_ok=True)
    lock = index.with_name(index.name + ".lock")
    lock.write_text("", encoding="utf-8")  # another git holds the index
    try:
        assert manager.ensure_checkpoint(str(work), "baseline") is False
    finally:
        lock.unlink()
    assert manager.list_checkpoints(str(work)) == []


def test_unreadable_paths_parses_git_diagnostics():
    stderr = "\n".join([
        "warning: unable to access 'pocs/tls/run/.gitignore': Permission denied",
        'error: open("adams-mbp.ts.net.key"): Permission denied',
        "error: unable to index file 'adams-mbp.ts.net.key'",
        "warning: could not open directory 'locked/': Permission denied",
        "locked/inner.txt: Permission denied",
        "fatal: unable to stat 'd/inner.txt': Permission denied",
        "fatal: Unable to create '/s/indexes/x.lock': File exists.",
        "fatal: unable to stat '/abs/x': Permission denied",
        "fatal: adding files failed",
    ])
    assert checkpoints._unreadable_paths(stderr) == [
        "adams-mbp.ts.net.key", "locked", "locked/inner.txt", "d",
    ]
