"""Tests for the post-pull HEAD-movement gate in ``hermes update``.

Issue #79678: a detached/pinned checkout can report "N new commit(s)"
against origin, run the ff-only merge successfully, and still sit on the
old commit afterward (the branch-switch step re-detaches to the raw SHA).
Before this guard ``hermes update`` printed "✓ Code updated!" and
reinstalled deps + rebuilt the desktop app against the stale tree — no
error, no warning. The gate compares the pre-pull and post-pull HEAD SHA
and fails loudly when the update was a no-op.
"""

from types import SimpleNamespace

from copy import deepcopy

import pytest

from hermes_cli import main as hermes_main
from hermes_cli import update_cmd
from hermes_cli.update_inventory import UpdatePlan


@pytest.fixture(autouse=True)
def _isolate_venv_holders(monkeypatch):
    """The update flow's venv-holder guard sees the live gateway processes on
    a dev machine and aborts with SystemExit 2 before reaching the HEAD-move
    gate under test.  Isolate it so the test exercises the intended path."""
    monkeypatch.setattr("hermes_cli.update_cmd_windows._detect_venv_python_processes", lambda: [])


def _make_head_moved_side_effect(pre_sha="abc123", post_sha="def456"):
    """Simulate git commands where HEAD advances from pre_sha to post_sha."""
    calls = {"n": 0}

    def side_effect(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)

        # git rev-parse --abbrev-ref HEAD  (get current branch)
        if "rev-parse" in joined and "--abbrev-ref" in joined:
            return SimpleNamespace(returncode=0, stdout="main\n", stderr="")

        # git rev-list HEAD..origin/main --count  (behind count)
        if "rev-list" in joined:
            return SimpleNamespace(returncode=0, stdout="3\n", stderr="")

        # git rev-parse HEAD  — pre-pull capture (first call) sees pre_sha;
        # post-pull capture (second call) sees post_sha. get_version_info
        # is mocked in _patch_update_deps so the startup banner makes no
        # rev-parse calls of its own.
        if joined.endswith("rev-parse HEAD"):
            if calls["n"] < 1:
                calls["n"] += 1
                return SimpleNamespace(returncode=0, stdout=f"{pre_sha}\n", stderr="")
            return SimpleNamespace(returncode=0, stdout=f"{post_sha}\n", stderr="")

        # Everything else (merge, checkout, etc.) succeeds quietly.
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return side_effect


def _make_head_pinned_side_effect(sha="abc123"):
    """Simulate a detached checkout pinned to ``sha``: HEAD never moves."""

    def side_effect(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)

        if "rev-parse" in joined and "--abbrev-ref" in joined:
            return SimpleNamespace(returncode=0, stdout="HEAD\n", stderr="")

        if "rev-list" in joined:
            return SimpleNamespace(returncode=0, stdout="3\n", stderr="")

        if joined.endswith("rev-parse HEAD"):
            return SimpleNamespace(returncode=0, stdout=f"{sha}\n", stderr="")

        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return side_effect


def _patch_update_deps(monkeypatch, tmp_path, run_side_effect):
    """Exercise the Git gate, capturing the request before the child runs."""
    monkeypatch.setattr(hermes_main.subprocess, "run", run_side_effect)
    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", tmp_path)
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(hermes_main, "_resolve_update_branch", lambda args: "main")
    monkeypatch.setattr(
        hermes_main, "_get_origin_url",
        lambda *a, **k: "https://github.com/NousResearch/hermes-agent.git",
    )
    monkeypatch.setattr(update_cmd, "_is_fork", lambda *a, **k: False)
    monkeypatch.setattr(hermes_main, "_stash_local_changes_if_needed", lambda *a, **k: None)
    monkeypatch.setattr(hermes_main, "_run_pre_update_backup", lambda *a: "snapshot-before-pull")
    monkeypatch.setattr(hermes_main, "_pause_windows_gateways_for_update", lambda: None)
    monkeypatch.setattr(hermes_main, "_resume_windows_gateways_after_update", lambda *a: None)
    monkeypatch.setattr(hermes_main, "_install_hangup_protection", lambda **k: {"installed": False})
    monkeypatch.setattr(hermes_main, "_finalize_update_output", lambda *a: None)
    # Keep incidental version/inventory probes out of the mock's pre/post HEAD count.
    monkeypatch.setattr(
        "hermes_cli.version_info.get_version_info", lambda *a, **k: SimpleNamespace(),
    )
    plan = UpdatePlan(install_method="git", expected_sha="abc123", profiles=["default"])
    monkeypatch.setattr("hermes_cli.update_inventory.collect_runtime_inventory", lambda: plan)
    monkeypatch.setattr("hermes_cli.update_receipt._code_identity", lambda **k: {"commit": "abc123"})
    requests = []

    def capture(request):
        requests.append(deepcopy(request))
        return {"exit_code": 0, "receipt": None}

    monkeypatch.setattr(update_cmd, "run_completion", capture)
    return requests, plan


def test_update_hands_off_verified_head_when_it_moves(monkeypatch, tmp_path, capsys):
    """Only the verified new tree and its pre-update state reach completion."""
    args = SimpleNamespace(branch=None, yes=False, force=False, force_venv=False)
    requests, plan = _patch_update_deps(monkeypatch, tmp_path, _make_head_moved_side_effect())

    hermes_main.cmd_update(args)  # completes normally (no SystemExit)

    out = capsys.readouterr().out
    assert "Code did not move" not in out
    request, = requests
    assert request["source"] == str(tmp_path.resolve())
    assert request["branch"] == "main"
    assert request["expected_sha"] == "def456"
    assert request["plan"] == plan.to_dict()
    assert request["receipt"]["plan"] == plan.to_dict()
    assert request["snapshot_id"] == "snapshot-before-pull"


def test_update_fails_loudly_when_head_pinned(monkeypatch, tmp_path, capsys):
    """A detached/pinned HEAD that never moves must fail loudly, not print
    '✓ Code updated!' against the stale tree."""
    args = SimpleNamespace(branch=None, yes=False, force=False, force_venv=False)
    requests, _ = _patch_update_deps(monkeypatch, tmp_path, _make_head_pinned_side_effect())

    with pytest.raises(SystemExit) as exc_info:
        hermes_main.cmd_update(args)

    assert exc_info.value.code == 1
    assert requests == []
    out = capsys.readouterr().out
    assert "Code did not move" in out
    assert "✓ Code updated!" not in out
    assert "checkout main" in out
