"""Tests for the explicit turn-close contract (agent/end_turn.py).

The contract replaces the promise gate: a turn that used a tool may not end on prose. These
tests pin the decision table and the payload->reply rendering; the loop wiring is exercised by
the live one-shot in FORK.md.
"""

from agent.end_turn import (
    CLOSE,
    END_TURN_SCHEMA,
    ESCAPE,
    HARNESS_ERROR,
    MAX_TURN_CONTRACT_REJECTIONS,
    PASS,
    REJECT,
    TURN_STATUSES,
    attempted_end_turn_close,
    clear_end_turn,
    contract_decision,
    end_turn_payload,
    end_turn_tool,
    ensure_end_turn_schema,
    recover_close,
    register_end_turn,
    render_summary,
    turn_contract_enabled,
)


class _Agent:
    def __init__(self, enabled=None):
        if enabled is not None:
            self._turn_contract_enabled = enabled


class TestSchema:
    def test_schema_shape(self):
        fn = END_TURN_SCHEMA["function"]
        assert fn["name"] == "end_turn"
        props = fn["parameters"]["properties"]
        assert set(fn["parameters"]["required"]) == {"status", "summary", "open_items"}
        assert props["status"]["enum"] == list(TURN_STATUSES)
        assert fn.get("description")

    def test_injected_once_and_latched(self):
        agent = _Agent(enabled=True)
        tools = [{"type": "function", "function": {"name": "terminal"}}]
        once = ensure_end_turn_schema(agent, tools)
        assert [t["function"]["name"] for t in once] == ["terminal", "end_turn"]
        twice = ensure_end_turn_schema(agent, once)
        assert [t["function"]["name"] for t in twice] == ["terminal", "end_turn"]
        assert agent._turn_contract_enabled is True

    def test_disabled_when_the_agent_says_so(self):
        agent = _Agent(enabled=False)
        tools = [{"type": "function", "function": {"name": "terminal"}}]
        assert ensure_end_turn_schema(agent, tools) == tools
        assert agent._turn_contract_enabled is False
        assert turn_contract_enabled(agent) is False

    def test_the_injected_schema_is_a_copy(self):
        agent = _Agent(enabled=True)
        tools = ensure_end_turn_schema(agent, [])
        tools[0]["function"]["description"] = "mutated"
        assert END_TURN_SCHEMA["function"]["description"] != "mutated"


class TestTool:
    def test_records_the_payload(self):
        agent = _Agent()
        out = end_turn_tool(
            status="done", summary="42 lines.", open_items=[], agent=agent,
        )
        assert '"accepted": true' in out
        assert end_turn_payload(agent) == {
            "status": "done", "summary": "42 lines.", "open_items": [], "blocker": None,
        }

    def test_trims_open_items_and_keeps_the_blocker(self):
        agent = _Agent()
        end_turn_tool(
            status="blocked", summary="Waiting.", open_items=["probe 118", "  "],
            blocker="console is off", agent=agent,
        )
        payload = end_turn_payload(agent)
        assert payload["open_items"] == ["probe 118"]
        assert payload["blocker"] == "console is off"

    def test_unknown_status_falls_back_to_done(self):
        agent = _Agent()
        end_turn_tool(status="finished!", summary="ok", agent=agent)
        assert end_turn_payload(agent)["status"] == "done"

    def test_empty_summary_is_refused_and_records_nothing(self):
        # A refused close must not leave a payload behind: a prose ending after it would
        # otherwise be judged CLOSE and deliver an empty reply.
        agent = _Agent()
        out = end_turn_tool(status="done", summary="   ", agent=agent)
        assert '"accepted": false' in out
        assert end_turn_payload(agent) is None

    def test_clear(self):
        agent = _Agent()
        end_turn_tool(status="done", summary="x", agent=agent)
        clear_end_turn(agent)
        assert end_turn_payload(agent) is None


class TestRender:
    def test_done_with_no_open_items_is_just_the_summary(self):
        assert render_summary({"status": "done", "summary": "All green.", "open_items": []}) == "All green."

    def test_blocked_names_the_blocker(self):
        text = render_summary({
            "status": "blocked", "summary": "Cannot read the table.",
            "open_items": ["read sysent"], "blocker": "console is off",
        })
        assert text.startswith("Cannot read the table.")
        assert "Blocked: console is off" in text
        assert "- read sysent" in text

    def test_deferred_lists_the_work(self):
        text = render_summary({
            "status": "deferred", "summary": "Measured 3 of 5.",
            "open_items": ["probe 118", "probe 119"],
        })
        assert text.count("- ") == 2


class TestDecision:
    def test_closed_turn(self):
        assert contract_decision(payload={"summary": "x"}, tool_calls_this_turn=5, rejections=0) == CLOSE

    def test_text_only_after_tools_is_rejected(self):
        assert contract_decision(payload=None, tool_calls_this_turn=3, rejections=0) == REJECT

    def test_rejections_are_bounded(self):
        assert contract_decision(
            payload=None, tool_calls_this_turn=3, rejections=MAX_TURN_CONTRACT_REJECTIONS,
        ) == ESCAPE

    def test_plain_answer_without_tools_passes(self):
        # A chat-only turn (no tool calls) is a normal answer, not a contract violation.
        assert contract_decision(payload=None, tool_calls_this_turn=0, rejections=0) == PASS

    def test_an_attempted_close_the_harness_lost_is_a_harness_error_not_a_rejection(self):
        # The model invoked end_turn and got no payload out of it: that is the harness's
        # failure, whatever the rejection budget says.
        for rejections in (0, MAX_TURN_CONTRACT_REJECTIONS, 99):
            assert contract_decision(
                payload=None, tool_calls_this_turn=1, rejections=rejections, close_attempted=True,
            ) == HARNESS_ERROR

    def test_a_real_close_still_wins_over_an_earlier_failed_attempt(self):
        assert contract_decision(
            payload={"summary": "x"}, tool_calls_this_turn=2, rejections=0, close_attempted=True,
        ) == CLOSE

    def test_every_decision_is_covered(self):
        seen = {
            contract_decision(payload=None, tool_calls_this_turn=0, rejections=0),
            contract_decision(payload=None, tool_calls_this_turn=1, rejections=0),
            contract_decision(payload=None, tool_calls_this_turn=1, rejections=99),
            contract_decision(payload={}, tool_calls_this_turn=1, rejections=0),
            contract_decision(payload=None, tool_calls_this_turn=1, rejections=0, close_attempted=True),
        }
        assert seen == {PASS, REJECT, ESCAPE, CLOSE, HARNESS_ERROR}


class TestCounters:
    """The escape counter is the go/no-go metric, so it has to be mechanical."""

    def test_events_accumulate_per_day(self, tmp_path, monkeypatch):
        from agent import end_turn as et

        monkeypatch.setattr(et, "STATS_PATH", tmp_path / "tc.json")
        et.record_turn_contract_event("closed")
        et.record_turn_contract_event("closed")
        et.record_turn_contract_event("escaped")
        stats = et.read_turn_contract_stats()
        assert stats["closed"] == 2
        assert stats["escaped"] == 1

    def test_missing_file_reads_empty(self, tmp_path, monkeypatch):
        from agent import end_turn as et

        monkeypatch.setattr(et, "STATS_PATH", tmp_path / "nope.json")
        assert et.read_turn_contract_stats() == {}

    def test_bookkeeping_never_raises(self, tmp_path, monkeypatch):
        from agent import end_turn as et

        monkeypatch.setattr(et, "STATS_PATH", tmp_path / "deep" / "blocked" / "tc.json")
        (tmp_path / "deep").write_text("not a dir")   # parent is a file: mkdir must fail
        et.record_turn_contract_event("closed")       # swallowed, per contract


class TestDefaultOff:
    """The contract ships OFF (2026-10-25: rejected:closed = 31:4 on the day it went live), and
    an unreadable config must never switch enforcement on by accident."""

    def test_default_is_off(self, monkeypatch):
        import hermes_cli.config as cfg

        monkeypatch.setattr(cfg, "load_config_readonly", lambda: {"agent": {}})
        assert turn_contract_enabled(_Agent()) is False

    def test_config_opt_in(self, monkeypatch):
        import hermes_cli.config as cfg

        monkeypatch.setattr(cfg, "load_config_readonly", lambda: {"agent": {"turn_contract": True}})
        assert turn_contract_enabled(_Agent()) is True
        monkeypatch.setattr(cfg, "load_config_readonly", lambda: {"agent": {"turn_contract": "on"}})
        assert turn_contract_enabled(_Agent()) is True

    def test_a_broken_config_is_off(self, monkeypatch):
        import hermes_cli.config as cfg

        def _boom():
            raise RuntimeError("config unreadable")

        monkeypatch.setattr(cfg, "load_config_readonly", _boom)
        assert turn_contract_enabled(_Agent()) is False

    def test_the_shipped_default_matches(self):
        from hermes_cli.config_defaults import DEFAULT_CONFIG

        assert DEFAULT_CONFIG["agent"]["turn_contract"] is False


def _tool(name):
    return {"type": "function", "function": {"name": name, "description": "", "parameters": {}}}


class TestOneRegistry:
    """``end_turn`` is registered in one place that owns BOTH the wire list and the dispatch
    gate (``agent.valid_tool_names``). On 2026-10-25 the request builder put the tool on the
    wire while a tool-snapshot rebuild had dropped it from the gate, so every direct call came
    back "Tool 'end_turn' does not exist" and the model could only reach it through the bridge.
    """

    def test_registering_the_wire_list_registers_the_gate(self):
        agent = _Agent(enabled=True)
        agent.valid_tool_names = {"terminal"}
        tools = register_end_turn(agent, [_tool("terminal")])
        assert [t["function"]["name"] for t in tools] == ["terminal", "end_turn"]
        assert "end_turn" in agent.valid_tool_names

    def test_a_staged_pair_is_registered_together(self):
        agent = _Agent(enabled=True)
        agent.valid_tool_names = {"terminal"}   # live set: must stay untouched until publish
        staged_names = {"terminal"}
        staged = register_end_turn(agent, [_tool("terminal")], names=staged_names)
        assert "end_turn" in staged_names
        assert "end_turn" in {t["function"]["name"] for t in staged}
        assert "end_turn" not in agent.valid_tool_names

    def test_disabled_registers_nothing(self):
        agent = _Agent(enabled=False)
        agent.valid_tool_names = {"terminal"}
        assert register_end_turn(agent, [_tool("terminal")]) == [_tool("terminal")]
        assert agent.valid_tool_names == {"terminal"}

    def test_the_validator_resolves_a_direct_call(self):
        """The acceptance check: a direct ``end_turn`` call passes the name gate the tool
        round validates against, which is what produced "does not exist"."""
        from agent.conversation_loop import _invalid_tool_name_error_content

        agent = _Agent(enabled=True)
        agent.valid_tool_names = {"terminal"}
        register_end_turn(agent, [_tool("terminal")])
        assert "end_turn" in agent.valid_tool_names
        # And the executor table — the other half of dispatch — already knows the name.
        from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS
        assert "end_turn" in INLINE_TOOL_EXECUTORS
        assert "end_turn" in _invalid_tool_name_error_content("nope", agent.valid_tool_names)

    def test_the_bridge_unwrap_special_case_is_gone(self):
        import agent.inline_tool_executors as ite

        assert not hasattr(ite, "unwrap_end_turn_bridge")

    def test_snapshot_rebuild_keeps_end_turn_on_both_halves(self, monkeypatch):
        """``tools/mcp_tool_agent.refresh_agent_mcp_tools`` is the rebuild that dropped it."""
        import types

        import model_tools
        from tools import mcp_tool_agent

        agent = types.SimpleNamespace(
            tools=[_tool("terminal"), _tool("end_turn")], valid_tool_names={"terminal", "end_turn"},
            enabled_toolsets=None, disabled_toolsets=None, _turn_contract_enabled=True,
        )
        fresh = [_tool("terminal"), _tool("read_file")]
        monkeypatch.setattr(model_tools, "get_tool_definitions", lambda **kw: list(fresh))
        monkeypatch.setattr(mcp_tool_agent, "persist_agent_tool_names", lambda a: None)
        added = mcp_tool_agent.refresh_agent_mcp_tools(agent)
        assert added == {"read_file"}
        assert "end_turn" in agent.valid_tool_names
        assert "end_turn" in {t["function"]["name"] for t in agent.tools}

    def test_snapshot_rebuild_with_the_contract_off_does_not_add_it(self, monkeypatch):
        import types

        import model_tools
        from tools import mcp_tool_agent

        agent = types.SimpleNamespace(
            tools=[_tool("terminal")], valid_tool_names={"terminal"},
            enabled_toolsets=None, disabled_toolsets=None, _turn_contract_enabled=False,
        )
        monkeypatch.setattr(model_tools, "get_tool_definitions", lambda **kw: [_tool("terminal"), _tool("x")])
        monkeypatch.setattr(mcp_tool_agent, "persist_agent_tool_names", lambda a: None)
        mcp_tool_agent.refresh_agent_mcp_tools(agent)
        assert "end_turn" not in agent.valid_tool_names


def _end_turn_call(call_id, arguments):
    import json

    return {
        "role": "assistant", "content": "",
        "tool_calls": [{"id": call_id, "type": "function",
                        "function": {"name": "end_turn", "arguments": json.dumps(arguments)}}],
    }


class TestHarnessFault:
    """Regression for the 2026-10-25 counters (4 closed / 31 rejected / 8 escaped): a close the
    model attempted and the harness failed to run is delivered and recorded as
    ``harness_error`` — never as a rejection, never as a refusal."""

    CLOSE_ARGS = {"status": "deferred", "summary": "Three probes done.", "open_items": ["probe 4"]}

    def _dispatch_failed_turn(self):
        from agent.conversation_loop import _invalid_tool_name_error_content

        # Exactly what the tool round appends when the name gate rejects the call.
        return [
            {"role": "user", "content": "run the probes"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "name": "terminal", "content": "ok"},
            _end_turn_call("c2", self.CLOSE_ARGS),
            {"role": "tool", "tool_call_id": "c2", "name": "end_turn",
             "content": _invalid_tool_name_error_content("end_turn", {"terminal", "clarify"})},
        ]

    def test_dispatch_failure_is_delivered_and_recorded_as_harness_error(self, tmp_path, monkeypatch):
        from agent import end_turn as et
        from agent.agent_runtime_helpers import tool_calls_this_turn

        monkeypatch.setattr(et, "STATS_PATH", tmp_path / "tc.json")
        agent = _Agent(enabled=True)
        messages = self._dispatch_failed_turn()
        assert end_turn_payload(agent) is None          # nothing reached the executor
        attempt = attempted_end_turn_close(messages)
        assert attempt is not None
        assert "does not exist" in attempt["result"]
        decision = contract_decision(
            payload=None, tool_calls_this_turn=tool_calls_this_turn(messages),
            rejections=0, close_attempted=attempt is not None,
        )
        assert decision == HARNESS_ERROR
        # The reply is what the model wrote into the call, rendered like any close.
        recovered = recover_close(attempt)
        assert render_summary(recovered) == render_summary({**self.CLOSE_ARGS, "blocker": None})
        assert "Three probes done." in render_summary(recovered)
        assert "- probe 4" in render_summary(recovered)
        # And the loop records it against the harness, not the model.
        et.record_turn_contract_event(decision)
        stats = et.read_turn_contract_stats()
        assert stats == {"harness_error": 1}
        assert "rejected" not in stats and "escaped" not in stats

    def test_an_executor_exception_is_also_a_harness_fault(self):
        messages = [
            {"role": "user", "content": "go"},
            _end_turn_call("c9", self.CLOSE_ARGS),
            {"role": "tool", "tool_call_id": "c9", "name": "end_turn",
             "content": '{"error": "Tool execution failed: KeyError(\'agent\')"}'},
        ]
        attempt = attempted_end_turn_close(messages)
        assert attempt is not None and recover_close(attempt)["summary"] == "Three probes done."

    def test_a_close_the_executor_itself_refused_is_the_models_fault(self):
        # accepted:false (empty summary) came from end_turn_tool: a prose ending after it is a
        # plain REJECT, not a harness error.
        messages = [
            {"role": "user", "content": "go"},
            _end_turn_call("c3", {"status": "done", "summary": "", "open_items": []}),
            {"role": "tool", "tool_call_id": "c3", "name": "end_turn",
             "content": '{"accepted": false, "error": "summary is required and must not be empty"}'},
        ]
        assert attempted_end_turn_close(messages) is None
        assert contract_decision(payload=None, tool_calls_this_turn=1, rejections=0,
                                 close_attempted=False) == REJECT

    def test_an_attempt_before_the_nudge_does_not_excuse_prose_after_it(self):
        messages = self._dispatch_failed_turn() + [
            {"role": "assistant", "content": "Here is prose."},
            {"role": "user", "content": "nudge", "_turn_contract_nudge": True},
            {"role": "assistant", "content": "More prose."},
        ]
        assert attempted_end_turn_close(messages) is None

    def test_no_attempt_without_an_end_turn_call(self):
        messages = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "name": "terminal", "content": "ok"},
        ]
        assert attempted_end_turn_close(messages) is None

    def test_unparseable_arguments_still_count_as_an_attempt_with_no_recovery(self):
        messages = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c7", "type": "function", "function": {"name": "end_turn", "arguments": "{not json"}}]},
            {"role": "tool", "tool_call_id": "c7", "name": "end_turn", "content": "Tool 'end_turn' does not exist."},
        ]
        attempt = attempted_end_turn_close(messages)
        assert attempt is not None and attempt["arguments"] is None
        assert recover_close(attempt) is None   # the loop falls back to the prose


class TestLoopVerdict:
    """``apply_turn_contract`` is what ``finish_text_response`` acts on: drive it end to end
    with real message rows and assert the delivered reply and the counter together."""

    @staticmethod
    def _agent(tmp_path, monkeypatch, *, rejections=0):
        import types

        from agent import end_turn as et

        monkeypatch.setattr(et, "STATS_PATH", tmp_path / "tc.json")
        statuses = []
        agent = types.SimpleNamespace(
            _turn_contract_enabled=True, _turn_contract_rejections=rejections,
            model="m", provider="p", _emit_diagnostic_status=statuses.append, statuses=statuses,
        )
        return agent

    @staticmethod
    def _rows(*extra):
        return [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "name": "terminal", "content": "ok"},
            *extra,
        ]

    def test_dispatch_failure_delivers_the_summary_and_records_harness_error(self, tmp_path, monkeypatch):
        from agent import end_turn as et
        from agent.conversation_loop import _invalid_tool_name_error_content
        from agent.turn_final_response_contract import apply_turn_contract

        agent = self._agent(tmp_path, monkeypatch, rejections=1)
        messages = self._rows(
            _end_turn_call("c2", {"status": "done", "summary": "Fixed it.", "open_items": []}),
            {"role": "tool", "tool_call_id": "c2", "name": "end_turn",
             "content": _invalid_tool_name_error_content("end_turn", {"terminal"})},
        )
        final_msg = {"role": "assistant", "content": "The tool does not exist, so: fixed it."}
        verdict = apply_turn_contract(agent, messages=messages, final_msg=final_msg, tool_calls_this_turn=2)
        assert verdict.decision == HARNESS_ERROR
        assert verdict.reprompt is False
        assert verdict.final_response == "Fixed it."
        assert final_msg["content"] == "Fixed it."
        assert et.read_turn_contract_stats() == {"harness_error": 1}
        assert agent._turn_contract_rejections == 0
        assert any("harness could not run it" in s for s in agent.statuses)
        assert not any("refused" in s for s in agent.statuses)

    def test_dispatch_failure_without_a_recoverable_summary_keeps_the_prose(self, tmp_path, monkeypatch):
        from agent.turn_final_response_contract import apply_turn_contract

        agent = self._agent(tmp_path, monkeypatch)
        messages = self._rows(
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c2", "type": "function", "function": {"name": "end_turn", "arguments": "{broken"}}]},
            {"role": "tool", "tool_call_id": "c2", "name": "end_turn", "content": "Tool 'end_turn' does not exist."},
        )
        final_msg = {"role": "assistant", "content": "prose answer"}
        verdict = apply_turn_contract(agent, messages=messages, final_msg=final_msg, tool_calls_this_turn=2)
        assert verdict.decision == HARNESS_ERROR
        assert verdict.final_response is None          # the loop keeps its prose
        assert final_msg["content"] == "prose answer"

    def test_a_proper_close_is_delivered(self, tmp_path, monkeypatch):
        from agent import end_turn as et
        from agent.turn_final_response_contract import apply_turn_contract

        agent = self._agent(tmp_path, monkeypatch)
        end_turn_tool(status="deferred", summary="Half done.", open_items=["rest"], agent=agent)
        final_msg = {"role": "assistant", "content": ""}
        verdict = apply_turn_contract(agent, messages=self._rows(), final_msg=final_msg, tool_calls_this_turn=2)
        assert verdict.decision == CLOSE
        assert verdict.final_response.startswith("Half done.") and "- rest" in verdict.final_response
        assert et.read_turn_contract_stats() == {"closed": 1}
        assert end_turn_payload(agent) is None

    def test_prose_with_no_attempt_is_rejected_then_escapes(self, tmp_path, monkeypatch):
        from agent import end_turn as et
        from agent.turn_final_response_contract import apply_turn_contract

        agent = self._agent(tmp_path, monkeypatch)
        messages = self._rows()
        for n in range(MAX_TURN_CONTRACT_REJECTIONS):
            final_msg = {"role": "assistant", "content": "I'll do it next."}
            verdict = apply_turn_contract(agent, messages=messages, final_msg=final_msg, tool_calls_this_turn=1)
            assert verdict.decision == REJECT and verdict.reprompt is True
            assert messages[-1]["_turn_contract_nudge"] is True and messages[-1]["role"] == "user"
            assert agent._turn_contract_rejections == n + 1
        verdict = apply_turn_contract(
            agent, messages=messages, final_msg={"role": "assistant", "content": "still prose"},
            tool_calls_this_turn=1,
        )
        assert verdict.decision == ESCAPE and verdict.reprompt is False and verdict.final_response is None
        assert et.read_turn_contract_stats() == {"rejected": MAX_TURN_CONTRACT_REJECTIONS, "escaped": 1}

    def test_off_means_pass_through(self, tmp_path, monkeypatch):
        from agent.turn_final_response_contract import apply_turn_contract

        agent = self._agent(tmp_path, monkeypatch)
        agent._turn_contract_enabled = False
        verdict = apply_turn_contract(agent, messages=self._rows(), final_msg={"role": "assistant", "content": "x"},
                                      tool_calls_this_turn=1)
        assert verdict.enabled is False and verdict.decision is None and verdict.reprompt is False
