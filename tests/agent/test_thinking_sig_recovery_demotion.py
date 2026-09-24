"""Fork regression: the thinking-signature retry recovery must not be forced to the UI.

FORK.md ("thinking-signature retry recovery no longer forces a user-facing ⚠️
warning", 2026-07-24): the recovery is a one-shot, self-healing strip of the
dead reasoning fields from the wire-payload copy (``api_messages``) and is not
actionable by the user, so it must NOT be printed via a forced ``_vprint`` and
must log at DEBUG, not WARNING. The strip/retry mechanics themselves are
unchanged: only ``api_messages`` is touched, never the canonical ``messages``.

The demotion was dropped when the v2026.9.14 merge took upstream's refactor of
this region; this suite pins it back down.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

from agent.turn_recovery import _recover_format_errors
from agent.turn_retry_state import TurnRetryState


class _Agent:
    """Minimal stand-in: only the fields the thinking-signature branch reads."""

    log_prefix = "[test] "
    api_mode = "chat_completions"

    def __init__(self):
        self.printed = []

    def _vprint(self, *args, **kwargs):
        self.printed.append(" ".join(str(a) for a in args))


def _classified():
    from agent.error_classifier import FailoverReason
    return SimpleNamespace(reason=FailoverReason.thinking_signature)


def _run(agent, api_messages, messages):
    retry = TurnRetryState()
    recovered = _recover_format_errors(
        agent,
        api_error=RuntimeError("400 thinking block signature invalid"),
        classified=_classified(),
        _retry=retry,
        messages=messages,
        api_messages=api_messages,
    )
    return recovered, retry


def test_thinking_signature_recovery_strips_api_messages_only():
    """Mechanics untouched: the strip targets api_messages, never canonical messages."""
    agent = _Agent()
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hey", "reasoning_details": [{"type": "thinking"}]},
    ]
    api_messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hey", "reasoning_details": [{"type": "thinking"}]},
    ]

    recovered, retry = _run(agent, api_messages, messages)

    assert recovered is True
    assert retry.thinking_sig_retry_attempted is True
    assert "reasoning_details" not in api_messages[1]
    # Canonical (state.db) list must be untouched.
    assert messages[1]["reasoning_details"] == [{"type": "thinking"}]


def test_thinking_signature_recovery_does_not_force_a_user_facing_warning():
    """The user-visible ⚠️ line must not be forced to the UI (FORK demotion)."""
    agent = _Agent()
    api_messages = [{"role": "assistant", "content": "x", "reasoning_details": [{"type": "thinking"}]}]
    messages = [dict(m) for m in api_messages]

    recovered, _ = _run(agent, api_messages, messages)

    assert recovered is True
    assert agent.printed == [], f"recovery must stay silent on the UI, got: {agent.printed}"


def test_thinking_signature_recovery_logs_at_debug_not_warning(caplog):
    """Traceability is preserved at DEBUG; no WARNING is emitted for the self-heal."""
    agent = _Agent()
    api_messages = [{"role": "assistant", "content": "x", "reasoning_details": [{"type": "thinking"}]}]
    messages = [dict(m) for m in api_messages]

    with caplog.at_level(logging.DEBUG, logger="agent.conversation_loop"):
        recovered, _ = _run(agent, api_messages, messages)

    assert recovered is True
    assert "Thinking block signature recovery" in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], (
        "the self-healing thinking-signature recovery must not log at WARNING+"
    )
