"""A lost managed worker must be DISPOSED OF on the board, within one dispatcher tick.

Regression for a card left ``running`` after its managed worker died in the ``worker.adopt``
handshake (claim expired, every recorded pid dead, the owner gateway alive and its admission
settled ``terminal/interrupted``). These tests pin the disposition the dispatcher owes every
lost attempt:

  A1  a killed worker under a live claim is booked ``crashed`` within one tick;
  A2  an elapsed ``claim_expires`` never leaves the card ``running`` after one tick, even
      with no heartbeat and no pid (and even behind a settled owner receipt);
  A3  every such booking increments ``consecutive_failures``, so ``kanban.failure_limit``
      covers this outcome class and the card blocks at the limit.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import psutil
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_owner_recovery

NOW = int(time.time())
HOST = kb._claimer_id().split(":", 1)[0]
FINGERPRINT = "6a232414-2dbf-40ab-a534-d9b71db05658:116|4162240"
CLAIM_LAPSED = -1800  # claim_expires_in for a claim that expired half an hour ago


def _absent_pid(base: int = 3_800_000) -> int:
    pid = base
    while psutil.pid_exists(pid):
        pid += 1
    return pid


@pytest.fixture
def board(tmp_path, monkeypatch):
    """An isolated HERMES_HOME board plus the owner ledger a receipt points at."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    conn = kbc.connect()
    ledger = tmp_path / "state.db"
    with sqlite3.connect(ledger) as store:
        store.execute(
            "CREATE TABLE session_admissions "
            "(target_session_id TEXT, request_id TEXT, status TEXT, outcome TEXT)"
        )
    scratch = _Board(conn, ledger)
    yield scratch
    conn.close()


class _Board:
    def __init__(self, conn, ledger: Path):
        self.conn = conn
        self.ledger = ledger

    # --- seeding ----------------------------------------------------------------------
    def running(
        self, tid: str = "t_lost", *, worker_pid=None, record_worker_pid: bool = True,
        claim_expires_in: int = 900, heartbeat_age=None, owner: bool = True,
        admission=("terminal", "interrupted"), worker_lost=None, dead_seconds: int = 120,
        owner_pid=None, loss_extra: dict | None = None,
    ) -> int:
        """A ``running`` card whose managed worker was lost."""
        worker_pid = _absent_pid() if worker_pid is None else int(worker_pid)
        lock = f"{HOST}:{_absent_pid(base=3_900_000)}"
        started = NOW - dead_seconds
        run_id = int(self.conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, 'fan', 'running', ?, ?, ?, ?)",
            (tid, lock, NOW + claim_expires_in, worker_pid if record_worker_pid else None, started),
        ).lastrowid)
        self.conn.execute(
            "INSERT INTO tasks (id, title, assignee, status, created_at, claim_lock, claim_expires, "
            "worker_pid, worker_started_at, last_heartbeat_at, current_run_id, started_at, priority) "
            "VALUES (?, ?, 'fan', 'running', ?, ?, ?, ?, ?, ?, ?, ?, 5)",
            (tid, tid, NOW - 86400, lock, NOW + claim_expires_in,
             worker_pid if record_worker_pid else None,
             FINGERPRINT if record_worker_pid else None,
             (NOW - heartbeat_age) if heartbeat_age is not None else None, run_id, started),
        )
        self.event(tid, run_id, "claimed", {"lock": lock, "expires": NOW + claim_expires_in}, started)
        self.event(tid, run_id, "spawned",
                   {"pid": worker_pid, "started_at": FINGERPRINT} if record_worker_pid else {}, started)
        if owner:
            request_id = f'kanban:["default","{tid}",{run_id}]'
            self.event(tid, run_id, "owner_admitted",
                       {"db": str(self.ledger), "session_id": "sess-1", "pid": owner_pid or _absent_pid(base=3_950_000),
                        "request_id": request_id}, started + 3)
            if admission:
                with sqlite3.connect(self.ledger) as store:
                    store.execute("INSERT INTO session_admissions VALUES ('sess-1', ?, ?, ?)",
                                  (request_id, admission[0], admission[1]))
        if worker_lost:
            self.event(tid, run_id, "worker_lost",
                       {"reason": worker_lost, "claim_lock": lock, "run_id": run_id,
                        "pid": worker_pid, "bound": False, **(loss_extra or {})}, started + 24)
        self.conn.commit()
        return run_id

    def event(self, tid: str, run: int, kind: str, payload: dict, ts: int) -> None:
        self.conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)", (tid, run, kind, json.dumps(payload), ts))

    # --- reading ---------------------------------------------------------------------
    def state(self, tid: str) -> dict:
        return dict(self.conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())

    def run(self, run_id: int) -> dict:
        return dict(self.conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone())

    def tick(self, *, failure_limit: int | None = None) -> kbd.DispatchResult:
        """One real dispatcher tick: reclaim phase + promotion, no spawn slot."""
        return kbd.dispatch_once(self.conn, max_spawn=0, failure_limit=failure_limit,
                                 stale_timeout_seconds=0)


# --- A1: a killed worker under a LIVE claim is booked crashed within one tick --------------

def test_a_killed_managed_worker_is_disposed_of_within_one_tick(board):
    run_id = board.running("t_killed", claim_expires_in=900, heartbeat_age=30,
                           worker_lost="managed_worker_failed")
    board.tick(failure_limit=2)
    row, run = board.state("t_killed"), board.run(run_id)
    assert row["status"] == "ready"                      # left ``running``
    assert (row["claim_lock"], row["worker_pid"], row["current_run_id"]) == (None, None, None)
    assert run["outcome"] == "crashed"
    assert "managed worker lost: managed_worker_failed" in run["error"]
    assert json.loads(run["metadata"])["attempt_note"] == "managed worker lost: managed_worker_failed"
    assert row["consecutive_failures"] == 1               # A3: the breaker sees it
    assert row["last_failure_error"]


def test_a_killed_managed_worker_blocks_the_card_at_the_failure_limit(board):
    """The breaker covers this class: one loss short of the limit, the next loss blocks.

    ``_account_crashes`` books a plain crash against ``DEFAULT_FAILURE_LIMIT`` (the sweep
    resolves the threshold there rather than against the dispatcher's ``failure_limit``),
    so the card is seeded one failure short of that limit instead of pinning a threshold
    this path never reads.
    """
    board.running("t_killed", claim_expires_in=900, heartbeat_age=30,
                  worker_lost="managed_worker_failed")
    board.conn.execute("UPDATE tasks SET consecutive_failures = ? WHERE id = 't_killed'",
                       (kbd.DEFAULT_FAILURE_LIMIT - 1,))
    board.conn.commit()
    board.tick()
    row = board.state("t_killed")
    assert row["status"] == "blocked"
    assert row["consecutive_failures"] == kbd.DEFAULT_FAILURE_LIMIT
    kinds = [r["kind"] for r in board.conn.execute(
        "SELECT kind FROM task_events WHERE task_id = 't_killed' ORDER BY id")]
    assert "gave_up" in kinds


def test_a_lost_worker_that_exited_cleanly_still_charges_the_breaker(board):
    """rc=0 after a lost managed execution is not a protocol violation: the turn never ran."""
    run_id = board.running("t_clean", claim_expires_in=900, heartbeat_age=30,
                           worker_lost="managed_worker_failed")
    # The worker's own exit trailer says rc=0 (the CLI's epilogue after a lost turn).
    log = kb.worker_log_path("t_clean")
    log.parent.mkdir(parents=True, exist_ok=True)
    from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER
    log.write_text(f"I fixed the protocol violation from the last run.\n{KANBAN_WORKER_EXIT_TRAILER}0\n",
                   encoding="utf-8")
    board.tick(failure_limit=2)
    run = board.run(run_id)
    assert run["outcome"] == "crashed"
    assert "managed worker lost" in run["error"]
    assert run["error"].startswith("pid ") and "exited cleanly after its managed worker was lost." in run["error"]
    assert "I fixed the protocol violation" in run["error"]  # the worker's own output is kept
    assert run["error"].index("[managed worker lost") < run["error"].index("Worker's last output")
    assert "without kanban_complete" not in run["error"]
    assert board.state("t_clean")["consecutive_failures"] == 1
    # Not a protocol violation as well: one loss is charged to one budget.
    assert "protocol_violation" not in json.loads(run["metadata"] or "{}")
    kinds = [r[0] for r in board.conn.execute(
        "SELECT kind FROM task_events WHERE task_id='t_clean' AND run_id=?", (run_id,))]
    assert "protocol_violation" not in kinds and "crashed" in kinds
    assert kbd._protocol_violation_streak(board.conn, "t_clean") == 0


def test_a_crash_whose_output_quotes_protocol_violation_is_not_one(board):
    """The streak's text match reads our own error head, never the worker's quoted output."""
    run_id = board.running("t_quote", claim_expires_in=900, heartbeat_age=30, owner=False)
    log = kb.worker_log_path("t_quote")
    log.parent.mkdir(parents=True, exist_ok=True)
    from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER
    log.write_text(f"Retrying after the earlier protocol violation.\n{KANBAN_WORKER_EXIT_TRAILER}1\n",
                   encoding="utf-8")
    board.tick(failure_limit=5)
    run = board.run(run_id)
    assert run["outcome"] == "crashed" and "protocol violation" in run["error"]
    assert kbd._protocol_violation_streak(board.conn, "t_quote") == 0


# --- A2: an elapsed claim lock is never "still running" after one tick ---------------------

@pytest.mark.parametrize("shape, error", [
    (dict(record_worker_pid=False, owner=False), "stale_lock="),
    (dict(record_worker_pid=False, owner=True), "owner admission settled interrupted"),
    (dict(owner=False), "stale_lock="),
    (dict(owner=True, worker_lost="managed_worker_failed"), "managed worker lost: managed_worker_failed"),
], ids=["no-pid", "no-pid-receipt", "dead-pid", "dead-pid-lost"])
def test_an_elapsed_claim_never_stays_running_after_one_tick(board, shape, error):
    run_id = board.running("t_lapsed", claim_expires_in=CLAIM_LAPSED, heartbeat_age=None, **shape)
    board.tick(failure_limit=2)
    row, run = board.state("t_lapsed"), board.run(run_id)
    assert row["status"] == "ready"
    assert run["outcome"] == "reclaimed"
    assert error in run["error"]
    assert row["consecutive_failures"] == 1


def test_a_settled_receipt_with_a_live_gateway_owner_no_longer_holds_the_claim(board):
    """The fence itself: a live owner PROCESS is not a live ATTEMPT."""
    import os
    board.running("t_gateway", claim_expires_in=CLAIM_LAPSED, owner=True, owner_pid=os.getpid(),
                  admission=("terminal", "interrupted"))
    assert kanban_owner_recovery.owner_reclaim_paused(board.conn, "t_gateway") is True
    assert kanban_owner_recovery.owner_attempt_provably_over(board.conn, "t_gateway") is True
    board.tick(failure_limit=2)
    assert board.state("t_gateway")["status"] == "ready"


# --- the reclaim's kill step: an absent pid is gone, not "survived" -----------------------

@pytest.mark.parametrize("live", [False, True], ids=["absent-pid", "live-pid"])
def test_a_refused_signal_reads_the_pid_not_the_error(board, live):
    """A bare OSError from the kill (Windows reports an absent pid as error 87) is a success only
    when the pid is really gone; a live worker that refuses the signal is not terminated."""
    import os
    pid = os.getpid() if live else _absent_pid(base=3_970_000)

    def refusing_kill(pid, sig):
        raise OSError(87, "The parameter is incorrect")

    info = kbd._terminate_reclaimed_worker(pid, f"{HOST}:{_absent_pid(base=3_980_000)}",
                                           started_at=kbd._process_fingerprint(pid) if live else None,
                                           signal_fn=refusing_kill)
    assert info["terminated"] is (not live)


# --- D1: the OWNER writes the loss where the dispatcher can read it -----------------------
# ``worker.adopt`` precedes ``bind_worker_context``, so a worker that dies in the handshake
# writes neither ``worker_bound`` nor ``worker_result``: without this event the run learns
# nothing about its own loss and the dispatcher can only guess from a dead pid.

def _owner_policy(board, tid, run_id):
    from types import SimpleNamespace
    row = board.state(tid)
    return SimpleNamespace(kanban_json=json.dumps({
        "task_id": tid, "db": str(kb.kanban_db_path()), "run_id": run_id,
        "claim_lock": row["claim_lock"], "board": None,
    }))


@pytest.mark.asyncio
async def test_the_owner_records_the_loss_on_the_board_and_one_tick_disposes_of_it(board):
    """Through the gateway's loss handler: a Stop, a superseded generation or a cancellation
    records nothing; a real loss leaves one worker_lost event that the next tick books."""
    import asyncio
    from types import SimpleNamespace
    from gateway.session_managed_worker import _record_lost_attempt
    run_id = board.running("t_owner", claim_expires_in=900, heartbeat_age=30)
    policy = _owner_policy(board, "t_owner", run_id)
    authority = SimpleNamespace()
    for exc, reason in [(RuntimeError(), "managed_worker_stopped"), (RuntimeError(), "stale_generation"),
                        (asyncio.CancelledError(), "CancelledError"),
                        (RuntimeError(), "managed_worker_failed")]:
        recording = _record_lost_attempt(authority, policy, exc, reason, None)
        if recording is not None:
            await recording
    events = [json.loads(r[0]) for r in board.conn.execute(
        "SELECT payload FROM task_events WHERE task_id='t_owner' AND kind='worker_lost'")]
    assert [e["reason"] for e in events] == ["managed_worker_failed"]

    board.tick(failure_limit=2)
    run = board.run(run_id)
    assert run["outcome"] == "crashed"
    assert "managed worker lost: managed_worker_failed" in run["error"]
    assert board.state("t_owner")["consecutive_failures"] == 1


@pytest.mark.asyncio
async def test_a_loss_taken_during_a_drain_records_the_owners_restart_state(board):
    """A restart-time loss must be readable as one: the owner's drain state rides in the event."""
    from types import SimpleNamespace
    from gateway.session_managed_worker import _record_lost_attempt
    run_id = board.running("t_drain", claim_expires_in=900, heartbeat_age=30)
    policy = _owner_policy(board, "t_drain", run_id)
    authority = SimpleNamespace(_restart_requested=True, _draining=True, _external_drain_active=False)

    recording = _record_lost_attempt(authority, policy, RuntimeError(), "EOFError", None)
    if recording is not None:
        await recording

    payload = json.loads(board.conn.execute(
        "SELECT payload FROM task_events WHERE task_id='t_drain' AND kind='worker_lost' "
        "ORDER BY id DESC LIMIT 1").fetchone()[0])
    assert payload["reason"] == "EOFError"
    assert payload["owner"] == {"restart_requested": True, "draining": True}


def test_a_loss_recorded_against_a_superseded_run_is_dropped(board):
    """The owner's note is run-scoped: it may never be evidence about a LATER attempt."""
    from gateway.session_kanban import record_worker_loss
    run_id = board.running("t_stale", claim_expires_in=900, heartbeat_age=30)
    policy = _owner_policy(board, "t_stale", run_id)

    def count():
        return board.conn.execute(
            "SELECT count(*) FROM task_events WHERE task_id='t_stale' AND kind='worker_lost'").fetchone()[0]
    record_worker_loss(policy, "managed_worker_failed", pid=1234, bound=False)
    assert count() == 1
    board.conn.execute("UPDATE tasks SET status='ready', current_run_id=NULL WHERE id='t_stale'")
    board.conn.commit()
    record_worker_loss(policy, "managed_worker_failed", pid=1234, bound=False)
    assert count() == 1


# --- the loss must say WHY the pipe closed, not only that it did --------------------------
#
# 2026-11-17: four card attempts died with ``[managed worker lost: EOFError]`` during two
# update restarts, and the card that read those records concluded a memory-cgroup OOM kill
# from a ``free -g`` and one unrelated kernel line. The owner knows the worker's cgroup and
# the planned-restart marker at the moment of loss; carrying them into the ``worker_lost``
# payload is what makes the next reader's question answerable.

def test_a_loss_record_carries_the_cgroup_counters_and_the_restart_marker(board, monkeypatch):
    from gateway import session_kanban
    run_id = board.running("t_evidence", claim_expires_in=900, heartbeat_age=30)
    policy = _owner_policy(board, "t_evidence", run_id)
    monkeypatch.setattr(session_kanban, "_loss_memory",
                        lambda pid: {"path": "/x.scope", "max": 6442450944,
                                     "peak": 6597069766, "events": {"oom_kill": 0}})
    monkeypatch.setattr(session_kanban, "_planned_restart_evidence",
                        lambda: {"requested_at": 1.0, "age_s": 152})

    session_kanban.record_worker_loss(policy, "EOFError", pid=4242, bound=True)

    payload = json.loads(board.conn.execute(
        "SELECT payload FROM task_events WHERE task_id='t_evidence' AND kind='worker_lost' "
        "ORDER BY id DESC LIMIT 1").fetchone()[0])
    assert payload["reason"] == "EOFError" and payload["bound"] is True
    assert payload["memory"]["events"]["oom_kill"] == 0
    assert payload["memory"]["peak"] == 6597069766
    assert payload["planned_restart"]["age_s"] == 152


def test_the_run_error_names_the_cgroup_and_the_planned_restart(board):
    """The booked crash says ``oom_kill=0`` and the restart age, so memory is not the suspect."""
    run_id = board.running("t_note", claim_expires_in=900, heartbeat_age=30, worker_lost="EOFError")
    board.event("t_note", run_id, "worker_lost", {
        "reason": "EOFError", "run_id": run_id, "pid": 1, "bound": True,
        "memory": {"max": 6442450944, "peak": 6597069766, "events": {"oom_kill": 0}},
        "planned_restart": {"age_s": 152},
    }, NOW + 24)
    board.conn.commit()

    board.tick(failure_limit=2)

    run = board.run(run_id)
    assert run["outcome"] == "crashed"
    assert "managed worker lost: EOFError" in run["error"]
    assert "oom_kill=0" in run["error"]
    assert "cgroup peak 6.14 GiB of 6.00 GiB cap" in run["error"]
    assert "planned restart 152s earlier" in run["error"]
    # The record says a planned restart was in flight: the host's drain, not the card's work.
    # (The old assertion here was ``consecutive_failures == 1`` — the charge this card removes.)
    assert board.state("t_note")["consecutive_failures"] == 0
    assert json.loads(board.conn.execute(
        "SELECT payload FROM task_events WHERE task_id='t_note' AND kind='crashed' "
        "ORDER BY id DESC LIMIT 1").fetchone()[0])["infrastructure"] is True


# --- a restart-caused loss is the HOST's, whatever words the worker left --------------------
#
# After a worker waits out a planned restart (``test_kanban_worker_restart_patience.py``) the
# attempt can still end when the drain's cap interrupts the turn. That record no longer quotes
# "Kanban owner silent for ...": the words are the drain's own, so the booking must read the
# owner's structured evidence (``planned_restart`` / ``owner.restart_requested``) instead of a
# phrase, and it must never charge the card — `hermes update` runs on a timer, so a card in
# flight often enough would otherwise block itself on the third restart.

def test_a_planned_restart_loss_is_booked_host_side_and_charges_nothing(board):
    run_id = board.running("t_restart", claim_expires_in=900, heartbeat_age=30,
                           worker_lost="EOFError",
                           loss_extra={"owner": {"restart_requested": True, "draining": True}})

    board.tick(failure_limit=2)

    row, run = board.state("t_restart"), board.run(run_id)
    assert row["status"] == "ready" and row["consecutive_failures"] == 0
    crashed = json.loads(board.conn.execute(
        "SELECT payload FROM task_events WHERE task_id='t_restart' AND kind='crashed' "
        "ORDER BY id DESC LIMIT 1").fetchone()[0])
    assert crashed["infrastructure"] is True
    assert "managed worker lost: EOFError" in run["error"]
    assert "owner restart requested" in run["error"]


def test_a_planned_restart_never_exhausts_the_strike_free_budget(board):
    """The fourth restart of one card is still the host's: `hermes update` is not the card's bug."""
    run_id = board.running("t_many", claim_expires_in=900, heartbeat_age=30, worker_lost="EOFError",
                           loss_extra={"planned_restart": {"requested_at": 1.0, "age_s": 30}})
    for _ in range(kbd.INFRA_STRIKE_FREE_LIMIT):
        board.event("t_many", run_id, "crashed", {
            "pid": 1, "claimer": "x", "infrastructure": True,
            "worker_lost": {"reason": "EOFError", "planned_restart": {"requested_at": 1.0, "age_s": 30}},
        }, NOW + 24)
    board.conn.commit()

    board.tick(failure_limit=2)

    row = board.state("t_many")
    assert row["status"] == "ready" and row["consecutive_failures"] == 0
    assert not any(r["kind"] == "gave_up" for r in board.conn.execute(
        "SELECT kind FROM task_events WHERE task_id='t_many'"))


def test_an_oom_kill_during_a_drain_is_still_the_cards_failure():
    # The marker says a restart was planned; the cgroup says memory killed the worker anyway.
    # The counter outranks the marker, so the loss is not booked strike-free.
    planned = {"reason": "EOFError", "planned_restart": {"requested_at": 1.0, "age_s": 5},
               "owner": {"draining": True}}
    assert kanban_owner_recovery.planned_restart_loss(planned) is True
    assert kanban_owner_recovery.planned_restart_loss(
        {**planned, "memory": {"events": {"oom_kill": 0}}}) is True
    assert kanban_owner_recovery.planned_restart_loss(
        {**planned, "memory": {"events": {"oom_kill": 1}}}) is False
