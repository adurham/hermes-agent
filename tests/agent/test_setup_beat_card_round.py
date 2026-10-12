"""A setup_beat round never ends in the empty-response nudge: the line written with the card is the
turn's reply when the follow-up comes back empty (agent/turn_tool_round.py ``_LINE_IS_REPLY_TOOLS``)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.agent_init import _CALLBACK_PARAMS
from agent.turn_empty_response import recover_empty_response
from agent.turn_tool_round import stage_tool_call_message
from run_agent import AIAgent


def _agent():
    agent = AIAgent.__new__(AIAgent)
    agent.provider, agent.model, agent.session_id, agent.tools = "nous", "m", "s1", []
    agent.log_prefix, agent.quiet_mode, agent.verbose_logging = "", True, False
    agent._mute_post_response, agent._base_url, agent.api_mode = False, "", "chat_completions"
    agent._current_streamed_assistant_text = ""
    agent._stream_callback = None
    for name in _CALLBACK_PARAMS:
        setattr(agent, name, None)
    return agent


def _call(name: str):
    return SimpleNamespace(id=f"call_{name}", type="function",
                           function=SimpleNamespace(name=name, arguments="{}"))


@pytest.mark.parametrize(("tool", "reused"), [("setup_beat", True), ("terminal", False)])
def test_an_empty_follow_up_after_a_card_round_keeps_the_cards_line(tool, reused):
    agent = _agent()
    messages = [{"role": "user", "content": "hi"}]
    staged, _ = stage_tool_call_message(
        agent, assistant_message=SimpleNamespace(content="Pick a colour whenever you're back.",
                                                 tool_calls=[_call(tool)], reasoning=None),
        finish_reason="tool_calls", messages=messages)
    messages += [staged, {"role": "tool", "tool_call_id": f"call_{tool}", "content": "{}"}]

    verdict = recover_empty_response(
        agent, SimpleNamespace(content="", tool_calls=None, reasoning=None), None, "stop", final_response="",
        messages=messages, api_messages=[], conversation_history=[], active_system_prompt="",
        api_call_count=2, turn_exit_reason=None, preflight_compression_blocked=False)

    if reused:
        assert verdict.action == "break" and verdict.final_response == "Pick a colour whenever you're back."
        assert verdict.turn_exit_reason == "fallback_prior_turn_content"
    else:
        assert verdict.final_response != "Pick a colour whenever you're back."
    assert agent._mute_post_response is False
