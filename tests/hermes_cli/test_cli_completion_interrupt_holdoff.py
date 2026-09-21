"""Regression: background completion notifications must NOT auto-trigger a
new turn while a user interrupt is in flight.

Bug: a long-running session with many background processes finishing in a
short window saw every Ctrl+C get clobbered by the next queued completion.
Each ``[IMPORTANT: Background process ...]`` completion is injected into
``_pending_input`` by ``_drain_process_notifications``, which the CLI's
process_loop picks up as a fresh turn on the next 0.1s tick — racing the
user's interrupt and immediately re-starting the very session they were
trying to stop.

Fix: while ``_last_turn_interrupted`` is set (the flag chat_once sets when
the turn returned with ``interrupted=True``), skip the drain. The events
stay queued in ``completion_queue`` and get delivered on the NEXT
user-initiated turn (chat_once resets the flag at turn start, then the
post-turn drain runs normally).
"""

import queue

from cli import HermesCLI


def _make_event(pid: str = "proc-1") -> dict:
    return {
        "type": "completion",
        "session_id": pid,
        "process_id": pid,
    }


class _FakeRegistry:
    def __init__(self, events):
        self._events = list(events)
        self.calls = 0

    def drain_notifications(self, *, session_key="", owns_event=None):
        self.calls += 1
        out = [(evt, f"[IMPORTANT: {evt['process_id']} done]") for evt in self._events]
        self._events = []
        return out


def _install_delegation_stubs(monkeypatch):
    monkeypatch.setattr(
        "tools.async_delegation.claim_event_delivery",
        lambda evt, consumer: "claim",
    )
    monkeypatch.setattr(
        "tools.async_delegation.complete_event_delivery",
        lambda evt, token: None,
    )


def test_drain_skipped_while_interrupt_in_flight(monkeypatch):
    """With _last_turn_interrupted=True, drain must be a no-op — no event
    gets injected into _pending_input, and the registry is never drained
    (events remain queued for the next user turn)."""
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "visible-session"
    cli._pending_input = queue.Queue()
    cli._last_turn_interrupted = True

    registry = _FakeRegistry([_make_event("proc-A"), _make_event("proc-B")])
    monkeypatch.setattr("tools.process_registry.process_registry", registry)
    _install_delegation_stubs(monkeypatch)

    cli._drain_process_notifications("cli-post-turn")

    assert cli._pending_input.empty(), (
        "notification must not be injected while user interrupt is in flight"
    )
    assert registry.calls == 0, "registry drain must be skipped entirely"


def test_drain_resumes_on_next_user_turn(monkeypatch):
    """After the user starts a new turn (flag cleared), the SAME queued
    completions get delivered normally — nothing is lost."""
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "visible-session"
    cli._pending_input = queue.Queue()
    cli._last_turn_interrupted = True

    registry = _FakeRegistry([_make_event("proc-A")])
    monkeypatch.setattr("tools.process_registry.process_registry", registry)
    _install_delegation_stubs(monkeypatch)

    # Interrupt in flight → skipped.
    cli._drain_process_notifications("cli-idle")
    assert cli._pending_input.empty()

    # Simulate chat_once beginning the next user-initiated turn: the flag
    # resets at run_turn entry (cli.py line ~16702).
    cli._last_turn_interrupted = False

    cli._drain_process_notifications("cli-post-turn")
    delivered = []
    while not cli._pending_input.empty():
        delivered.append(cli._pending_input.get_nowait())
    assert delivered == ["[IMPORTANT: proc-A done]"], (
        "queued completion must be delivered on the next non-interrupted turn"
    )


def test_drain_default_behavior_unchanged(monkeypatch):
    """Non-interrupt case: drain works as before — one event, one message."""
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "visible-session"
    cli._pending_input = queue.Queue()
    cli._last_turn_interrupted = False

    registry = _FakeRegistry([_make_event("proc-X")])
    monkeypatch.setattr("tools.process_registry.process_registry", registry)
    _install_delegation_stubs(monkeypatch)

    cli._drain_process_notifications("cli-idle")
    assert cli._pending_input.get_nowait() == "[IMPORTANT: proc-X done]"
    assert registry.calls == 1
