"""Tests for the orphaned-buffer sweep (shutdown-deferred session-end proposals).

The sweep is the second half of ``_defer_memory_extraction``: a gateway shutdown keeps
the buffer but the "next session end of the same session_id" never arrives, so the
7-day prune would drop it. These tests pin the selection rules (gate, live sessions,
fresh buffers) with the classifier and writer stubbed out — no warm store needed.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from tools.memory_extraction import buffer as mbuf
from tools.memory_extraction import conflict, extractor, orphan_sweep


class _Verdict:
    def __init__(self, verdict: str = "NEW"):
        self.verdict = verdict


def _seed(home, session_id: str, contents: list[str], *, age_seconds: int) -> None:
    mbuf.append(session_id, [{"content": c, "category": "general", "tags": "", "rationale": ""} for c in contents],
                source="per_turn")
    path = home / "memory_extraction_buffer.json"
    data = json.loads(path.read_text())
    stamp = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=age_seconds)
    data[session_id]["updated_at"] = stamp.isoformat()
    path.write_text(json.dumps(data))


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """Buffer under the test HERMES_HOME; classifier + writer stubbed."""
    from hermes_constants import get_hermes_home

    calls: list[dict] = []

    monkeypatch.setattr(extractor, "is_enabled", lambda: True)
    monkeypatch.setattr(extractor, "_get_extraction_config",
                        lambda: {"auto_commit_session_end": True})
    monkeypatch.setattr(orphan_sweep, "_live_session_ids", lambda home, idle: set())
    monkeypatch.setattr(conflict, "classify", lambda content: _Verdict("NEW"))

    def _apply(verdict, proposal, *, auto_commit=False):
        calls.append({"content": proposal["content"], "auto_commit": auto_commit})
        return {"action": "stored", "fact_id": len(calls)}

    monkeypatch.setattr(conflict, "apply_verdict", _apply)
    return Path(get_hermes_home()), calls


def test_sweeps_stale_ended_buffers(harness):
    home, calls = harness
    _seed(home, "sess-stale", ["fact one", "fact two"], age_seconds=3600)
    result = orphan_sweep.sweep_orphaned_buffers()

    assert result["sessions"] == 1
    assert result["committed"] == 2
    assert [c["content"] for c in calls] == ["fact one", "fact two"]
    # contradiction verdicts stay uncommitted, exactly like the session-end path
    assert all(c["auto_commit"] is False for c in calls)
    assert "sess-stale" not in mbuf.all_sessions()


def test_skips_fresh_and_live_sessions(harness, monkeypatch):
    home, calls = harness
    _seed(home, "sess-fresh", ["fresh fact"], age_seconds=30)
    _seed(home, "sess-live", ["live fact"], age_seconds=3600)
    monkeypatch.setattr(orphan_sweep, "_live_session_ids", lambda h, idle: {"sess-live"})

    result = orphan_sweep.sweep_orphaned_buffers()

    assert result["sessions"] == 0
    assert calls == []
    assert {"sess-fresh", "sess-live"} <= set(mbuf.all_sessions())


def test_skips_duplicate_verdict_without_writing(harness, monkeypatch):
    home, calls = harness
    _seed(home, "sess-dup", ["already known"], age_seconds=3600)
    monkeypatch.setattr(conflict, "classify", lambda content: _Verdict("DUPLICATE"))

    result = orphan_sweep.sweep_orphaned_buffers()

    assert (result["committed"], result["skipped"]) == (0, 1)
    assert calls == []
    assert "sess-dup" not in mbuf.all_sessions()  # consumed; a re-run cannot duplicate


def test_no_op_when_auto_commit_is_off(harness, monkeypatch):
    home, calls = harness
    _seed(home, "sess-stale", ["fact"], age_seconds=3600)
    monkeypatch.setattr(extractor, "_get_extraction_config",
                        lambda: {"auto_commit_session_end": False})

    result = orphan_sweep.sweep_orphaned_buffers()

    assert result.get("reason", "").startswith("auto_commit_session_end off")
    assert calls == []
    assert "sess-stale" in mbuf.all_sessions()


def test_bounded_per_call(harness):
    home, _calls = harness
    for i in range(4):
        _seed(home, f"sess-{i}", [f"fact {i}"], age_seconds=3600 + i)

    result = orphan_sweep.sweep_orphaned_buffers(max_sessions=2)

    assert result["sessions"] == 2
    assert len(mbuf.all_sessions()) == 2  # the rest wait for the next cadence
