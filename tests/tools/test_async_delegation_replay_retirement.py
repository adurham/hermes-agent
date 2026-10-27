"""A dead delegation's completion replay is retired the moment it reaches a terminal state.

deleg_49a11cc8 and deleg_4613deec died on 2026-10-23 (manifest status=interrupted,
exit_reason=interrupted) but their durable rows were still ``delivery_state='pending'`` three days
later, so the only thing that ever cleared them was the 48 h age cap during gateway startup
("pending completion is 72.3h old (cap 48.0h); terminally dropping the replay"). Interrupted /
failed / cancelled work has no result anyone is still waiting for: its completion must reach a LIVE
parent once, through the in-process queue, and must never become a replay. A genuinely completed
delegation keeps its replay until the cap, exactly once.

Rows here are produced by the real dispatch/finalize path against a real temp ``state.db``; the
startup case writes the row shape the pre-fix code left on disk (the leaked delegations above).
"""

import json
import logging
import os
import queue
import sqlite3
import subprocess
import sys
import time

import pytest

from tools import async_delegation as ad
from tools.process_registry import process_registry


@pytest.fixture(autouse=True)
def _isolated_queue(monkeypatch):
    ad._reset_for_tests()
    isolated = queue.Queue()
    monkeypatch.setattr(process_registry, "completion_queue", isolated)
    yield isolated
    deadline = time.monotonic() + 5.0
    while ad.active_count() and time.monotonic() < deadline:
        time.sleep(0.02)
    ad._reset_for_tests()


def _row(home, delegation_id: str) -> dict:
    conn = sqlite3.connect(home / "state.db")
    try:
        conn.row_factory = sqlite3.Row
        return dict(conn.execute("SELECT * FROM async_delegations WHERE delegation_id=?",
                                 (delegation_id,)).fetchone())
    finally:
        conn.close()


def _insert_row(home, **cols) -> None:
    """Insert a row with the exact column set the ledger writes (schema must already exist)."""
    conn = sqlite3.connect(home / "state.db")
    try:
        conn.execute(f"INSERT INTO async_delegations ({', '.join(cols)}) "
                     f"VALUES ({', '.join('?' for _ in cols)})", tuple(cols.values()))
        conn.commit()
    finally:
        conn.close()


def _dead_pid() -> int:
    """A pid that is guaranteed not to be running."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=60)
    return proc.pid


def _await_completion(delegation_id: str, q, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not q.empty():
            evt = q.get_nowait()
            if evt.get("delegation_id") == delegation_id:
                return evt
        time.sleep(0.01)
    raise AssertionError(f"no completion event for {delegation_id}")


def _drain(q) -> list:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def _dispatch(home, monkeypatch, runner, *, batch: bool = False, **kwargs) -> tuple[str, dict]:
    monkeypatch.setenv("HERMES_HOME", str(home))
    goal = "test whether the APNG window discloses a pointer"
    dispatch = ad.dispatch_async_delegation_batch if batch else ad.dispatch_async_delegation
    handle = dispatch(**({"goals": [goal]} if batch else {"goal": goal}),
                      context=None, toolsets=None, role="leaf", model="m", session_key="bot-chat",
                      parent_session_id="bot-parent", runner=runner, **kwargs)
    delegation_id = handle["delegation_id"]
    evt = _await_completion(delegation_id, process_registry.completion_queue)
    deadline = time.monotonic() + 10.0
    while ad.active_count() and time.monotonic() < deadline:
        time.sleep(0.02)
    return delegation_id, evt


@pytest.mark.parametrize("status", ["interrupted", "error"])
def test_dead_delegation_retires_its_replay_as_it_finalizes(tmp_path, monkeypatch, caplog, status):
    """(1) interrupted/failed -> the replay is gone NOW, not at the 48 h cap, and the live parent
    still receives the completion in-process (the row stays claimable, the result stays queryable)."""
    home = tmp_path / "home"
    home.mkdir()
    with caplog.at_level(logging.INFO, logger="tools.async_delegation"):
        delegation_id, evt = _dispatch(
            home, monkeypatch,
            lambda: {"status": status, "summary": None, "exit_reason": "interrupted",
                     "error": "Operation interrupted: waiting for model response"})

    row = _row(home, delegation_id)
    assert (row["state"], row["delivery_state"]) == (status, "pending")
    # The replay is retired at the terminal transition: nothing for a later process to re-offer,
    # while the outcome itself (result_json/state) stays queryable.
    assert row["event_json"] is None
    assert json.loads(row["result_json"])["status"] == status
    assert row["completed_at"]
    assert f"Async delegation {delegation_id}: terminal state '{status}'; retiring its pending " \
           "completion replay" in caplog.text

    # Fresh (nowhere near the cap) and still not replayable by a restart or by the orphan sweep.
    assert ad.restore_undelivered_completions(queue.Queue()) == 0
    later = time.time() + ad._ORPHAN_STALE_S + 1
    assert ad.sweep_orphaned_completions(queue.Queue(), now=later) == 0

    # ...but the in-process copy is still delivered exactly as before: the parent learns its
    # background work died, and the ack settles the row.
    assert evt["status"] == status
    claim = ad.claim_event_delivery(evt, "parent")
    assert claim and ad.complete_completion_delivery(delegation_id, claim)
    assert _row(home, delegation_id)["delivery_state"] == "delivered"


def test_batch_that_lost_every_child_retires_its_replay(tmp_path, monkeypatch, caplog):
    """deleg_4613deec: a fan-out unit whose every child was interrupted/failed lands state='error'
    and must be retired like any other dead delegation (its per-task results stay queryable)."""
    home = tmp_path / "home"
    home.mkdir()
    with caplog.at_level(logging.INFO, logger="tools.async_delegation"):
        delegation_id, _evt = _dispatch(
            home, monkeypatch,
            lambda: {"results": [{"task_index": 0, "status": "interrupted", "summary": None,
                                  "error": "Parent agent interrupted — child did not finish in time"}]},
            batch=True)

    row = _row(home, delegation_id)
    assert (row["state"], row["event_json"], row["delivery_state"]) == ("error", None, "pending")
    assert json.loads(row["result_json"])["results"][0]["status"] == "interrupted"
    assert ad.restore_undelivered_completions(queue.Queue()) == 0
    assert f"Async delegation {delegation_id}: terminal state 'error'; retiring its pending " \
           "completion replay" in caplog.text


@pytest.mark.parametrize("state", ["error", "interrupted"])
def test_startup_sweep_retires_a_terminal_delegations_pending_replay(tmp_path, monkeypatch, caplog, state):
    """(2) the on-disk leftovers from before the fix: terminal row, pending, replay payload, owner
    process long gone -> startup retires the replay instead of re-offering it until the cap."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    ad._connect().close()  # create the ledger schema

    delegation_id = "deleg_49a11cc8"
    completed = time.time() - 72.3 * 3600.0  # the leaked delegation's age; past no cap of ours
    event = {"type": "async_delegation", "delegation_id": delegation_id, "session_key": "bot-chat",
             "parent_session_id": "bot-parent", "status": state, "exit_reason": "interrupted",
             "completed_at": completed}
    _insert_row(
        home, delegation_id=delegation_id, origin_session="bot-chat", origin_ui_session_id="",
        parent_session_id="bot-parent", state=state, dispatched_at=completed - 96.5,
        completed_at=completed, updated_at=completed, event_json=json.dumps(event),
        result_json=json.dumps({"status": state, "error": "Operation interrupted"}),
        delivery_state="pending", delivery_attempts=0, owner_pid=_dead_pid(), owner_started_at=None,
        task_json=json.dumps({"goal": "test whether the APNG window discloses a pointer"}))

    with caplog.at_level(logging.INFO, logger="tools.async_delegation"):
        q = queue.Queue()
        assert ad.restore_undelivered_completions(q) == 0
    assert q.empty()

    row = _row(home, delegation_id)
    assert row["delivery_state"] == "dropped"
    assert json.loads(row["result_json"])["status"] == state  # still queryable
    assert f"Async delegation {delegation_id}: terminal state '{state}' with no owner left; " \
           "retiring its pending completion replay" in caplog.text


def test_completed_delegation_still_replays_exactly_once(tmp_path, monkeypatch):
    """(3) real work keeps its replay: delivered once, never twice, until the cap."""
    home = tmp_path / "home"
    home.mkdir()
    delegation_id, _evt = _dispatch(
        home, monkeypatch, lambda: {"status": "completed", "summary": "done", "api_calls": 1})

    row = _row(home, delegation_id)
    assert (row["state"], row["delivery_state"]) == ("completed", "pending")
    assert row["event_json"]  # a real result keeps its replay payload

    q = queue.Queue()
    assert ad.restore_undelivered_completions(q) == 1
    (restored,) = _drain(q)
    assert restored["delegation_id"] == delegation_id
    assert restored["summary"] == "done" and restored["restored"] is True

    # Exactly once: a second replay pass (next start, competing restore) offers nothing.
    assert ad.restore_undelivered_completions(queue.Queue()) == 0

    claim = ad.claim_event_delivery(restored, "parent")
    assert claim and ad.complete_completion_delivery(delegation_id, claim)
    assert _row(home, delegation_id)["delivery_state"] == "delivered"
    assert ad.restore_undelivered_completions(queue.Queue()) == 0


def test_startup_retirement_leaves_a_live_owners_row_alone(tmp_path, monkeypatch):
    """Startup must not retire a pending row a LIVE process may still be delivering in-process."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    ad._connect().close()

    delegation_id = "deleg_liveowner"
    now = time.time()
    event = {"type": "async_delegation", "delegation_id": delegation_id, "status": "interrupted",
             "completed_at": now}
    _insert_row(
        home, delegation_id=delegation_id, origin_session="bot-chat", origin_ui_session_id="",
        parent_session_id="bot-parent", state="interrupted", dispatched_at=now - 10,
        completed_at=now, updated_at=now - 120, event_json=json.dumps(event),
        result_json=json.dumps({"status": "interrupted"}), delivery_state="pending",
        delivery_attempts=0, owner_pid=os.getpid(), owner_started_at=None,
        task_json=json.dumps({"goal": "g"}))

    q = queue.Queue()
    assert ad.restore_undelivered_completions(q) == 1
    assert _row(home, delegation_id)["delivery_state"] == "pending"
    assert [e["delegation_id"] for e in _drain(q)] == [delegation_id]
