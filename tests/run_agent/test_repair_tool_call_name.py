"""Tests for AIAgent._repair_tool_call — tool-name normalization.

Regression guard for #14784: Claude-style models sometimes emit
class-like tool-call names (``TodoTool_tool``, ``Patch_tool``,
``BrowserClick_tool``, ``PatchTool``). Before the fix they returned
"Unknown tool" even though the target tool was registered under a
snake_case name. The repair routine now normalizes CamelCase,
strips trailing ``_tool`` / ``-tool`` / ``tool`` suffixes (up to
twice to handle double-tacked suffixes like ``TodoTool_tool``), and
falls back to fuzzy match.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest


VALID = {
    "todo",
    "patch",
    "browser_click",
    "browser_navigate",
    "web_search",
    "read_file",
    "write_file",
    "terminal",
    "execute_code",
    "session_search",
}


@pytest.fixture
def repair():
    """Return a bound _repair_tool_call built on a minimal shell agent.

    We avoid constructing a real AIAgent (which pulls in credential
    resolution, session DB, etc.) because the repair routine only
    reads self.valid_tool_names. A SimpleNamespace stub is enough to
    bind the unbound function.
    """
    from run_agent import AIAgent
    stub = SimpleNamespace(valid_tool_names=VALID)
    return AIAgent._repair_tool_call.__get__(stub, AIAgent)


class TestExistingBehaviorStillWorks:
    """Pre-existing repairs must keep working (no regressions)."""

    def test_lowercase_already_matches(self, repair):
        assert repair("browser_click") == "browser_click"

    def test_uppercase_simple(self, repair):
        assert repair("TERMINAL") == "terminal"

    def test_dash_to_underscore(self, repair):
        assert repair("web-search") == "web_search"

    def test_space_to_underscore(self, repair):
        assert repair("write file") == "write_file"

    def test_fuzzy_near_miss(self, repair):
        # One-character typo — fuzzy match at 0.7 cutoff
        assert repair("terminall") == "terminal"

    def test_unknown_returns_none(self, repair):
        assert repair("xyz_no_such_tool") is None


class TestClassLikeEmissions:
    """Regression coverage for #14784 — CamelCase + _tool suffix variants."""

    def test_camel_case_no_suffix(self, repair):
        assert repair("BrowserClick") == "browser_click"

    def test_camel_case_with_underscore_tool_suffix(self, repair):
        assert repair("BrowserClick_tool") == "browser_click"

    def test_camel_case_with_Tool_class_suffix(self, repair):
        assert repair("PatchTool") == "patch"

    def test_double_tacked_class_and_snake_suffix(self, repair):
        # Hardest case from the report: TodoTool_tool — strip both
        # '_tool' (trailing) and 'Tool' (CamelCase embedded) to reach 'todo'.
        assert repair("TodoTool_tool") == "todo"

    def test_simple_name_with_tool_suffix(self, repair):
        assert repair("Patch_tool") == "patch"

    def test_simple_name_with_dash_tool_suffix(self, repair):
        assert repair("patch-tool") == "patch"

    def test_camel_case_preserves_multi_word_match(self, repair):
        assert repair("ReadFile_tool") == "read_file"
        assert repair("WriteFileTool") == "write_file"

    def test_mixed_separators_and_suffix(self, repair):
        assert repair("write-file_Tool") == "write_file"


class TestEdgeCases:
    """Edge inputs that must not crash or produce surprising results."""

    def test_empty_string(self, repair):
        assert repair("") is None

    def test_only_tool_suffix(self, repair):
        # '_tool' by itself is not a valid tool name — must not match
        # anything plausible.
        assert repair("_tool") is None

    def test_none_passed_as_name(self, repair):
        # Defensive: real callers always pass str, but guard against
        # a bug upstream that sends None.
        assert repair(None) is None

    def test_very_long_name_does_not_match_by_accident(self, repair):
        # Fuzzy match should not claim a tool for something obviously unrelated.
        assert repair("ThisIsNotRemotelyARealToolName_tool") is None


class TestCCCanonicalAliasFastPath:
    """Anthropic OAuth path emits CC canonical names (Bash, Read, Edit,
    Write, Grep) because cc_aliases.replace_with_cc_canonical substitutes
    them on the outbound side to satisfy the plan-budget billing
    classifier. Validation runs before dispatch, so _repair_tool_call
    must translate these back to their hermes equivalents — exact match,
    case-sensitive, no normalization.
    """

    def test_repairs_cc_bash_to_terminal(self):
        from run_agent import AIAgent
        stub = SimpleNamespace(valid_tool_names={"terminal", "read_file"})
        repair = AIAgent._repair_tool_call.__get__(stub, AIAgent)
        assert repair("Bash") == "terminal"

    def test_repairs_cc_read_to_read_file(self, repair):
        assert repair("Read") == "read_file"

    def test_repairs_cc_edit_to_patch(self, repair):
        assert repair("Edit") == "patch"

    def test_repairs_cc_write_to_write_file(self, repair):
        assert repair("Write") == "write_file"

    def test_repairs_cc_grep_to_search_files(self):
        # search_files isn't in the default VALID set; build a fixture
        # that includes it so we can verify the alias resolves.
        from run_agent import AIAgent
        stub = SimpleNamespace(valid_tool_names=VALID | {"search_files"})
        repair = AIAgent._repair_tool_call.__get__(stub, AIAgent)
        assert repair("Grep") == "search_files"

    def test_cc_alias_only_when_hermes_name_valid(self):
        # If the mapped hermes name isn't registered, the fast-path must
        # NOT return it — fall through to the rest of the repair logic
        # (which has nothing matching "Bash" → returns None here).
        from run_agent import AIAgent
        stub = SimpleNamespace(valid_tool_names={"read_file", "patch"})
        repair = AIAgent._repair_tool_call.__get__(stub, AIAgent)
        assert repair("Bash") is None


class TestCCArgsTranslationAfterRepair:
    """``_translate_cc_args_after_repair`` is the second half of the CC
    alias fast-path: after ``_repair_tool_call`` renames ``Read`` →
    ``read_file``, this helper also rewrites the args from CC's shape
    (``{"file_path": ...}``) to hermes's shape (``{"path": ...}``).
    Without it, the handler reads ``args["path"]`` → "" → "File not
    found: " with no path and bogus similar-files suggestions.
    """

    @staticmethod
    def _make_tc(name: str, args_json: str):
        """Build a minimal tool_call object with the shape the agent loop sees."""
        function = SimpleNamespace(name=name, arguments=args_json)
        return SimpleNamespace(function=function)

    @staticmethod
    def _make_translator():
        from run_agent import AIAgent
        stub = SimpleNamespace()
        return AIAgent._translate_cc_args_after_repair.__get__(stub, AIAgent)

    def test_read_file_args_translated(self):
        import json
        translate = self._make_translator()
        tc = self._make_tc("read_file", '{"file_path": "/tmp/x", "offset": 5}')
        translate(tc, original_name="Read")
        # name unchanged here (the loop set it before calling us)
        assert tc.function.name == "read_file"
        # but args translated to hermes shape
        new_args = json.loads(tc.function.arguments)
        assert new_args == {"path": "/tmp/x", "offset": 5}

    def test_write_file_args_translated(self):
        import json
        translate = self._make_translator()
        tc = self._make_tc("write_file", '{"file_path": "/tmp/x", "content": "hi"}')
        translate(tc, original_name="Write")
        new_args = json.loads(tc.function.arguments)
        assert new_args == {"path": "/tmp/x", "content": "hi"}

    def test_patch_args_translated_from_edit(self):
        import json
        translate = self._make_translator()
        tc = self._make_tc(
            "patch",
            '{"file_path": "/tmp/x", "old_string": "a", "new_string": "b"}',
        )
        translate(tc, original_name="Edit")
        new_args = json.loads(tc.function.arguments)
        assert new_args == {
            "mode": "replace",
            "path": "/tmp/x",
            "old_string": "a",
            "new_string": "b",
            "replace_all": False,
        }

    def test_terminal_args_translated_from_bash(self):
        import json
        translate = self._make_translator()
        tc = self._make_tc(
            "terminal",
            '{"command": "ls", "run_in_background": true, "timeout": 5000}',
        )
        translate(tc, original_name="Bash")
        new_args = json.loads(tc.function.arguments)
        # CC timeout is ms; hermes is seconds — adapter divides
        assert new_args["command"] == "ls"
        assert new_args["background"] is True
        assert new_args["timeout"] == 5

    def test_non_cc_alias_is_noop(self):
        """Non-CC names (e.g. ``read_file`` already, or fuzzy-matched
        ``write file`` → ``write_file``) must NOT trigger arg
        translation — only CC name hits should."""
        translate = self._make_translator()
        original_json = '{"path": "/tmp/x", "offset": 1}'
        tc = self._make_tc("read_file", original_json)
        translate(tc, original_name="read_file")  # not a CC name
        assert tc.function.arguments == original_json

    def test_empty_args_is_safe(self):
        """An empty args string must not crash the helper."""
        translate = self._make_translator()
        tc = self._make_tc("read_file", "")
        # The adapter raises on missing file_path, but the helper catches it
        # and leaves args unchanged. That's fine — the downstream handler
        # will return its actionable "missing required field 'path'" error.
        translate(tc, original_name="Read")
        # arguments unchanged (couldn't translate empty dict)
        assert tc.function.arguments == ""

    def test_malformed_json_args_is_safe(self):
        """Malformed JSON in args must not crash the helper."""
        translate = self._make_translator()
        tc = self._make_tc("read_file", "{not valid json")
        translate(tc, original_name="Read")
        # arguments unchanged
        assert tc.function.arguments == "{not valid json"

    def test_grep_to_search_files_args_translated(self):
        import json
        translate = self._make_translator()
        tc = self._make_tc(
            "search_files",
            '{"pattern": "foo", "path": "/tmp", "-i": true}',
        )
        translate(tc, original_name="Grep")
        new_args = json.loads(tc.function.arguments)
        assert new_args["pattern"] == "foo"
        assert new_args["path"] == "/tmp"
        # CC's -i flag maps to case_insensitive
        assert new_args["case_insensitive"] is True


class TestCCAgentToDelegateTaskInbound:
    """Inbound-only CC ``Agent`` → hermes ``delegate_task`` adapter.

    The model is heavily trained to emit ``Agent(...)`` for subagent spawns
    (Anthropic's canonical name).  Before this adapter, when the model
    reflexively emitted ``Agent`` instead of ``delegate_task`` on our CC-
    OAuth path, the tool name fell through to "Unknown tool" — burning a
    self-correction round-trip.  This adapter routes inbound only: the
    model still SEES hermes's full ``delegate_task`` schema (batch tasks,
    agent_type, ACP, toolsets) and can use it directly; ``Agent`` is just
    a safety net for the trained reflex.
    """

    def test_agent_in_cc_to_hermes(self):
        from agent.cc_aliases import CC_TO_HERMES
        assert CC_TO_HERMES.get("Agent") == "delegate_task"

    def test_agent_NOT_in_hermes_to_cc(self):
        """Outbound substitution must NOT replace delegate_task with Agent
        on the wire — that would hide hermes's batch/ACP/agent_type fields
        from the model."""
        from agent.cc_aliases import HERMES_TO_CC
        assert "delegate_task" not in HERMES_TO_CC
        assert "Agent" not in HERMES_TO_CC.values()

    def test_prompt_maps_to_goal(self):
        from agent.cc_aliases import adapt_tool_use
        name, args = adapt_tool_use("Agent", {"prompt": "Find auth bugs"})
        assert name == "delegate_task"
        assert args == {"goal": "Find auth bugs"}

    def test_description_maps_to_context(self):
        from agent.cc_aliases import adapt_tool_use
        _, args = adapt_tool_use(
            "Agent",
            {"description": "Bug hunt", "prompt": "Find auth bugs"},
        )
        assert args["goal"] == "Find auth bugs"
        assert args["context"] == "Bug hunt"

    def test_model_passes_through(self):
        from agent.cc_aliases import adapt_tool_use
        _, args = adapt_tool_use(
            "Agent",
            {"prompt": "do thing", "model": "claude-haiku-4-5"},
        )
        assert args["model"] == "claude-haiku-4-5"

    def test_subagent_type_dropped(self):
        """CC's subagent_type (Explore/Plan/general-purpose/statusline-setup)
        doesn't map to hermes's ruflo agent_type (researcher/coder/...).
        Forcing a wrong mapping would inject the wrong persona — drop it."""
        from agent.cc_aliases import adapt_tool_use
        _, args = adapt_tool_use(
            "Agent",
            {"prompt": "do thing", "subagent_type": "Explore"},
        )
        assert "agent_type" not in args
        assert "subagent_type" not in args

    def test_run_in_background_dropped(self):
        """delegate_task is synchronous; background work needs cron/process.
        Silently dropping is better than misleading the model into thinking
        the request was queued asynchronously."""
        from agent.cc_aliases import adapt_tool_use
        _, args = adapt_tool_use(
            "Agent",
            {"prompt": "do thing", "run_in_background": True},
        )
        assert "run_in_background" not in args
        assert "background" not in args

    def test_isolation_dropped(self):
        from agent.cc_aliases import adapt_tool_use
        _, args = adapt_tool_use(
            "Agent",
            {"prompt": "do thing", "isolation": "worktree"},
        )
        assert "isolation" not in args

    def test_empty_args_safe(self):
        """Empty input must not crash; downstream delegate_task gives a
        clear 'Provide either goal or tasks' error."""
        from agent.cc_aliases import adapt_tool_use
        name, args = adapt_tool_use("Agent", {})
        assert name == "delegate_task"
        assert args == {}

    def test_empty_string_model_skipped(self):
        """Defensive: empty/whitespace model should NOT be forwarded
        (would override config-default model with empty string)."""
        from agent.cc_aliases import adapt_tool_use
        _, args = adapt_tool_use("Agent", {"prompt": "x", "model": ""})
        assert "model" not in args
        _, args = adapt_tool_use("Agent", {"prompt": "x", "model": "   "})
        assert "model" not in args

    def test_repair_picks_up_agent(self):
        """``_repair_tool_call`` should resolve ``Agent`` to
        ``delegate_task`` when delegate_task is registered."""
        from run_agent import AIAgent
        stub = SimpleNamespace(valid_tool_names={"delegate_task", "read_file"})
        repair = AIAgent._repair_tool_call.__get__(stub, AIAgent)
        assert repair("Agent") == "delegate_task"

    def test_translate_cc_args_after_repair_fires_for_agent(self):
        """End-to-end: after _repair_tool_call renames Agent → delegate_task,
        ``_translate_cc_args_after_repair`` must run ``_adapt_agent`` on
        the args — same as it does for Read/Write/Edit/Bash/Grep."""
        import json
        from run_agent import AIAgent
        translate = AIAgent._translate_cc_args_after_repair.__get__(
            SimpleNamespace(), AIAgent,
        )
        tc = SimpleNamespace(function=SimpleNamespace(
            name="delegate_task",
            arguments='{"prompt": "search for X", "description": "task",'
                      ' "subagent_type": "Explore"}',
        ))
        translate(tc, original_name="Agent")
        new_args = json.loads(tc.function.arguments)
        assert new_args == {"goal": "search for X", "context": "task"}


class TestVolcEngineXmlPollution:
    """Regression coverage for #33007 — VolcEngine ``api/plan`` endpoint
    leaks raw XML attribute fragments into ``tool_use.name``.

    Observed in production with the ``anthropic_messages`` API mode:

        terminal" parameter="command" string="true
        execute_code" parameter="code" string="true
        session_search" parameter="session_id" string="true

    The fix trims at the first ``"``/``'``/``<``/``>`` so the rest of
    the repair pipeline can resolve the cleaned name to a real tool.
    """

    def test_terminal_with_xml_attribute_pollution(self, repair):
        # Exact pattern from the bug report (terminal call).
        polluted = 'terminal" parameter="command" string="true'
        assert repair(polluted) == "terminal"

    def test_execute_code_with_xml_attribute_pollution(self, repair):
        polluted = 'execute_code" parameter="code" string="true'
        assert repair(polluted) == "execute_code"

    def test_session_search_with_xml_attribute_pollution(self, repair):
        polluted = 'session_search" parameter="session_id" string="true'
        assert repair(polluted) == "session_search"

    def test_camel_case_tool_with_xml_pollution(self, repair):
        # If the polluted prefix is CamelCase / suffixed, the rest of
        # the pipeline (CamelCase -> snake_case, _tool strip) still runs.
        polluted = 'BrowserClick_tool" parameter="selector" string="true'
        assert repair(polluted) == "browser_click"

    def test_tool_name_with_trailing_quote_only(self, repair):
        # Minimal leak — just a stray trailing quote, no full attribute.
        assert repair('terminal"') == "terminal"

    def test_tool_name_with_angle_bracket_pollution(self, repair):
        # Defensive — same root cause, raw '<' bleeding through.
        assert repair("terminal<parameter=command") == "terminal"

    def test_tool_name_with_single_quote_pollution(self, repair):
        # Defensive — same root cause, single-quoted attribute style.
        assert repair("terminal' parameter='command' string='true") == "terminal"

    def test_clean_tool_name_unaffected_by_sanitizer(self, repair):
        # Pure passthrough — no XML/quote chars, no change.
        assert repair("execute_code") == "execute_code"
        assert repair("session_search") == "session_search"

    def test_space_separated_name_still_normalizes(self, repair):
        # Critical: the XML strip must NOT consume whitespace, or the
        # legitimate ``"write file" -> write_file`` repair path breaks.
        assert repair("write file") == "write_file"

    def test_pollution_with_unknown_tool_root_still_fails(self, repair):
        # Sanitizer must not mask invalid tool names by laundering them
        # through the cleaner.
        polluted = 'no_such_tool" parameter="x" string="true'
        assert repair(polluted) is None

    def test_leading_quote_falls_through_to_fuzzy_match(self, repair):
        # Sanitizer only trims when the XML char is at idx > 0 — a
        # name that *starts* with a quote is left untouched so the
        # rest of the pipeline (fuzzy match at 0.7 cutoff) can still
        # recover the obvious target.
        assert repair('"terminal"') == "terminal"
