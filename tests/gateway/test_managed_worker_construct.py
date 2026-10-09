"""Managed execution gets the same owner-resolved agent-construction inputs as the in-process
turn (``construct_v1``): the configured fallback chain (andrexibiza N3).

One ordinary daemon, two sessions: in-process and ``gateway.managed_workers``. The loopback
primary refuses with 429 ``insufficient_quota``; a healthy loopback backup is configured in
``fallback_providers``. Both turns must reach the backup and complete.
"""
import asyncio
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from tests.gateway.fixtures.local_recovery_probe import child_env, daemon, rpc, websocket


def _reply(handler, body, text):
    choice = {'index': 0, 'delta': {'role': 'assistant', 'content': text}, 'finish_reason': 'stop'}
    frame = {'id': 'c', 'model': body.get('model'), 'choices': [choice],
             'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}
    if body.get('stream'):
        payload, kind = ('data: ' + json.dumps(frame) + '\n\ndata: [DONE]\n\n').encode(), 'text/event-stream'
    else:
        choice['message'] = choice.pop('delta')
        payload, kind = json.dumps(frame).encode(), 'application/json'
    handler.send_response(200)
    handler.send_header('Content-Type', kind)
    handler.send_header('Content-Length', str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


class Primary(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if not body.get('messages'):
            return _reply(self, body, 'meta')
        self.server.requests.append(body)
        if self.server.refuse:
            data = json.dumps({'error': {'message': 'You exceeded your current quota', 'type': 'insufficient_quota',
                                         'code': 'insufficient_quota'}}).encode()
            self.send_response(429)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        _reply(self, body, 'PRIMARY_OK')


class Backup(Primary):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if body.get('messages'):
            self.server.requests.append(body)
        _reply(self, body, 'BACKUP_OK')


def _serve(cls):
    server = ThreadingHTTPServer(('127.0.0.1', 0), cls)
    server.requests, server.refuse = [], False
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def run_pair(tmp_path, extra_config, turn):
    """Boot one ordinary daemon; create an in-process and a managed session from the same config;
    run ``turn(ws, sid, name, settle, servers)`` for each and return its results by session."""
    root = Path(__file__).resolve().parents[2]
    home, user = tmp_path / 'state', tmp_path / 'user'
    home.mkdir(mode=0o700)
    user.mkdir()
    primary, backup = _serve(Primary), _serve(Backup)
    purl, burl = (f'http://127.0.0.1:{s.server_port}/v1' for s in (primary, backup))
    config = {'gateway': {'multiplex_profiles': False, 'managed_workers': False},
              'model': {'provider': 'custom', 'default': 'primary-model', 'base_url': purl},
              'auxiliary': {'title_generation': {'enabled': False}}, 'platform_toolsets': {'cli': []},
              'fallback_providers': [{'provider': 'custom', 'model': 'backup-model', 'base_url': burl,
                                      'api_key': 'loopback-only'}], **extra_config(home)}
    (home / 'config.yaml').write_text(json.dumps(config))
    env = {**child_env(), **{k: os.environ[k] for k in ('TIRITH_ENABLED',) if k in os.environ}}
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), PYTHONPATH=str(root),
               OPENAI_API_KEY='loopback-only', OPENAI_BASE_URL=purl)

    def query(sql, args=()):
        with closing(sqlite3.connect(f'file:{home / "state.db"}?mode=ro', uri=True)) as db:
            return db.execute(sql, args).fetchall()

    async def settle(ws, sid, request_id, text):
        submitted = await rpc(ws, 'prompt.submit', session_id=sid, input_id=request_id, text=text)
        assert 'result' in submitted, submitted
        async with asyncio.timeout(120):
            while query('SELECT status FROM session_admissions WHERE request_id=?', (request_id,)) != [('terminal',)]:
                await asyncio.sleep(.05)
        [(admission,)] = query('SELECT admission_id FROM session_admissions WHERE request_id=?', (request_id,))
        receipt = await rpc(ws, 'prompt.receipt', session_id=sid, admission_id=admission, include_result=True)
        return receipt['result']['result']

    results = {}

    async def exercise(desc):
        async with websocket(home, desc) as ws:
            for name in ('inproc', 'managed'):
                if name == 'managed':
                    edited = json.loads((home / 'config.yaml').read_text())
                    edited['gateway']['managed_workers'] = True
                    (home / 'config.yaml').write_text(json.dumps(edited))
                created = await rpc(ws, 'session.create', request_id=name, source='cli', cwd=str(home),
                                    model='primary-model', provider='custom', base_url=purl,
                                    api_key='loopback-only', toolsets=[])
                assert 'result' in created, created
                sid = created['result']['session_id']
                results[name] = await turn(ws, sid, name, settle, SimpleNamespace(primary=primary, backup=backup))
                results[name]['workers'] = query('SELECT COUNT(*) FROM worker_executions WHERE session_id=?', (sid,))[0][0]
    try:
        with daemon(root, home, env, barrier=False) as (_owner, desc):
            asyncio.run(exercise(desc))
    finally:
        for server in (primary, backup):
            server.shutdown()
            server.server_close()
    return results


@pytest.mark.platforms("linux")
def test_managed_turn_falls_back_like_the_in_process_turn(tmp_path):
    async def turn(ws, sid, name, settle, servers):
        servers.primary.refuse = True
        start = len(servers.backup.requests)
        result = await settle(ws, sid, name + '-refused', 'NEED_FALLBACK')
        return {'result': {k: result.get(k) for k in ('final_response', 'failed', 'completed', 'model')},
                'backup_models': {r.get('model') for r in servers.backup.requests[start:]}}
    results = run_pair(tmp_path, lambda home: {}, turn)
    expected = {'final_response': 'BACKUP_OK', 'failed': False, 'completed': True, 'model': 'backup-model'}
    for name in ('inproc', 'managed'):
        assert results[name]['result'] == expected, (name, results)
        assert results[name]['backup_models'] == {'backup-model'}, results
    assert (results['inproc']['workers'], results['managed']['workers']) == (0, 1), results


def test_construct_inputs_follow_the_session_mode(tmp_path, monkeypatch):
    """Ordinary: the owner's per-turn profile refresh; config-only: the frozen snapshot, never the
    profile; safe mode: no chain. The child accepts only the closed shape."""
    from gateway.session_local import _bypass_policy
    from gateway.session_policy import build_policy
    from gateway.session_worker_construct import construct_inputs, construct_kwargs
    chain = [{'provider': 'custom', 'model': 'b', 'base_url': 'http://127.0.0.1:9/v1'}]
    refreshed = []
    runner = SimpleNamespace(_refresh_fallback_model=lambda: refreshed.append(1) or chain, config=None)
    authority = SimpleNamespace(runner=runner, profile_id=str(tmp_path))
    launch = {'cwd': str(tmp_path), 'model': 'm', 'provider': 'custom', 'base_url': 'http://127.0.0.1:9/v1'}
    ordinary = build_policy(launch, {'fallback_providers': [{'provider': 'custom', 'model': 'frozen'}]})
    config_only = _bypass_policy({**launch, 'ignore_user_config': True}, private_secrets={})
    safe = _bypass_policy({**launch, 'safe_mode': True}, private_secrets={})
    assert construct_inputs(authority, ordinary)['fallback_model'] == chain and refreshed == [1]
    assert construct_inputs(authority, config_only)['fallback_model'] is None
    assert construct_inputs(authority, safe)['fallback_model'] is None and refreshed == [1]
    request = {'construct_v1': construct_inputs(authority, ordinary)}
    frame = {'policy': {'request_json': json.dumps(request)}}
    assert construct_kwargs(frame)['fallback_model'] == chain
    assert construct_kwargs({'policy': {'request_json': '{}'}}) == {}
    for bad in ({'fallback_model': 'x'}, {'fallback_model': None, 'extra': 1}, []):
        with pytest.raises(ValueError):
            construct_kwargs({'policy': {'request_json': json.dumps({'construct_v1': bad})}})
