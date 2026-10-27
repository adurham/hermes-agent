"""What an old conversation started keeps landing in it after its chat moved on (re-review of #144840).

Teknium's ruling: nothing a conversation the chat moved past (A) produces reaches the chat, and A stays
fully usable from the apps, its reminders and background results included. Real GatewayRunner,
DiscordAdapter (``send`` recorded), SessionAuthority, drain and SessionStore over a real SessionDB; the
agent run is the stand-in of ``test_authority_lane_moves_on``.
"""
import asyncio
from pathlib import Path

import pytest

from tests.gateway.test_authority_lane_moves_on import _chat
from tests.gateway.test_detached_conversation_lifecycle import _moved_on
from tests.waiting import async_wait_until


def _noted(authority, session_id, text):
    return any(text in str(message.get('content')) for message in authority.db.get_messages(session_id))


@pytest.mark.asyncio
async def test_a_reminder_the_app_set_on_the_old_conversation_fires_after_a_resume_and_after_a_restart(monkeypatch):
    """N1: the reminder names A (``local:<A>``). Back on the chat (its ``/resume``) A's fire is A's own
    turn there; with A not mounted (any restart) it mounts off the chat and is written into A. It is
    never refused for a local receipt A never had, so a recurring reminder keeps firing."""
    from gateway.session_cron import deliver_to_local_chat
    from gateway.session_mutations import retire_live_sessions
    _runner, adapter, authority, source, sent, ran, _gates, lane, ref_a = await _moved_on(monkeypatch)
    a, home = ref_a.session_id, Path(authority.db.db_path).parent

    await _chat(adapter, source, sent, '/resume First chat')
    assert await asyncio.to_thread(deliver_to_local_chat, home, a, 'fire after resume') is None
    await async_wait_until(lambda: f"reply to 'fire after resume' from {a}" in sent, timeout=10, what="A's turn in the chat")
    assert [turn[1:] for turn in ran if turn[0] == 'fire after resume'] == [(a, lane)]

    await _chat(adapter, source, sent, '/new')
    retire_live_sessions(authority, [a])  # what a restart leaves: A not mounted
    chat_before = len(sent)
    assert await asyncio.to_thread(deliver_to_local_chat, home, a, 'fire after restart') is None
    await async_wait_until(lambda: _noted(authority, a, 'fire after restart'), timeout=10, what='the note in A')
    assert authority.sessions[a].detached and sent[chat_before:] == []


@pytest.mark.asyncio
async def test_a_note_into_the_old_conversation_leaves_the_row_the_chats_new_ended_closed(monkeypatch):
    """N2a: a reminder written into A as a note (no viewer, no turn) keeps the ``/new``'s end stamp, as
    a note into a closed local session does; only a turn reopens the row."""
    from gateway.session_cron import deliver_to_local_chat
    _runner, _adapter, authority, _source, _sent, ran, _gates, _lane, ref_a = await _moved_on(monkeypatch)
    a = ref_a.session_id
    ran_before = len(ran)
    assert await asyncio.to_thread(deliver_to_local_chat, Path(authority.db.db_path).parent, a, 'a reminder') is None
    await async_wait_until(lambda: _noted(authority, a, 'a reminder'), timeout=10, what='the note in A')
    await asyncio.wait_for(authority.sessions[a].task, 10)
    assert ran[ran_before:] == []
    assert authority.db.get_session(a)['end_reason'] == 'session_reset'
