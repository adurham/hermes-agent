"""Fork regression: a child dispatched with ``agent_type=`` must actually RECEIVE its persona brief.

The v2026.9.14 upstream merge silently dropped the RUFLO persona injection when
``_build_child_system_prompt`` moved into ``tools/delegate_tool_progress.py`` with upstream's
signature (no ``agent_type`` param) and the ``_build_child_agent`` call site stopped passing it.
The net effect was invisible from the outside: ``agent_type`` still pinned model/provider/fallback,
``child._delegate_agent_type`` was still stamped, the logs still said ``agent_type=sr-coder`` — but
the child's system prompt was the generic one, so every ``personas/delegation/*.md`` role brief was
dead weight and their documented behavior (e.g. ``sr-coder.md``'s fallback route) was unenforced.

These are behavior contracts on the real path, not isolated unit checks: the persona files are
written to a temp personas root (``HERMES_PERSONAS_PATH``) and read back out of the
``ephemeral_system_prompt`` that ``_build_child_agent`` hands the real ``AIAgent`` constructor.
The same block also carries ``cwd_collision_warning``, dropped in the same edit.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from tools.delegate_tool import _build_child_agent
from tools.delegate_tool_progress import _build_child_system_prompt

PERSONA_BODY = "You are the senior coder. Route overflow work to the mid-coder before escalating."


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
    parent._active_children_lock = None
    return parent


class _TempPersonas:
    """Temp personas root wired in via HERMES_PERSONAS_PATH, with one real .md on disk."""

    def __init__(self, name="sr-coder", category="delegation", body=PERSONA_BODY):
        self._name, self._category, self._body = name, category, body
        self._tmp = TemporaryDirectory()

    def __enter__(self):
        root = Path(self._tmp.name)
        (root / self._category).mkdir(parents=True, exist_ok=True)
        (root / self._category / f"{self._name}.md").write_text(
            f"---\nname: {self._name}\ndescription: Senior coder persona.\n---\n\n{self._body}\n",
            encoding="utf-8",
        )
        self._patcher = patch.dict("os.environ", {"HERMES_PERSONAS_PATH": str(root)})
        self._patcher.start()
        # The hermes wrapper consults delegation.personas_path in config.yaml FIRST; an empty
        # config makes it fall through to the env var this test sets.
        self._cfg_patcher = patch("hermes_cli.config.load_config", return_value={})
        self._cfg_patcher.start()
        return root

    def __exit__(self, *exc):
        self._cfg_patcher.stop()
        self._patcher.stop()
        self._tmp.cleanup()
        return False


class TestPersonaInjectionIntoChildPrompt(unittest.TestCase):
    """The persona brief reaches the prompt the child is actually constructed with."""

    @patch("tools.delegate_tool._load_config", return_value={})
    def test_dispatched_child_prompt_carries_ruflo_persona_block(self, _cfg):
        """E2E through _build_child_agent: agent_type= puts the persona in ephemeral_system_prompt."""
        parent = _make_parent()
        with _TempPersonas():
            with patch("run_agent.AIAgent") as MockAgent:
                MockAgent.return_value = MagicMock()
                _build_child_agent(
                    task_index=0, goal="ship the fix", context=None, toolsets=None, model=None,
                    max_iterations=10, parent_agent=parent, task_count=1, agent_type="sr-coder",
                )
            prompt = MockAgent.call_args[1]["ephemeral_system_prompt"]

        self.assertIn("# RUFLO PERSONA: sr-coder (delegation)", prompt)
        self.assertIn(PERSONA_BODY, prompt)
        # Persona is a PREFIX: it precedes the generic subagent boilerplate.
        # (The goal is deliberately NOT in this system prompt any more — it is the
        # child's first user turn; see _build_child_system_prompt's docstring and
        # tools/delegate_tool_child_run.py:940.)
        self.assertLess(prompt.index("RUFLO PERSONA"), prompt.index("focused subagent"))
        # Frontmatter is stripped, not pasted through.
        self.assertNotIn("description: Senior coder persona.", prompt)
        # The generic brief still follows — persona augments, never replaces.
        self.assertIn("focused subagent", prompt)
        self.assertNotIn("ship the fix", prompt)

    @patch("tools.delegate_tool._load_config", return_value={})
    def test_no_agent_type_means_no_persona_block(self, _cfg):
        """Symmetric negative: a persona-less dispatch gets the plain prompt."""
        parent = _make_parent()
        with _TempPersonas():
            with patch("run_agent.AIAgent") as MockAgent:
                MockAgent.return_value = MagicMock()
                _build_child_agent(
                    task_index=0, goal="ship the fix", context=None, toolsets=None, model=None,
                    max_iterations=10, parent_agent=parent, task_count=1,
                )
            prompt = MockAgent.call_args[1]["ephemeral_system_prompt"]

        self.assertNotIn("RUFLO PERSONA", prompt)
        self.assertIn("focused subagent", prompt)

    @patch("tools.delegate_tool._load_config", return_value={})
    def test_unknown_agent_type_falls_through_silently(self, _cfg):
        """An agent_type with no matching .md must not break the dispatch."""
        parent = _make_parent()
        with _TempPersonas():
            with patch("run_agent.AIAgent") as MockAgent:
                MockAgent.return_value = MagicMock()
                _build_child_agent(
                    task_index=0, goal="ship the fix", context=None, toolsets=None, model=None,
                    max_iterations=10, parent_agent=parent, task_count=1,
                    agent_type="no-such-persona-exists",
                )
            prompt = MockAgent.call_args[1]["ephemeral_system_prompt"]

        self.assertNotIn("RUFLO PERSONA", prompt)
        self.assertIn("focused subagent", prompt)

    def test_builder_accepts_agent_type_and_collision_warning(self):
        """Unit-level: both params dropped by the merge are back on the signature and honored."""
        with _TempPersonas():
            prompt = _build_child_system_prompt(
                "audit the diff", agent_type="sr-coder",
                cwd_collision_warning="WARNING: 1 other live subagent(s) ... sa-0-other",
            )
        self.assertIn("# RUFLO PERSONA: sr-coder (delegation)", prompt)
        self.assertIn(PERSONA_BODY, prompt)
        self.assertIn("sa-0-other", prompt)
        # The collision warning lands BEFORE the generic completion boilerplate the child skims.
        self.assertLess(prompt.index("sa-0-other"), prompt.index("Complete this task"))

    def test_persona_survives_orchestrator_role(self):
        """Persona prefix and the orchestrator block coexist (different ends of the prompt)."""
        with _TempPersonas(name="pm"):
            prompt = _build_child_system_prompt(
                "plan the work", agent_type="pm", role="orchestrator", max_spawn_depth=2, child_depth=1,
            )
        self.assertIn("# RUFLO PERSONA: pm (delegation)", prompt)
        self.assertIn("Orchestrator Role", prompt)
        self.assertLess(prompt.index("RUFLO PERSONA"), prompt.index("Orchestrator Role"))


if __name__ == "__main__":
    unittest.main()
