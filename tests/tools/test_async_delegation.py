"""Tests for async (background) delegation — tools/async_delegation.py.

Covers the dispatch handle, non-blocking behavior, completion-event delivery
onto the shared process_registry.completion_queue, the rich re-injection block
formatting, capacity rejection, and crash handling.
"""

import json
import os
import queue
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from tools import async_delegation as ad
from tools.process_registry import process_registry
from tools.process_registry_notifications import format_process_notification


@pytest.fixture(autouse=True)
def _clean_state():
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    yield
    # Give just-released workers a beat to finalize BEFORE draining, so their
    # completion events land now instead of leaking into the next test's
    # queue (worker threads push events asynchronously; a drain that races an
    # in-flight _finalize misses it).
    deadline = time.monotonic() + 2.0
    while ad.active_count() and time.monotonic() < deadline:
        time.sleep(0.02)
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()


def _drain_one(timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_registry.completion_queue.empty():
            return process_registry.completion_queue.get_nowait()
        time.sleep(0.02)
    return None


def _drain_for(delegation_id, timeout=5.0):
    """Drain until the event for *delegation_id* appears (discarding others).

    Completion events are pushed asynchronously by worker threads, so a
    straggler from a PREVIOUS test can land after that test's teardown drain
    and leak into the current test's queue. Matching on delegation_id makes
    the assertion immune to that cross-test leak.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_registry.completion_queue.empty():
            evt = process_registry.completion_queue.get_nowait()
            if evt.get("delegation_id") == delegation_id:
                return evt
            continue
        time.sleep(0.02)
    return None


def test_schema_init_preserves_shared_state_db_journal_mode(tmp_path):
    """The delegation ledger is a guest in state.db, not its mode owner."""
    conn = sqlite3.connect(tmp_path / "state.db")
    try:
        assert conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"

        ad._initialize_schema(conn)

        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='async_delegations'"
        ).fetchone() == ("async_delegations",)
    finally:
        conn.close()


def test_schema_init_preserves_shared_state_db_wal_mode(tmp_path):
    """Schema initialization must not replace an existing WAL mode."""
    conn = sqlite3.connect(tmp_path / "state.db")
    try:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"

        ad._initialize_schema(conn)

        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='async_delegations'"
        ).fetchone() == ("async_delegations",)
    finally:
        conn.close()


@pytest.mark.macos_only
def test_connect_preserves_wal_and_applies_macos_durability_barriers(
    tmp_path, monkeypatch
):
    """Each ledger connection must carry the macOS write barriers."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    seed = sqlite3.connect(tmp_path / "state.db")
    try:
        assert seed.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    finally:
        seed.close()

    conn = ad._connect()
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert conn.execute("PRAGMA checkpoint_fullfsync").fetchone()[0] == 1
    finally:
        conn.close()


def test_dispatch_returns_immediately_without_blocking():
    gate = threading.Event()

    def runner():
        gate.wait(timeout=60)
        return {"status": "completed", "summary": "done", "api_calls": 1,
                "duration_seconds": 0.1, "model": "m"}

    t0 = time.monotonic()
    res = ad.dispatch_async_delegation(
        goal="g", context=None, toolsets=None, role="leaf", model="m",
        session_key="", runner=runner, max_async_children=3,
    )
    elapsed = time.monotonic() - t0

    assert res["status"] == "dispatched"
    assert res["delegation_id"].startswith("deleg_")
    # Non-blocking invariant: dispatch returned while the runner is still
    # gated (active), so it cannot have waited on the gate. The active_count
    # check is the environment-independent proof; the generous wall-clock
    # bound is a loose sanity backstop, not the primary assertion (a loaded
    # CI runner can be slow but never anywhere near the runner's 5s gate).
    assert ad.active_count() == 1
    assert elapsed < 4.0, f"dispatch blocked {elapsed:.2f}s (gate is 5s)"
    gate.set()


def test_async_executor_workers_are_daemon_threads():
    gate = threading.Event()

    def runner():
        gate.wait(timeout=60)
        return {"status": "completed", "summary": "done"}

    res = ad.dispatch_async_delegation(
        goal="daemon check", context=None, toolsets=None, role="leaf", model="m",
        session_key="", runner=runner, max_async_children=1,
    )
    assert res["status"] == "dispatched"

    deadline = time.monotonic() + 2
    worker = None
    while time.monotonic() < deadline:
        worker = next(
            (t for t in threading.enumerate() if t.name.startswith("async-delegate")),
            None,
        )
        if worker is not None:
            break
        time.sleep(0.02)
    assert worker is not None
    assert worker.daemon is True
    gate.set()
    assert _drain_one() is not None


def test_completion_event_lands_on_shared_queue_with_session_key():
    def runner():
        return {"status": "completed", "summary": "the result",
                "api_calls": 3, "duration_seconds": 2.0, "model": "test-model"}

    res = ad.dispatch_async_delegation(
        goal="compute X", context="some context", toolsets=["web", "file"],
        role="leaf", model="test-model", session_key="agent:main:cli:dm:local",
        parent_session_id="20260703_parent_sid",
        runner=runner, max_async_children=3,
    )
    assert res["status"] == "dispatched"

    evt = _drain_one()
    assert evt is not None
    assert evt["type"] == "async_delegation"
    assert evt["summary"] == "the result"
    assert evt["session_key"] == "agent:main:cli:dm:local"
    assert evt["parent_session_id"] == "20260703_parent_sid"
    assert evt["delegation_id"] == res["delegation_id"]


def test_rich_reinjection_block_is_self_contained():
    def runner():
        return {"status": "completed", "summary": "The answer is 42.",
                "api_calls": 7, "duration_seconds": 3.5, "model": "test-model"}

    ad.dispatch_async_delegation(
        goal="Compute the meaning of life",
        context="User is a philosopher. Respond tersely.",
        toolsets=["web"], role="leaf", model="test-model",
        session_key="", runner=runner, max_async_children=3,
    )
    evt = _drain_one()
    assert evt is not None
    text = format_process_notification(evt)
    assert text is not None
    for needle in [
        "ASYNC DELEGATION COMPLETE",
        "Compute the meaning of life",
        "User is a philosopher",
        "Toolsets: web",
        "The answer is 42.",
        "Status: completed",
        "API calls: 7",
    ]:
        assert needle in text, f"missing {needle!r}"


def test_dispatch_rejected_at_capacity():
    ev = threading.Event()

    def blocker():
        ev.wait(timeout=60)
        return {"status": "completed", "summary": "x"}

    for i in range(2):
        r = ad.dispatch_async_delegation(
            goal=f"task{i}", context=None, toolsets=None, role="leaf",
            model="m", session_key="", runner=blocker, max_async_children=2,
        )
        assert r["status"] == "dispatched"

    r3 = ad.dispatch_async_delegation(
        goal="task3", context=None, toolsets=None, role="leaf", model="m",
        session_key="", runner=blocker, max_async_children=2,
    )
    assert r3["status"] == "rejected"
    assert "capacity reached" in r3["error"]
    ev.set()


def test_interrupt_all_signals_running_children():
    ev = threading.Event()
    interrupted = {"count": 0}
    # No short internal timeout: the blocker holds until interrupt_fn fires.
    # The old ev.wait(timeout=5) made this test a change-detector for CI
    # worker load — on a CPU-starved runner the 5s expired before
    # interrupt_all() ran, the record finalized, and interrupt_all() found
    # nothing running (n == 0). The pytest-level timeout is the real
    # runaway guard.

    def blocker():
        ev.wait(timeout=60)
        return {"status": "interrupted", "summary": None,
                "error": "cancelled"}

    def interrupt_fn():
        interrupted["count"] += 1
        ev.set()

    r = ad.dispatch_async_delegation(
        goal="long task", context=None, toolsets=None, role="leaf",
        model="m", session_key="", runner=blocker,
        interrupt_fn=interrupt_fn, max_async_children=3,
    )
    n = ad.interrupt_all(reason="test")
    assert n == 1
    assert interrupted["count"] == 1
    # child still emits a completion event after interrupt. Match on THIS
    # delegation's id — straggler 'completed' events from a previous test's
    # workers can finalize after that test's teardown drain and leak into
    # this queue (observed on loaded CI workers).
    evt = _drain_for(r["delegation_id"])
    assert evt is not None
    assert evt["status"] == "interrupted"


def _fast_stale_monitor(monkeypatch, *, idle=0.15, in_tool=0.3, grace=0.15):
    """Shrink the stale-monitor cadence so tests run in milliseconds."""
    monkeypatch.setattr(ad, "_STALE_CHECK_INTERVAL", 0.03)
    monkeypatch.setattr(ad, "_STALE_IDLE_SECONDS", idle)
    monkeypatch.setattr(ad, "_STALE_IN_TOOL_SECONDS", in_tool)
    monkeypatch.setattr(ad, "_STALL_GRACE_SECONDS", grace)


def test_stalled_runner_is_interrupted_then_finalized(monkeypatch):
    _fast_stale_monitor(monkeypatch)
    gate = threading.Event()
    interrupted = {"count": 0}

    def stuck_runner():
        gate.wait(timeout=10)
        return {"status": "completed", "summary": "too late"}

    def interrupt_fn():
        interrupted["count"] += 1

    res = ad.dispatch_async_delegation(
        goal="stuck child", context=None, toolsets=None, role="leaf",
        model="m", session_key="", runner=stuck_runner,
        interrupt_fn=interrupt_fn, max_async_children=1,
        # Frozen progress token: the child never advances an API call.
        progress_fn=lambda: ((0, None), False),
    )
    assert res["status"] == "dispatched"

    evt = _drain_for(res["delegation_id"], timeout=5.0)
    try:
        assert evt is not None
        assert evt["type"] == "async_delegation"
        assert evt["status"] == "stalled"
        assert evt["delegation_id"] == res["delegation_id"]
        assert evt["api_calls"] == 0
        assert "stopped responding" in evt["error"]  # status carries "stalled"; the text is for the user
        # Interrupt was requested BEFORE force-finalization (grace window).
        assert interrupted["count"] >= 1
        assert ad.active_count() == 0
    finally:
        gate.set()

    # If the ignored runner eventually returns, it must not enqueue a second
    # completion for a delegation the monitor already finalized.
    assert _drain_one(timeout=0.5) is None


def test_progressing_runner_is_never_stalled(monkeypatch):
    """A child that keeps advancing is left alone no matter how long it runs."""
    _fast_stale_monitor(monkeypatch)
    gate = threading.Event()
    ticks = {"n": 0}

    def slow_but_alive_runner():
        gate.wait(timeout=10)
        return {"status": "completed", "summary": "done", "api_calls": 7}

    def progress_fn():
        # Token advances on every sample — simulates a child making steady
        # API-call progress.
        ticks["n"] += 1
        return (ticks["n"], None), False

    res = ad.dispatch_async_delegation(
        goal="slow child", context=None, toolsets=None, role="leaf",
        model="m", session_key="", runner=slow_but_alive_runner,
        max_async_children=1, progress_fn=progress_fn,
    )
    assert res["status"] == "dispatched"

    # Run well past the (shrunk) idle threshold — several monitor sweeps.
    time.sleep(0.6)
    assert ad.active_count() == 1
    assert process_registry.completion_queue.empty()

    gate.set()
    evt = _drain_for(res["delegation_id"], timeout=5.0)
    assert evt is not None
    assert evt["status"] == "completed"
    assert evt["summary"] == "done"


def test_stalling_runner_that_honors_interrupt_keeps_its_result(monkeypatch):
    """Interrupt-responsive children finalize through the NORMAL path.

    The monitor's interrupt gives a wedged-looking child a grace window; if
    the runner returns during it, the real result (partial work, api_calls)
    is delivered instead of a synthetic stalled event.
    """
    _fast_stale_monitor(monkeypatch, grace=5.0)
    interrupted = threading.Event()

    def runner():
        # "Wedged" until interrupted, then unwinds and reports partial work.
        interrupted.wait(timeout=10)
        return {
            "status": "interrupted",
            "summary": "partial work saved",
            "api_calls": 3,
        }

    res = ad.dispatch_async_delegation(
        goal="responsive child", context=None, toolsets=None, role="leaf",
        model="m", session_key="", runner=runner,
        interrupt_fn=interrupted.set, max_async_children=1,
        progress_fn=lambda: ((3, None), False),
    )
    assert res["status"] == "dispatched"

    evt = _drain_for(res["delegation_id"], timeout=5.0)
    assert evt is not None
    assert evt["status"] == "interrupted"
    assert evt["summary"] == "partial work saved"
    assert evt["api_calls"] == 3
    assert ad.active_count() == 0


def test_streaming_child_counts_as_alive(monkeypatch):
    """A child mid-stream (api_call_count frozen, last_activity_ts ticking)
    must never be stalled — streamed chunks tick _touch_activity, and the
    progress token includes that timestamp (same liveness signal as the
    compaction inactivity budget, PR #71508)."""
    _fast_stale_monitor(monkeypatch)
    gate = threading.Event()
    now = {"ts": 1000.0}

    def progress_fn():
        # api_call_count and current_tool frozen (long streaming response in
        # flight), but the activity timestamp advances with every chunk.
        now["ts"] += 1.0
        return ((1, None, now["ts"]),), False

    res = ad.dispatch_async_delegation(
        goal="streaming child", context=None, toolsets=None, role="leaf",
        model="m", session_key="", max_async_children=1,
        runner=lambda: (gate.wait(timeout=10), {"status": "completed", "summary": "streamed"})[1],
        progress_fn=progress_fn,
    )
    assert res["status"] == "dispatched"

    time.sleep(0.6)  # several sweeps past the shrunk idle threshold
    assert ad.active_count() == 1
    assert process_registry.completion_queue.empty()

    gate.set()
    evt = _drain_for(res["delegation_id"], timeout=5.0)
    assert evt is not None
    assert evt["status"] == "completed"


def test_stalled_batch_is_interrupted_then_finalized(monkeypatch):
    _fast_stale_monitor(monkeypatch)
    gate = threading.Event()
    interrupted = {"count": 0}

    def stuck_batch():
        gate.wait(timeout=10)
        return {"results": [{"status": "completed", "summary": "too late"}]}

    def interrupt_fn():
        interrupted["count"] += 1

    res = ad.dispatch_async_delegation_batch(
        goals=["a", "b"], context="ctx", toolsets=None, role="leaf",
        model="m", session_key="", runner=stuck_batch,
        interrupt_fn=interrupt_fn, max_async_children=1,
        progress_fn=lambda: (((0, None), (0, None)), False),
    )
    assert res["status"] == "dispatched"

    evt = _drain_for(res["delegation_id"], timeout=5.0)
    try:
        assert evt is not None
        assert evt["type"] == "async_delegation"
        assert evt["status"] == "stalled"
        assert evt["is_batch"] is True
        assert evt["goals"] == ["a", "b"]
        assert evt["results"] == []
        assert "stalled" in evt["error"]
        assert interrupted["count"] >= 1
        assert ad.active_count() == 0
    finally:
        gate.set()

    assert _drain_one(timeout=0.5) is None


def test_in_tool_stall_uses_higher_threshold(monkeypatch):
    """A frozen child inside a tool gets the in-tool ceiling, not the idle one."""
    _fast_stale_monitor(monkeypatch, idle=0.1, in_tool=10.0, grace=0.1)
    gate = threading.Event()

    def runner():
        gate.wait(timeout=10)
        return {"status": "completed", "summary": "long tool finished"}

    res = ad.dispatch_async_delegation(
        goal="long tool child", context=None, toolsets=None, role="leaf",
        model="m", session_key="", runner=runner, max_async_children=1,
        # Frozen token but in_tool=True — a legitimately slow terminal
        # command / web fetch. Must NOT be stalled at the idle threshold.
        progress_fn=lambda: ((1, "terminal"), True),
    )
    assert res["status"] == "dispatched"

    time.sleep(0.5)  # far past idle threshold, well under in-tool threshold
    assert ad.active_count() == 1
    assert process_registry.completion_queue.empty()

    gate.set()
    evt = _drain_for(res["delegation_id"], timeout=5.0)
    assert evt is not None
    assert evt["status"] == "completed"


def test_stall_stays_finalizing_until_durable_persistence(tmp_path, monkeypatch):
    _fast_stale_monitor(monkeypatch)
    gate = threading.Event()
    persist_entered = threading.Event()
    allow_persist = threading.Event()
    real_persist = ad._persist_completion

    def blocking_persist(event, result):
        persist_entered.set()
        allow_persist.wait(timeout=5)
        real_persist(event, result)

    def stuck_runner():
        gate.wait(timeout=10)
        return {"status": "completed", "summary": "too late"}

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(ad, "_persist_completion", blocking_persist)
    dispatched = ad.dispatch_async_delegation(
        goal="durable stall", context=None, toolsets=None, role="leaf",
        model="m", session_key="owner", runner=stuck_runner,
        max_async_children=1, progress_fn=lambda: ((0, None), False),
    )

    try:
        assert persist_entered.wait(timeout=5)
        assert ad.active_count() == 1
        record = next(
            item for item in ad.list_async_delegations()
            if item["delegation_id"] == dispatched["delegation_id"]
        )
        assert record["status"] == "finalizing"
        assert process_registry.completion_queue.empty()

        allow_persist.set()
        evt = _drain_for(dispatched["delegation_id"])
        assert evt is not None
        assert evt["status"] == "stalled"
        assert ad.active_count() == 0
        durable = ad.get_durable_delegation(dispatched["delegation_id"])
        assert durable["state"] == "stalled"
        assert durable["delivery_state"] == "pending"
    finally:
        allow_persist.set()
        gate.set()


def test_stalled_completion_restores_once_after_process_restart(tmp_path):
    repo = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    env = {**os.environ, "HERMES_HOME": str(tmp_path), "PYTHONPATH": repo}
    producer = r'''
import json
import threading
import time
from tools import async_delegation as ad
ad._STALE_CHECK_INTERVAL = 0.03
ad._STALE_IDLE_SECONDS = 0.1
ad._STALL_GRACE_SECONDS = 0.1
gate = threading.Event()
r = ad.dispatch_async_delegation(
    goal="restart stall", context=None, toolsets=None, role="leaf", model="m",
    session_key="owner-session", parent_session_id="durable-parent",
    runner=lambda: gate.wait(timeout=60),
    progress_fn=lambda: ((0, None), False),
)
deadline = time.time() + 10
while ad.active_count() and time.time() < deadline:
    time.sleep(.01)
row = ad.get_durable_delegation(r["delegation_id"])
print(json.dumps({"delegation_id": r["delegation_id"], "row": row}, sort_keys=True))
'''
    first = subprocess.run(
        [sys.executable, "-c", producer], cwd=repo, env=env,
        text=True, capture_output=True, timeout=30, check=True,
    )
    produced = json.loads(first.stdout.strip().splitlines()[-1])
    delegation_id = produced["delegation_id"]
    assert produced["row"]["state"] == "stalled"
    assert produced["row"]["delivery_state"] == "pending"

    consumer = r'''
import json
from tools.process_registry import process_registry
evt = process_registry.completion_queue.get_nowait()
print(json.dumps({"event": evt, "remaining": process_registry.completion_queue.qsize()}, sort_keys=True))
'''
    second = subprocess.run(
        [sys.executable, "-c", consumer], cwd=repo, env=env,
        text=True, capture_output=True, timeout=15, check=True,
    )
    restored = json.loads(second.stdout.strip().splitlines()[-1])
    assert restored["remaining"] == 0
    assert restored["event"]["delegation_id"] == delegation_id
    assert restored["event"]["status"] == "stalled"
    assert restored["event"]["restored"] is True

    acker = f'''
from tools import async_delegation as ad
assert ad.mark_completion_delivered({delegation_id!r})
'''
    subprocess.run(
        [sys.executable, "-c", acker], cwd=repo, env=env,
        text=True, capture_output=True, timeout=15, check=True,
    )
    probe = subprocess.run(
        [sys.executable, "-c", "from tools.process_registry import process_registry; print(process_registry.completion_queue.qsize())"],
        cwd=repo, env=env, text=True, capture_output=True, timeout=15, check=True,
    )
    assert probe.stdout.strip().splitlines()[-1] == "0"


def test_completed_records_pruned_to_cap():
    # Run more than the retention cap quickly; ensure list doesn't grow forever.
    for i in range(ad._MAX_RETAINED_COMPLETED + 10):
        ad.dispatch_async_delegation(
            goal=f"t{i}", context=None, toolsets=None, role="leaf", model="m",
            session_key="", runner=lambda: {"status": "completed", "summary": "ok"},
            max_async_children=ad._MAX_RETAINED_COMPLETED + 20,
        )
    # let workers finish
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and ad.active_count() > 0:
        time.sleep(0.05)
    assert len(ad.list_async_delegations()) <= ad._MAX_RETAINED_COMPLETED


def test_stalled_event_carries_structured_stall_metadata(monkeypatch):
    """The terminal stalled event must expose machine-readable stall context
    (#51690) — quiet duration, tripped threshold, phase, grace — mirroring
    the sync path's timeout_seconds/timed_out_after_seconds/timeout_phase."""
    _fast_stale_monitor(monkeypatch)
    gate = threading.Event()

    res = ad.dispatch_async_delegation(
        goal="stall metadata", context=None, toolsets=None, role="leaf",
        model="m", session_key="", max_async_children=1,
        runner=lambda: {} if gate.wait(timeout=10) else {},
        progress_fn=lambda: ((0, "terminal"), True),
    )
    assert res["status"] == "dispatched"

    evt = _drain_for(res["delegation_id"], timeout=5.0)
    try:
        assert evt is not None
        assert evt["status"] == "stalled"
        assert evt["stalled_after_quiet_seconds"] >= 0.3  # in-tool threshold
        assert evt["stall_threshold_seconds"] == ad._STALE_IN_TOOL_SECONDS
        assert evt["stall_phase"] == "in_tool"
        assert evt["stall_grace_seconds"] == ad._STALL_GRACE_SECONDS
    finally:
        gate.set()


def test_list_async_delegations_exposes_live_activity(monkeypatch):
    """list_async_delegations must expose per-child live activity sampled
    from progress_fn plus seconds_since_progress, for /agents UIs (#51690)."""
    monkeypatch.setattr(ad, "_STALE_CHECK_INTERVAL", 0.03)
    gate = threading.Event()
    base_ts = time.time() - 12.0

    res = ad.dispatch_async_delegation(
        goal="live listing", context=None, toolsets=None, role="leaf",
        model="m", session_key="", max_async_children=1,
        runner=lambda: {} if gate.wait(timeout=10) else {},
        progress_fn=lambda: (((3, "web_search", base_ts),), True),
    )
    try:
        time.sleep(0.1)  # let the monitor stamp _progress_ts at least once
        item = next(
            d for d in ad.list_async_delegations()
            if d["delegation_id"] == res["delegation_id"]
        )
        assert item["status"] == "running"
        assert item["in_tool"] is True
        assert "seconds_since_progress" in item
        (child,) = item["children_activity"]
        assert child["api_calls"] == 3
        assert child["current_tool"] == "web_search"
        assert 10.0 <= child["seconds_since_activity"] <= 20.0
        # Callables and private bookkeeping must never leak.
        assert "progress_fn" not in item
        assert "interrupt_fn" not in item
        assert not any(k.startswith("_") for k in item)
    finally:
        gate.set()


def test_real_process_restart_restores_owned_completion_once(tmp_path):
    """Real-import E2E: a fresh interpreter restores a prior process's result."""
    repo = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    env = {**os.environ, "HERMES_HOME": str(tmp_path), "PYTHONPATH": repo}
    producer = r'''
import time
from tools import async_delegation as ad
r = ad.dispatch_async_delegation(
    goal="restart", context=None, toolsets=None, role="leaf", model="m",
    session_key="owner-session", parent_session_id="durable-parent",
    runner=lambda: {"status": "completed", "summary": "after restart"},
)
deadline = time.time() + 5
while ad.active_count() and time.time() < deadline:
    time.sleep(.01)
print(r["delegation_id"])
'''
    first = subprocess.run(
        [sys.executable, "-c", producer], cwd=repo, env=env,
        text=True, capture_output=True, timeout=15, check=True,
    )
    delegation_id = first.stdout.strip().splitlines()[-1]

    consumer = r'''
import json
from tools.process_registry import process_registry
evt = process_registry.completion_queue.get_nowait()
print(json.dumps(evt, sort_keys=True))
'''
    second = subprocess.run(
        [sys.executable, "-c", consumer], cwd=repo, env=env,
        text=True, capture_output=True, timeout=15, check=True,
    )
    evt = json.loads(second.stdout.strip().splitlines()[-1])
    assert evt["delegation_id"] == delegation_id
    assert evt["session_key"] == "owner-session"
    assert evt["parent_session_id"] == "durable-parent"
    assert evt["summary"] == "after restart"

    acker = f'''
from tools import async_delegation as ad
assert ad.mark_completion_delivered({delegation_id!r})
'''
    subprocess.run(
        [sys.executable, "-c", acker], cwd=repo, env=env,
        text=True, capture_output=True, timeout=15, check=True,
    )
    probe = subprocess.run(
        [sys.executable, "-c", "from tools.process_registry import process_registry; print(process_registry.completion_queue.qsize())"],
        cwd=repo, env=env, text=True, capture_output=True, timeout=15, check=True,
    )
    assert probe.stdout.strip().splitlines()[-1] == "0"


# ---------------------------------------------------------------------------
# Integration: delegate_task(background=True) routing
# ---------------------------------------------------------------------------

def test_delegate_task_background_routes_async_and_does_not_block(monkeypatch):
    """delegate_task(background=True) returns a handle without running the
    child synchronously, and the child completes on the background thread.
    A single task is dispatched as a one-item background batch unit."""
    from unittest.mock import MagicMock, patch
    import tools.delegate_tool as dt

    parent = MagicMock()
    parent._delegate_depth = 0
    parent.session_id = "sess"
    parent._interrupt_requested = False
    parent._active_children = []
    parent._active_children_lock = None
    fake_child = MagicMock()
    fake_child._delegate_role = "leaf"
    fake_child._subagent_id = "s1"

    gate = threading.Event()

    def slow_child(task_index, goal, child=None, parent_agent=None, **kw):
        gate.wait(timeout=60)  # a sync impl would hang delegate_task here
        return {
            "task_index": 0, "status": "completed", "summary": f"done: {goal}",
            "api_calls": 1, "duration_seconds": 0.1, "model": "m",
            "exit_reason": "completed",
        }

    creds = {
        "model": "m", "provider": None, "base_url": None, "api_key": None,
        "api_mode": None, "command": None, "args": None,
    }
    # monkeypatch (not `with`) so patches outlive delegate_task's return and
    # remain active while the background worker runs.
    monkeypatch.setattr(dt, "_build_child_agent", lambda **kw: fake_child)
    monkeypatch.setattr(dt, "_run_single_child", slow_child)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: creds)
    out = dt.delegate_task(
        goal="the real task", context="ctx",
        background=True, parent_agent=parent,
    )

    import json
    parsed = json.loads(out)
    assert parsed["status"] == "dispatched"
    assert parsed["mode"] == "background"
    assert parsed["delegation_id"].startswith("deleg_")
    # Non-blocking invariant: delegate_task returned while the child is STILL
    # blocked on the closed gate, so no completion event exists yet.
    assert process_registry.completion_queue.empty()
    assert ad.active_count() == 1  # one background batch unit, not finished

    gate.set()
    evt = _drain_one()
    assert evt is not None
    assert evt["type"] == "async_delegation"
    # Single task rides the batch path → carries a 1-item results list.
    assert evt.get("is_batch") is True
    assert len(evt["results"]) == 1
    assert evt["results"][0]["summary"] == "done: the real task"
    text = format_process_notification(evt)
    assert text is not None
    assert "the real task" in text


def test_delegate_task_background_uses_live_tui_agent_session_id(monkeypatch):
    """TUI async delegation must route to the live/compressed agent id.

    Regression: delegate_task captured the stale approval/session context key
    after compression rotated parent_agent.session_id. The resulting completion
    was orphaned and could be consumed by an unrelated desktop session poller.
    """
    import json
    from unittest.mock import MagicMock
    import tools.delegate_tool as dt
    from gateway.session_context import clear_session_vars, set_session_vars
    from tools.approval_context import reset_current_session_key, set_current_session_key

    parent = MagicMock()
    parent._delegate_depth = 0
    parent.session_id = "post-compress-tip"
    parent._interrupt_requested = False
    parent._active_children = []
    parent._active_children_lock = None
    fake_child = MagicMock()
    fake_child._delegate_role = "leaf"

    creds = {
        "model": "m", "provider": None, "base_url": None, "api_key": None,
        "api_mode": None, "command": None, "args": None,
    }
    monkeypatch.setattr(dt, "_build_child_agent", lambda **kw: fake_child)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: creds)
    monkeypatch.setattr(
        dt,
        "_run_single_child",
        lambda *a, **k: {
            "task_index": 0,
            "status": "completed",
            "summary": "done",
            "api_calls": 1,
            "duration_seconds": 0.1,
            "model": "m",
            "exit_reason": "completed",
        },
    )

    approval_token = set_current_session_key("pre-compress-parent")
    session_tokens = set_session_vars(
        source="tui",
        session_key="pre-compress-parent",
        ui_session_id="origin-tab",
    )
    try:
        out = dt.delegate_task(goal="bg task", background=True, parent_agent=parent)
        assert json.loads(out)["status"] == "dispatched"
        evt = _drain_one()
    finally:
        reset_current_session_key(approval_token)
        clear_session_vars(session_tokens)

    assert evt is not None
    assert evt["type"] == "async_delegation"
    assert evt["session_key"] == "post-compress-tip"
    assert evt["origin_ui_session_id"] == "origin-tab"


def test_delegate_task_background_batch_runs_as_one_unit(monkeypatch):
    """A multi-item batch with background=True dispatches the WHOLE fan-out as
    ONE background unit (one handle, one async slot). The children run in
    parallel and join; the consolidated results come back as a single
    completion event when ALL of them finish."""
    import json
    from unittest.mock import MagicMock, patch
    import tools.delegate_tool as dt

    parent = MagicMock()
    parent._delegate_depth = 0
    parent.session_id = "sess"
    parent._interrupt_requested = False
    parent._active_children = []
    parent._active_children_lock = None

    fake_child = MagicMock()
    fake_child._delegate_role = "leaf"

    gate = threading.Event()

    def _blocking_child(task_index, goal, child=None, parent_agent=None, **kw):
        gate.wait(timeout=60)
        return {
            "task_index": task_index, "status": "completed",
            "summary": f"done: {goal}", "api_calls": 1,
            "duration_seconds": 0.1, "model": "m", "exit_reason": "completed",
        }

    creds = {
        "model": "m", "provider": None, "base_url": None, "api_key": None,
        "api_mode": None, "command": None, "args": None,
    }

    # Use monkeypatch (not a `with` block) so the patches stay active while the
    # background worker thread runs _execute_and_aggregate AFTER delegate_task
    # has already returned.
    monkeypatch.setattr(dt, "_build_child_agent", lambda **kw: fake_child)
    monkeypatch.setattr(dt, "_run_single_child", _blocking_child)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: creds)
    out = dt.delegate_task(
        tasks=[{"goal": "a"}, {"goal": "b"}, {"goal": "c"}],
        background=True,
        parent_agent=parent,
    )

    parsed = json.loads(out)
    assert parsed["status"] == "dispatched"
    assert parsed["mode"] == "background"
    assert parsed["count"] == 3
    assert parsed["delegation_id"].startswith("deleg_")
    assert parsed["goals"] == ["a", "b", "c"]
    # ONE background unit for the whole fan-out (not three), and the call
    # returned while all children are still blocked → chat not blocked.
    assert process_registry.completion_queue.empty()
    assert ad.active_count() == 1

    # Release the children; the whole batch joins and emits ONE event.
    gate.set()
    evt = _drain_one()
    assert evt is not None
    assert evt["type"] == "async_delegation"
    assert evt.get("is_batch") is True
    assert len(evt["results"]) == 3
    summaries = sorted(r["summary"] for r in evt["results"])
    assert summaries == ["done: a", "done: b", "done: c"]
    # The consolidated notification names all three tasks in one block.
    text = format_process_notification(evt)
    assert text is not None
    assert "TASK 1/3" in text and "TASK 2/3" in text and "TASK 3/3" in text
    assert "done: a" in text and "done: b" in text and "done: c" in text
    # No more events — it's a single combined completion, not N of them.
    assert _drain_one() is None


def test_delegate_task_background_passes_progress_fn_to_async_registry(monkeypatch):
    import json
    from unittest.mock import MagicMock
    import tools.delegate_tool as dt

    parent = MagicMock()
    parent._delegate_depth = 0
    parent.session_id = "sess"
    parent._interrupt_requested = False
    parent._active_children = []
    parent._active_children_lock = None

    fake_child = MagicMock()
    fake_child._delegate_role = "leaf"
    fake_child._subagent_id = "s1"
    fake_child.get_activity_summary.return_value = {
        "api_call_count": 4,
        "current_tool": "terminal",
        "last_activity_ts": 1234.5,
    }

    creds = {
        "model": "m", "provider": None, "base_url": None, "api_key": None,
        "api_mode": None, "command": None, "args": None,
    }
    captured = {}

    def fake_dispatch(**kwargs):
        captured.update(kwargs)
        return {"status": "dispatched", "delegation_id": "deleg_progress"}

    monkeypatch.setattr(dt, "_build_child_agent", lambda **kw: fake_child)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: creds)
    monkeypatch.setattr(ad, "dispatch_async_delegation_batch", fake_dispatch)

    out = dt.delegate_task(goal="background stall guard", background=True, parent_agent=parent)

    parsed = json.loads(out)
    assert parsed["status"] == "dispatched"
    assert parsed["delegation_id"] == "deleg_progress"
    # The dispatch wires a live progress sampler over the child agents so the
    # async registry's stale monitor can watch the detached batch. The token
    # includes last_activity_ts so streamed chunks count as liveness (each
    # chunk ticks _touch_activity), not just completed API calls.
    progress_fn = captured["progress_fn"]
    assert callable(progress_fn)
    token, in_tool = progress_fn()
    assert token == ((4, "terminal", 1234.5),)
    assert in_tool is True


def test_model_dispatch_forces_background():
    """The MODEL-facing dispatch path forces background=True for any top-level
    delegation (single task OR batch), and keeps it off for an orchestrator
    subagent (depth > 0). Direct delegate_task() callers are unaffected (they
    keep the synchronous default)."""
    import tools.delegate_tool as dt
    from unittest.mock import MagicMock

    top = MagicMock()
    top._delegate_depth = 0
    sub = MagicMock()
    sub._delegate_depth = 1

    # Registry-fallback helper: top-level always background, regardless of
    # single vs batch; subagent never.
    assert dt._model_background_value({"goal": "x"}, top) is True
    assert dt._model_background_value(
        {"tasks": [{"goal": "a"}, {"goal": "b"}]}, top
    ) is True
    assert dt._model_background_value({"tasks": [{"goal": "a"}]}, top) is True
    assert dt._model_background_value({"goal": "x"}, sub) is False
    assert dt._model_background_value(
        {"tasks": [{"goal": "a"}, {"goal": "b"}]}, sub
    ) is False


def test_run_agent_dispatch_forces_background():
    """run_agent._dispatch_delegate_task — the live model path — forces
    background on for any top-level delegation (single OR batch) and off for a
    subagent."""
    from unittest.mock import patch
    import run_agent

    class _FakeAgent:
        _delegate_depth = 0

    captured = {}

    def _fake_delegate(**kwargs):
        captured.update(kwargs)
        return "{}"

    with patch("tools.delegate_tool.delegate_task", _fake_delegate):
        agent = _FakeAgent()
        run_agent.AIAgent._dispatch_delegate_task(agent, {"goal": "x"})
        assert captured["background"] is True

        run_agent.AIAgent._dispatch_delegate_task(
            agent, {"tasks": [{"goal": "a"}, {"goal": "b"}]}
        )
        assert captured["background"] is True

        sub = _FakeAgent()
        sub._delegate_depth = 1
        run_agent.AIAgent._dispatch_delegate_task(sub, {"goal": "x"})
        assert captured["background"] is False


def test_dispatch_never_forwards_model_toolsets():
    """The model has no toolsets argument — subagents always inherit the
    parent's toolsets. Even if a model smuggles a `toolsets` key into the
    tool-call args, the live dispatch path must NOT forward it to
    delegate_task (which no longer accepts it) and must not crash."""
    from unittest.mock import patch
    import run_agent

    class _FakeAgent:
        _delegate_depth = 0

    captured = {}

    def _fake_delegate(**kwargs):
        captured.update(kwargs)
        return "{}"

    with patch("tools.delegate_tool.delegate_task", _fake_delegate):
        run_agent.AIAgent._dispatch_delegate_task(
            _FakeAgent(), {"goal": "x", "toolsets": ["web", "terminal"]}
        )
    assert "toolsets" not in captured


def test_dispatch_forwards_model_and_agent_type():
    """The live model dispatch path (run_agent._dispatch_delegate_task) MUST
    forward an explicit top-level `model` / `agent_type` through to
    delegate_task(). Regression test: these two schema fields were added to
    DELEGATE_TASK_SCHEMA and to the registry-fallback lambda, but the live
    dispatch call site (the one actually used for every top-level model tool
    call) never forwarded them — so a model-requested `model="claude-opus-4-7"`
    was silently dropped and fell through to delegation.model /
    delegation.by_provider.<provider>.model / the parent's model instead of
    honoring the caller's explicit choice."""
    from unittest.mock import patch
    import run_agent

    class _FakeAgent:
        _delegate_depth = 0

    captured = {}

    def _fake_delegate(**kwargs):
        captured.update(kwargs)
        return "{}"

    with patch("tools.delegate_tool.delegate_task", _fake_delegate):
        run_agent.AIAgent._dispatch_delegate_task(
            _FakeAgent(),
            {"goal": "x", "model": "claude-opus-4-7", "agent_type": "coder"},
        )

    assert captured.get("model") == "claude-opus-4-7"
    assert captured.get("agent_type") == "coder"


def test_delegate_task_background_detaches_child_from_parent(monkeypatch):
    """A background child must NOT remain in parent._active_children —
    otherwise parent-turn interrupts / cache evicts / session close would
    kill the detached subagent mid-run."""
    from unittest.mock import MagicMock, patch
    import tools.delegate_tool as dt

    parent = MagicMock()
    parent._delegate_depth = 0
    parent.session_id = "sess"
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    fake_child = MagicMock()
    fake_child._delegate_role = "leaf"
    fake_child._subagent_id = "s1"

    gate = threading.Event()
    entered = threading.Event()

    def slow_child(task_index, goal, child=None, parent_agent=None, **kw):
        entered.set()
        gate.wait(timeout=60)
        return {"task_index": 0, "status": "completed", "summary": "ok"}

    def build_and_register(**kw):
        # Mirror what the real _build_child_agent does: register the child
        # for interrupt propagation.
        parent._active_children.append(fake_child)
        return fake_child

    creds = {
        "model": "m", "provider": None, "base_url": None, "api_key": None,
        "api_mode": None, "command": None, "args": None,
    }
    with patch.object(dt, "_build_child_agent", side_effect=build_and_register), \
         patch.object(dt, "_run_single_child", side_effect=slow_child), \
         patch.object(dt, "_resolve_delegation_credentials", return_value=creds):
        out = dt.delegate_task(goal="bg task", background=True, parent_agent=parent)
        # Wait for the detached worker thread to actually reach the patched
        # _run_single_child WHILE the patches are still active. Without
        # this, the `with patch.object(...)` block exits (reverting
        # _run_single_child to the real function) before the daemon worker
        # thread gets scheduled — a genuine race, not specific to any one
        # code path, that widens whenever the worker's pre-call setup does
        # more work (e.g. SwarmBoard registration).
        assert entered.wait(timeout=10), "worker thread never reached _run_single_child"

    import json
    assert json.loads(out)["status"] == "dispatched"
    # Child detached immediately at dispatch, while it is still running.
    assert fake_child not in parent._active_children
    gate.set()

    assert _drain_one() is not None


def test_concurrent_dispatch_respects_capacity():
    """Two threads racing dispatch with cap=1 must yield exactly one accept
    (capacity check and record insert are atomic under the records lock)."""
    gate = threading.Event()

    def blocker():
        gate.wait(timeout=60)
        return {"status": "completed", "summary": "x"}

    results = []
    barrier = threading.Barrier(2)

    def racer():
        barrier.wait(timeout=5)
        results.append(
            ad.dispatch_async_delegation(
                goal="race", context=None, toolsets=None, role="leaf",
                model="m", session_key="", runner=blocker,
                max_async_children=1,
            )
        )

    threads = [threading.Thread(target=racer) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    statuses = sorted(r["status"] for r in results)
    assert statuses == ["dispatched", "rejected"]
    gate.set()


# ---------------------------------------------------------------------------
# Gateway routing: session_key -> platform/chat_id, rich formatting, injection
# ---------------------------------------------------------------------------

def _make_async_evt(**over):
    evt = {
        "type": "async_delegation",
        "delegation_id": "deleg_x1",
        "session_key": "agent:main:telegram:dm:12345:678",
        "goal": "Investigate flaky test",
        "context": "repo /tmp/p",
        "toolsets": ["terminal"],
        "role": "leaf",
        "model": "m",
        "status": "completed",
        "summary": "Found the bug in test_foo",
        "api_calls": 4,
        "duration_seconds": 12.0,
        "dispatched_at": 1000.0,
        "completed_at": 1012.0,
    }
    evt.update(over)
    return evt


def test_gateway_formatter_renders_async_block():
    from gateway.run import _format_gateway_process_notification

    txt = _format_gateway_process_notification(_make_async_evt())
    assert txt is not None
    assert "ASYNC DELEGATION COMPLETE" in txt
    assert "Found the bug in test_foo" in txt
    assert "Investigate flaky test" in txt


def test_gateway_cli_origin_event_left_unrouted():
    """An empty session_key (CLI origin) is left without routing fields."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    evt = _make_async_evt(session_key="")
    runner._enrich_async_delegation_routing(evt)
    assert "platform" not in evt


# ── active_task_count() — expands batches to real child-task count ────────
# Distinct from active_count() (units/slots: a batch is 1 regardless of its
# child count). This is the source of truth for the CLI/TUI ⛓ status-bar
# badge, which is supposed to show how many subagents are actually working
# right now, not how many async-pool slots are occupied.


class TestActiveForSession:
    """active_for_session() is a plugin-compat shim (COMPAT_MANIFEST.md,
    tools.async_delegation) with no internal callers -- this is its only
    coverage. It counts LIVE delegations (running/stalling/finalizing) owned
    by one UI session; completed ones and other sessions must not count.
    """

    @pytest.fixture(autouse=True)
    def _clean_records(self):
        ad._reset_for_tests()
        yield
        ad._reset_for_tests()

    def test_active_for_session_counts_every_live_delegation_state(self):
        with ad._records_lock:
            ad._records.update(
                {
                    "running": {
                        "status": "running",
                        "origin_ui_session_id": "desktop-sid",
                    },
                    "stalling": {
                        "status": "stalling",
                        "origin_ui_session_id": "desktop-sid",
                    },
                    "finalizing": {
                        "status": "finalizing",
                        "origin_ui_session_id": "desktop-sid",
                    },
                    "completed": {
                        "status": "completed",
                        "origin_ui_session_id": "desktop-sid",
                    },
                    "other-session": {
                        "status": "running",
                        "origin_ui_session_id": "other-sid",
                    },
                }
            )

        assert ad.active_for_session("desktop-sid") == 3
        assert ad.active_for_session("other-sid") == 1
        assert ad.active_for_session("") == 0


class TestActiveTaskCount:
    @pytest.fixture(autouse=True)
    def _clean_records(self):
        ad._reset_for_tests()
        yield
        ad._reset_for_tests()

    def test_zero_when_no_records(self):
        assert ad.active_task_count() == 0

    def test_single_non_batch_delegation_counts_as_one(self):
        with ad._records_lock:
            ad._records["single-1"] = {
                "delegation_id": "single-1",
                "status": "running",
                "is_batch": False,
            }
        assert ad.active_task_count() == 1

    def test_batch_expands_to_child_goal_count(self):
        with ad._records_lock:
            ad._records["batch-1"] = {
                "delegation_id": "batch-1",
                "status": "running",
                "is_batch": True,
                "goals": ["a", "b", "c"],
            }
        # active_count() (units/slots) would report 1 here — the whole point
        # of active_task_count() is to report the real child count instead.
        assert ad.active_count() == 1
        assert ad.active_task_count() == 3

    def test_batch_missing_goals_falls_back_to_one(self):
        with ad._records_lock:
            ad._records["batch-1"] = {
                "delegation_id": "batch-1",
                "status": "running",
                "is_batch": True,
                "goals": None,
            }
        assert ad.active_task_count() == 1

    def test_mixed_batch_and_single_records_sum_correctly(self):
        with ad._records_lock:
            ad._records["batch-1"] = {
                "delegation_id": "batch-1",
                "status": "running",
                "is_batch": True,
                "goals": ["a", "b"],
            }
            ad._records["single-1"] = {
                "delegation_id": "single-1",
                "status": "finalizing",
                "is_batch": False,
            }
        assert ad.active_task_count() == 3

    def test_stalling_batch_still_counted(self):
        """A stalling record (child gone quiet, in grace period before
        force-finalize) is still alive and must not disappear from the
        badge count."""
        with ad._records_lock:
            ad._records["batch-1"] = {
                "delegation_id": "batch-1",
                "status": "stalling",
                "is_batch": True,
                "goals": ["a", "b", "c", "d"],
            }
        assert ad.active_task_count() == 4

    def test_completed_and_failed_records_excluded(self):
        with ad._records_lock:
            ad._records["done-1"] = {
                "delegation_id": "done-1",
                "status": "completed",
                "is_batch": True,
                "goals": ["a", "b", "c"],
            }
            ad._records["failed-1"] = {
                "delegation_id": "failed-1",
                "status": "failed",
                "is_batch": False,
            }
        assert ad.active_task_count() == 0


# ── interrupt_by_id() — cancel ONE in-flight delegation by its public id ──
# Powers the CLI's targeted `/stop <delegation_id>`, the gateway's mirrored
# `/stop <delegation_id>`, and the model-facing `delegate_task(cancel=...)`
# self-correction path.


class TestInterruptById:
    @pytest.fixture(autouse=True)
    def _clean_records(self):
        ad._reset_for_tests()
        yield
        ad._reset_for_tests()

    def test_unknown_id_reports_not_found(self):
        result = ad.interrupt_by_id("deleg_never_existed")
        assert result == {"found": False, "already_done": False, "interrupted": False}

    def test_running_delegation_gets_interrupted(self):
        calls = []
        with ad._records_lock:
            ad._records["deleg_x"] = {
                "delegation_id": "deleg_x",
                "status": "running",
                "interrupt_fn": lambda: calls.append(1),
            }
        result = ad.interrupt_by_id("deleg_x")
        assert result == {"found": True, "already_done": False, "interrupted": True}
        assert calls == [1]

    def test_stalling_delegation_gets_interrupted(self):
        """A stalling record is still live (in its grace period) — must be
        cancellable, not treated as already finished."""
        calls = []
        with ad._records_lock:
            ad._records["deleg_y"] = {
                "delegation_id": "deleg_y",
                "status": "stalling",
                "interrupt_fn": lambda: calls.append(1),
            }
        result = ad.interrupt_by_id("deleg_y")
        assert result["interrupted"] is True
        assert calls == [1]

    def test_finalizing_delegation_gets_interrupted(self):
        calls = []
        with ad._records_lock:
            ad._records["deleg_z"] = {
                "delegation_id": "deleg_z",
                "status": "finalizing",
                "interrupt_fn": lambda: calls.append(1),
            }
        result = ad.interrupt_by_id("deleg_z")
        assert result["interrupted"] is True

    def test_completed_delegation_reports_already_done(self):
        with ad._records_lock:
            ad._records["deleg_done"] = {
                "delegation_id": "deleg_done",
                "status": "completed",
            }
        result = ad.interrupt_by_id("deleg_done")
        assert result == {"found": True, "already_done": True, "interrupted": False}

    def test_failed_delegation_reports_already_done(self):
        with ad._records_lock:
            ad._records["deleg_failed"] = {
                "delegation_id": "deleg_failed",
                "status": "failed",
            }
        result = ad.interrupt_by_id("deleg_failed")
        assert result["already_done"] is True

    def test_missing_interrupt_fn_degrades_honestly(self):
        """A record with no interrupt_fn wired must not falsely report success."""
        with ad._records_lock:
            ad._records["deleg_no_fn"] = {
                "delegation_id": "deleg_no_fn",
                "status": "running",
                "interrupt_fn": None,
            }
        result = ad.interrupt_by_id("deleg_no_fn")
        assert result["found"] is True
        assert result["interrupted"] is False

    def test_raising_interrupt_fn_does_not_propagate(self):
        def _boom():
            raise RuntimeError("child unreachable")

        with ad._records_lock:
            ad._records["deleg_boom"] = {
                "delegation_id": "deleg_boom",
                "status": "running",
                "interrupt_fn": _boom,
            }
        result = ad.interrupt_by_id("deleg_boom")
        assert result["found"] is True
        assert result["interrupted"] is False

    def test_batch_interrupt_fn_signals_all_children(self):
        """A batch's interrupt_fn (fans out to every child agent, mirroring
        delegate_tool.py's _batch_interrupt) is called exactly once — the
        fan-out itself is the batch's own responsibility, not this
        function's; interrupt_by_id only needs to trigger it."""
        signalled_children = []

        def _batch_interrupt():
            signalled_children.extend(["child-1", "child-2", "child-3"])

        with ad._records_lock:
            ad._records["deleg_batch"] = {
                "delegation_id": "deleg_batch",
                "status": "running",
                "is_batch": True,
                "goals": ["a", "b", "c"],
                "interrupt_fn": _batch_interrupt,
            }
        result = ad.interrupt_by_id("deleg_batch")
        assert result["interrupted"] is True
        assert signalled_children == ["child-1", "child-2", "child-3"]

    def test_only_targets_matching_id_leaves_others_untouched(self):
        """Cancelling one delegation must not touch a sibling's interrupt_fn —
        the whole point of targeted cancel vs interrupt_all()."""
        calls_a, calls_b = [], []
        with ad._records_lock:
            ad._records["deleg_a"] = {
                "delegation_id": "deleg_a",
                "status": "running",
                "interrupt_fn": lambda: calls_a.append(1),
            }
            ad._records["deleg_b"] = {
                "delegation_id": "deleg_b",
                "status": "running",
                "interrupt_fn": lambda: calls_b.append(1),
            }
        result = ad.interrupt_by_id("deleg_a")
        assert result["interrupted"] is True
        assert calls_a == [1]
        assert calls_b == []


# ── interrupt_by_id() session-ownership scoping ──────────────────────────
# _records is one process-global dict shared by every session a gateway
# process serves. Without a selector, any caller who obtains a
# delegation_id (printed in plaintext in status lines / dispatch
# confirmations / /agents output) could cancel a DIFFERENT session's
# background work. When any of session_key/origin_ui_session_id/
# parent_session_id is passed, the record must match via the same
# _matches_session_selectors() interrupt_for_session() already uses, or
# the call reports not-found -- identical to a genuinely nonexistent id,
# so this can't be used to probe whether another session's id exists.


class TestInterruptByIdSessionScoping:
    @pytest.fixture(autouse=True)
    def _clean_records(self):
        ad._reset_for_tests()
        yield
        ad._reset_for_tests()

    def test_matching_session_key_is_interrupted(self):
        calls = []
        with ad._records_lock:
            ad._records["deleg_owned"] = {
                "delegation_id": "deleg_owned",
                "status": "running",
                "session_key": "gw:owner-session",
                "interrupt_fn": lambda: calls.append(1),
            }
        result = ad.interrupt_by_id(
            "deleg_owned", session_key="gw:owner-session"
        )
        assert result == {"found": True, "already_done": False, "interrupted": True}
        assert calls == [1]

    def test_mismatched_session_key_reports_not_found(self):
        """A different session's delegation must be reported identically to
        a nonexistent id -- not distinguished as 'found but not yours',
        which would let a caller probe for other sessions' ids."""
        calls = []
        with ad._records_lock:
            ad._records["deleg_other"] = {
                "delegation_id": "deleg_other",
                "status": "running",
                "session_key": "gw:someone-elses-session",
                "interrupt_fn": lambda: calls.append(1),
            }
        result = ad.interrupt_by_id(
            "deleg_other", session_key="gw:attacker-session"
        )
        assert result == {"found": False, "already_done": False, "interrupted": False}
        assert calls == []

    def test_matching_parent_session_id_is_interrupted(self):
        """delegate_task(cancel=...) scopes by parent_session_id."""
        calls = []
        with ad._records_lock:
            ad._records["deleg_parent"] = {
                "delegation_id": "deleg_parent",
                "status": "running",
                "parent_session_id": "sess-abc123",
                "interrupt_fn": lambda: calls.append(1),
            }
        result = ad.interrupt_by_id(
            "deleg_parent", parent_session_id="sess-abc123"
        )
        assert result["interrupted"] is True
        assert calls == [1]

    def test_mismatched_parent_session_id_reports_not_found(self):
        calls = []
        with ad._records_lock:
            ad._records["deleg_parent2"] = {
                "delegation_id": "deleg_parent2",
                "status": "running",
                "parent_session_id": "sess-abc123",
                "interrupt_fn": lambda: calls.append(1),
            }
        result = ad.interrupt_by_id(
            "deleg_parent2", parent_session_id="sess-xyz999"
        )
        assert result["found"] is False
        assert calls == []

    def test_unscoped_call_ignores_ownership_matches_any_session(self):
        """The CLI's default (no selectors passed) preserves the pre-fix
        behavior -- a single-session process has nothing to scope against,
        since every id in _records was dispatched by the one session that
        process is running."""
        calls = []
        with ad._records_lock:
            ad._records["deleg_cli"] = {
                "delegation_id": "deleg_cli",
                "status": "running",
                "session_key": "some-key-the-caller-never-passes",
                "interrupt_fn": lambda: calls.append(1),
            }
        result = ad.interrupt_by_id("deleg_cli")
        assert result["interrupted"] is True
        assert calls == [1]




def test_single_task_truncation_banner_when_max_iterations():
    """A single async subagent that hit its iteration cap (exit_reason=
    max_iterations) must surface a TRUNCATED marker in the formatted result,
    even though status stays 'completed' (a summary exists)."""
    evt = _make_async_evt(
        status="completed",
        summary="Did part of the work then ran out of budget.",
        exit_reason="max_iterations",
    )
    text = format_process_notification(evt)
    assert text is not None
    assert "TRUNCATED" in text
    assert "max_iterations" in text
    # The summary is still shown, just flagged.
    assert "Did part of the work" in text


def test_single_task_no_banner_when_clean():
    """A cleanly-finished subagent must NOT get a truncation banner."""
    evt = _make_async_evt(status="completed", summary="All done.", exit_reason="completed")
    text = format_process_notification(evt)
    assert text is not None
    assert "TRUNCATED" not in text


def test_batch_truncation_banner_marks_only_truncated_task():
    """In a batch, only the task that hit max_iterations gets the TRUNCATED
    marker; a clean sibling keeps the normal check icon."""
    evt = _make_async_evt(
        is_batch=True,
        goals=["clean task", "truncated task"],
        results=[
            {
                "task_index": 0,
                "status": "completed",
                "summary": "finished cleanly",
                "api_calls": 5,
                "exit_reason": "completed",
                "truncated": False,
            },
            {
                "task_index": 1,
                "status": "completed",
                "summary": "cut off mid-work",
                "api_calls": 250,
                "exit_reason": "max_iterations",
                "truncated": True,
            },
        ],
    )
    text = format_process_notification(evt)
    assert text is not None
    assert "TRUNCATED" in text
    # The clean task's summary and the truncated one's both render...
    assert "finished cleanly" in text
    assert "cut off mid-work" in text
    # ...but the banner is tied to the truncated task, not the clean one.
    trunc_pos = text.index("cut off mid-work")
    clean_pos = text.index("finished cleanly")
    banner_pos = text.index("TRUNCATED")
    # The header banner for task 2 appears after task 1's summary.
    assert banner_pos > clean_pos


def _patch_delegation_cfg(monkeypatch, model="upstage/solar-pro-4", provider="openrouter"):
    """Pin the delegation config the notice renderer reads (adapts the
    #97667 tests to the shipped implementation, which reads the configured
    model from config rather than the event's model field)."""
    import tools.process_registry_notifications as _prn

    monkeypatch.setattr(
        _prn, "_delegation_config", lambda: {"model": model, "provider": provider}
    )


def test_batch_model_rejection_notice_prepended(monkeypatch):
    """A rejected delegation model must surface ONE config-level notice above
    the per-task blocks instead of staying buried in each summary (#97654)."""
    rejection = "HTTP 400: upstage/solar-pro-4 is not a valid model ID"
    _patch_delegation_cfg(monkeypatch)
    evt = _make_async_evt(
        is_batch=True,
        model="upstage/solar-pro-4",
        goals=["task a", "task b"],
        results=[
            {
                "task_index": 0,
                "status": "completed",
                "summary": rejection,
                "api_calls": 1,
                "duration_seconds": 0.74,
                "exit_reason": "max_iterations",
                "truncated": True,
            },
            {
                "task_index": 1,
                "status": "completed",
                "summary": rejection,
                "api_calls": 1,
                "duration_seconds": 0.71,
                "exit_reason": "max_iterations",
                "truncated": True,
            },
        ],
    )
    text = format_process_notification(evt)
    assert text is not None
    assert "SUBAGENT MODEL REJECTED" in text
    assert "upstage/solar-pro-4" in text
    assert "delegation.model" in text
    # The notice precedes the per-task blocks, not just trails them.
    assert text.index("SUBAGENT MODEL REJECTED") < text.index("TASK 1/2")


def test_batch_model_rejection_notice_absent_when_clean(monkeypatch):
    """Ordinary summaries must not grow a model-rejection notice."""
    _patch_delegation_cfg(monkeypatch, model="upstage/solar-pro4")
    evt = _make_async_evt(
        is_batch=True,
        model="upstage/solar-pro4",
        goals=["task a"],
        results=[
            {
                "task_index": 0,
                "status": "completed",
                "summary": "did the work",
                "api_calls": 3,
                "exit_reason": "completed",
                "truncated": False,
            },
        ],
    )
    text = format_process_notification(evt)
    assert text is not None
    assert "SUBAGENT MODEL REJECTED" not in text


def test_batch_model_rejection_notice_requires_configured_model_in_text(monkeypatch):
    """A model_not_found pattern naming a DIFFERENT model than the configured
    delegation model is task-level noise, not a config-level rejection."""
    _patch_delegation_cfg(monkeypatch, model="upstage/solar-pro4")
    evt = _make_async_evt(
        is_batch=True,
        model="upstage/solar-pro4",
        goals=["task a"],
        results=[
            {
                "task_index": 0,
                "status": "completed",
                "summary": "HTTP 400: other/model-x is not a valid model ID",
                "api_calls": 1,
                "exit_reason": "max_iterations",
                "truncated": True,
            },
        ],
    )
    text = format_process_notification(evt)
    assert text is not None
    assert "SUBAGENT MODEL REJECTED" not in text


# ---------------------------------------------------------------------------
# Per-group completion units: ungrouped tasks return alone, a `group` returns
# together, and the units of one call share ONE capacity slot.
# ---------------------------------------------------------------------------

def _grouped_fanout(monkeypatch, tasks, gates):
    """delegate_task(tasks) in the background with gated fake children; returns the parsed handle."""
    from unittest.mock import MagicMock
    import tools.delegate_tool as dt

    parent = MagicMock()
    parent._delegate_depth = 0
    parent.session_id = "sess"
    parent._interrupt_requested = False
    parent._active_children = []
    parent._active_children_lock = None

    def child(task_index, goal, child=None, parent_agent=None, **kw):
        gates[task_index].wait(timeout=60)
        return {"task_index": task_index, "status": "completed", "summary": f"done: {goal}", "api_calls": 1,
                "duration_seconds": 0.1, "model": "m", "exit_reason": "completed"}

    def build(**kw):
        c = MagicMock()
        c._delegate_role = "leaf"
        c._subagent_id = f"s{kw['task_index']}"
        return c

    creds = {"model": "m", "provider": None, "base_url": None, "api_key": None, "api_mode": None, "command": None,
             "args": None}
    monkeypatch.setattr(dt, "_build_child_agent", build)
    monkeypatch.setattr(dt, "_run_single_child", child)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: creds)
    return json.loads(dt.delegate_task(tasks=tasks, background=True, parent_agent=parent))


def test_ungrouped_task_completes_alone_and_group_completes_together(monkeypatch):
    """With delegation.independent_completions on, a finished ungrouped task must not wait for its siblings;
    tasks sharing a `group` must."""
    import tools.delegate_tool as dt
    monkeypatch.setattr(dt, "_load_config", lambda: {"independent_completions": True})
    gates = [threading.Event() for _ in range(4)]
    tasks = [
        {"goal": "review PR 1 thoroughly and report"},
        {"goal": "compare approach A in detail", "group": "cmp"},
        {"goal": "compare approach B in detail", "group": "cmp"},
        {"goal": "review PR 2 thoroughly and report"},
    ]
    handle = _grouped_fanout(monkeypatch, tasks, gates)
    assert handle["status"] == "dispatched"
    by_group = {tuple(u["task_indexes"]): u["group"] for u in handle["units"]}
    assert by_group == {(0,): None, (1, 2): "cmp", (3,): None}

    gates[3].set()
    evt = _drain_one()
    assert [r["task_index"] for r in evt["results"]] == [3]  # PR 2 landed while everything else still runs
    assert "review PR 2" in format_process_notification(evt)

    gates[1].set()
    assert _drain_one(timeout=0.5) is None  # half a group is not a completion
    gates[2].set()
    evt = _drain_one()
    assert evt["group"] == "cmp" and [r["task_index"] for r in evt["results"]] == [1, 2]

    gates[0].set()
    assert [r["task_index"] for r in _drain_one()["results"]] == [0]


def test_units_of_one_call_share_a_single_capacity_slot():
    """Splitting a call into per-task completions must not consume more pool capacity than the call did."""
    gate = threading.Event()

    def blocker():
        gate.wait(timeout=60)
        return {"results": [], "total_duration_seconds": 0}

    common = dict(goals=["a", "b"], context=None, toolsets=None, role="leaf", model="m", session_key="",
                  runner=blocker, max_async_children=1)
    first = ad.dispatch_async_delegation_batch(delegation_id="deleg_call-1", task_indexes=[0], **common)
    second = ad.dispatch_async_delegation_batch(delegation_id="deleg_call-2", task_indexes=[1],
                                                slot_key="deleg_call-1", **common)
    other = ad.dispatch_async_delegation_batch(delegation_id="deleg_other", **common)
    assert (first["status"], second["status"], other["status"]) == ("dispatched", "dispatched", "rejected")
    assert ad.active_task_count() == 2
    gate.set()


def test_multi_task_call_is_one_completion_unless_independent_completions(monkeypatch):
    """Default: a background fan-out returns as ONE message when every task is done, so an orchestrator
    is not woken N times per call; `group` is inert until delegation.independent_completions is on."""
    import tools.delegate_tool as dt
    monkeypatch.setattr(dt, "_load_config", lambda: {})
    gates = [threading.Event() for _ in range(3)]
    tasks = [{"goal": "review PR 1 thoroughly and report"}, {"goal": "review PR 2 thoroughly and report", "group": "g"},
             {"goal": "review PR 3 thoroughly and report", "group": "g"}]
    handle = _grouped_fanout(monkeypatch, tasks, gates)
    assert handle["status"] == "dispatched" and "units" not in handle
    gates[0].set()
    gates[1].set()
    assert _drain_one(timeout=0.5) is None  # two of three done: no message yet
    gates[2].set()
    evt = _drain_one()
    assert sorted(r["task_index"] for r in evt["results"]) == [0, 1, 2]


def test_units_beyond_slot_count_still_start_and_are_not_stalled_while_queued(monkeypatch):
    """Units of one call share a slot, so live units can exceed the slot cap; every unit must still get a worker,
    and a unit must not be judged stalled for time it spent waiting to start."""
    _fast_stale_monitor(monkeypatch, idle=0.3, grace=0.2)
    started, release = [], threading.Event()
    frozen = lambda: (((0, None, None),), False)  # noqa: E731 - child never progresses => token never changes

    def blocker(uid):
        def run():
            started.append(uid)
            release.wait(timeout=10)
            return {"results": [{"task_index": 0, "status": "completed"}], "total_duration_seconds": 0}
        return run

    common = dict(goals=["x"], context=None, toolsets=None, role="leaf", model="m", session_key="", max_async_children=1)
    ad.dispatch_async_delegation_batch(delegation_id="deleg_c-1", runner=blocker("c-1"), progress_fn=frozen, **common)
    ad.dispatch_async_delegation_batch(delegation_id="deleg_c-2", runner=blocker("c-2"), slot_key="deleg_c-1", **common)
    deadline = time.monotonic() + 2.0
    while len(started) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert sorted(started) == ["c-1", "c-2"]  # second unit started despite a 1-slot pool

    # A unit whose runner has NOT started yet must not accrue stall time: pin the pool so it stays queued.
    monkeypatch.setattr(ad, "_get_executor", lambda n: ad._executor)
    ad.dispatch_async_delegation_batch(delegation_id="deleg_q", runner=blocker("q"), progress_fn=frozen,
                                       **{**common, "max_async_children": 3})
    time.sleep(0.7)  # > idle + grace with the unit still queued
    with ad._records_lock:
        assert ad._records["deleg_q"]["status"] == "running"
    assert "q" not in started
    release.set()


def test_child_finished_before_crash_is_recovered_with_its_result(tmp_path):
    """Real-import E2E: a 2-task group unit whose owner dies mid-run replays the finished child's real result
    and marks only the unfinished sibling unknown — a crash costs the stragglers, never the finished work."""
    repo = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    env = {**os.environ, "HERMES_HOME": str(tmp_path), "PYTHONPATH": repo}
    producer = r'''
import os, sys, time
from unittest.mock import MagicMock
import tools.delegate_tool as dt
parent = MagicMock(); parent._delegate_depth = 0; parent.session_id = "sess"; parent._interrupt_requested = False
parent._active_children = []; parent._active_children_lock = None
def child(task_index, goal, child=None, parent_agent=None, **kw):
    if task_index == 1:
        time.sleep(600)
    return {"task_index": task_index, "status": "completed", "summary": f"done: {goal}", "api_calls": 1,
            "duration_seconds": 0.1, "model": "m", "exit_reason": "completed"}
def build(**kw):
    c = MagicMock(); c._delegate_role = "leaf"; c._subagent_id = f"s{kw['task_index']}"; return c
creds = {"model": "m", "provider": None, "base_url": None, "api_key": None, "api_mode": None, "command": None, "args": None}
dt._build_child_agent = build; dt._run_single_child = child; dt._resolve_delegation_credentials = lambda *a, **k: creds
dt.delegate_task(tasks=[{"goal": "fast member of the group task", "group": "g"},
                        {"goal": "slow member of the group task", "group": "g"}], background=True, parent_agent=parent)
time.sleep(2.0)
sys.stdout.flush(); os._exit(1)
'''
    subprocess.run([sys.executable, "-c", producer], cwd=repo, env=env, text=True, capture_output=True, timeout=30)
    consumer = r'''
import json, queue
from tools import async_delegation as ad
q = queue.Queue(); ad.restore_undelivered_completions(q)
print(json.dumps(q.get_nowait(), sort_keys=True))
'''
    second = subprocess.run([sys.executable, "-c", consumer], cwd=repo, env=env, text=True, capture_output=True,
                            timeout=15, check=True)
    evt = json.loads(second.stdout.strip().splitlines()[-1])
    by_index = {r["task_index"]: r for r in evt["results"]}
    assert by_index[0]["status"] == "completed" and by_index[0]["summary"] == "done: fast member of the group task"
    assert by_index[1]["status"] == "unknown"
    assert "1/2 child results were recorded" in evt["error"]
    assert "done: fast member" in format_process_notification(evt)


def test_one_child_unit_keeps_its_finished_child_when_the_owner_dies(tmp_path):
    """#116000: a detached unit with exactly ONE child had NO durable record of that child at all — only the
    multi-child join path called ``record_unit_child`` — so an owner death (OOM-kill / orphaning) anywhere in the
    window after the child returned (host-owned finalize, transcripts, manifest, then the durable completion write)
    replayed a bare "outcome unknown" and threw the finished work away. Real-import E2E: a one-task background
    ``delegate_task`` child completes, the owner is killed while blocked inside that window, and a fresh process
    must replay the child's real result to the parent."""
    repo = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    marker = tmp_path / "child-returned.flag"
    env = {**os.environ, "HERMES_HOME": str(tmp_path), "PYTHONPATH": repo, "REPRO_MARKER": str(marker)}
    producer = r'''
import os, sys, time
from unittest.mock import MagicMock
import tools.delegate_tool as dt
import tools.delegate_tool_dispatch as dtd
parent = MagicMock(); parent._delegate_depth = 0; parent.session_id = "sess"; parent._interrupt_requested = False
parent._active_children = []; parent._active_children_lock = None
def child(task_index, goal, child=None, parent_agent=None, **kw):
    return {"task_index": task_index, "status": "completed", "summary": f"done: {goal}", "api_calls": 1,
            "duration_seconds": 0.1, "model": "m", "exit_reason": "completed"}
def build(**kw):
    c = MagicMock(); c._delegate_role = "leaf"; c._subagent_id = f"s{kw['task_index']}"; return c
creds = {"model": "m", "provider": None, "base_url": None, "api_key": None, "api_mode": None, "command": None, "args": None}
dt._build_child_agent = build; dt._run_single_child = child; dt._resolve_delegation_credentials = lambda *a, **k: creds
def held_finalize(*a, **k):
    # The child's result exists; the owner still has host-owned finalize + transcripts + manifest + the durable
    # write to do. Block HERE so the driver kills the owner inside that window: deterministic, no race.
    open(os.environ["REPRO_MARKER"], "w").write("child-returned")
    time.sleep(600)
dtd._finalize_child_results = held_finalize
dt.delegate_task(tasks=[{"goal": "single background subagent"}], background=True, parent_agent=parent)
time.sleep(600)
'''
    proc = subprocess.Popen([sys.executable, "-u", "-c", producer], cwd=repo, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 60
        while not marker.exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists(), "the child never returned, so the death window was never reached"
    finally:
        proc.kill()
        proc.wait(timeout=20)
        time.sleep(0.3)  # let the OS reap the owner before recovery asks whether its pid is alive
    consumer = r'''
import json, queue
from tools import async_delegation as ad
q = queue.Queue(); ad.restore_undelivered_completions(q)
print(json.dumps(q.get_nowait(), sort_keys=True))
'''
    second = subprocess.run([sys.executable, "-u", "-c", consumer], cwd=repo, env=env, text=True,
                            capture_output=True, timeout=30, check=True)
    evt = json.loads(second.stdout.strip().splitlines()[-1])
    (entry,) = evt["results"]  # the finished child, not a fabricated "unknown"
    assert entry["status"] == "completed" and entry["summary"] == "done: single background subagent"
    assert "1/1 child results were recorded" in evt["error"]
    assert "done: single background subagent" in format_process_notification(evt)


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX mode bits not enforced on Windows")
def test_connect_creates_state_db_0o600_under_permissive_umask(tmp_path, monkeypatch):
    """``_connect`` shares state.db with hermes_state.SessionDB -- a fresh
    HERMES_HOME must land the file (and its WAL sidecar, if created) at 0o600
    even under a permissive process umask, not the SessionDB-only path."""
    import stat

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    old_umask = os.umask(0o022)
    try:
        conn = ad._connect()
        conn.close()
    finally:
        os.umask(old_umask)

    db_path = tmp_path / "state.db"
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600

    for suffix in ("-wal", "-shm"):
        sidecar = tmp_path / f"state.db{suffix}"
        if sidecar.exists():
            assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600


def test_persist_failure_still_delivers_result_and_frees_slot(monkeypatch):
    """A failing terminal durable write (locked/full state.db) must not eat the completion
    event or park the record on ``finalizing`` (#76605, #112030): the event is the only delivery
    path and ``finalizing`` counts against ``max_concurrent_children`` forever."""
    def boom(event, result):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(ad, "_persist_completion", boom)
    res = ad.dispatch_async_delegation(
        goal="g", context=None, toolsets=None, role="leaf", model="m", session_key="",
        runner=lambda: {"status": "completed", "summary": "done"}, max_async_children=1,
    )
    evt = _drain_for(res["delegation_id"])

    assert evt is not None and evt["status"] == "completed" and evt["summary"] == "done"
    deadline = time.monotonic() + 2.0
    while ad.active_count() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ad.active_count() == 0
    with ad._records_lock:
        assert ad._records[res["delegation_id"]]["status"] == "completed"


def test_prune_never_evicts_live_records():
    """Retention pruning drops TERMINAL records only; ``stalling``/``finalizing`` are live work
    (#76605, #112030). A stalling record has no ``completed_at`` so it sorts oldest and was the
    first eviction candidate, sending its late runner return into the missing-record path."""
    with ad._records_lock:
        for status, ts in (("stalling", 1.0), ("finalizing", 2.0), ("running", 3.0)):
            ad._records[f"live-{status}"] = {"delegation_id": f"live-{status}", "status": status,
                                             "dispatched_at": ts, "completed_at": None}
        for i in range(ad._MAX_RETAINED_COMPLETED + 1):
            ad._records[f"done-{i}"] = {"delegation_id": f"done-{i}", "status": "completed",
                                        "dispatched_at": 100.0 + i, "completed_at": 200.0 + i}
        ad._prune_completed_locked()
        survivors = set(ad._records)

    assert {"live-stalling", "live-finalizing", "live-running"} <= survivors
    assert "done-0" not in survivors and len(survivors - {"live-stalling", "live-finalizing", "live-running"}) == ad._MAX_RETAINED_COMPLETED
