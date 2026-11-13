"""A lane must not die at the BOARD: infrastructure failures spend no crash strike, and a card
that gave up on one re-opens itself when the host recovers (kanban t_4c6382fd).

Measured on the ps5 board 2026-11-11: one ``spawn_failed`` (no user D-Bus / systemd scope) plus
three crashes whose text was ``Kanban owner refused: Kanban owner silent for 180s`` tripped
``effective_limit: 2`` -> ``gave_up``, and because a given-up card also gates its ``todo``
successors the ladder sat dead ~6 h with nothing wrong on the console.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from tools.process_registry import RestartSafeScopeUnavailable

OWNER_SILENT = (
    "pid 999999 exited with code 1 Worker's last output: 'Kanban owner refused: "
    "Kanban owner silent for 180s: TimeoutError Kanban owner refused: "
    "Kanban owner silent for 183s: Gateway draining: update_paused'"
)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kbc.connect() as c:
        yield c


def _failures(conn, tid):
    return int(conn.execute("SELECT consecutive_failures FROM tasks WHERE id = ?", (tid,)).fetchone()[0])


def _status(conn, tid):
    return conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0]


def _events(conn, tid, kind):
    return conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id", (tid, kind)
    ).fetchall()


def _dead(text, *, protocol_violation=False):
    return kbd._DeadWorker(
        "nonzero_exit", 1, text, "crashed", {"pid": 999999, "claimer": "test"}, protocol_violation,
    )


# ---------------------------------------------------------------------------
# 1. A spawn the HOST refused must not spend a crash strike
# ---------------------------------------------------------------------------

def test_spawn_failed_injection_does_not_burn_a_crash_strike(conn, all_assignees_spawnable):
    tid = kb.create_task(conn, title="infra spawn refusal", assignee="w")

    def refuse(task, workspace_path, board=None):
        raise RestartSafeScopeUnavailable("systemd-run --user --scope is unavailable (no user bus)")

    # Three consecutive ticks against a host that refuses: with failure_limit 2 a card-charged
    # failure would have given up on the second.
    results = [kbd.dispatch_once(conn, spawn_fn=refuse, failure_limit=2) for _ in range(3)]

    assert _failures(conn, tid) == 0, "a host refusal is not the card's failure"
    assert _status(conn, tid) == "ready"
    assert not _events(conn, tid, "gave_up"), "a host refusal must never gave_up a card"
    runs = conn.execute(
        "SELECT outcome, metadata FROM task_runs WHERE task_id = ? ORDER BY id", (tid,)
    ).fetchall()
    assert [r[0] for r in runs] == ["spawn_failed"]
    assert kb._json_dict(runs[0][1]).get("infrastructure") is True
    # Retries are SPACED, not charged: the guard names the host condition.
    assert [r for res in results[1:] for (_tid, r) in res.respawn_guarded] == [
        "infrastructure_cooldown", "infrastructure_cooldown"]


# ---------------------------------------------------------------------------
# 2. A crash whose text names a host fault must not spend a crash strike
# ---------------------------------------------------------------------------

def test_owner_handshake_crash_does_not_burn_a_crash_strike(conn):
    tid = kb.create_task(conn, title="owner silent", assignee="w")
    assert kb.claim_task(conn, tid) is not None

    dead = _dead(OWNER_SILENT)
    assert kbd._stamp_infrastructure(dead) is True
    assert dead.event_payload.get("infrastructure") is True
    kbd._account_crashes(conn, [(tid, 999999, "test", dead)])
    kbd._account_crashes(conn, [(tid, 999999, "test", _dead(OWNER_SILENT))])

    assert _failures(conn, tid) == 0, "an owner-handshake timeout says nothing about the card"
    assert not _events(conn, tid, "gave_up")
    err = conn.execute("SELECT last_failure_error FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    assert "Kanban owner silent" in (err or "")


def test_a_real_crash_still_trips_the_breaker(conn):
    """The infrastructure rule must not make the breaker unreachable."""
    tid = kb.create_task(conn, title="genuine bug", assignee="w")
    assert kb.claim_task(conn, tid) is not None

    kbd._account_crashes(conn, [(tid, 999999, "test", _dead("pid 999999 exited with code 1 ImportError: no module named frobnicate"))])
    assert _failures(conn, tid) == 1
    kbd._account_crashes(conn, [(tid, 999999, "test", _dead("pid 999999 exited with code 1 ImportError: no module named frobnicate"))])
    assert _failures(conn, tid) == 2
    assert _status(conn, tid) == "blocked"
    assert _events(conn, tid, "gave_up")


# ---------------------------------------------------------------------------
# 3. A gave-up-on-infrastructure card re-opens itself once the host recovers
# ---------------------------------------------------------------------------

def _blocked_with_gave_up(conn, *, payload):
    tid = kb.create_task(conn, title="gave up", assignee="w")
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'blocked', last_failure_error = ? WHERE id = ?",
            (payload.get("error", ""), tid),
        )
        kb._append_event(conn, tid, "gave_up", payload)
    return tid


def test_infrastructure_gave_up_reopens_without_a_human(conn):
    tid = _blocked_with_gave_up(conn, payload={
        "failures": 2, "effective_limit": 2, "trigger_outcome": "crashed",
        "error": OWNER_SILENT, "infrastructure": True,
    })

    reopened = kbd.reopen_infrastructure_gave_ups(
        conn, probe_fn=lambda: True, now=int(time.time()) + kbd.INFRA_REOPEN_MIN_AGE_S + 1,
    )

    assert reopened == [tid]
    assert _status(conn, tid) == "ready"
    assert _failures(conn, tid) == 0
    assert _events(conn, tid, "infrastructure_recovered")


def test_infrastructure_gave_up_waits_while_the_host_is_still_broken(conn):
    tid = _blocked_with_gave_up(conn, payload={
        "failures": 2, "effective_limit": 2, "trigger_outcome": "spawn_failed",
        "error": "cannot create restart-safe systemd scope for gateway child", "infrastructure": True,
    })

    assert kbd.reopen_infrastructure_gave_ups(
        conn, probe_fn=lambda: False, now=time.time() + kbd.INFRA_REOPEN_MIN_AGE_S + 1,
    ) == []
    assert _status(conn, tid) == "blocked"


def test_a_genuine_gave_up_is_reviewed_by_a_human_not_reopened(conn):
    tid = _blocked_with_gave_up(conn, payload={
        "failures": 2, "effective_limit": 2, "trigger_outcome": "crashed",
        "error": "pid 42 exited with code 1 AssertionError: harness invariant broken",
    })

    assert kbd.reopen_infrastructure_gave_ups(
        conn, probe_fn=lambda: True, now=int(time.time()) + kbd.INFRA_REOPEN_MIN_AGE_S + 1,
    ) == []
    assert _status(conn, tid) == "blocked"


def test_a_stale_give_up_is_left_to_a_human(conn):
    """A give-up older than the recovery window may have been seen by a human already."""
    tid = _blocked_with_gave_up(conn, payload={
        "failures": 2, "trigger_outcome": "spawn_failed",
        "error": "Kanban owner silent for 180s", "infrastructure": True,
    })
    now = int(time.time()) + kbd.INFRA_REOPEN_MAX_AGE_S + 60

    assert kbd.reopen_infrastructure_gave_ups(conn, probe_fn=lambda: True, now=now) == []
    assert _status(conn, tid) == "blocked"


def test_a_later_block_is_not_overruled_by_the_recovery(conn):
    """The give-up must be the card's last word: a card that blocked itself again stays blocked."""
    tid = _blocked_with_gave_up(conn, payload={
        "failures": 2, "trigger_outcome": "spawn_failed",
        "error": "Kanban owner silent for 180s", "infrastructure": True,
    })
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "blocked", {"reason": "goal unreachable by design"})

    assert kbd.reopen_infrastructure_gave_ups(
        conn, probe_fn=lambda: True, now=int(time.time()) + kbd.INFRA_REOPEN_MIN_AGE_S + 1,
    ) == []
    assert _status(conn, tid) == "blocked"


def test_reopen_is_bounded_per_tick(conn):
    ids = [_blocked_with_gave_up(conn, payload={
        "failures": 2, "trigger_outcome": "spawn_failed",
        "error": "Kanban owner silent for 180s", "infrastructure": True,
    }) for _ in range(5)]

    reopened = kbd.reopen_infrastructure_gave_ups(
        conn, probe_fn=lambda: True, now=int(time.time()) + kbd.INFRA_REOPEN_MIN_AGE_S + 1,
    )
    assert len(reopened) == kbd.INFRA_REOPEN_PER_TICK
    assert len([i for i in ids if _status(conn, i) == "ready"]) == kbd.INFRA_REOPEN_PER_TICK
