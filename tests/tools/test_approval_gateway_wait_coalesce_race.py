"""Coalesced gateway approvals under concurrent leader/follower threads.

Identical concurrent approvals share ONE prompt: the first caller (leader) enqueues and notifies, later
callers (followers) wait on the leader's event and adopt its outcome. These tests force the interleavings
where the leader's prompt goes away without an answer and where two identical requests race to lead.
"""

import threading

from tools import approval as mod
from tools import approval_gateway_wait as wait_mod


SESSION_KEY = "approval-coalesce-race"
APPROVAL = {"command": "rm -rf build", "description": "d", "pattern_key": "dangerous", "pattern_keys": ["dangerous"]}
# Long enough that a follower left waiting on a dead leader is unmistakable; the tests release it on failure.
APPROVAL_TIMEOUT = 60


def _clear():
    mod._gateway_queues.clear()
    mod._gateway_notify_cbs.clear()


def _leader_never_polls(follower_waiting):
    raise AssertionError("the leader's notify failed; it must not wait")


def _run_leader_then_follower(monkeypatch, notify_cb, *, leader_poll=_leader_never_polls):
    """Start a leader, then a follower that coalesces onto it; *leader_poll* decides how the leader's own
    wait ends once the follower is blocked on the leader's event. Returns (results, follower, release) —
    ``release()`` frees a follower still blocked on the leader's (already dequeued) entry."""
    monkeypatch.setattr(wait_mod._ctx, "_fire_approval_hook", lambda name, **kw: None)
    monkeypatch.setattr(wait_mod._ctx, "_get_approval_timeout", lambda: APPROVAL_TIMEOUT)
    real_poll = wait_mod._poll_event
    follower_waiting = threading.Event()
    leader_enqueued = threading.Event()
    results: dict = {}

    def poll(event, session_key, *, interrupt_log):
        if "Coalesced" in interrupt_log:
            follower_waiting.set()
            return real_poll(event, session_key, interrupt_log=interrupt_log)
        return leader_poll(follower_waiting)

    monkeypatch.setattr(wait_mod, "_poll_event", poll)

    def leader_notify(data):
        results["leader_entry"] = mod._gateway_queues[SESSION_KEY][0]
        leader_enqueued.set()
        notify_cb(data, follower_waiting)

    def run(name, cb):
        results[name] = wait_mod._await_gateway_decision(SESSION_KEY, cb, dict(APPROVAL))

    leader = threading.Thread(target=run, args=("leader", leader_notify), daemon=True)
    leader.start()
    assert leader_enqueued.wait(10)
    follower = threading.Thread(target=run, args=("follower", lambda data: None), daemon=True)
    follower.start()
    leader.join(10)
    assert not leader.is_alive()

    def release():
        results["leader_entry"].event.set()
        follower.join(10)

    return results, follower, release


def test_follower_adopts_the_leaders_timeout_instead_of_hanging(monkeypatch):
    _clear()

    def leader_times_out(follower_waiting):
        assert follower_waiting.wait(10)
        return "timeout"

    results, follower, release = _run_leader_then_follower(
        monkeypatch, lambda data, follower_waiting: None, leader_poll=leader_times_out)
    follower.join(10)
    alive = follower.is_alive()
    release()
    assert not alive, "follower still blocked after the leader's prompt expired"
    assert results["leader"] == {"resolved": False, "choice": None, "reason": None}
    assert results["follower"] == {"resolved": False, "choice": None, "reason": None, "coalesced": True}
    # No prompt is left pending for a late /approve to be told "ok" about.
    assert mod.resolve_gateway_approval(SESSION_KEY, "once") == 0


def test_follower_wakes_when_the_leaders_notify_fails(monkeypatch):
    _clear()

    def notify_fails_after_follower_joined(data, follower_waiting):
        assert follower_waiting.wait(10)
        raise RuntimeError("platform send failed")

    results, follower, release = _run_leader_then_follower(monkeypatch, notify_fails_after_follower_joined)
    follower.join(10)
    alive = follower.is_alive()
    release()
    assert not alive, "follower still blocked after the leader's prompt could not be delivered"
    assert results["leader"] == {"resolved": False, "choice": None, "notify_failed": True}
    decision = results["follower"]
    assert decision["resolved"] is False and decision["choice"] is None and decision["coalesced"] is True
    # Never delivered, so it is a withdrawal — not a user deny and not a silent timeout.
    assert decision["cancelled"]
