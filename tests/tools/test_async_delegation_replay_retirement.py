"""A dead delegation's completion replay is retired once nobody can still consume it.

deleg_49a11cc8 and deleg_4613deec died on 2026-10-23 (manifest status=interrupted,
exit_reason=interrupted) but their durable rows were still ``delivery_state='pending'`` three days
later, so the only thing that ever cleared them was the 48 h age cap during gateway startup
("pending completion is 72.3h old (cap 48.0h); terminally dropping the replay").

Age decides, not status: every failure keeps its replay payload, so a child that died while the
gateway was reloading is still pushed once. A dead row that went unclaimed past
``_DEAD_COMPLETION_GRACE_S`` is retired instead of parking until the 48 h cap. A retired outcome stays
queryable. A genuinely completed delegation keeps its replay until the cap, exactly once.

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


def _insert_dead(home, delegation_id: str, *, state="error", completed: float, owner_pid: int,
                 attempts: int = 0, event: bool = True) -> None:
    evt = {"type": "async_delegation", "delegation_id": delegation_id, "session_key": "bot-chat",
           "parent_session_id": "bot-parent", "status": state, "completed_at": completed}
    _insert_row(
        home, delegation_id=delegation_id, origin_session="bot-chat", origin_ui_session_id="",
        parent_session_id="bot-parent", state=state, dispatched_at=completed - 96.5,
        completed_at=completed, updated_at=completed, event_json=json.dumps(evt) if event else None,
        result_json=json.dumps({"status": state, "error": "Operation interrupted"}),
        delivery_state="pending", delivery_attempts=attempts, owner_pid=owner_pid, owner_started_at=None,
        task_json=json.dumps({"goal": "test whether the APNG window discloses a pointer"}))


def _ledger(home, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(home))
    ad._connect().close()  # create the ledger schema


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
def test_live_owner_dead_completion_keeps_its_replay_and_is_delivered(tmp_path, monkeypatch, caplog, status):
    """(1) The owner is alive at the terminal transition: the failure keeps its replay payload, is
    not retired, and the in-process copy is claimed and delivered."""
    home = tmp_path / "home"
    home.mkdir()
    with caplog.at_level(logging.INFO, logger="tools.async_delegation"):
        delegation_id, evt = _dispatch(
            home, monkeypatch,
            lambda: {"status": status, "summary": None, "exit_reason": "interrupted",
                     "error": "Operation interrupted: waiting for model response"})

    row = _row(home, delegation_id)
    assert (row["state"], row["delivery_state"]) == (status, "pending")
    assert row["owner_pid"] == os.getpid()
    assert json.loads(row["event_json"])["status"] == status  # replay kept: the owner can push it
    assert json.loads(row["result_json"])["status"] == status
    assert "retiring" not in caplog.text

    # A live owner's row is not stolen by the orphan sweep nor retired by its grace window yet.
    later = time.time() + ad._ORPHAN_STALE_S + 1
    assert ad.sweep_orphaned_completions(queue.Queue(), now=later) == 0
    assert _row(home, delegation_id)["delivery_state"] == "pending"

    assert evt["status"] == status
    claim = ad.claim_event_delivery(evt, "parent")
    assert claim and ad.complete_completion_delivery(delegation_id, claim)
    assert _row(home, delegation_id)["delivery_state"] == "delivered"
    assert ad.restore_undelivered_completions(queue.Queue()) == 0


def test_dead_owner_dead_completion_retires_at_the_terminal_transition(tmp_path, monkeypatch, caplog):
    """(2) The row's owner is provably gone when the terminal state is persisted: nobody can ever
    claim it from a replay, so the payload is retired immediately; the result stays queryable."""
    home = tmp_path / "home"
    home.mkdir()
    gone = _dead_pid()

    def runner():
        # The row's recorded owner is a process that no longer exists (e.g. a row adopted from an
        # owner that died before this child finished).
        conn = sqlite3.connect(home / "state.db")
        try:
            conn.execute("UPDATE async_delegations SET owner_pid=?, owner_started_at=NULL", (gone,))
            conn.commit()
        finally:
            conn.close()
        return {"status": "error", "summary": None, "error": "child crashed"}

    with caplog.at_level(logging.INFO, logger="tools.async_delegation"):
        delegation_id, evt = _dispatch(home, monkeypatch, runner)

    row = _row(home, delegation_id)
    assert (row["state"], row["event_json"]) == ("error", None)
    assert json.loads(row["result_json"])["error"] == "child crashed"
    assert f"Async delegation {delegation_id}: terminal state 'error' and its owner process (pid {gone}) " \
           "is gone; retiring its completion replay" in caplog.text
    assert ad.get_durable_delegation(delegation_id)["result"]["error"] == "child crashed"
    (failed,) = ad.failed_delegations_for_session(parent_session_id="bot-parent")
    assert (failed["delegation_id"], failed["error"]) == (delegation_id, "child crashed")
    # No payload means no later process can replay it; the row stays pending so this process's
    # in-process copy can still claim it, and the grace window retires it afterwards.
    assert ad.restore_undelivered_completions(queue.Queue()) == 0
    assert _row(home, delegation_id)["delivery_state"] == "pending"
    with caplog.at_level(logging.INFO, logger="tools.async_delegation"):
        assert ad.sweep_orphaned_completions(queue.Queue(), now=time.time() + ad._DEAD_COMPLETION_GRACE_S + 60) == 0
    assert _row(home, delegation_id)["delivery_state"] == "dropped"
    assert evt["status"] == "error"  # the in-process copy was still queued


def test_batch_that_lost_every_child_keeps_its_replay_while_owner_lives(tmp_path, monkeypatch):
    """deleg_4613deec shape: a fan-out unit whose every child died lands state='error'. Its owner is
    alive, so the failure stays pushable; per-task results stay queryable."""
    home = tmp_path / "home"
    home.mkdir()
    delegation_id, _evt = _dispatch(
        home, monkeypatch,
        lambda: {"results": [{"task_index": 0, "status": "interrupted", "summary": None,
                              "error": "Parent agent interrupted — child did not finish in time"}]},
        batch=True)
    row = _row(home, delegation_id)
    assert (row["state"], row["delivery_state"]) == ("error", "pending") and row["event_json"]
    assert json.loads(row["result_json"])["results"][0]["status"] == "interrupted"


@pytest.mark.parametrize("state", ["error", "interrupted"])
def test_startup_sweep_retires_a_STALE_dead_owners_pending_replay(tmp_path, monkeypatch, caplog, state):
    """(3a) Terminal dead row, pending, replay payload, owner long gone AND past the grace window ->
    startup retires it (dropped, INFO log) instead of re-offering it until the 48 h cap."""
    home = tmp_path / "home"
    home.mkdir()
    _ledger(home, monkeypatch)
    delegation_id = "deleg_49a11cc8"
    _insert_dead(home, delegation_id, state=state,
                 completed=time.time() - ad._DEAD_COMPLETION_GRACE_S - 60, owner_pid=_dead_pid())

    with caplog.at_level(logging.INFO, logger="tools.async_delegation"):
        q = queue.Queue()
        assert ad.restore_undelivered_completions(q) == 0
    assert q.empty()
    row = _row(home, delegation_id)
    assert row["delivery_state"] == "dropped"
    assert json.loads(row["result_json"])["status"] == state  # still queryable
    assert f"Async delegation {delegation_id}: terminal state '{state}' was never claimed" in caplog.text
    assert "pending completion is" not in caplog.text  # never reached the age-cap warning


@pytest.mark.parametrize("state", ["error", "interrupted"])
def test_startup_REPLAYS_a_fresh_failure_whose_owner_died_in_a_reload(tmp_path, monkeypatch, caplog, state):
    """(3b) A child that died while the gateway was reloading must still be pushed once. Age is the
    trigger, not owner liveness: a fresh dead-owner row is re-offered, not retired."""
    home = tmp_path / "home"
    home.mkdir()
    _ledger(home, monkeypatch)
    delegation_id = "deleg_fresh_reload"
    _insert_dead(home, delegation_id, state=state, completed=time.time() - 120, owner_pid=_dead_pid())

    q = queue.Queue()
    assert ad.restore_undelivered_completions(q) == 1
    assert _row(home, delegation_id)["delivery_state"] == "pending"
    assert [e["delegation_id"] for e in _drain(q)] == [delegation_id]


@pytest.mark.parametrize("entry", ["startup", "sweep"])
def test_grace_window_retires_a_live_owners_never_claimed_dead_completion(tmp_path, monkeypatch, caplog, entry):
    """(4) The original leak: owner alive, but nothing ever claimed the failure. Past
    _DEAD_COMPLETION_GRACE_S it is retired (startup and the running sweep); a fresh one is not."""
    assert ad._DEAD_COMPLETION_GRACE_S < ad._MAX_COMPLETION_REPLAY_AGE_S
    home = tmp_path / "home"
    home.mkdir()
    _ledger(home, monkeypatch)
    now = time.time()
    _insert_dead(home, "deleg_stale", completed=now - ad._DEAD_COMPLETION_GRACE_S - 60, owner_pid=os.getpid())
    _insert_dead(home, "deleg_fresh", completed=now - 3600, owner_pid=os.getpid())
    # Claimed once already: its owner had a consumer, so the grace rule does not apply.
    _insert_dead(home, "deleg_claimed", completed=now - ad._DEAD_COMPLETION_GRACE_S - 60,
                 owner_pid=os.getpid(), attempts=1)

    q = queue.Queue()
    with caplog.at_level(logging.INFO, logger="tools.async_delegation"):
        if entry == "startup":
            offered = ad.restore_undelivered_completions(q)
        else:
            offered = ad.sweep_orphaned_completions(q, now=now)
    assert _row(home, "deleg_stale")["delivery_state"] == "dropped"
    assert _row(home, "deleg_fresh")["delivery_state"] == "pending"
    assert _row(home, "deleg_claimed")["delivery_state"] == "pending"
    assert "Async delegation deleg_stale: terminal state 'error' was never claimed in 6.0h (grace 6.0h); " \
           "retiring its pending completion replay" in caplog.text
    assert "deleg_fresh: terminal state" not in caplog.text
    offered_ids = sorted(e["delegation_id"] for e in _drain(q))
    assert "deleg_stale" not in offered_ids
    # Startup re-offers the live, still-deliverable ones; the sweep leaves a live owner's rows alone.
    assert offered_ids == (["deleg_claimed", "deleg_fresh"] if entry == "startup" else [])
    assert offered == len(offered_ids)


def test_retired_dead_delegation_is_still_reported_as_failed(tmp_path, monkeypatch):
    """(5) Retiring the replay never loses the outcome: failed_delegations_for_session (the source of
    subagent.list's ``delegations``) still returns the dropped row from state/result_json."""
    home = tmp_path / "home"
    home.mkdir()
    _ledger(home, monkeypatch)
    now = time.time()
    _insert_dead(home, "deleg_graced", completed=now - ad._DEAD_COMPLETION_GRACE_S - 60, owner_pid=os.getpid())
    _insert_dead(home, "deleg_orphan", state="interrupted",
                 completed=now - ad._DEAD_COMPLETION_GRACE_S - 120, owner_pid=_dead_pid())
    assert ad.restore_undelivered_completions(queue.Queue()) == 0
    assert {_row(home, d)["delivery_state"] for d in ("deleg_graced", "deleg_orphan")} == {"dropped"}

    failed = ad.failed_delegations_for_session(parent_session_id="bot-parent", now=now)
    # Newest first: deleg_graced completed 60 s later than deleg_orphan.
    assert [(f["delegation_id"], f["status"], f["error"]) for f in failed] == [
        ("deleg_graced", "error", "Operation interrupted"),
        ("deleg_orphan", "interrupted", "Operation interrupted")]
    assert failed[0]["goal"] == "test whether the APNG window discloses a pointer"


def test_completed_delegation_still_replays_exactly_once(tmp_path, monkeypatch):
    """(6) real work keeps its replay: delivered once, never twice, until the cap -- and neither the
    owner-gone nor the grace rule touches a successful completion."""
    home = tmp_path / "home"
    home.mkdir()
    delegation_id, _evt = _dispatch(
        home, monkeypatch, lambda: {"status": "completed", "summary": "done", "api_calls": 1})

    row = _row(home, delegation_id)
    assert (row["state"], row["delivery_state"]) == ("completed", "pending")
    assert row["event_json"]  # a real result keeps its replay payload
    # Old and orphaned: still not a dead-completion retirement candidate.
    conn = sqlite3.connect(home / "state.db")
    try:
        conn.execute("UPDATE async_delegations SET owner_pid=?, completed_at=?",
                     (_dead_pid(), time.time() - ad._DEAD_COMPLETION_GRACE_S - 60))
        conn.commit()
    finally:
        conn.close()

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
