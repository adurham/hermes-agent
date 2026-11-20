"""Thinking-only responses are re-prompted with an escalating user-level steer.

The thinking-only prefill stub is dropped from the API copy on every route, so before the steer a
model stuck deliberating (reasoning, no text, no tool call) got a byte-identical request on each
recovery attempt and repeated itself until the ladder gave up with "(empty)".
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent import turn_empty_response as ter
from agent.conversation_loop import _EMPTY_TOOL_RESPONSE_NUDGE, _THINKING_ONLY_STEERS


def _tool_defs():
    return [{
        "type": "function",
        "function": {"name": "web_search", "description": "x",
                     "parameters": {"type": "object", "properties": {}}},
    }]


@pytest.fixture()
def loop_agent():
    from run_agent import AIAgent
    with (
        patch("model_tools.get_tool_definitions", return_value=_tool_defs()),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        agent.client = MagicMock()
        agent._cached_system_prompt = "You are helpful."
        agent._use_prompt_caching = False
        agent.tool_delay = 0
        agent.compression_enabled = False
        agent.save_trajectories = False
        return agent


def _thinking_only():
    from tests.agent.test_run_agent import _mock_response
    return _mock_response(content="", finish_reason="tool_calls", reasoning="Weighing option A against B.")


def _tool_call_response():
    from tests.agent.test_run_agent import _mock_response, _mock_tool_call
    return _mock_response(
        content="", finish_reason="tool_calls",
        tool_calls=[_mock_tool_call(name="web_search", arguments="{}", call_id="c1")],
    )


def _final(text="All done."):
    from tests.agent.test_run_agent import _mock_response
    return _mock_response(content=text, finish_reason="stop")


def _wire(create_mock, idx):
    call = create_mock.call_args_list[idx]
    return call.kwargs.get("messages") or call.args[0].get("messages")


def _text(msg):
    content = msg.get("content")
    if isinstance(content, list):
        return "\n".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return content or ""


def _run(agent, responses):
    agent.client.chat.completions.create.side_effect = responses
    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("model_tools.handle_function_call", return_value="ok"),
    ):
        return agent.run_conversation("do the thing")


def test_each_thinking_only_retry_sends_a_different_request(loop_agent):
    """Before the fix retries 2 and 3 were byte-identical to the request that already failed."""
    result = _run(loop_agent, [
        _tool_call_response(),   # tool round
        _thinking_only(),        # -> post-tool nudge
        _thinking_only(),        # -> steer 1
        _thinking_only(),        # -> steer 2
        _final(),
    ])
    create = loop_agent.client.chat.completions.create
    assert create.call_count == 5
    # Requests: 0 initial, 1 after the tool round, 2 after the nudge, 3 after steer 1, 4 after steer 2.
    nudged, steered_1, steered_2 = (_wire(create, i) for i in (2, 3, 4))

    assert _EMPTY_TOOL_RESPONSE_NUDGE in _text(nudged[-1])
    assert _THINKING_ONLY_STEERS[0] not in _text(nudged[-1])
    assert _THINKING_ONLY_STEERS[0] in _text(steered_1[-1])
    assert _THINKING_ONLY_STEERS[1] not in _text(steered_1[-1])
    assert _THINKING_ONLY_STEERS[1] in _text(steered_2[-1])
    assert _text(nudged[-1]) != _text(steered_1[-1]) != _text(steered_2[-1])
    # The steer rides the canonical tail row as a request-local suffix: the wire keeps the
    # same row sequence on every recovery attempt and never gains a synthetic user/assistant row.
    for w in (nudged, steered_1, steered_2):
        assert [m["role"] for m in w] == [m["role"] for m in nudged]
        assert w[-1]["role"] == "tool"
    assert result["final_response"] == "All done."


def test_steer_scaffolding_never_reaches_the_final_transcript(loop_agent):
    result = _run(loop_agent, [
        _tool_call_response(), _thinking_only(), _thinking_only(), _final(),
    ])
    blob = "\n".join(_text(m) for m in result["messages"])
    for steer in _THINKING_ONLY_STEERS:
        assert steer not in blob
    assert not any(m.get("_thinking_steer") or m.get("_thinking_prefill") for m in result["messages"])
    assert result["final_response"] == "All done."


def test_tool_call_after_steer_pops_the_scaffolding(loop_agent):
    """Recovering with a tool call resets the ladder and the steer rows leave history."""
    result = _run(loop_agent, [
        _tool_call_response(),   # tool round
        _thinking_only(),        # -> nudge
        _thinking_only(),        # -> steer 1
        _tool_call_response(),   # model acts
        _final(),
    ])
    create = loop_agent.client.chat.completions.create
    assert create.call_count == 5
    last_wire = _wire(create, 4)
    assert all(_THINKING_ONLY_STEERS[0] not in _text(m) for m in last_wire)
    assert last_wire[-1]["role"] == "tool"
    assert not any(m.get("_thinking_steer") or m.get("_thinking_prefill") for m in result["messages"])
    assert loop_agent._thinking_prefill_retries == 0


def _ladder_agent():
    a = MagicMock()
    a._current_streamed_assistant_text = ""
    a._has_content_after_think_block.return_value = False
    a._strip_think_blocks.side_effect = lambda t: t or ""
    a._last_content_with_tools = None
    a._thinking_prefill_retries = 0
    a._post_tool_empty_retried = True  # post-tool nudge already spent
    a._empty_content_retries = 0
    a._fallback_chain = []
    a._build_assistant_message.side_effect = lambda m, fr: {
        "role": "assistant", "content": "", "reasoning": getattr(m, "reasoning", None),
    }
    return a


def _recover(agent, messages):
    msg = SimpleNamespace(reasoning="still deciding", reasoning_content=None, reasoning_details=None)
    with patch("agent.conversation_loop._sync_failover_system_message", return_value="sys"):
        return ter.recover_empty_response(
            agent, msg, None, "tool_calls", final_response="", messages=messages,
            api_messages=[], conversation_history=[], active_system_prompt="sys", api_call_count=3,
            turn_exit_reason=None, preflight_compression_blocked=False,
        )


def test_steer_after_a_tool_result_rides_the_request_not_the_transcript():
    """A tool -> user boundary is rejected by strict APIs and the reasoning stub is dropped from
    the API copy, so the steer rides the outgoing tail as a suffix instead of a synthetic row."""
    from agent.agent_runtime_helpers import drop_thinking_only_and_merge_users
    from agent.turn_empty_response import add_empty_response_retry_hint

    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "web_search", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ]
    canonical = [dict(m) for m in messages]
    agent = _ladder_agent()
    with patch("agent.agent_runtime_helpers._ra") as ra:
        ra.return_value.AIAgent._is_thinking_only_assistant = staticmethod(
            lambda m, **kw: bool(m.get("_thinking_prefill"))
        )
        verdict = _recover(agent, messages)
        wire = [dict(m) for m in messages]
        suffix = add_empty_response_retry_hint(agent, wire)
        api_copy = drop_thinking_only_and_merge_users([dict(m) for m in wire])

    assert verdict.action == "continue"
    # The transcript itself is untouched: no synthetic assistant row, no stored steer row.
    assert messages == canonical
    # The outgoing copy gains one text suffix, never a new row.
    assert suffix and _THINKING_ONLY_STEERS[0] in suffix
    assert [m["role"] for m in wire] == ["user", "assistant", "tool"]
    assert wire[-1]["content"].endswith(_THINKING_ONLY_STEERS[0])
    # And the send-time sanitizer leaves the alternation alone: tool -> tool is fine, tool -> user is not.
    assert [m["role"] for m in api_copy] == ["user", "assistant", "tool"]


def test_steer_text_escalates_and_stops_at_the_last_entry():
    agent = _ladder_agent()
    messages = [{"role": "user", "content": "go"}]
    hints = []
    for _ in _THINKING_ONLY_STEERS:
        _recover(agent, messages)
        hints.append(agent._empty_response_retry_hint)
    assert _THINKING_ONLY_STEERS[0] in hints[0]
    assert _THINKING_ONLY_STEERS[1] in hints[1]
    assert hints[0] != hints[1]
    # The ladder never asks for an entry past the last one.
    assert [s for s in _THINKING_ONLY_STEERS if s in hints[-1]] == [_THINKING_ONLY_STEERS[-1]]
    assert agent._thinking_prefill_retries == len(_THINKING_ONLY_STEERS)
    # Nothing was written into the transcript along the way.
    assert messages == [{"role": "user", "content": "go"}]
