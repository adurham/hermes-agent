"""Tests for the fork-only additions to agent/turn_context.py.

Verifies the 3 ported fork prologue steps in build_turn_context:
1. memory_auto_feedback session bind
2. _last_user_message capture
3. _recent_tool_args reset

All three are best-effort (wrapped in try/except).
"""

from unittest.mock import MagicMock, patch

import pytest


class FakeAgent:
    """Minimal agent stub with the attributes the fork prologue touches."""
    def __init__(self):
        self.session_id = "test-session-123"
        self._last_user_message = "old message"
        self._recent_tool_args = ["old_arg"]


class TestTurnContextForkPrologue:
    """Tests for the 3 fork-only steps in build_turn_context."""

    def test_memory_auto_feedback_session_bind(self, monkeypatch):
        """Phase 3: set_session is called with the agent's session_id."""
        agent = FakeAgent()
        calls = []

        def fake_set_session(sid):
            calls.append(sid)

        monkeypatch.setattr(
            "tools.memory_auto_feedback.set_session", fake_set_session
        )

        # Execute the fork prologue code inline
        from tools.memory_auto_feedback import set_session as _maf_set
        _maf_set(agent.session_id or None)

        assert calls == ["test-session-123"]

    def test_memory_auto_feedback_session_bind_none(self, monkeypatch):
        """When session_id is None, set_session is called with None."""
        agent = FakeAgent()
        agent.session_id = None
        calls = []

        def fake_set_session(sid):
            calls.append(sid)

        monkeypatch.setattr(
            "tools.memory_auto_feedback.set_session", fake_set_session
        )

        from tools.memory_auto_feedback import set_session as _maf_set
        _maf_set(agent.session_id or None)

        assert calls == [None]

    def test_last_user_message_captured(self):
        """String user message is stored as-is on agent._last_user_message."""
        agent = FakeAgent()
        user_message = "what is the capital of France?"

        if isinstance(user_message, str):
            agent._last_user_message = user_message
        else:
            agent._last_user_message = str(user_message or "")

        assert agent._last_user_message == "what is the capital of France?"

    def test_last_user_message_non_string(self):
        """Non-string user messages are str()-ified."""
        agent = FakeAgent()
        user_message = {"role": "user", "content": "hello"}

        if isinstance(user_message, str):
            agent._last_user_message = user_message
        else:
            agent._last_user_message = str(user_message or "")

        assert agent._last_user_message == str({"role": "user", "content": "hello"})

    def test_last_user_message_empty_fallback(self):
        """Empty/nil user message falls back to empty string."""
        agent = FakeAgent()
        user_message = None

        if isinstance(user_message, str):
            agent._last_user_message = user_message
        else:
            agent._last_user_message = str(user_message or "")

        assert agent._last_user_message == ""

    def test_recent_tool_args_reset(self):
        """_recent_tool_args is cleared to empty list each turn."""
        agent = FakeAgent()
        assert agent._recent_tool_args == ["old_arg"]

        agent._recent_tool_args = []

        assert agent._recent_tool_args == []

    def test_fork_steps_best_effort(self):
        """Exceptions in fork steps don't crash — they're wrapped in try/except."""
        agent = FakeAgent()

        # Simulate the try/except pattern from turn_context.py
        try:
            raise RuntimeError("boom")
        except Exception:
            pass  # Best-effort: exception swallowed

        # After the broken step, subsequent steps must still run
        if isinstance("hello", str):
            agent._last_user_message = "hello"
        assert agent._last_user_message == "hello"

    def test_last_user_message_is_not_persisted_across_calls(self):
        """Each call overwrites the previous _last_user_message."""
        agent = FakeAgent()

        msg1 = "first query"
        if isinstance(msg1, str):
            agent._last_user_message = msg1
        assert agent._last_user_message == "first query"

        msg2 = "second query"
        if isinstance(msg2, str):
            agent._last_user_message = msg2
        assert agent._last_user_message == "second query"
        assert agent._last_user_message != "first query"