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


def test_non_ascii_unreadable_path_is_skipped_and_the_rest_is_snapshotted(project, caplog):
    """git echoes the raw path for a permission-denied file; a name outside ASCII
    must still be recognised, or the retry loop burns its rounds and the snapshot fails."""
    work, locked = project
    secret = work / "clé-秘密.key"
    secret.write_text("private\n", encoding="utf-8")
    _lock(locked, secret)
    manager = checkpoints.CheckpointManager(enabled=True)
    with caplog.at_level(logging.WARNING, logger="tools.checkpoint_manager"):
        assert manager._take(str(work), "non-ascii unreadable") is True
    assert str(secret.name) in caplog.text
    _assert_rest_restorable(manager, work, "unreadable paths not captured")


def test_root_collapsing_unreadable_report_fails_instead_of_excluding_everything(project, monkeypatch):
    """``fatal: unable to stat 'X'`` yields X's PARENT, so a top-level X collapses to the
    tree root. Excluding '.' would stage nothing and still report success — an empty
    checkpoint that a later rollback would restore over the project. It must fail instead."""
    work, _locked = project
    real_run_git = checkpoints._run_git

    def fake_run_git(args, store, working_dir, **kwargs):
        joined = " ".join(args)
        if args[:2] == ["add", "-A"] and "--pathspec-from-file" not in joined:
            return False, "", "fatal: unable to stat 'top-level.txt': Permission denied\n"
        return real_run_git(args, store, working_dir, **kwargs)

    monkeypatch.setattr(checkpoints, "_run_git", fake_run_git)
    store = checkpoints._store_path()
    store.mkdir(parents=True, exist_ok=True)
    index_file = checkpoints._index_path(store, checkpoints._project_hash(str(work)))
    ok, skipped, _err = checkpoints._stage_all(store, str(work), index_file)
    assert ok is False, "a root-collapsing report must fail the snapshot, not exclude the tree"
    assert skipped == [], "the tree root must never enter the skip list"


def test_unreadable_paths_never_returns_root_relative_or_absolute_paths():
    """The skip list is fed to ``:(exclude,literal)`` pathspecs; a root, parent or absolute
    entry there would exclude far more than the unreadable path."""
    stderr = "\n".join([
        "fatal: unable to stat 'top-level.txt': Permission denied",
        "error: open(\"../outside\"): Permission denied",
        "error: open(\"/etc/shadow\"): Permission denied",
        "error: open(\"sub/real.txt\"): Permission denied",
    ])
    assert checkpoints._unreadable_paths(stderr) == ["sub/real.txt"]


def test_rollback_never_deletes_a_path_the_checkpoint_could_not_capture(project):
    """``unlink`` needs write permission on the PARENT, not the file, so a safe rollback would
    happily delete a file Hermes once wrote that later became unreadable and that the snapshot
    had to skip — no checkpoint holds a copy. It must be kept and reported instead."""
    work, locked = project
    manager = checkpoints.CheckpointManager(enabled=True)
    assert manager._take(str(work), "before the key appears") is True
    [before] = manager.list_checkpoints(str(work))

    secret = work / "host.key"
    secret.write_text("private\n", encoding="utf-8")
    manager.record_agent_write(str(secret))          # ledger: Hermes wrote it
    _lock(locked, secret)                            # then it became unreadable (root-owned later)
    (work / "keep.txt").write_text("edited\n", encoding="utf-8")
    assert manager._take(str(work), "key is unreadable") is True

    result = manager.restore(str(work), before["hash"], safe=True)
    assert result["success"], result
    assert secret.exists(), "rollback deleted a file no checkpoint could capture"
    # A skipped path is excluded from staging, so it never enters the restore diff at all:
    # not restored, not deleted, not reported as either.
    assert "host.key" not in result.get("restored_files", [])
    assert "host.key" not in result.get("skipped_user_edits", [])
    assert "host.key" not in result.get("failed_deletes", [])


def test_file_captured_then_made_unreadable_round_trips_between_checkpoints(project):
    """The reverse ordering the first rollback test could not see: a file IS in snapshot 1, then
    becomes unreadable before snapshot 2 (so staging drops it from the index), then the user
    restores forward and back. It must keep existing with its bytes in every direction."""
    work, locked = project
    manager = checkpoints.CheckpointManager(enabled=True)
    key = work / "secret.key"
    key.write_text("PRIVATE-BYTES\n", encoding="utf-8")

    assert manager._take(str(work), "one: key readable and captured") is True
    [snapshot_one] = manager.list_checkpoints(str(work))

    _lock(locked, key)                                   # root-owned in the real case
    (work / "keep.txt").write_text("edited\n", encoding="utf-8")
    assert manager._take(str(work), "two: key unreadable") is True
    snapshots = manager.list_checkpoints(str(work))
    snapshot_two = next(s for s in snapshots if s["reason"].startswith("two"))
    assert "unreadable paths not captured: secret.key" in snapshot_two["reason"]

    def bytes_as_user():
        """Read it the way the owner would after regaining access, then lock it again."""
        assert key.exists(), "the restore deleted a file it never captured"
        os.chmod(key, 0o600)
        data = key.read_bytes()
        os.chmod(key, 0o000)
        return data

    # Forward: restore to the snapshot that does NOT contain the key. It must not be deleted.
    forward = manager.restore(str(work), snapshot_two["hash"], safe=True)
    assert forward["success"], forward
    assert bytes_as_user() == b"PRIVATE-BYTES\n"

    # Back: restore to the snapshot that DOES contain it. It must come back byte-identical.
    back = manager.restore(str(work), snapshot_one["hash"], safe=True)
    assert back["success"], back
    assert bytes_as_user() == b"PRIVATE-BYTES\n"
    assert (work / "keep.txt").read_text(encoding="utf-8") == "original\n"


def test_ledger_owned_file_that_became_unreadable_survives_a_restore(project):
    """The ordering that could reach the restore plan's delete branch: Hermes itself wrote the
    file (agent-write ledger entry, captured in snapshot 1), the user then makes it unreadable,
    and snapshot 2 therefore cannot capture it. Restoring to snapshot 2 must not delete it just
    because unreadability made it invisible to the diff."""
    work, locked = project
    manager = checkpoints.CheckpointManager(enabled=True)
    key = work / "secret.key"
    key.write_text("PRIVATE-BYTES\n", encoding="utf-8")
    manager.record_agent_write(str(key))            # ledger: Hermes wrote this file
    assert manager._take(str(work), "one: captured") is True
    [snapshot_one] = manager.list_checkpoints(str(work))

    _lock(locked, key)                              # user makes it unreadable
    (work / "keep.txt").write_text("edited\n", encoding="utf-8")
    assert manager._take(str(work), "two: skipped") is True
    snapshot_two = next(s for s in manager.list_checkpoints(str(work)) if s["reason"].startswith("two"))

    result = manager.restore(str(work), snapshot_two["hash"], safe=True)
    assert result["success"], result
    assert key.exists(), "a restore deleted a file that exists on disk and only became unreadable"
    if "secret.key" in result.get("restored_files", []):
        # If it ever is written back, the checkpointed bytes must be what lands.
        os.chmod(key, 0o600)
        assert key.read_bytes() == b"PRIVATE-BYTES\n"
    assert key.exists() and snapshot_one["hash"] != snapshot_two["hash"]
