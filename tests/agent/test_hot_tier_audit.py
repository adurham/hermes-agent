"""Tests for agent/hot_tier_audit.py — hot-tier stale-path audit (dry-run MVP)."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

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
    from agent import curator_backup
    importlib.reload(curator_backup)
    from agent import hot_tier_audit
    importlib.reload(hot_tier_audit)

    from tools import memory_warm
    memory_warm.reset_warm_store_for_testing()
    yield {"home": home, "memories": memories, "mod": hot_tier_audit,
           "curator_backup": curator_backup}
    memory_warm.reset_warm_store_for_testing()


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


def test_live_mode_takes_snapshot_before_mutating(audit_env, tmp_path, monkeypatch):
    mod = audit_env["mod"]
    ghost_dir = tmp_path / "ghost_snap"
    ghost_dir.mkdir()
    entry = "Old repo lived at ~/ghost_snap."
    ghost_dir.rmdir()
    _write_memory(audit_env["memories"], [entry])

    call_order = []

    def fake_snapshot(reason):
        call_order.append("snapshot")
        return tmp_path / "fake-snapshot-dir"

    monkeypatch.setattr(mod, "snapshot_memory", fake_snapshot)

    orig_write = mod._write_entries

    def tracking_write(path, entries):
        call_order.append("write")
        return orig_write(path, entries)

    monkeypatch.setattr(mod, "_write_entries", tracking_write)

    mod.run_hot_tier_audit(dry_run=False)

    assert call_order[0] == "snapshot"
    assert "write" in call_order
    assert call_order.index("snapshot") < call_order.index("write")


def test_live_mode_aborts_if_snapshot_fails(audit_env, tmp_path, monkeypatch):
    mod = audit_env["mod"]
    ghost_dir = tmp_path / "ghost_abort"
    ghost_dir.mkdir()
    entry = "Old repo lived at ~/ghost_abort."
    ghost_dir.rmdir()
    memory_path = audit_env["memories"] / "MEMORY.md"
    _write_memory(audit_env["memories"], [entry])
    before = memory_path.read_bytes()

    monkeypatch.setattr(mod, "snapshot_memory", lambda reason: None)

    from tools.memory_warm import get_warm_store
    store = get_warm_store()
    before_count = len(store.recall(query="ghost_abort", top_k=25))

    with pytest.raises(RuntimeError):
        mod.run_hot_tier_audit(dry_run=False)

    after = memory_path.read_bytes()
    assert before == after
    after_count = len(get_warm_store().recall(query="ghost_abort", top_k=25))
    assert after_count == before_count


def test_live_mode_demotes_stale_entry_to_warm_and_removes_from_hot_tier(audit_env, tmp_path):
    mod = audit_env["mod"]
    ghost_dir = tmp_path / "ghost_demote"
    ghost_dir.mkdir()
    entry = "Old repo lived at ~/ghost_demote before it moved."
    ghost_dir.rmdir()
    memory_path = audit_env["memories"] / "MEMORY.md"
    _write_memory(audit_env["memories"], [entry])

    summary = mod.run_hot_tier_audit(dry_run=False)

    assert summary["demoted_count"] == 1
    assert summary["snapshot_path"]
    remaining = memory_path.read_text(encoding="utf-8") if memory_path.exists() else ""
    assert entry not in remaining

    from tools.memory_warm import get_warm_store
    store = get_warm_store()
    results = store.recall(query="ghost_demote", top_k=10)
    assert any(entry in r.get("content", "") for r in results)


def test_live_mode_leaves_non_stale_entries_untouched(audit_env, tmp_path):
    mod = audit_env["mod"]
    ghost_dir = tmp_path / "ghost_mix"
    ghost_dir.mkdir()
    real_dir = tmp_path / "real_mix"
    real_dir.mkdir()

    stale_entry = "Old repo lived at ~/ghost_mix and is gone."
    ghost_dir.rmdir()
    keep_entry_1 = "The repo lives at ~/real_mix and is actively used."
    keep_entry_2 = "Prefers concise commit messages over verbose ones."

    memory_path = audit_env["memories"] / "MEMORY.md"
    _write_memory(audit_env["memories"], [keep_entry_1, stale_entry, keep_entry_2])

    summary = mod.run_hot_tier_audit(dry_run=False)

    assert summary["demoted_count"] == 1
    remaining_entries = [
        e.strip() for e in memory_path.read_text(encoding="utf-8").split(
            __import__("tools.memory_tool", fromlist=["ENTRY_DELIMITER"]).ENTRY_DELIMITER
        ) if e.strip()
    ]
    assert remaining_entries == [keep_entry_1, keep_entry_2]


def test_live_mode_no_op_when_no_stale_candidates(audit_env, tmp_path):
    mod = audit_env["mod"]
    real_dir = tmp_path / "real_noop"
    real_dir.mkdir()
    entry = "The repo lives at ~/real_noop and is actively used."

    memory_path = audit_env["memories"] / "MEMORY.md"
    _write_memory(audit_env["memories"], [entry])
    before_bytes = memory_path.read_bytes()
    before_mtime = memory_path.stat().st_mtime_ns

    summary = mod.run_hot_tier_audit(dry_run=False)

    assert summary["demoted_count"] == 0
    assert summary["snapshot_path"]
    after_bytes = memory_path.read_bytes()
    after_mtime = memory_path.stat().st_mtime_ns
    assert before_bytes == after_bytes
    assert before_mtime == after_mtime


def test_live_mode_handles_both_memory_and_user_files(audit_env, tmp_path):
    mod = audit_env["mod"]
    ghost_mem = tmp_path / "ghost_mem_file"
    ghost_mem.mkdir()
    ghost_user = tmp_path / "ghost_user_file"
    ghost_user.mkdir()

    stale_mem_entry = "Memory repo lived at ~/ghost_mem_file."
    stale_user_entry = "User repo lived at ~/ghost_user_file."
    ghost_mem.rmdir()
    ghost_user.rmdir()

    keep_mem_entry = "Keep this memory entry with no paths."
    keep_user_entry = "Keep this user entry with no paths."

    memory_path = audit_env["memories"] / "MEMORY.md"
    user_path = audit_env["memories"] / "USER.md"
    _write_memory(audit_env["memories"], [keep_mem_entry, stale_mem_entry], filename="MEMORY.md")
    _write_memory(audit_env["memories"], [keep_user_entry, stale_user_entry], filename="USER.md")

    summary = mod.run_hot_tier_audit(dry_run=False)

    assert summary["demoted_count"] == 2

    from tools.memory_tool import ENTRY_DELIMITER
    mem_remaining = [e.strip() for e in memory_path.read_text(encoding="utf-8").split(ENTRY_DELIMITER) if e.strip()]
    user_remaining = [e.strip() for e in user_path.read_text(encoding="utf-8").split(ENTRY_DELIMITER) if e.strip()]

    assert mem_remaining == [keep_mem_entry]
    assert user_remaining == [keep_user_entry]

    from tools.memory_warm import get_warm_store
    store = get_warm_store()
    mem_results = store.recall(query="ghost_mem_file", top_k=10)
    user_results = store.recall(query="ghost_user_file", top_k=10)
    assert any(stale_mem_entry in r.get("content", "") for r in mem_results)
    assert any(stale_user_entry in r.get("content", "") for r in user_results)
    # No cross-contamination: the mem-stale entry shouldn't show up when
    # searching for the user-stale token, and vice versa.
    assert not any(stale_user_entry in r.get("content", "") for r in mem_results)
    assert not any(stale_mem_entry in r.get("content", "") for r in user_results)


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
        lambda dry_run, consolidate=None: calls.append((dry_run, consolidate)) or {"entries_checked": 0, "stale_path_candidates": [], "written_report_path": None},
    )

    c.maybe_run_curator()

    assert calls == [(True, False)]


def test_maybe_run_curator_skips_audit_when_disabled(curator_env, monkeypatch):
    c = curator_env["curator"]
    monkeypatch.setattr(c, "should_run_now", lambda: True)
    monkeypatch.setattr(c, "_load_config", lambda: {"hot_tier_audit": False})
    monkeypatch.setattr(c, "run_curator_review", lambda **kwargs: {"ok": True})

    from agent import hot_tier_audit
    calls = []
    monkeypatch.setattr(
        hot_tier_audit, "run_hot_tier_audit",
        lambda dry_run, consolidate=None: calls.append((dry_run, consolidate)),
    )

    c.maybe_run_curator()

    assert calls == []


def test_maybe_run_curator_passes_consolidate_true_to_audit(curator_env, monkeypatch):
    """When curator.consolidate is on, the hot-tier audit call should carry
    the same consolidate=True value the skill pass was gated on, so both
    passes stay in lockstep (heuristic-only vs LLM-classification mode)."""
    c = curator_env["curator"]
    monkeypatch.setattr(c, "should_run_now", lambda: True)
    monkeypatch.setattr(
        c, "_load_config",
        lambda: {"hot_tier_audit": True, "hot_tier_audit_dry_run": True, "consolidate": True},
    )
    monkeypatch.setattr(c, "run_curator_review", lambda **kwargs: {"ok": True})

    from agent import hot_tier_audit
    calls = []
    monkeypatch.setattr(
        hot_tier_audit, "run_hot_tier_audit",
        lambda dry_run, consolidate=None: calls.append((dry_run, consolidate)) or {"entries_checked": 0, "stale_path_candidates": [], "written_report_path": None},
    )

    c.maybe_run_curator()

    assert calls == [(True, True)]


# ---------------------------------------------------------------------------
# LLM keep/demote/stale/dead classification (consolidate=True)
# ---------------------------------------------------------------------------

def _llm_response(payload) -> SimpleNamespace:
    """Build a fake call_llm() response wrapping a fenced-JSON payload."""
    text = "```json\n" + json.dumps(payload) + "\n```"
    message = SimpleNamespace(content=text)
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice])


def test_consolidate_false_never_invokes_llm(audit_env, monkeypatch):
    """Heuristic-only mode (the default) must never touch the LLM path,
    even when a stale-path candidate exists."""
    mod = audit_env["mod"]
    _write_memory(audit_env["memories"], ["Prefers concise commit messages."])

    called = []
    monkeypatch.setattr(mod, "_llm_classify_entries", lambda entries_meta: called.append(1) or {})

    mod.run_hot_tier_audit(dry_run=True, consolidate=False)
    mod.run_hot_tier_audit(dry_run=False, consolidate=False)

    assert called == []


def test_dry_run_consolidate_true_runs_llm_without_mutating(audit_env, monkeypatch):
    mod = audit_env["mod"]
    entry = "Prefers concise commit messages."
    memory_path = audit_env["memories"] / "MEMORY.md"
    _write_memory(audit_env["memories"], [entry])
    before = memory_path.read_bytes()

    monkeypatch.setattr(
        mod, "_llm_classify_entries",
        lambda entries_meta: {m["id"]: {"classification": "demote", "reason": "not every-turn"} for m in entries_meta},
    )

    summary = mod.run_hot_tier_audit(dry_run=True, consolidate=True)

    assert memory_path.read_bytes() == before  # dry-run: never mutates
    llm = summary["llm_classification"]
    assert llm["ran"] is True
    assert llm["succeeded"] is True
    assert llm["classifications"][0]["classification"] == "demote"


def test_live_llm_classification_overrides_heuristic_false_positive(audit_env, tmp_path, monkeypatch):
    """Heuristic pre-flags a path as stale, but the LLM says keep — the
    entry must be left untouched (LLM has final say when consolidate=True)."""
    mod = audit_env["mod"]
    ghost_dir = tmp_path / "ghost_override"
    ghost_dir.mkdir()
    entry = "Old repo lived at ~/ghost_override before it moved. Still relevant every session."
    ghost_dir.rmdir()

    memory_path = audit_env["memories"] / "MEMORY.md"
    _write_memory(audit_env["memories"], [entry])

    monkeypatch.setattr(
        mod, "_llm_classify_entries",
        lambda entries_meta: {m["id"]: {"classification": "keep", "reason": "still true"} for m in entries_meta},
    )

    summary = mod.run_hot_tier_audit(dry_run=False, consolidate=True)

    assert summary["demoted_count"] == 0
    assert summary["deleted_count"] == 0
    remaining = memory_path.read_text(encoding="utf-8")
    assert entry in remaining


def test_live_llm_classification_demote_moves_entry_to_warm(audit_env, monkeypatch):
    mod = audit_env["mod"]
    entry = "Prefers concise commit messages over verbose ones."
    memory_path = audit_env["memories"] / "MEMORY.md"
    _write_memory(audit_env["memories"], [entry])

    monkeypatch.setattr(
        mod, "_llm_classify_entries",
        lambda entries_meta: {m["id"]: {"classification": "demote", "reason": "not every-turn"} for m in entries_meta},
    )

    summary = mod.run_hot_tier_audit(dry_run=False, consolidate=True)

    assert summary["demoted_count"] == 1
    assert summary["deleted_count"] == 0
    remaining = memory_path.read_text(encoding="utf-8") if memory_path.exists() else ""
    assert entry not in remaining

    from tools.memory_warm import get_warm_store
    store = get_warm_store()
    results = store.recall(query="concise commit messages", top_k=10)
    assert any(entry in r.get("content", "") for r in results)


def test_live_llm_classification_dead_entry_deleted_when_prune_builtins_true(audit_env, monkeypatch):
    mod = audit_env["mod"]
    dead_entry = "[BLOCKED: ...] rejected write placeholder"
    memory_path = audit_env["memories"] / "MEMORY.md"
    _write_memory(audit_env["memories"], [dead_entry])

    monkeypatch.setattr(
        mod, "_llm_classify_entries",
        lambda entries_meta: {m["id"]: {"classification": "dead", "reason": "placeholder"} for m in entries_meta},
    )
    from agent import curator
    monkeypatch.setattr(curator, "get_prune_builtins", lambda: True)

    summary = mod.run_hot_tier_audit(dry_run=False, consolidate=True)

    assert summary["deleted_count"] == 1
    assert summary["demoted_count"] == 0
    remaining = memory_path.read_text(encoding="utf-8") if memory_path.exists() else ""
    assert dead_entry not in remaining

    # Hard-deleted entries do NOT get written to warm tier.
    from tools.memory_warm import get_warm_store
    store = get_warm_store()
    results = store.recall(query="rejected write placeholder", top_k=10)
    assert not any(dead_entry in r.get("content", "") for r in results)


def test_live_llm_classification_stale_dead_left_untouched_when_prune_builtins_false(audit_env, monkeypatch):
    mod = audit_env["mod"]
    dead_entry = "[BLOCKED: ...] rejected write placeholder"
    memory_path = audit_env["memories"] / "MEMORY.md"
    _write_memory(audit_env["memories"], [dead_entry])

    monkeypatch.setattr(
        mod, "_llm_classify_entries",
        lambda entries_meta: {m["id"]: {"classification": "dead", "reason": "placeholder"} for m in entries_meta},
    )
    from agent import curator
    monkeypatch.setattr(curator, "get_prune_builtins", lambda: False)

    summary = mod.run_hot_tier_audit(dry_run=False, consolidate=True)

    assert summary["deleted_count"] == 0
    assert summary["demoted_count"] == 0
    remaining = memory_path.read_text(encoding="utf-8")
    assert dead_entry in remaining
    # Still surfaced in the report even though untouched.
    classifications = summary["llm_classification"]["classifications"]
    assert classifications[0]["classification"] == "dead"


def test_live_llm_classification_failure_raises_and_never_mutates(audit_env, monkeypatch):
    """An LLM error must abort with zero mutation — it must NOT fall back
    to the heuristic-only demote-everything-flagged behavior."""
    mod = audit_env["mod"]
    entry = "Prefers concise commit messages."
    memory_path = audit_env["memories"] / "MEMORY.md"
    _write_memory(audit_env["memories"], [entry])
    before = memory_path.read_bytes()

    monkeypatch.setattr(mod, "_llm_classify_entries", lambda entries_meta: None)

    with pytest.raises(RuntimeError):
        mod.run_hot_tier_audit(dry_run=False, consolidate=True)

    assert memory_path.read_bytes() == before


def test_live_llm_classification_sanity_cap_exceeded_raises_and_never_mutates(audit_env, monkeypatch):
    """If the LLM flags an implausibly large fraction of entries for
    demote/stale/dead in one pass, abort rather than mutate — guards
    against a degenerate or adversarial classification."""
    mod = audit_env["mod"]
    entries = [f"Entry number {i} with unique content." for i in range(10)]
    memory_path = audit_env["memories"] / "MEMORY.md"
    _write_memory(audit_env["memories"], entries)
    before = memory_path.read_bytes()

    # Flag all 10 as demote — exceeds the 50% cap.
    monkeypatch.setattr(
        mod, "_llm_classify_entries",
        lambda entries_meta: {m["id"]: {"classification": "demote", "reason": "x"} for m in entries_meta},
    )

    with pytest.raises(RuntimeError):
        mod.run_hot_tier_audit(dry_run=False, consolidate=True)

    assert memory_path.read_bytes() == before


def test_llm_classify_entries_calls_call_llm_and_parses_response(audit_env, monkeypatch):
    """End-to-end (mocked) check of the call_llm plumbing: correct runtime
    binding reused from agent.curator, correct prompt shape, correct parse."""
    mod = audit_env["mod"]
    entries_meta = [
        {"id": 0, "filename": "MEMORY.md", "local_index": 0, "content": "Entry A"},
        {"id": 1, "filename": "MEMORY.md", "local_index": 1, "content": "Entry B"},
    ]

    from agent import curator
    fake_binding = curator._ReviewRuntimeBinding("anthropic", "claude-sonnet-4-6", None, None)
    monkeypatch.setattr(curator, "_resolve_review_runtime", lambda cfg: fake_binding)

    import agent.auxiliary_client as aux_client
    captured_kwargs = {}

    def fake_call_llm(**kwargs):
        captured_kwargs.update(kwargs)
        return _llm_response([
            {"id": 0, "classification": "keep", "reason": "r0"},
            {"id": 1, "classification": "demote", "reason": "r1"},
        ])

    monkeypatch.setattr(aux_client, "call_llm", fake_call_llm)

    result = mod._llm_classify_entries(entries_meta)

    assert result == {
        0: {"classification": "keep", "reason": "r0"},
        1: {"classification": "demote", "reason": "r1"},
    }
    assert captured_kwargs["provider"] == "anthropic"
    assert captured_kwargs["model"] == "claude-sonnet-4-6"
    assert "Entry A" in captured_kwargs["messages"][1]["content"]
    assert "Entry B" in captured_kwargs["messages"][1]["content"]


def test_llm_classify_entries_returns_none_on_call_llm_exception(audit_env, monkeypatch):
    mod = audit_env["mod"]
    entries_meta = [{"id": 0, "filename": "MEMORY.md", "local_index": 0, "content": "Entry A"}]

    from agent import curator
    monkeypatch.setattr(
        curator, "_resolve_review_runtime",
        lambda cfg: curator._ReviewRuntimeBinding("anthropic", "claude-sonnet-4-6", None, None),
    )

    import agent.auxiliary_client as aux_client

    def raising_call_llm(**kwargs):
        raise RuntimeError("network exploded")

    monkeypatch.setattr(aux_client, "call_llm", raising_call_llm)

    assert mod._llm_classify_entries(entries_meta) is None


# ---------------------------------------------------------------------------
# _parse_llm_classification validation
# ---------------------------------------------------------------------------

def test_parse_llm_classification_valid_response(audit_env):
    mod = audit_env["mod"]
    text = "```json\n" + json.dumps([
        {"id": 0, "classification": "keep", "reason": "r"},
        {"id": 1, "classification": "stale", "reason": "r2"},
    ]) + "\n```"
    result = mod._parse_llm_classification(text, {0, 1})
    assert result == {
        0: {"classification": "keep", "reason": "r"},
        1: {"classification": "stale", "reason": "r2"},
    }


def test_parse_llm_classification_rejects_missing_ids(audit_env):
    mod = audit_env["mod"]
    text = "```json\n" + json.dumps([{"id": 0, "classification": "keep", "reason": "r"}]) + "\n```"
    assert mod._parse_llm_classification(text, {0, 1}) is None


def test_parse_llm_classification_rejects_invalid_label(audit_env):
    mod = audit_env["mod"]
    text = "```json\n" + json.dumps([{"id": 0, "classification": "delete_forever", "reason": "r"}]) + "\n```"
    assert mod._parse_llm_classification(text, {0}) is None


def test_parse_llm_classification_rejects_duplicate_ids(audit_env):
    mod = audit_env["mod"]
    text = "```json\n" + json.dumps([
        {"id": 0, "classification": "keep", "reason": "r"},
        {"id": 0, "classification": "demote", "reason": "r2"},
    ]) + "\n```"
    assert mod._parse_llm_classification(text, {0}) is None


def test_parse_llm_classification_rejects_malformed_json(audit_env):
    mod = audit_env["mod"]
    assert mod._parse_llm_classification("not json at all", {0}) is None


def test_parse_llm_classification_rejects_non_list(audit_env):
    mod = audit_env["mod"]
    text = "```json\n" + json.dumps({"id": 0, "classification": "keep"}) + "\n```"
    assert mod._parse_llm_classification(text, {0}) is None
