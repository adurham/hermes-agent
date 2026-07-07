"""Tests for the consult-nudge reminder (agent.fork.consult_nudge).

Mirrors ``tests/test_skill_recall_reminder.py`` / the memory-recall reminder
test's shape: a minimal ``FakeAgent`` stub carrying only the attributes the
module functions touch, exercised directly (no full AIAgent construction).
"""

from __future__ import annotations

import pytest

from agent.fork import consult_nudge


class FakeAgent:
    """Stand-in for the AIAgent attributes consult_nudge touches."""

    def __init__(
        self,
        *,
        interval: int = 8,
        tools=("consult", "terminal", "write_file"),
    ):
        self._consult_nudge_interval = interval
        self._risky_ops_since_consult = 0
        self.valid_tool_names = tools


class TestMaybeConsultNudge:
    def test_interval_zero_disables(self):
        a = FakeAgent(interval=0)
        for _ in range(50):
            assert consult_nudge.maybe_consult_nudge(a, "terminal") is None

    def test_no_nudge_when_consult_tool_unavailable(self):
        a = FakeAgent(interval=1, tools=("terminal", "write_file"))
        for _ in range(10):
            assert consult_nudge.maybe_consult_nudge(a, "terminal") is None

    def test_no_nudge_on_non_risky_tool(self):
        a = FakeAgent(interval=1)
        for _ in range(20):
            assert consult_nudge.maybe_consult_nudge(a, "web_search") is None
        assert a._risky_ops_since_consult == 0

    def test_nudge_fires_at_interval(self):
        a = FakeAgent(interval=3)
        assert consult_nudge.maybe_consult_nudge(a, "terminal") is None
        assert consult_nudge.maybe_consult_nudge(a, "write_file") is None
        hint = consult_nudge.maybe_consult_nudge(a, "patch")
        assert hint is not None
        assert "consult reminder" in hint
        assert "consult(question=" in hint

    def test_nudge_resets_counter(self):
        a = FakeAgent(interval=2)
        consult_nudge.maybe_consult_nudge(a, "terminal")
        consult_nudge.maybe_consult_nudge(a, "terminal")  # fires, resets
        assert a._risky_ops_since_consult == 0
        assert consult_nudge.maybe_consult_nudge(a, "terminal") is None

    def test_nudge_is_a_soft_suggestion_not_a_requirement(self):
        a = FakeAgent(interval=1)
        hint = consult_nudge.maybe_consult_nudge(a, "terminal")
        assert hint is not None
        assert "nudge, not a requirement" in hint


class TestRecordVoluntaryConsult:
    def test_resets_counter(self):
        a = FakeAgent(interval=5)
        a._risky_ops_since_consult = 3
        consult_nudge.record_voluntary_consult(a)
        assert a._risky_ops_since_consult == 0

    def test_never_raises_on_missing_attribute(self):
        class Bare:
            pass

        # Should not raise even though Bare has no _risky_ops_since_consult.
        consult_nudge.record_voluntary_consult(Bare())


class TestInitState:
    def test_sets_defaults(self):
        a = FakeAgent.__new__(FakeAgent)
        consult_nudge.init_state(a)
        assert a._consult_nudge_interval == 8
        assert a._risky_ops_since_consult == 0


class TestRiskyToolNamesShared:
    """consult_nudge reuses skill_recall's risky-tool set on purpose —
    both reminders care about the same class of consequential actions."""

    def test_shares_skill_recall_risky_set(self):
        from agent.fork.skill_recall import _RISKY_TOOL_NAMES as skill_set
        from agent.fork.consult_nudge import _RISKY_TOOL_NAMES as consult_set

        assert consult_set is skill_set
