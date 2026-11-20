"""A dead delegation's completion keeps its replay until the replay age cap, then is dropped.

The fork used to retire a terminal completion whose owner was gone (at the terminal transition, or
unclaimed past a 6 h grace) by writing ``delivery_state='dropped'``. The persistence authority store
upstream replaced the raw-SQL ledger with has no such verb, and owner liveness is no longer a
retirement rule: a dead owner's pending completion is re-offered by the startup replay and by the
running orphan sweep, and only ``DELEGATION_REPLAY_MAX_AGE_SECONDS`` (48 h) terminally drops it.

Rows here are produced by the real dispatch/finalize path against a real temp ``state.db``; the
startup cases write the row shape a previous process left on disk (the leaked delegations above).
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
                 attempts: int = 0) -> None:
    evt = {"type": "async_delegation", "delegation_id": delegation_id, "session_key": "bot-chat",
           "parent_session_id": "bot-parent", "status": state, "completed_at": completed}
    _insert_row(
        home, delegation_id=delegation_id, origin_session="bot-chat", origin_ui_session_id="",
        parent_session_id="bot-parent", state=state, dispatched_at=completed - 96.5,
        completed_at=completed, updated_at=completed, event_json=json.dumps(evt),
        result_json=json.dumps({"status": state, "error": "Operation interrupted"}),
        delivery_state="pending", delivery_attempts=attempts, owner_pid=owner_pid, owner_started_at=None,
        task_json=json.dumps({"goal": "test whether the APNG window discloses a pointer"}))


def _ledger(home, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(home))
    with ad._ledger() as store:  # create the ledger schema (the store binds on first use)
        store.coordination.has_replayable_completions()


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

    # A live owner's row with its copy still queued is not re-offered by the orphan sweep.
    later = time.time() + ad._ORPHAN_STALE_S + 1
    assert ad.sweep_orphaned_completions(queue.Queue(), now=later) == 0
    assert _row(home, delegation_id)["delivery_state"] == "pending"

    assert evt["status"] == status
    claim = ad.claim_event_delivery(evt, "parent")
    assert claim and ad.complete_completion_delivery(delegation_id, claim)
    assert _row(home, delegation_id)["delivery_state"] == "delivered"
    assert ad.restore_undelivered_completions(queue.Queue()) == 0


def test_dead_owner_dead_completion_is_reoffered_not_retired(tmp_path, monkeypatch, caplog):
    """(2) The row's owner is provably gone when the terminal state is persisted: the failure still
    keeps its replay payload (owner liveness retires nothing), the result stays queryable, and the
    running sweep offers it to a live consumer instead of parking it."""
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
    assert row["state"] == "error" and row["delivery_state"] == "pending"
    assert json.loads(row["event_json"])["status"] == "error"  # replay kept despite the dead owner
    assert json.loads(row["result_json"])["error"] == "child crashed"
    durable = ad.get_durable_delegation(delegation_id)
    assert durable is not None and durable["result"]["error"] == "child crashed"
    (failed,) = ad.failed_delegations_for_session(parent_session_id="bot-parent")
    assert (failed["delegation_id"], failed["error"]) == (delegation_id, "child crashed")
    assert "retiring" not in caplog.text

    # The dead owner's row is re-offered by the running sweep and the winner's claim settles it.
    q = queue.Queue()
    with caplog.at_level(logging.INFO, logger="tools.async_delegation"):
        assert ad.sweep_orphaned_completions(q, now=time.time() + ad._ORPHAN_STALE_S + 60) == 1
    (offered,) = _drain(q)
    assert (offered["delegation_id"], offered["status"], offered["restored"]) == (delegation_id, "error", True)
    claim = ad.claim_event_delivery(offered, "parent")
    assert claim and ad.complete_completion_delivery(delegation_id, claim)
    assert _row(home, delegation_id)["delivery_state"] == "delivered"
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
def test_startup_reoffers_a_dead_owners_pending_replay_until_the_age_cap(tmp_path, monkeypatch, caplog, state):
    """(3a) A dead owner's terminal row is re-offered by the startup replay, not retired: age is the
    only cap, so a row well past the retired 6 h grace still replays once."""
    home = tmp_path / "home"
    home.mkdir()
    _ledger(home, monkeypatch)
    delegation_id = "deleg_49a11cc8"
    _insert_dead(home, delegation_id, state=state,
                 completed=time.time() - 6 * 3600, owner_pid=_dead_pid())

    with caplog.at_level(logging.INFO, logger="tools.async_delegation"):
        q = queue.Queue()
        assert ad.restore_undelivered_completions(q) == 1
    assert [e["delegation_id"] for e in _drain(q)] == [delegation_id]
    row = _row(home, delegation_id)
    assert row["delivery_state"] == "pending"
    assert json.loads(row["result_json"])["status"] == state  # still queryable
    assert "terminally dropping the replay" not in caplog.text


@pytest.mark.parametrize("state", ["error", "interrupted"])
def test_startup_drops_a_dead_owners_replay_only_past_the_age_cap(tmp_path, monkeypatch, caplog, state):
    """(3b) The replay age cap is the one retirement rule: a dead owner's row older than
    ``DELEGATION_REPLAY_MAX_AGE_SECONDS`` is terminally dropped at startup, and its outcome stays
    queryable."""
    home = tmp_path / "home"
    home.mkdir()
    _ledger(home, monkeypatch)
    delegation_id = "deleg_49a11cc9"
    _insert_dead(home, delegation_id, state=state,
                 completed=time.time() - ad._MAX_COMPLETION_REPLAY_AGE_S - 60, owner_pid=_dead_pid())

    with caplog.at_level(logging.INFO, logger="tools.async_delegation"):
        q = queue.Queue()
        assert ad.restore_undelivered_completions(q) == 0
    assert q.empty()
    row = _row(home, delegation_id)
    assert row["delivery_state"] == "dropped"
    assert json.loads(row["result_json"])["status"] == state  # still queryable
    assert f"Async delegation {delegation_id}: pending completion is" in caplog.text
    assert "terminally dropping the replay (result remains queryable)" in caplog.text


@pytest.mark.parametrize("state", ["error", "interrupted"])
def test_startup_replays_a_fresh_failure_whose_owner_died_in_a_reload(tmp_path, monkeypatch, caplog, state):
    """(3c) A child that died while the gateway was reloading must still be pushed once. Age is the
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
def test_the_replay_age_cap_is_the_only_retirement_rule(tmp_path, monkeypatch, caplog, entry):
    """(4) Ownership and claims decide nothing about a row's life: a pending completion just under the
    cap is re-offered (startup and the running sweep), one just past it is dropped at both entries, and
    the dropped outcome stays queryable."""
    assert ad._MAX_COMPLETION_REPLAY_AGE_S > ad._ORPHAN_STALE_S
    home = tmp_path / "home"
    home.mkdir()
    _ledger(home, monkeypatch)
    now = time.time()
    _insert_dead(home, "deleg_under", completed=now - ad._MAX_COMPLETION_REPLAY_AGE_S + 600, owner_pid=os.getpid())
    _insert_dead(home, "deleg_over", completed=now - ad._MAX_COMPLETION_REPLAY_AGE_S - 60, owner_pid=os.getpid())

    q = queue.Queue()
    with caplog.at_level(logging.INFO, logger="tools.async_delegation"):
        if entry == "startup":
            offered = ad.restore_undelivered_completions(q)
        else:
            offered = ad.sweep_orphaned_completions(q, now=now)
    assert _row(home, "deleg_over")["delivery_state"] == "dropped"
    assert json.loads(_row(home, "deleg_over")["result_json"])["status"] == "error"
    assert _row(home, "deleg_under")["delivery_state"] == "pending"
    assert [e["delegation_id"] for e in _drain(q)] == ["deleg_under"]
    assert offered == 1
    assert "deleg_over: pending completion is" in caplog.text
    assert "terminally dropping the replay" in caplog.text


def test_a_dropped_delegation_is_still_reported_as_failed(tmp_path, monkeypatch):
    """(5) Dropping the replay never loses the outcome: a row dropped inside the failure window (here
    for spending its delivery budget) is still returned by failed_delegations_for_session, the source
    of subagent.list's ``delegations``."""
    home = tmp_path / "home"
    home.mkdir()
    _ledger(home, monkeypatch)
    now = time.time()
    _insert_dead(home, "deleg_exhausted", completed=now - 300, owner_pid=_dead_pid(),
                 attempts=ad._MAX_DELIVERY_ATTEMPTS)
    _insert_dead(home, "deleg_orphan", state="interrupted", completed=now - 360, owner_pid=_dead_pid(),
                 attempts=ad._MAX_DELIVERY_ATTEMPTS)
    assert ad.sweep_orphaned_completions(queue.Queue(), now=now) == 0
    assert {_row(home, d)["delivery_state"] for d in ("deleg_exhausted", "deleg_orphan")} == {"dropped"}

    failed = ad.failed_delegations_for_session(parent_session_id="bot-parent", now=now)
    # Newest first: deleg_exhausted completed 60 s later than deleg_orphan.
    assert [(f["delegation_id"], f["status"], f["error"]) for f in failed] == [
        ("deleg_exhausted", "error", "Operation interrupted"),
        ("deleg_orphan", "interrupted", "Operation interrupted")]
    assert failed[0]["goal"] == "test whether the APNG window discloses a pointer"
    durable = ad.get_durable_delegation("deleg_exhausted")
    assert durable is not None and durable["result"]["error"] == "Operation interrupted"


def test_completed_delegation_still_replays_exactly_once(tmp_path, monkeypatch):
    """(6) real work keeps its replay: delivered once, never twice, until the cap -- and the age rule
    never singles out a failure."""
    home = tmp_path / "home"
    home.mkdir()
    delegation_id, _evt = _dispatch(
        home, monkeypatch, lambda: {"status": "completed", "summary": "done", "api_calls": 1})

    row = _row(home, delegation_id)
    assert (row["state"], row["delivery_state"]) == ("completed", "pending")
    assert row["event_json"]  # a real result keeps its replay payload
    # Old and orphaned, still far inside the replay cap: not a retirement candidate.
    conn = sqlite3.connect(home / "state.db")
    try:
        conn.execute("UPDATE async_delegations SET owner_pid=?, completed_at=?",
                     (_dead_pid(), time.time() - 12 * 3600))
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
