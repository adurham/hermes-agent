"""Tests for the explicit turn-close contract (agent/end_turn.py).

The contract replaces the promise gate: a turn that used a tool may not end on prose. These
tests pin the decision table and the payload->reply rendering; the loop wiring is exercised by
the live one-shot in FORK.md.
"""

from agent.end_turn import (
    CLOSE,
    CONTINUE,
    CONTINUE_EXHAUSTED,
    END_TURN_SCHEMA,
    ESCAPE,
    HARNESS_ERROR,
    IDLE,
    MAX_TURN_CONTINUATIONS,
    MAX_TURN_CONTRACT_REJECTIONS,
    PASS,
    REJECT,
    STOP,
    TURN_STATUSES,
    WORK_OPEN,
    attempted_end_turn_close,
    clear_end_turn,
    close_verdict,
    continuation_nudge,
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
        assert payload["open_items"] == [{"item": "probe 118", "owner": "agent", "needs": None}]
        assert payload["blocker"] == "console is off"

    def test_an_item_object_keeps_its_owner_and_needs(self):
        agent = _Agent()
        end_turn_tool(
            status="done", summary="Report.",
            open_items=[{"item": "relaunch the console", "owner": "user", "needs": "you relaunch it"},
                        {"item": "audit the parser"}],
            agent=agent,
        )
        items = end_turn_payload(agent)["open_items"]
        assert items[0] == {"item": "relaunch the console", "owner": "user", "needs": "you relaunch it"}
        assert items[1] == {"item": "audit the parser", "owner": "agent", "needs": None}

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


class TestCloseVerdict:
    """The gate keys on the OWNER the model commits to, not on its prose: agent-owned work is
    never a stop, user-owned work must say what it needs, and `done` may carry user items."""

    @staticmethod
    def _p(**kw):
        from agent.end_turn import normalize_payload

        return normalize_payload(**kw)

    def test_agent_owned_work_is_never_a_stop_whatever_the_status(self):
        for status in ("done", "deferred", "blocked", "need_input"):
            payload = self._p(status=status, summary="s", open_items=[{"item": "audit the parser"}],
                              blocker="x")
            assert close_verdict(payload) == (WORK_OPEN, "agent_work_open"), status

    def test_a_bare_string_item_is_agent_owned(self):
        # The pre-owner shape (and what a model ignoring the schema emits) must not become an
        # escape hatch: a string claims work the model can do.
        assert close_verdict(self._p(status="done", summary="s", open_items=["still open"])) == (
            WORK_OPEN, "agent_work_open")

    def test_deferred_with_no_items_names_nothing(self):
        assert close_verdict(self._p(status="deferred", summary="s", open_items=[])) == (WORK_OPEN, "deferred")

    def test_a_user_item_without_needs_is_unusable(self):
        payload = self._p(status="done", summary="s", open_items=[{"item": "relaunch it", "owner": "user"}])
        assert close_verdict(payload) == (WORK_OPEN, "user_item_without_needs")

    def test_a_terminal_status_needs_a_user_item_or_a_blocker(self):
        for status in ("blocked", "need_input"):
            assert close_verdict(self._p(status=status, summary="s")) == (WORK_OPEN, "terminal_without_blocker")

    def test_done_carrying_only_well_formed_user_items_is_a_report(self):
        payload = self._p(status="done", summary="Here is where things stand.",
                          open_items=[{"item": "relaunch the console", "owner": "user",
                                       "needs": "you relaunch the loader"}])
        assert close_verdict(payload) == (STOP, "")

    def test_done_and_a_named_blocker_stop(self):
        assert close_verdict(self._p(status="done", summary="s", open_items=[])) == (STOP, "")
        assert close_verdict(self._p(status="blocked", summary="s", blocker="owner relaunch")) == (STOP, "")

    def test_open_items_are_rendered_even_when_the_status_says_done(self):
        # The old renderer dropped the list for `done`, so a contradictory close became silent
        # ghost tasks the operator never saw.
        reply = render_summary(self._p(status="done", summary="Wrapped.", open_items=["still open"]))
        assert reply == "Wrapped.\n\nOpen:\n\n- still open"

    def test_user_items_render_as_waiting_on_you_with_their_need(self):
        reply = render_summary(self._p(
            status="done", summary="Report.",
            open_items=[{"item": "relaunch the console", "owner": "user", "needs": "you relaunch the loader"}],
        ))
        assert "Waiting on you:" in reply
        assert "- relaunch the console (needs: you relaunch the loader)" in reply
        assert "Open:" not in reply

    def test_the_nudge_numbers_the_items_and_names_the_way_out(self):
        text = continuation_nudge(self._p(status="deferred", summary="s", open_items=["a", "b"]), "deferred")
        assert "1. a" in text and "2. b" in text
        assert "Act on item 1 now" in text
        assert "`blocked`" in text and "`need_input`" in text

    def test_the_idle_nudge_names_what_was_missing(self):
        text = continuation_nudge(self._p(status="done", summary="s", open_items=["a"]), "agent_work_open", idle=True)
        assert "without running a single tool" in text


class TestLoopVerdict:
    """``apply_turn_contract`` is what ``finish_text_response`` acts on: drive it end to end
    with real message rows and assert the delivered reply and the counter together."""

    @staticmethod
    def _agent(tmp_path, monkeypatch, *, rejections=0, continuations=0, budget=MAX_TURN_CONTINUATIONS):
        import types

        from agent import end_turn as et

        monkeypatch.setattr(et, "STATS_PATH", tmp_path / "tc.json")
        statuses = []
        agent = types.SimpleNamespace(
            _turn_contract_enabled=True, _turn_contract_rejections=rejections,
            _turn_continuations=continuations, _turn_continuations_budget=budget,
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

    @staticmethod
    def _nudge(messages):
        """The continuation row the harness appended, asserting it is a nudge user row."""
        row = messages[-1]
        assert row["role"] == "user" and row["_turn_contract_nudge"] is True
        return row["content"]

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
        # The recovered summary replaced the streamed prose: the same fact a proper close records.
        assert agent._final_content_replaced is True

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
        # The prose stands as the reply: no replacement to tell clients about.
        assert not getattr(agent, "_final_content_replaced", False)

    def test_a_proper_close_is_delivered(self, tmp_path, monkeypatch):
        from agent import end_turn as et
        from agent.turn_final_response_contract import apply_turn_contract

        agent = self._agent(tmp_path, monkeypatch)
        end_turn_tool(status="done", summary="Half done.", open_items=[], agent=agent)
        final_msg = {"role": "assistant", "content": ""}
        verdict = apply_turn_contract(agent, messages=self._rows(), final_msg=final_msg, tool_calls_this_turn=2)
        assert verdict.decision == CLOSE
        assert verdict.final_response == "Half done."
        assert et.read_turn_contract_stats() == {"closed": 1}
        assert end_turn_payload(agent) is None
        # The close wrote the summary over the text this message had streamed: clients that paint
        # streamed text as its own bubble are told the draft was replaced (final_content_replaced).
        assert agent._final_content_replaced is True

    def test_a_deferred_close_is_handed_back_with_its_items(self, tmp_path, monkeypatch):
        # The regression the owner reported: "new attack surface" / "Open:" and then nothing.
        # `deferred` is a claim that work remains that the model can still do, so the harness
        # must not deliver it as an answer — it echoes the items back as the turn's assignment.
        from agent import end_turn as et
        from agent.turn_final_response_contract import apply_turn_contract

        agent = self._agent(tmp_path, monkeypatch)
        end_turn_tool(
            status="deferred", summary="Found a new attack surface.",
            open_items=["audit FUN_0010a360 for count→size overflow", "confirm which method P/Invokes hpatchz"],
            agent=agent,
        )
        messages = self._rows()
        verdict = apply_turn_contract(
            agent, messages=messages, final_msg={"role": "assistant", "content": ""}, tool_calls_this_turn=2,
        )
        assert verdict.decision == CONTINUE and verdict.reprompt is True
        assert verdict.final_response is None
        assert agent._turn_continuations == 1
        assert end_turn_payload(agent) is None          # the old payload must not close the next attempt
        text = self._nudge(messages)
        assert "1. audit FUN_0010a360 for count→size overflow" in text
        assert "2. confirm which method P/Invokes hpatchz" in text
        assert "Act on item 1 now" in text
        assert et.read_turn_contract_stats() == {"continued": 1}
        assert any("continuing" in s for s in agent.statuses)

    def test_done_with_open_items_is_the_same_claim_in_disguise(self, tmp_path, monkeypatch):
        from agent import end_turn as et
        from agent.turn_final_response_contract import apply_turn_contract

        agent = self._agent(tmp_path, monkeypatch)
        end_turn_tool(status="done", summary="Wrapped up.", open_items=["still need the apply bridge"], agent=agent)
        messages = self._rows()
        verdict = apply_turn_contract(
            agent, messages=messages, final_msg={"role": "assistant", "content": ""}, tool_calls_this_turn=3,
        )
        assert verdict.decision == CONTINUE
        assert "it carries 1 item(s) owned by you" in self._nudge(messages)
        assert et.read_turn_contract_stats() == {"continued": 1}

    def test_a_terminal_status_needs_a_named_blocker(self, tmp_path, monkeypatch):
        from agent.turn_final_response_contract import apply_turn_contract

        agent = self._agent(tmp_path, monkeypatch)
        end_turn_tool(status="blocked", summary="Waiting.", open_items=[], agent=agent)
        messages = self._rows()
        verdict = apply_turn_contract(
            agent, messages=messages, final_msg={"role": "assistant", "content": ""}, tool_calls_this_turn=1,
        )
        assert verdict.decision == CONTINUE
        assert "must carry either an `owner=\"user\"` item with `needs`, or a `blocker`" in self._nudge(messages)

    def test_a_user_owned_item_with_needs_closes_the_turn(self, tmp_path, monkeypatch):
        # The over-fire guard: a real status report whose remaining work belongs to the owner
        # is terminal, and the reply says who is being waited on.
        from agent.turn_final_response_contract import apply_turn_contract

        agent = self._agent(tmp_path, monkeypatch)
        end_turn_tool(
            status="blocked", summary="Console parked.",
            open_items=[{"item": "relaunch the loader", "owner": "user", "needs": "you relaunch the compat loader"}],
            agent=agent,
        )
        verdict = apply_turn_contract(
            agent, messages=self._rows(), final_msg={"role": "assistant", "content": ""}, tool_calls_this_turn=1,
        )
        assert verdict.decision == CLOSE
        reply = verdict.final_response or ""
        assert "Waiting on you:" in reply
        assert "- relaunch the loader (needs: you relaunch the compat loader)" in reply

    def test_an_idle_re_close_spends_the_rejection_budget(self, tmp_path, monkeypatch):
        # Bounced to act, closed again with zero tool calls: that is a refusal, not a
        # continuation. It must not buy another free re-entry.
        from agent import end_turn as et
        from agent.turn_final_response_contract import apply_turn_contract

        agent = self._agent(tmp_path, monkeypatch, continuations=1)
        end_turn_tool(status="deferred", summary="Still thinking.", open_items=["audit the parser"], agent=agent)
        messages = self._rows()
        verdict = apply_turn_contract(
            agent, messages=messages, final_msg={"role": "assistant", "content": ""}, tool_calls_this_turn=0,
        )
        assert verdict.decision == IDLE and verdict.reprompt is True
        assert agent._turn_contract_rejections == 1
        assert "without running a single tool" in self._nudge(messages)
        assert et.read_turn_contract_stats() == {"idle_continuation": 1}

    def test_a_second_idle_re_close_escapes_and_is_delivered(self, tmp_path, monkeypatch):
        from agent.turn_final_response_contract import apply_turn_contract

        agent = self._agent(tmp_path, monkeypatch, continuations=1,
                            rejections=MAX_TURN_CONTRACT_REJECTIONS)
        end_turn_tool(status="deferred", summary="Still thinking.", open_items=["audit the parser"], agent=agent)
        verdict = apply_turn_contract(
            agent, messages=self._rows(), final_msg={"role": "assistant", "content": ""}, tool_calls_this_turn=0,
        )
        assert verdict.decision == ESCAPE and verdict.reprompt is False

    def test_a_user_owned_item_with_needs_closes_the_turn_even_as_done(self, tmp_path, monkeypatch):
        from agent.turn_final_response_contract import apply_turn_contract

        agent = self._agent(tmp_path, monkeypatch)
        end_turn_tool(status="done", summary="Where things stand.",
                      open_items=[{"item": "relaunch it", "owner": "user", "needs": "you"}], agent=agent)
        verdict = apply_turn_contract(
            agent, messages=self._rows(), final_msg={"role": "assistant", "content": ""}, tool_calls_this_turn=2,
        )
        assert verdict.decision == CLOSE

    def test_budget_exhaustion_delivers_with_an_honest_label(self, tmp_path, monkeypatch):
        from agent import end_turn as et
        from agent.turn_final_response_contract import apply_turn_contract

        agent = self._agent(tmp_path, monkeypatch, continuations=MAX_TURN_CONTINUATIONS)
        end_turn_tool(status="deferred", summary="Still mid-way.", open_items=["item one"], agent=agent)
        verdict = apply_turn_contract(
            agent, messages=self._rows(), final_msg={"role": "assistant", "content": ""}, tool_calls_this_turn=1,
        )
        assert verdict.decision == CONTINUE_EXHAUSTED and verdict.reprompt is False
        reply = verdict.final_response or ""
        assert reply.startswith("Still mid-way.")
        assert "Open (turn budget exhausted — still undone):" in reply
        assert "- item one" in reply
        assert et.read_turn_contract_stats() == {"deferred_delivered": 1}

    def test_the_ceiling_carries_agent_items_into_the_todo_list(self, tmp_path, monkeypatch):
        # The ceiling is a runaway stop, not permission to drop the queue: the remaining
        # agent-owned items must survive the turn.
        from tools.todo_tool import TodoStore

        from agent.turn_final_response_contract import apply_turn_contract

        agent = self._agent(tmp_path, monkeypatch, continuations=MAX_TURN_CONTINUATIONS)
        agent._todo_store = TodoStore()
        agent.session_id = "sess-abc123"
        end_turn_tool(status="deferred", summary="Mid-way.", open_items=["audit the parser"], agent=agent)
        apply_turn_contract(
            agent, messages=self._rows(), final_msg={"role": "assistant", "content": ""}, tool_calls_this_turn=1,
        )
        items = agent._todo_store.read()
        assert len(items) == 1
        assert "audit the parser" in items[0]["content"]
        assert items[0]["status"] == "pending"

    def test_budget_zero_restores_the_old_deliver_anything_behaviour(self, tmp_path, monkeypatch):
        from agent.turn_final_response_contract import apply_turn_contract

        agent = self._agent(tmp_path, monkeypatch, budget=0)
        end_turn_tool(status="deferred", summary="Half done.", open_items=["rest"], agent=agent)
        verdict = apply_turn_contract(
            agent, messages=self._rows(), final_msg={"role": "assistant", "content": ""}, tool_calls_this_turn=1,
        )
        assert verdict.decision == CONTINUE_EXHAUSTED

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
