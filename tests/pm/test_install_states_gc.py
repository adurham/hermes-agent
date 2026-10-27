"""Dependency state of a deleted checkout is reclaimed; state anything live could still read, or
whose checkout this process merely cannot see, is kept (``pm/install_states.py``)."""

from __future__ import annotations

import os
import subprocess
import time

import pm.environments
import pytest
from hermes_cli.worktree_ops import _prune_stale_worktrees
from pm.filesystem import lock_fd
from pm.install_states import collect_orphan_install_states, orphan_install_states


def _state(installs, key, project_root):
    state = installs / key
    (state / "inputs").mkdir(parents=True)
    (state / "inputs" / ".project-root").write_text(str(project_root), encoding="utf-8")
    (state / "environments" / "gen" / "venv").mkdir(parents=True)
    return state


def test_startup_prune_reclaims_only_provably_deleted_checkouts(tmp_path, monkeypatch):
    installs = tmp_path / "home" / "installs"
    repo = tmp_path / "repo"
    (repo / ".worktrees").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    live_root = tmp_path / "home" / "hermes-agent"
    live_root.mkdir(parents=True)
    live = _state(installs, "aaaa", live_root)
    gone_scratch = _state(installs, "bbbb", tmp_path / "home" / "cache" / "scratch" / "clone")
    gone_worktree = _state(installs, "cccc", repo / ".worktrees" / "removed-by-hand")
    # Pre-record state (no .project-root) has no evidence either way: never touched.
    unknown = installs / "dddd"
    (unknown / "environments").mkdir(parents=True)
    # A data root shared across a mount boundary records checkouts this process cannot see:
    # a host install from inside a container, a container install from the host.
    host_install = _state(installs, "eeee", tmp_path / "other-host-home" / ".hermes" / "hermes-agent")
    container_install = _state(installs, "ffff", tmp_path / "opt-hermes")

    assert orphan_install_states(installs) == [gone_scratch, gone_worktree]
    monkeypatch.setattr(pm.environments, "installs_root", lambda: installs)
    # No tree under .worktrees/ is old enough to prune: the reclaim must still run.
    _prune_stale_worktrees(str(repo))
    assert not gone_scratch.exists() and not gone_worktree.exists()
    assert all(p.is_dir() for p in (live, unknown, host_install, container_install))


def test_held_orphan_is_kept(tmp_path):
    installs = tmp_path / "installs"
    locked = _state(installs, "dddd", tmp_path / "gone-a")
    leased = _state(installs, "eeee", tmp_path / "gone-b")
    runtime = _state(installs, "ffff", tmp_path / "gone-c")

    fds = [os.open(locked / ".install.lock", os.O_CREAT | os.O_RDWR, 0o600)]
    for lease_dir in (leased / "environments" / "gen" / ".leases",
                      runtime / "pm-runtime" / "generations" / "gen" / ".leases"):
        lease_dir.mkdir(parents=True)
        fds.append(os.open(lease_dir / "reader", os.O_CREAT | os.O_RDWR, 0o600))
    assert all(lock_fd(fd, wait=False) for fd in fds)
    try:
        assert collect_orphan_install_states(installs) == []
        assert locked.is_dir() and leased.is_dir() and runtime.is_dir()
    finally:
        for fd in fds:
            os.close(fd)
    # Locks released (owner exited): all are reclaimed on the next pass.
    assert sorted(p.name for p in collect_orphan_install_states(installs)) == ["dddd", "eeee", "ffff"]


@pytest.mark.platforms("posix")
@pytest.mark.skipif(getattr(os, "geteuid", lambda: 1)() == 0, reason="root ignores directory modes")
def test_partly_removed_orphan_keeps_its_record_until_retried(tmp_path):
    installs = tmp_path / "installs"
    state = _state(installs, "aaaa", tmp_path / "gone")
    stuck = state / "environments" / "gen" / "venv"
    (stuck / "pyvenv.cfg").write_text("", encoding="utf-8")
    stuck.chmod(0o500)  # its entries cannot be unlinked: the removal stops part-way
    try:
        assert collect_orphan_install_states(installs) == []
        assert orphan_install_states(installs) == [state]
    finally:
        stuck.chmod(0o700)
    assert collect_orphan_install_states(installs) == [state] and not state.exists()


def test_unprovable_missing_checkout_is_reclaimed_once_unused_past_retention(tmp_path):
    installs = tmp_path / "home" / "installs"
    present_root = tmp_path / "review" / "present"
    present_root.mkdir(parents=True)
    fresh = _state(installs, "aaaa", tmp_path / "review" / "fresh")
    idle = _state(installs, "bbbb", tmp_path / "review" / "idle")
    leased = _state(installs, "cccc", tmp_path / "review" / "leased")
    present = _state(installs, "dddd", present_root)
    lease_dir = leased / "environments" / "gen" / ".leases"
    lease_dir.mkdir()
    fd = os.open(lease_dir / "reader", os.O_CREAT | os.O_RDWR, 0o600)
    assert lock_fd(fd, wait=False)
    month_ago = time.time() - 30 * 86400
    for state in (idle, leased, present):
        for path in sorted(state.rglob("*"), reverse=True) + [state]:
            os.utime(path, (month_ago, month_ago), follow_symlinks=False)
    try:
        assert collect_orphan_install_states(installs) == [idle]
    finally:
        os.close(fd)
    assert all(p.is_dir() for p in (fresh, leased, present)) and not idle.exists()


@pytest.mark.parametrize(("days", "reclaimed"), [(0, False), (1, True), (None, False)])
def test_retention_days_from_config_yaml_decides_reclaim(tmp_path, days, reclaimed):
    """0 keeps an unprovable state forever; a custom value moves the cutoff (2 days idle: kept
    under the 14-day default, reclaimed under 1)."""
    from hermes_constants import get_hermes_home

    if days is not None:
        (get_hermes_home() / "config.yaml").write_text(
            f"worktrees:\n  install_state_retention_days: {days}\n", encoding="utf-8")
    installs = tmp_path / "home" / "installs"
    state = _state(installs, "aaaa", tmp_path / "review" / "gone")
    two_days_ago = time.time() - 2 * 86400
    for path in sorted(state.rglob("*"), reverse=True) + [state]:
        os.utime(path, (two_days_ago, two_days_ago), follow_symlinks=False)
    assert (collect_orphan_install_states(installs) == [state]) is reclaimed
    assert state.exists() is not reclaimed
