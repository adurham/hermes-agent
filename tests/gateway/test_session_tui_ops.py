"""Ink's /save, /reload, /agents pause and /rollback on the shared owner (main's sidecar parity)."""
import asyncio
import json
from pathlib import Path

import pytest

from tests.gateway.test_session_busy_controls import owner, settled
from tests.gateway.fixtures.local_recovery_probe import rpc, websocket


@pytest.mark.platforms("linux")
def test_session_save_exports_the_stored_transcript_under_the_profile_home(tmp_path):
    with owner(tmp_path) as (home, _peer, desc):
        async def probe():
            async with websocket(home, desc) as ws:
                sid = (await rpc(ws, 'session.create', request_id='save', source='tui', toolsets=[]))['result']['session_id']
                await rpc(ws, 'prompt.submit', session_id=sid, input_id='first', text='SAVE_ME')
                await settled(home)
                saved = await rpc(ws, 'session.save', session_id=sid)
                assert 'result' in saved, saved
                path = saved['result']['file']
                assert path.startswith(str(home / 'sessions' / 'saved' / 'hermes_conversation_')), path
                data = json.loads(await asyncio.to_thread(Path(path).read_text, encoding='utf-8'))
                contents = [m.get('content') for m in data['messages']]
                assert 'SAVE_ME' in contents and 'CONTROL_ACK' in contents, data
                for params, reason in [({'session_id': sid, 'format': 'md'}, 'invalid_params'),
                                       ({'session_id': sid, 'profile': 'foreign'}, 'profile_mismatch'),
                                       ({}, 'invalid_params')]:
                    assert (await rpc(ws, 'session.save', **params))['error']['message'] == reason
        asyncio.run(probe())


@pytest.mark.platforms("linux")
def test_reload_env_rereads_the_profile_dotenv_for_a_controlled_session(tmp_path):
    with owner(tmp_path) as (home, _peer, desc):
        async def probe():
            async with websocket(home, desc) as ws:
                sid = (await rpc(ws, 'session.create', request_id='reload', source='tui', toolsets=[]))['result']['session_id']
                env = home / '.env'
                await asyncio.to_thread(env.write_text, 'TUI_RELOAD_PROBE=one\n')
                first = await rpc(ws, 'reload.env', session_id=sid)
                assert first.get('result', {}).get('updated', 0) >= 1, first
                assert (await rpc(ws, 'reload.env', session_id=sid))['result'] == {'updated': 0}
                await asyncio.to_thread(env.write_text, 'TUI_RELOAD_PROBE=two\n')
                assert (await rpc(ws, 'reload.env', session_id=sid))['result'] == {'updated': 1}
                # A bare frame never rewrites the shared owner's environment.
                for params, reason in [({}, 'invalid_params'), ({'session_id': sid, 'profile': 'foreign'}, 'profile_mismatch'),
                                       ({'session_id': sid, 'force': True}, 'invalid_params')]:
                    assert (await rpc(ws, 'reload.env', **params))['error']['message'] == reason
        asyncio.run(probe())
