"""Tests for the explicit turn-close contract (agent/end_turn.py).

The contract replaces the promise gate: a turn that used a tool may not end on prose. These
tests pin the decision table and the payload->reply rendering; the loop wiring is exercised by
the live one-shot in FORK.md.
"""

from agent.end_turn import (
    CLOSE,
    END_TURN_SCHEMA,
    ESCAPE,
    MAX_TURN_CONTRACT_REJECTIONS,
    PASS,
    REJECT,
    TURN_STATUSES,
    clear_end_turn,
    contract_decision,
    end_turn_payload,
    end_turn_tool,
    ensure_end_turn_schema,
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
        assert "description" in fn and fn["description"]

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

    def test_empty_summary_is_refused(self):
        agent = _Agent()
        out = end_turn_tool(status="done", summary="   ", agent=agent)
        assert '"accepted": false' in out

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

    def test_every_decision_is_covered(self):
        seen = {
            contract_decision(payload=None, tool_calls_this_turn=0, rejections=0),
            contract_decision(payload=None, tool_calls_this_turn=1, rejections=0),
            contract_decision(payload=None, tool_calls_this_turn=1, rejections=99),
            contract_decision(payload={}, tool_calls_this_turn=1, rejections=0),
        }
        assert seen == {PASS, REJECT, ESCAPE, CLOSE}


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
