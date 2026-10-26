"""The lifecycle of a conversation its chat moved past (review of #144840 at 03d04918dde).

Teknium's ruling: once a messaging chat names another conversation, nothing the old one produces
reaches the chat, and the old one stays fully usable from the apps. Real GatewayRunner, DiscordAdapter
(``send`` recorded), SessionAuthority, drain and SessionStore over a real SessionDB; the agent run is
the stand-in of ``test_authority_lane_moves_on``.
"""
import pytest

from tests.gateway.test_authority_lane_moves_on import _app_turn, _chat, _gateway
from tests.waiting import async_wait_until


@pytest.mark.asyncio
async def test_the_chats_context_exhaustion_reset_moves_the_old_conversation_off_the_chat(monkeypatch):
    """F1: the auto-reset after context exhaustion rotates the chat to B without a /new. The app's
    next prompt on A runs in A, and the chat gets nothing of it."""
    from gateway.session_results import execution_result
    runner, adapter, authority, source, sent, ran, _gates = await _gateway(monkeypatch)
    lane = runner.session_store._generate_session_key(source)
    await _chat(adapter, source, sent, 'hello')
    ref_a = authority.register(source)
    prepare = runner._hmwa_prepare_turn

    async def exhausting(event, source, session_entry, session_key, *args):
        if event.text != 'too big':
            return await prepare(event, source, session_entry, session_key, *args)
        execution_result.get()['result'] = {'final_response': 'Context too large', 'messages': []}
        entry = await runner._hmwa_rotate_exhausted_session(session_entry, session_key, source)
        return 'Context too large', entry
    monkeypatch.setattr(runner, '_hmwa_prepare_turn', exhausting)
    await _chat(adapter, source, sent, 'too big')
    await async_wait_until(lambda: runner.session_store.peek_session_id(lane) != ref_a.session_id,
                           what='the route to rotate')
    chat_replies = len(sent)

    assert await _app_turn(authority, ref_a, 'app prompt on A') == ('terminal', 'completed')
    assert ran[-1][:2] == ('app prompt on A', ref_a.session_id) and ran[-1][2] != lane
    assert sent[chat_replies:] == [], f'the chat got the old conversation\'s reply: {sent[chat_replies:]}'
