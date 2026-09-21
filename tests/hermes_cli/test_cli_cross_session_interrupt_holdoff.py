"""Regression: cross-session inbox drain must NOT auto-trigger a new turn
while a user interrupt is in flight.

Same bug class as ``test_cli_completion_interrupt_holdoff.py`` but for
Transport B's idle-recipient drain path (``_drain_cross_session_inbox``):
each drained cross-session message is injected into ``_pending_input``,
which the CLI's process_loop picks up as a fresh turn on the next 0.1s
tick — racing the user's interrupt and immediately re-starting the very
session they were trying to stop.

Fix: while ``_last_turn_interrupted`` is set, skip the drain entirely.
The rows stay ``pending`` in ``cross_session_inbox`` (not lost, not
silently dropped) and are delivered on the next user-initiated turn,
which resets ``_last_turn_interrupted`` at turn start.
"""

import queue

from cli import HermesCLI


def test_cross_session_drain_skipped_while_interrupt_in_flight(monkeypatch):
    """With _last_turn_interrupted=True, the drain must be a no-op — no
    message gets injected into _pending_input, and drain_to_idle_injection
    is never called (rows remain pending for the next user turn)."""
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "visible-session"
    cli._pending_input = queue.Queue()
    cli._last_turn_interrupted = True

    calls = {"drain": 0}

    def _fake_drain(*, session_id, inject, on_held):
        calls["drain"] += 1
        inject("[CROSS-AGENT MESSAGE] should not be injected")

    monkeypatch.setattr(
        "tools.cross_session_integration.install_transport", lambda: None
    )
    monkeypatch.setattr(
        "tools.cross_session_integration.register_session_participant_for",
        lambda agent, cli_obj: None,
    )
    monkeypatch.setattr(
        "tools.cross_session_integration.maintenance_tick", lambda: None
    )
    monkeypatch.setattr(
        "tools.cross_session_integration.drain_to_idle_injection", _fake_drain
    )

    cli._drain_cross_session_inbox()

    assert cli._pending_input.empty(), (
        "cross-session message must not be injected while a user interrupt "
        "is in flight"
    )
    assert calls["drain"] == 0, "drain_to_idle_injection must be skipped entirely"


def test_cross_session_drain_resumes_on_next_user_turn(monkeypatch):
    """After the user starts a new turn (flag cleared), the drain runs
    normally and delivers the message."""
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "visible-session"
    cli._pending_input = queue.Queue()
    cli._last_turn_interrupted = True

    calls = {"drain": 0}

    def _fake_drain(*, session_id, inject, on_held):
        calls["drain"] += 1
        inject("[CROSS-AGENT MESSAGE] hello")

    monkeypatch.setattr(
        "tools.cross_session_integration.install_transport", lambda: None
    )
    monkeypatch.setattr(
        "tools.cross_session_integration.register_session_participant_for",
        lambda agent, cli_obj: None,
    )
    monkeypatch.setattr(
        "tools.cross_session_integration.maintenance_tick", lambda: None
    )
    monkeypatch.setattr(
        "tools.cross_session_integration.drain_to_idle_injection", _fake_drain
    )

    # Interrupt in flight → skipped.
    cli._drain_cross_session_inbox()
    assert cli._pending_input.empty()
    assert calls["drain"] == 0

    # Simulate chat_once beginning the next user-initiated turn.
    cli._last_turn_interrupted = False

    cli._drain_cross_session_inbox()
    assert calls["drain"] == 1
    assert cli._pending_input.get_nowait() == "[CROSS-AGENT MESSAGE] hello"


def test_cross_session_drain_default_behavior_unchanged(monkeypatch):
    """Non-interrupt case: drain works as before."""
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "visible-session"
    cli._pending_input = queue.Queue()
    cli._last_turn_interrupted = False

    calls = {"drain": 0}

    def _fake_drain(*, session_id, inject, on_held):
        calls["drain"] += 1
        inject("[CROSS-AGENT MESSAGE] hi")

    monkeypatch.setattr(
        "tools.cross_session_integration.install_transport", lambda: None
    )
    monkeypatch.setattr(
        "tools.cross_session_integration.register_session_participant_for",
        lambda agent, cli_obj: None,
    )
    monkeypatch.setattr(
        "tools.cross_session_integration.maintenance_tick", lambda: None
    )
    monkeypatch.setattr(
        "tools.cross_session_integration.drain_to_idle_injection", _fake_drain
    )

    cli._drain_cross_session_inbox()
    assert calls["drain"] == 1
    assert cli._pending_input.get_nowait() == "[CROSS-AGENT MESSAGE] hi"


def test_cross_session_drain_noop_without_session_id(monkeypatch):
    """No session_id -> early return, regardless of interrupt flag."""
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = ""
    cli._pending_input = queue.Queue()
    cli._last_turn_interrupted = False

    calls = {"drain": 0}

    def _fake_drain(*, session_id, inject, on_held):
        calls["drain"] += 1

    monkeypatch.setattr(
        "tools.cross_session_integration.install_transport", lambda: None
    )
    monkeypatch.setattr(
        "tools.cross_session_integration.register_session_participant_for",
        lambda agent, cli_obj: None,
    )
    monkeypatch.setattr(
        "tools.cross_session_integration.maintenance_tick", lambda: None
    )
    monkeypatch.setattr(
        "tools.cross_session_integration.drain_to_idle_injection", _fake_drain
    )

    cli._drain_cross_session_inbox()
    assert calls["drain"] == 0
