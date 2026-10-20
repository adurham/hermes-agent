"""`hermes sessions adopt` brings a profile's pre-gateway chats under its gateway, as the host.

The Webapp's browser reaches the gateway through its dashboard mount as the login it authenticated
(on a loopback bind, the session-token owner), which may not make the first claim on an unowned
historical row (gateway-local-sessions.md, shared operator access). The host's own native client
adopts them in one pass, after which the browser login continues each.
"""
import argparse
import asyncio
import json

import pytest

from tests.gateway.fixtures.owner_process import running_owner

#: The owner's dashboard session token (``HERMES_DASHBOARD_SESSION_TOKEN``): what a loopback browser
#: login presents on the gateway's dashboard mount.
_BROWSER_TOKEN = 'b' * 43
_OWNER_ENV = {'HERMES_DASHBOARD_SESSION_TOKEN': _BROWSER_TOKEN}

_OLD_CHATS = (('old-desktop', 'desktop', None), ('old-desktop-nous', 'desktop', 'nous:account'),
              ('old-cli', 'cli', None), ('foreign-desktop', 'desktop', 'telegram:42'),
              ('api-row', 'api_server', None))


def _seed(home):
    from hermes_state import SessionDB
    db = SessionDB(db_path=home / 'state.db')
    try:
        for sid, source, user_id in _OLD_CHATS:
            db.create_session(sid, source=source, model='old-model', cwd=str(home), user_id=user_id)
            db.append_message(sid, role='user', content=sid.upper())
            db.end_session(sid, 'session_switch')
    finally:
        db.close()


def _adopt(capsys, *argv):
    from hermes_cli import sessions_cmd
    from hermes_cli.subcommands.sessions import build_sessions_parser
    parser = argparse.ArgumentParser()
    build_sessions_parser(parser.add_subparsers(dest='command'), cmd_sessions=sessions_cmd.cmd_sessions)
    args = parser.parse_args(['sessions', 'adopt', *argv])
    status = args.func(args)
    return status, capsys.readouterr().out


async def _rpc(socket, method, **params):
    await socket.send(json.dumps({'jsonrpc': '2.0', 'id': method, 'method': method, 'params': params}))
    async with asyncio.timeout(15):
        while True:
            reply = json.loads(await socket.recv())
            if reply.get('id') == method:
                return reply


async def _browser_resume(home, descriptor, sid):
    """What a browser opens on the gateway's own listener: ``/api/ws`` on its dashboard mount
    (``chat_ws.gateway_ws``), with an Origin, as the loopback session-token login; never a native grant."""
    from websockets.asyncio.client import connect
    from hermes_cli.web_server_boundary import _DESKTOP_ATTACHMENT_WS_MAX_BYTES
    origin = descriptor['api_origin']
    url = origin.replace('http:', 'ws:') + '/api/ws?token=' + _BROWSER_TOKEN
    async with connect(url, origin=origin, proxy=None, max_size=_DESKTOP_ATTACHMENT_WS_MAX_BYTES) as socket:
        return await _rpc(socket, 'session.resume', session_id=sid)


@pytest.mark.platforms("linux")
def test_adopting_old_chats_lets_the_browser_login_continue_them(tmp_path, model_peer, monkeypatch, capsys):
    with running_owner(tmp_path, model_peer, prepare=_seed, extra_env=_OWNER_ENV) as (home, descriptor, _env):
        monkeypatch.setenv('HERMES_HOME', str(home))
        assert asyncio.run(_browser_resume(home, descriptor, 'old-desktop'))['error']['message'] == 'not_found'

        status, out = _adopt(capsys, '--dry-run')
        assert status == 0 and '3 pre-gateway chat(s) to adopt (1 cli, 2 desktop)' in out, out
        assert asyncio.run(_browser_resume(home, descriptor, 'old-desktop'))['error']['message'] == 'not_found'

        status, out = _adopt(capsys)
        assert status == 0 and 'Adopted 3 chat(s).' in out, out
        for sid in ('old-desktop', 'old-desktop-nous', 'old-cli'):
            resumed = asyncio.run(_browser_resume(home, descriptor, sid))
            assert resumed.get('result', {}).get('session_id') == sid, resumed
            assert [m['content'] for m in resumed['result']['messages']] == [sid.upper()]
        # Another identity's row and a non-local row were never offered, so they stay as they were.
        assert asyncio.run(_browser_resume(home, descriptor, 'foreign-desktop'))['error']['message'] == 'not_found'

        status, out = _adopt(capsys)
        assert status == 0 and 'Nothing to adopt' in out, out


def _seed_long_and_moved(home):
    from hermes_state import SessionDB
    db = SessionDB(db_path=home / 'state.db')
    try:
        # The owner's resume reply always carries the whole history: this one is over the 8 MiB a
        # default native client accepts in one frame.
        db.create_session('long-desktop', source='desktop', model='old-model', cwd=str(home))
        db.append_message('long-desktop', role='user', content='x' * (9 * 1024 * 1024))
        db.create_session('moved-desktop', source='desktop', model='old-model', cwd=str(home / 'deleted-worktree'))
        db.append_message('moved-desktop', role='user', content='MOVED')
        for sid in ('long-desktop', 'moved-desktop'):
            db.end_session(sid, 'session_switch')
    finally:
        db.close()


@pytest.mark.platforms("linux")
def test_adopt_survives_a_long_history_and_names_what_the_owner_refused(tmp_path, model_peer, monkeypatch, capsys):
    with running_owner(tmp_path, model_peer, prepare=_seed_long_and_moved, extra_env=_OWNER_ENV) as (home, descriptor, _env):
        monkeypatch.setenv('HERMES_HOME', str(home))
        status, out = _adopt(capsys)
        assert status == 0 and 'Adopted 1 chat(s).' in out, out
        assert 'Skipped 1: invalid_params (cwd: must be an absolute path to an existing directory).' in out, out
        resumed = asyncio.run(_browser_resume(home, descriptor, 'long-desktop'))
        assert resumed.get('result', {}).get('session_id') == 'long-desktop', str(resumed)[:300]
