"""Queued-follow-up lane: the fallback first-response send and the normal completion send share one
"already delivered" source of truth (#81052).

With streaming disabled (the shipped default) there is never a stream consumer, so the queued
lane always sends the first turn's final itself before running the follow-up. When the lane then
hands the FIRST turn's result back (the follow-up's text was refused), the normal completion path
must see ``already_sent`` and not send that text a second time.
"""

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType, SessionSource
from gateway.run import GatewayRunner
from tests.gateway.test_run_progress_topics import ProgressCaptureAdapter, _make_runner, _run_with_agent


class _FirstTurnAgent:
    calls = 0

    def __init__(self, **kwargs):
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None, **kwargs):
        type(self).calls += 1
        return {"final_response": f"answer {type(self).calls}", "messages": [], "api_calls": 1}


@pytest.mark.asyncio
async def test_refused_followup_returns_first_result_marked_delivered(monkeypatch, tmp_path):
    """Follow-up text refused after the fallback send: the returned first-turn result carries
    ``already_sent`` so the caller's normal send is suppressed, and the text went out exactly once."""
    _FirstTurnAgent.calls = 0
    monkeypatch.setattr(
        GatewayRunner, "_expand_inbound_context_references",
        lambda self, source, session_key, message_text: _none(),
    )
    adapter, result = await _run_with_agent(
        monkeypatch, tmp_path, _FirstTurnAgent, session_id="sess-refused-followup",
        pending_text="@file:/etc/shadow please",
    )
    assert _FirstTurnAgent.calls == 1
    assert [c["content"] for c in adapter.sent] == ["answer 1"]
    assert result["final_response"] == "answer 1"
    assert result.get("already_sent") is True


@pytest.mark.asyncio
async def test_completion_path_skips_body_the_queued_lane_delivered(monkeypatch, tmp_path):
    """Control at the completion seam: a result the queued lane marked delivered is not re-sent,
    while an unmarked result still is."""
    adapter = ProgressCaptureAdapter(platform=Platform.TELEGRAM)
    runner = _make_runner(adapter)
    source = SessionSource(platform=adapter.platform, chat_id="-1001", chat_type="group", user_id="u1")
    event = MessageEvent(text="hi", message_type=MessageType.TEXT, source=source, message_id="1")

    class _Entry:
        session_id = "s1"

    async def _noop_media(*a, **k):
        return None

    monkeypatch.setattr(runner, "_deliver_media_from_response", _noop_media)
    delivered = await runner._hmwa_deliver_turn_response(
        event, source, _Entry(), "agent:main:telegram:group:-1001", None,
        {"final_response": "answer 1", "already_sent": True}, [], "answer 1", None, False,
    )
    assert delivered is None
    not_marked = await runner._hmwa_deliver_turn_response(
        event, source, _Entry(), "agent:main:telegram:group:-1001", None,
        {"final_response": "answer 1"}, [], "answer 1", None, False,
    )
    assert not_marked == "answer 1"


async def _none():
    return None
