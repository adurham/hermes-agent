"""Tests for agent/hot_tier_audit.py — hot-tier stale-path audit (dry-run MVP)."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from tools.memory_tool import ENTRY_DELIMITER


@pytest.fixture
def audit_env(monkeypatch, tmp_path):
    """Isolate HERMES_HOME + reload modules so every test starts clean."""
    home = tmp_path / ".hermes"
    home.mkdir()
    memories = home / "memories"
    memories.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))

    import hermes_constants
    importlib.reload(hermes_constants)
    from agent import hot_tier_audit
    importlib.reload(hot_tier_audit)
    return {"home": home, "memories": memories, "mod": hot_tier_audit}


def _write_memory(memories_dir: Path, entries, filename="MEMORY.md"):
    (memories_dir / filename).write_text(ENTRY_DELIMITER.join(entries), encoding="utf-8")


# ---------------------------------------------------------------------------
# classify_entries
# ---------------------------------------------------------------------------

def test_no_entries_returns_empty_summary(audit_env):
    mod = audit_env["mod"]
    summary = mod.run_hot_tier_audit(dry_run=True)
    assert summary["entries_checked"] == 0
    assert summary["stale_path_candidates"] == []


def test_detects_stale_path_in_entry(audit_env, tmp_path):
    mod = audit_env["mod"]
    ghost_dir = tmp_path / "ghost_repo"
    ghost_dir.mkdir()
    entry = "Old repo lived at ~/ghost_repo before it moved."
    ghost_dir.rmdir()  # now the path no longer exists

    _write_memory(audit_env["memories"], [entry])

    summary = mod.run_hot_tier_audit(dry_run=True)
    assert summary["entries_checked"] == 1
    assert len(summary["stale_path_candidates"]) == 1
    candidate = summary["stale_path_candidates"][0]
    assert candidate["is_stale_path_candidate"] is True
    assert "~/ghost_repo" in candidate["extracted_paths"]


def test_does_not_flag_valid_existing_path(audit_env, tmp_path):
    mod = audit_env["mod"]
    real_dir = tmp_path / "real_repo"
    real_dir.mkdir()
    entry = "The repo lives at ~/real_repo and is actively used."

    _write_memory(audit_env["memories"], [entry])

    summary = mod.run_hot_tier_audit(dry_run=True)
    assert summary["entries_checked"] == 1
    assert summary["stale_path_candidates"] == []


def test_entry_with_no_paths_not_flagged(audit_env):
    mod = audit_env["mod"]
    entry = "Prefers concise commit messages over verbose ones."

    _write_memory(audit_env["memories"], [entry])

    summary = mod.run_hot_tier_audit(dry_run=True)
    assert summary["entries_checked"] == 1
    assert summary["stale_path_candidates"] == []


def test_dry_run_never_mutates_files(audit_env, tmp_path):
    mod = audit_env["mod"]
    ghost_dir = tmp_path / "ghost_repo2"
    ghost_dir.mkdir()
    entry = "Old repo lived at ~/ghost_repo2."
    ghost_dir.rmdir()

    memory_path = audit_env["memories"] / "MEMORY.md"
    _write_memory(audit_env["memories"], [entry])
    before = memory_path.read_bytes()

    mod.run_hot_tier_audit(dry_run=True)

    after = memory_path.read_bytes()
    assert before == after


def test_live_mode_raises_not_implemented(audit_env):
    mod = audit_env["mod"]
    with pytest.raises(NotImplementedError):
        mod.run_hot_tier_audit(dry_run=False)


# ---------------------------------------------------------------------------
# classify_entries directly
# ---------------------------------------------------------------------------

def test_classify_entries_returns_expected_shape(audit_env, tmp_path):
    mod = audit_env["mod"]
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    entries = [f"Uses {real_dir} for stuff.", "No path here at all."]

    classified = mod.classify_entries(entries)
    assert len(classified) == 2
    for c in classified:
        assert "content" in c
        assert "is_stale_path_candidate" in c
        assert "extracted_paths" in c
    assert classified[0]["is_stale_path_candidate"] is False
    assert classified[1]["extracted_paths"] == []


# ---------------------------------------------------------------------------
# Config accessors on agent/curator.py
# ---------------------------------------------------------------------------

@pytest.fixture
def curator_env(tmp_path, monkeypatch):
    """Isolated HERMES_HOME + freshly reloaded curator module."""
    home = tmp_path / ".hermes"
    (home / "skills").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    import agent.curator as curator
    importlib.reload(curator)
    monkeypatch.setattr(curator, "_load_config", lambda: {})
    return {"home": home, "curator": curator}


def test_get_hot_tier_audit_config_default_false(curator_env):
    c = curator_env["curator"]
    assert c.get_hot_tier_audit() is False


def test_get_hot_tier_audit_dry_run_config_default_true(curator_env):
    c = curator_env["curator"]
    assert c.get_hot_tier_audit_dry_run() is True


def test_get_hot_tier_audit_enabled_via_config(curator_env, monkeypatch):
    c = curator_env["curator"]
    monkeypatch.setattr(c, "_load_config", lambda: {"hot_tier_audit": True})
    assert c.get_hot_tier_audit() is True


def test_get_hot_tier_audit_dry_run_disabled_via_config(curator_env, monkeypatch):
    c = curator_env["curator"]
    monkeypatch.setattr(c, "_load_config", lambda: {"hot_tier_audit_dry_run": False})
    assert c.get_hot_tier_audit_dry_run() is False


# ---------------------------------------------------------------------------
# maybe_run_curator hook point
# ---------------------------------------------------------------------------

def test_maybe_run_curator_invokes_audit_when_enabled(curator_env, monkeypatch):
    c = curator_env["curator"]
    monkeypatch.setattr(c, "should_run_now", lambda: True)
    monkeypatch.setattr(c, "_load_config", lambda: {"hot_tier_audit": True, "hot_tier_audit_dry_run": True})
    monkeypatch.setattr(c, "run_curator_review", lambda **kwargs: {"ok": True})

    from agent import hot_tier_audit
    calls = []
    monkeypatch.setattr(
        hot_tier_audit, "run_hot_tier_audit",
        lambda dry_run: calls.append(dry_run) or {"entries_checked": 0, "stale_path_candidates": [], "written_report_path": None},
    )

    c.maybe_run_curator()

    assert calls == [True]


def test_maybe_run_curator_skips_audit_when_disabled(curator_env, monkeypatch):
    c = curator_env["curator"]
    monkeypatch.setattr(c, "should_run_now", lambda: True)
    monkeypatch.setattr(c, "_load_config", lambda: {"hot_tier_audit": False})
    monkeypatch.setattr(c, "run_curator_review", lambda **kwargs: {"ok": True})

    from agent import hot_tier_audit
    calls = []
    monkeypatch.setattr(
        hot_tier_audit, "run_hot_tier_audit",
        lambda dry_run: calls.append(dry_run),
    )

    c.maybe_run_curator()

    assert calls == []
