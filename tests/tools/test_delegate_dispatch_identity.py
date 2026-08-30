"""Fork observability: the dispatched agent_type/role of a subagent must be
answerable after the fact.

The motivating gap (2026-08-30): a PM dispatched a coder-tier task and the
only per-child signal in agent.log was ``model=glm-5.3 provider=ollama-cloud``
— ambiguous because BOTH the ``pm`` and ``coder`` roles default to glm-5.3 in
this user's config. The resolved role/persona was used transiently to pick a
model/persona at dispatch time and then thrown away.

The stash itself is NOT new: ``_build_child_agent`` has stamped
``child._delegate_role`` (since 2026-04-21) and ``child._delegate_agent_type``
(since 2026-07-18). What was missing — and what these tests pin — is that the
identity actually reaches the two places an operator (and the parent model)
can see it:

1. agent.log: a spawn-time line from ``_build_child_agent`` and a per-turn
   ``conversation turn:`` line from ``agent/turn_context.py`` (which reads the
   stashed attributes back with a "none" default so the TOP-LEVEL main session
   — which has no delegation role/persona at all — still logs cleanly).
2. The model-visible result entry: ``role``/``agent_type`` alongside the
   existing ``model`` field (the internal ``_child_role`` is stripped before
   serialization, so before this fix the parent model had NO role signal).

Behavior contracts, not change-detector tests: each dispatch shape (WITH
agent_type, WITH role='orchestrator', bare leaf with NEITHER) must produce the
right stash + log line, and the main-session path must log ``none``/``none``
without breaking.
"""

import logging
import unittest
from unittest.mock import MagicMock, patch

from tools.delegate_tool import _build_child_agent
from agent import turn_context


def _make_parent(depth=0):
    parent = MagicMock()
    parent.base_url = "https://api.openrouter.ai/api/v1"
    parent.api_key = "parent-key"
    parent.provider = "openrouter"
    parent.api_mode = "chat_completions"
    parent.model = "glm-5.3"
    parent.platform = "cli"
    parent.enabled_toolsets = ["terminal", "file"]
    parent.disabled_toolsets = None
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent._session_db = None
    parent._delegate_depth = depth
    parent._active_children = []
    parent._active_children_lock = None  # builder guards on lock truthiness
    return parent


def _build(parent, **overrides):
    """Run _build_child_agent with the AIAgent constructor mocked out."""
    kwargs = dict(
        task_index=0,
        goal="test goal",
        context=None,
        toolsets=None,
        model=None,
        max_iterations=10,
        parent_agent=parent,
        task_count=1,
    )
    kwargs.update(overrides)
    with patch("run_agent.AIAgent") as MockAgent:
        MockAgent.return_value = MagicMock()
        _build_child_agent(**kwargs)
        return MockAgent.return_value


class TestDispatchIdentityStashAndSpawnLog(unittest.TestCase):
    """_build_child_agent stamps the identity AND logs it at spawn time."""

    @patch("tools.delegate_tool._load_config", return_value={})
    def test_agent_type_dispatch_stash_and_log(self, _cfg):
        parent = _make_parent()
        with self.assertLogs("tools.delegate_tool", level="INFO") as cm:
            child = _build(parent, agent_type="coder")

        self.assertEqual(child._delegate_agent_type, "coder")
        self.assertEqual(child._delegate_role, "leaf")
        spawn_lines = [
            m for m in cm.output if "spawned subagent" in m and "role=" in m
        ]
        self.assertTrue(spawn_lines, f"no spawn log line captured: {cm.output}")
        line = spawn_lines[0]
        self.assertIn("role=leaf", line)
        self.assertIn("agent_type=coder", line)

    @patch("tools.delegate_tool._load_config", return_value={"max_spawn_depth": 2})
    def test_orchestrator_dispatch_stash_and_log(self, _cfg):
        parent = _make_parent()
        with self.assertLogs("tools.delegate_tool", level="INFO") as cm:
            child = _build(parent, role="orchestrator")

        self.assertEqual(child._delegate_role, "orchestrator")
        self.assertEqual(child._delegate_agent_type, "")
        spawn_lines = [
            m for m in cm.output if "spawned subagent" in m and "role=" in m
        ]
        self.assertTrue(spawn_lines, f"no spawn log line captured: {cm.output}")
        line = spawn_lines[0]
        self.assertIn("role=orchestrator", line)
        self.assertIn("agent_type=none", line)

    @patch("tools.delegate_tool._load_config", return_value={})
    def test_bare_leaf_dispatch_stash_and_log(self, _cfg):
        parent = _make_parent()
        with self.assertLogs("tools.delegate_tool", level="INFO") as cm:
            child = _build(parent)

        self.assertEqual(child._delegate_role, "leaf")
        self.assertEqual(child._delegate_agent_type, "")
        spawn_lines = [
            m for m in cm.output if "spawned subagent" in m and "role=" in m
        ]
        self.assertTrue(spawn_lines, f"no spawn log line captured: {cm.output}")
        line = spawn_lines[0]
        self.assertIn("role=leaf", line)
        self.assertIn("agent_type=none", line)


class TestTurnContextLogIdentity(unittest.TestCase):
    """The per-turn 'conversation turn:' log line (agent/turn_context.py)
    must name the delegation-identity fields. The real drive of
    build_turn_context (with the full fake-agent harness) lives in
    tests/agent/test_turn_context.py::test_turn_log_line_carries_delegation_identity;
    this pins the format-string contract here, next to the builder tests."""

    def test_log_format_string_names_identity_fields(self):
        # Contract on the emitted line shape: the new fields in fixed order
        # between platform= and history=, so downstream log parsing stays
        # stable. Sourced from the real module's logging call, not a copy.
        import inspect

        src = inspect.getsource(turn_context.build_turn_context)
        self.assertIn(
            "agent_type=%s role=%s", src,
            "build_turn_context must log agent_type=/role= (fork 2026-08-30 "
            "dispatch-identity observability fix)",
        )
        # Old fields must survive — the line stays parseable for existing
        # tooling that greps session=/model=/provider=/platform=/history=.
        for field in ("session=%s", "model=%s", "provider=%s", "platform=%s",
                      "history=%d", "msg=%r"):
            self.assertIn(field, src)


class TestResultEntryIdentity(unittest.TestCase):
    """The model-visible result entry carries role/agent_type."""

    def _child(self, agent_type="", role="leaf"):
        child = MagicMock()
        child._delegate_agent_type = agent_type
        child._delegate_role = role
        child.model = "glm-5.3"
        child.session_prompt_tokens = 0
        child.session_completion_tokens = 0
        child.session_estimated_cost_usd = 0.0
        child.session_reasoning_tokens = 0
        child.tool_trace = []
        child.session_cost_status = "unknown"
        return child

    def test_success_entry_carries_identity(self):
        from tools.delegate_tool import _run_single_child

        child = self._child(agent_type="coder", role="leaf")

        def _run_conversation(*_a, **_k):
            return {
                "final_response": "done",
                "completed": True,
                "api_calls": 1,
                "messages": [],
            }

        child.run_conversation = _run_conversation
        parent = _make_parent()
        with patch("tools.delegate_tool._load_config", return_value={}):
            entry = _run_single_child(0, "goal", child, parent)

        self.assertEqual(entry["role"], "leaf")
        self.assertEqual(entry["agent_type"], "coder")

    def test_success_entry_identity_none_for_untagged_child(self):
        from tools.delegate_tool import _run_single_child

        child = self._child()

        def _run_conversation(*_a, **_k):
            return {
                "final_response": "done",
                "completed": True,
                "api_calls": 1,
                "messages": [],
            }

        child.run_conversation = _run_conversation
        parent = _make_parent()
        with patch("tools.delegate_tool._load_config", return_value={}):
            entry = _run_single_child(0, "goal", child, parent)

        # role is always stashed by _build_child_agent ("leaf" is the
        # post-degradation default); only agent_type is absent for a
        # bare, persona-less dispatch.
        self.assertEqual(entry["role"], "leaf")
        self.assertIsNone(entry["agent_type"])


if __name__ == "__main__":
    unittest.main()