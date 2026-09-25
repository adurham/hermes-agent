"""Tests for scripts/autostash_cleanup.py — containment audit + safe drop of aged autostash entries.

`hermes update` parks ``hermes-update-autostash-*`` entries and only WARNS about aged ones; a
cleanup tool that gets containment wrong destroys the only copy of someone's work. The cases
below are the ones that matter, each on a synthetic repo (never the live checkout):

  * superseded-by-commit   -> dropped
  * superseded-by-removal  -> dropped only under --allow-removed, and only when the path was
                              TRACKED at the stash base
  * unique UNTRACKED work  -> never dropped (the bug this suite exists for: an untracked path is
                              absent from HEAD by definition, so "gone from HEAD" alone would
                              read as superseded)
  * fresh entries          -> left alone
  * report mode            -> mutates nothing
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS_DIR))

import autostash_cleanup as asc  # noqa: E402

STAMP_A = "20260801-120000"  # superseded by a later commit
STAMP_B = "20260802-110000"  # superseded by removal (path tracked, then deleted)
STAMP_C = "20260803-090000"  # unique untracked work


def _git(repo: Path, *args, date="2026-08-01T12:00:00Z"):
    env = {**__import__("os").environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, check=True).stdout.strip()


def _init(repo: Path) -> Path:
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _stash(repo: Path, stamp: str, include_untracked=False, date="2026-08-01T12:00:00Z"):
    args = ["stash", "push", "-q", "-m", f"{asc.PREFIX}{stamp}"]
    if include_untracked:
        args.insert(3, "--include-untracked")
    _git(repo, *args, date=date)


def _fixture(repo: Path) -> Path:
    """Three aged autostash entries + one fresh one, covering every disposition."""
    _init(repo)
    # (A) superseded by commit: the stashed content lands in a later commit
    (repo / "src" / "app.py").write_text("v2\n", encoding="utf-8")
    _stash(repo, STAMP_A)
    _git(repo, "stash", "apply", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "v2 lands", date="2026-08-01T13:00:00Z")
    # (B) superseded by removal: file was tracked, stashed, then deleted upstream
    (repo / "legacy.txt").write_text("tracked\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add legacy", date="2026-08-02T10:00:00Z")
    (repo / "legacy.txt").write_text("stashed-edit\n", encoding="utf-8")
    _stash(repo, STAMP_B, date="2026-08-02T11:00:00Z")
    _git(repo, "rm", "-q", "legacy.txt")
    _git(repo, "commit", "-qm", "drop legacy", date="2026-08-02T12:00:00Z")
    # (C) unique untracked work: exists in no commit anywhere
    (repo / "precious.txt").write_text("irreplaceable\n", encoding="utf-8")
    _stash(repo, STAMP_C, include_untracked=True, date="2026-08-03T09:00:00Z")
    # (D) fresh: must be left alone by every mode
    (repo / "src" / "app.py").write_text("v3\n", encoding="utf-8")
    _stash(repo, "20990101-000000")
    return repo


def _run(repo: Path, *args, expect=0):
    argv = sys.argv
    sys.argv = ["autostash_cleanup.py", "--repo", str(repo), *args]
    try:
        code = asc.main()
    finally:
        sys.argv = argv
    assert code == expect, f"expected exit {expect}, got {code}"
    return code


def _arc(repo: Path) -> str:
    """Archive OUTSIDE the repo (the real tool writes to ~/.hermes/backups)."""
    return str(repo.parent / "arc")


def _subjects(repo: Path):
    return _git(repo, "stash", "list", "--format=%s").splitlines()


def test_fixture_reproduces_the_four_dispositions(tmp_path):
    """Guard the fixture itself: the audit must classify A/B/C/D as intended."""
    repo = _fixture(tmp_path / "r")
    memo = {}
    entries = asc.entries(repo)
    assert len(entries) == 4
    by_subject = {e["subject"]: e for e in entries}

    a = by_subject[f"On main: {asc.PREFIX}{STAMP_A}"]
    _, _, uncontained_a, removed_a = asc.audit(repo, a, memo, allow_removed=True)
    assert uncontained_a == [] and removed_a == []  # in HEAD / contained in a commit

    b = by_subject[f"On main: {asc.PREFIX}{STAMP_B}"]
    _, _, uncontained_b, removed_b = asc.audit(repo, b, memo, allow_removed=True)
    assert uncontained_b == []
    assert [p for p, _, _ in removed_b] == ["legacy.txt"]  # tracked at base, gone from HEAD

    c = by_subject[f"On main: {asc.PREFIX}{STAMP_C}"]
    _, _, uncontained_c, removed_c = asc.audit(repo, c, memo, allow_removed=True)
    assert [p for p, _ in uncontained_c] == ["precious.txt"]
    assert removed_c == [], "unique untracked work must never be classified as superseded"


def test_report_mode_mutates_nothing(tmp_path):
    repo = _fixture(tmp_path / "r")
    before = _subjects(repo)
    _run(repo, expect=2)  # 2 = uncontained work present
    assert _subjects(repo) == before


def test_apply_without_allow_removed_keeps_removed_and_unique(tmp_path):
    repo = _fixture(tmp_path / "r")
    _run(repo, "--apply", "--archive-dir", _arc(repo), expect=2)
    remaining = _subjects(repo)
    assert f"On main: {asc.PREFIX}{STAMP_A}" not in remaining  # provably superseded: dropped
    assert f"On main: {asc.PREFIX}{STAMP_B}" in remaining      # removal needs the flag
    assert f"On main: {asc.PREFIX}{STAMP_C}" in remaining      # unique work: kept
    assert f"On main: {asc.PREFIX}20990101-000000" in remaining  # fresh: untouched


def test_apply_with_allow_removed_drops_removal_but_keeps_unique_work(tmp_path):
    """The whole point: --allow-removed must not become a licence to drop untracked work."""
    repo = _fixture(tmp_path / "r")
    _run(repo, "--apply", "--allow-removed", "--archive-dir", _arc(repo), expect=2)
    remaining = _subjects(repo)
    assert f"On main: {asc.PREFIX}{STAMP_A}" not in remaining
    assert f"On main: {asc.PREFIX}{STAMP_B}" not in remaining
    assert f"On main: {asc.PREFIX}{STAMP_C}" in remaining, "untracked unique work was dropped!"
    assert f"On main: {asc.PREFIX}20990101-000000" in remaining
    # and the unique work is still recoverable from its own entry
    assert _git(repo, "show", "stash@{1}^3:precious.txt") == "irreplaceable"


def test_archive_bundle_holds_every_dropped_entry(tmp_path):
    repo = _fixture(tmp_path / "r")
    _run(repo, "--apply", "--allow-removed", "--archive-dir", _arc(repo), expect=2)
    bundles = list(Path(_arc(repo)).glob("*.bundle"))
    assert len(bundles) == 1
    manifest = json.loads(bundles[0].with_suffix(".json").read_text(encoding="utf-8"))
    archived_subjects = {e["subject"] for e in manifest["entries"]}
    assert archived_subjects == {f"On main: {asc.PREFIX}{STAMP_A}", f"On main: {asc.PREFIX}{STAMP_B}"}
    heads = subprocess.run(["git", "bundle", "list-heads", str(bundles[0])], cwd=repo,
                           capture_output=True, text=True, check=True).stdout
    for entry in manifest["entries"]:
        assert entry["sha"] in heads


def test_head_and_tree_untouched_by_a_full_run(tmp_path):
    repo = _fixture(tmp_path / "r")
    head_before = _git(repo, "rev-parse", "HEAD")
    _run(repo, "--apply", "--allow-removed", "--archive-dir", _arc(repo), expect=2)
    assert _git(repo, "rev-parse", "HEAD") == head_before


def test_zero_warning_count_when_only_fresh_entries_remain(tmp_path):
    """With nothing aged left, the tool must be a no-op that reports 0 warnings (exit 0)."""
    repo = _init(tmp_path / "r")
    (repo / "src" / "app.py").write_text("fresh\n", encoding="utf-8")
    _stash(repo, "20990101-000000")
    _run(repo, expect=0)
    assert asc.warn_count(repo, 7) == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
