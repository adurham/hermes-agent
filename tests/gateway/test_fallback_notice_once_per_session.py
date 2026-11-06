"""The pre-agent provider-switch notice is once per SESSION, not once per message.

Owner rule (2026-11-04): a fallback announcement is durable session state — "this session is running on
the fallback provider" — so repeating it on every reply is noise. The gate must suppress repeats inside a
conversation and re-announce after a session boundary clears ``ConversationState``.
"""
from types import SimpleNamespace

from gateway.run_turn_runner import _fallback_notice_once_per_session
from gateway.session_state import SessionState


class _Runner:
    """Minimal stand-in for GatewayRunner._peek_session_state (no state creation)."""

    def __init__(self):
        self._sessions = {}

    def _peek_session_state(self, key):
        return self._sessions.get(key)


def test_notice_shown_once_per_session_then_suppressed():
    runner = _Runner()
    runner._sessions["discord:dm:1"] = SessionState()

    assert _fallback_notice_once_per_session(runner, "discord:dm:1", "⚠️ N1") == "⚠️ N1"
    assert _fallback_notice_once_per_session(runner, "discord:dm:1", "⚠️ N2") is None
    assert _fallback_notice_once_per_session(runner, "discord:dm:1", "⚠️ N3") is None


def test_session_boundary_reannounces():
    runner = _Runner()
    runner._sessions["k"] = SessionState()

    assert _fallback_notice_once_per_session(runner, "k", "N1") == "N1"
    runner._sessions["k"].conversation.clear()
    assert _fallback_notice_once_per_session(runner, "k", "N2") == "N2"


def test_sessions_are_independent():
    runner = _Runner()
    runner._sessions["a"] = SessionState()
    runner._sessions["b"] = SessionState()

    assert _fallback_notice_once_per_session(runner, "a", "N") == "N"
    assert _fallback_notice_once_per_session(runner, "b", "N") == "N"


def test_without_session_state_keeps_legacy_behaviour():
    runner = _Runner()

    assert _fallback_notice_once_per_session(runner, "missing", "N") == "N"
    assert _fallback_notice_once_per_session(SimpleNamespace(), "k", "N") == "N"
    assert _fallback_notice_once_per_session(runner, None, "N") == "N"


def test_empty_notice_passthrough():
    runner = _Runner()

    assert _fallback_notice_once_per_session(runner, "k", None) is None
