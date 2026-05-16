"""Tests for the skill-recall reminder hooks on AIAgent.

The hooks track skills loaded via ``skill_view`` and inject a one-line
nudge into the result of every Nth "risky" tool call (terminal, Bash,
file writes, etc.) asking the agent to call ``skill_pitfalls(name)``
before proceeding. The goal is to keep loaded-skill pitfalls operationally
accessible after the full skill content has scrolled out of attention.

We bypass full AIAgent construction (it has ~60 required params, talks to
provider plugins, loads memory, etc.) by exercising the helpers as
``__func__`` bound to a minimal stub instance. The helper methods are
self-contained — they only touch ``self._loaded_skills_this_session``,
``self._risky_ops_since_skill_recall``, and
``self._skill_recall_reminder_interval``.
"""

import json

import pytest

from run_agent import AIAgent


class FakeAgent:
    """Stand-in for ``self`` when calling AIAgent helper methods directly.

    Holds only the attributes the recall-reminder helpers touch.
    """

    def __init__(self, *, interval: int = 6):
        self._loaded_skills_this_session = set()
        self._risky_ops_since_skill_recall = 0
        self._skill_recall_reminder_interval = interval

    # Borrow the real helpers from AIAgent — they're self-contained.
    _record_loaded_skill = AIAgent._record_loaded_skill
    _maybe_skill_recall_hint = AIAgent._maybe_skill_recall_hint
    _RISKY_TOOL_NAMES = AIAgent._RISKY_TOOL_NAMES


class TestRecordLoadedSkill:
    def test_records_on_success(self):
        a = FakeAgent()
        result = json.dumps({"success": True, "name": "exo-cluster-operations"})
        a._record_loaded_skill("exo-cluster-operations", result)
        assert "exo-cluster-operations" in a._loaded_skills_this_session

    def test_resets_counter_on_record(self):
        a = FakeAgent()
        a._risky_ops_since_skill_recall = 4
        a._record_loaded_skill(
            "foo", json.dumps({"success": True, "name": "foo"})
        )
        # Counter resets so the agent does NOT get a reminder on the
        # very next tool — it just saw the full skill.
        assert a._risky_ops_since_skill_recall == 0

    def test_ignores_failed_skill_view(self):
        a = FakeAgent()
        a._record_loaded_skill(
            "bad", json.dumps({"success": False, "error": "not found"})
        )
        assert "bad" not in a._loaded_skills_this_session

    def test_resolves_to_canonical_name(self):
        """skill_view returns the canonical name in the payload; if the
        passed name differs (e.g. 'category:skill' vs 'skill'), use the
        canonical form so reminders refer to it correctly."""
        a = FakeAgent()
        a._record_loaded_skill(
            "mlops/foo", json.dumps({"success": True, "name": "foo"})
        )
        assert "foo" in a._loaded_skills_this_session

    def test_handles_non_json_gracefully(self):
        """A non-JSON tool result must not crash the recorder."""
        a = FakeAgent()
        a._record_loaded_skill("foo", "not json at all")
        # No crash, no record.
        assert "foo" not in a._loaded_skills_this_session

    def test_handles_empty_name(self):
        a = FakeAgent()
        a._record_loaded_skill("", json.dumps({"success": True}))
        assert len(a._loaded_skills_this_session) == 0


class TestMaybeSkillRecallHint:
    def test_no_hint_when_no_skill_loaded(self):
        a = FakeAgent()
        # Even running 100 risky tools, no hint until a skill is loaded.
        for _ in range(100):
            assert a._maybe_skill_recall_hint("terminal") is None

    def test_no_hint_on_non_risky_tool(self):
        a = FakeAgent()
        a._loaded_skills_this_session.add("exo-cluster-operations")
        # web_search is not in _RISKY_TOOL_NAMES; counter shouldn't tick
        # and hint shouldn't fire.
        for _ in range(20):
            assert a._maybe_skill_recall_hint("web_search") is None
        assert a._risky_ops_since_skill_recall == 0

    def test_hint_fires_at_interval(self):
        a = FakeAgent(interval=3)
        a._loaded_skills_this_session.add("exo-cluster-operations")

        # First two calls: no hint (counter = 1, 2)
        assert a._maybe_skill_recall_hint("terminal") is None
        assert a._maybe_skill_recall_hint("terminal") is None

        # Third call: hint fires
        hint = a._maybe_skill_recall_hint("terminal")
        assert hint is not None
        assert "skill-recall reminder" in hint
        assert "skill_pitfalls" in hint
        assert "exo-cluster-operations" in hint

    def test_hint_resets_counter(self):
        a = FakeAgent(interval=2)
        a._loaded_skills_this_session.add("foo")

        a._maybe_skill_recall_hint("terminal")  # counter = 1
        a._maybe_skill_recall_hint("terminal")  # counter = 2 -> fires + reset
        # Counter is back to 0; next call doesn't fire.
        assert a._maybe_skill_recall_hint("terminal") is None

    def test_interval_zero_disables(self):
        a = FakeAgent(interval=0)
        a._loaded_skills_this_session.add("foo")
        for _ in range(50):
            assert a._maybe_skill_recall_hint("terminal") is None

    def test_lists_all_loaded_skills_in_hint(self):
        a = FakeAgent(interval=1)
        a._loaded_skills_this_session.update(
            {"exo-cluster-operations", "claude-code", "python-debugpy"}
        )
        hint = a._maybe_skill_recall_hint("terminal")
        assert hint is not None
        # All three skill names should appear in the listing.
        for name in ("exo-cluster-operations", "claude-code", "python-debugpy"):
            assert name in hint


class TestRiskyToolNames:
    """Make sure the risky-tool list covers the operations that bit us in
    practice — terminal, Bash, file writes, ssh-via-process, etc."""

    def test_covers_destructive_tools(self):
        for name in (
            "terminal",
            "Bash",
            "write_file",
            "Write",
            "patch",
            "Edit",
            "execute_code",
            "process",
        ):
            assert name in AIAgent._RISKY_TOOL_NAMES, name

    def test_excludes_readonly_tools(self):
        for name in (
            "web_search",
            "read_file",
            "Read",
            "Grep",
            "skills_list",
            "skill_view",
            "skill_pitfalls",
            "memory",
        ):
            assert name not in AIAgent._RISKY_TOOL_NAMES, name
