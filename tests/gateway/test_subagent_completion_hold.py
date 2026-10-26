"""Subagent-owned completion hold: a still-WORKING child must keep its completions.

``should_hold_completion_event`` holds a completion whose owning subagent is live so it does not
bubble into the parent chat mid-task. Its max-hold bound exists for a WEDGED-but-alive child. It
used to be a wall-clock stopwatch from first hold, so every completion of a child that was still
actively working was released into the parent chat exactly ``_EVENT_MAX_HOLD_SECONDS`` after it was
first held (state.db audit: the parent-chat deliveries of live children cluster at 30-31 minutes
after the process started). The bound must measure the OWNER's inactivity instead.

These tests use a REAL background process spawned through ``ProcessRegistry.spawn_local`` with a
subagent ``owner_task_id`` (the completion event the registry itself produces), a REAL live-registry
record from ``delegate_tool._register_subagent`` whose ``agent`` exposes the production activity
clock, and drive all three consumers' shared gate plus the CLI drain and the gateway watcher.
"""

import asyncio
import time
from types import SimpleNamespace

import pytest

import tools.delegate_tool as dt
import tools.process_registry as pr


class _Child:
    """A subagent agent whose activity clock is the production ``get_activity_summary`` shape."""

    def __init__(self, last_activity_ts):
        self.last_activity_ts = last_activity_ts

    def get_activity_summary(self):
        from agent.session_activity import build_activity_snapshot
        return build_activity_snapshot(last_activity_at=self.last_activity_ts,
                                       last_activity_description="tool: terminal")


@pytest.fixture()
def live_child():
    added = []

    def _register(sid, *, last_activity_ts):
        child = _Child(last_activity_ts)
        dt._register_subagent({
            "subagent_id": sid, "parent_id": None, "depth": 0, "goal": "long task",
            "delegation_id": "deleg_hold", "model": "m", "started_at": time.time(),
            "status": "running", "tool_count": 3, "agent": child,
        })
        added.append(sid)
        return child

    yield _register
    with dt._active_subagents_lock:
        for sid in added:
            dt._active_subagents.pop(sid, None)


@pytest.fixture()
def registry(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", tmp_path / "processes.json")
    reg = pr.ProcessRegistry()
    monkeypatch.setattr(pr, "process_registry", reg)
    return reg


@pytest.fixture()
def clock(monkeypatch):
    """Monotonic clock for the hold bound, advanced explicitly (no real sleeping)."""
    state = SimpleNamespace(now=1_000_000.0)
    monkeypatch.setattr(pr, "_monotonic", lambda: state.now)
    return state


def _real_subagent_completion(registry, owner):
    """Spawn a real background process owned by ``owner`` (task_id collapsed to the container key,
    exactly as terminal_tool passes it for a subagent on the local backend) and return the completion
    event the registry enqueues when it exits."""
    session = registry.spawn_local("echo built", task_id="default", owner_task_id=owner,
                                   session_key="parent-key")
    session.notify_on_complete = True
    deadline = time.monotonic() + 15
    while not session.exited and time.monotonic() < deadline:
        time.sleep(0.02)
    assert session.exited, "background process never exited"
    deadline = time.monotonic() + 5
    while registry.completion_queue.empty() and time.monotonic() < deadline:
        time.sleep(0.02)
    evt = registry.completion_queue.get_nowait()
    assert evt["type"] == "completion"
    assert evt["owner_task_id"] == owner and evt["task_id"] == "default"
    return session, evt


def test_working_child_completion_held_past_max_hold(registry, live_child, clock):
    """THE BUG: a child still making progress has its completion released after the bound."""
    owner = "sa-0-holdwork"
    child = live_child(owner, last_activity_ts=time.time())
    _session, evt = _real_subagent_completion(registry, owner)

    assert pr.should_hold_completion_event(evt) is True  # first hold stamps the clock
    clock.now += pr._EVENT_MAX_HOLD_SECONDS + 60  # 31 min held ...
    child.last_activity_ts = time.time()  # ... while the child is still actively working
    assert pr.event_owner_still_running(evt) is True
    assert pr.should_hold_completion_event(evt) is True, (
        "a still-working subagent's completion was released into the parent chat")


def test_cli_drain_keeps_working_child_completion_queued(registry, live_child, clock):
    """End to end through drain_notifications (the CLI consumer, where the audit's leaks landed)."""
    owner = "sa-0-holddrain"
    child = live_child(owner, last_activity_ts=time.time())
    _session, evt = _real_subagent_completion(registry, owner)
    registry.completion_queue.put(evt)

    assert registry.drain_notifications(session_key="parent-key") == []
    clock.now += pr._EVENT_MAX_HOLD_SECONDS + 60
    child.last_activity_ts = time.time()
    assert registry.drain_notifications(session_key="parent-key") == []
    assert registry.completion_queue.qsize() == 1  # still held, not lost

    # Child finishes -> released to the parent once, with attribution (the orphan path).
    dt._unregister_subagent(owner)
    results = registry.drain_notifications(session_key="parent-key")
    assert len(results) == 1
    assert f"Started by subagent {owner}" in results[0][1]


def test_wedged_child_still_released_after_max_hold(registry, live_child, clock):
    """The bound keeps its teeth: an owner with no progress for the whole bound is released."""
    owner = "sa-0-holdwedge"
    live_child(owner, last_activity_ts=time.time() - pr._EVENT_MAX_HOLD_SECONDS - 60)
    _session, evt = _real_subagent_completion(registry, owner)

    assert pr.should_hold_completion_event(evt) is True
    clock.now += pr._EVENT_MAX_HOLD_SECONDS + 1
    assert pr.should_hold_completion_event(evt) is False


def test_gateway_watcher_holds_working_child_past_max_hold(registry, live_child, clock, monkeypatch):
    """Gateway consumer: _run_process_watcher persists held_at across polls; a working owner must
    still pin the completion after the bound instead of injecting it into the parent chat."""
    from unittest.mock import AsyncMock
    from tests.gateway.test_completion_delivery import AdmittingHandler, _gateway_watcher, _runner

    owner = "sa-0-holdgw"
    child = live_child(owner, last_activity_ts=time.time())
    session, _evt = _real_subagent_completion(registry, owner)

    polls = SimpleNamespace(n=0)

    class _Reg:
        def get(self, _sid):
            polls.n += 1
            if polls.n > 4:
                return None  # end the watcher deterministically
            # Each poll is 20 minutes later; the child keeps working throughout.
            clock.now += 20 * 60
            child.last_activity_ts = time.time()
            return session

        def is_completion_consumed(self, _sid):
            return False

    monkeypatch.setattr(pr, "process_registry", _Reg())

    async def _instant_sleep(*_a, **_kw):
        pass
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

    adapter = SimpleNamespace(handle_message=AdmittingHandler(), send=AsyncMock())
    runner = _runner(adapter)
    # Assert on the delivery ATTEMPT, not the adapter: this bare runner refuses the batched enqueue, so
    # an adapter-only assertion would pass even when the hold released (it did, before the fix).
    attempts = []

    async def _record_enqueue(text, evt):
        attempts.append(evt)
        return True
    monkeypatch.setattr(runner, "_enqueue_process_completion_notification", _record_enqueue)
    asyncio.run(runner._run_process_watcher(_gateway_watcher(session.id)))

    assert polls.n == 5  # 80 minutes of holding, well past the 30-minute bound
    assert attempts == [], "the watcher released a still-working subagent's completion to the parent"
    adapter.handle_message.assert_not_awaited()
