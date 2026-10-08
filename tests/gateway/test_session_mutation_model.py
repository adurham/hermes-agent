"""Explicit model changes cross the real provider wire, not profile defaults."""
import asyncio
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import threading

import pytest
from tests.gateway.fixtures.local_recovery_probe import Model, daemon, rpc, websocket


class CatalogModel(Model):
    def do_GET(self):
        payload = json.dumps({'data': [{'id': name} for name in ('original', 'switched')]}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.mark.platforms("linux")
def test_model_receipt_changes_next_wire_and_branch_keeps_independent_history(tmp_path):
    root = Path(__file__).resolve().parents[2]
    home, user = tmp_path / 'state', tmp_path / 'user'
    home.mkdir(mode=0o700)
    user.mkdir()
    peer = ThreadingHTTPServer(('127.0.0.1', 0), CatalogModel)
    peer.requests = []
    peer.blocked, peer.release = threading.Event(), threading.Event()
    thread = threading.Thread(target=peer.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{peer.server_port}/v1'
    cfg = {'gateway': {'multiplex_profiles': False},
           'model': {'provider': 'custom', 'default': 'original', 'base_url': url},
           'auxiliary': {'title_generation': {'enabled': False}}, 'platform_toolsets': {'cli': []}}
    (home / 'config.yaml').write_text(json.dumps(cfg))
    (home / 'models_dev_cache.json').write_text(json.dumps({'custom': {'id': 'custom', 'models': {
        name: {'id': name, 'name': name, 'limit': {'context': 100000, 'output': 4000}}
        for name in ('original', 'switched')}}}))
    env = {k: os.environ[k] for k in ('PATH', 'LANG', 'TZ', 'TIRITH_ENABLED') if k in os.environ}
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), PYTHONPATH=str(root),
               OPENAI_API_KEY='loopback-only', OPENAI_BASE_URL=url, PYTHONUNBUFFERED='1')

    async def run(desc):
        async with websocket(home, desc) as ws:
            async def call(method, **params):
                response = await rpc(ws, method, **params)
                assert 'result' in response, response
                return response['result']
            async def turn(sid, text):
                await call('prompt.submit', session_id=sid, input_id=text, text=text)
                async with asyncio.timeout(30):
                    while True:
                        with sqlite3.connect(home / 'state.db') as db:
                            status = db.execute('SELECT status FROM session_admissions WHERE request_id=?', (text,)).fetchone()
                        if status and status[0] == 'terminal':
                            return await call('session.resume', session_id=sid)
                        await asyncio.sleep(.03)
            sid = (await call('session.create', source='cli', request_id='owner', toolsets=[]))['session_id']
            before = await turn(sid, 'ORIGINAL_HISTORY')
            params = dict(session_id=sid, request_id='switch', operation='model', payload={'model': 'switched'},
                expected_revision=before['revision'], expected_generation=before['execution_generation'])
            from hermes_cli.gateway_chat_view import GatewayChatView
            from acp_adapter.gateway_server import GatewayACPAgent
            class Client:
                async def rpc(self, method, **kwargs):
                    return await call(method, **kwargs)
            client = Client()
            view = GatewayChatView(client, before)
            await view.command('/model switched')
            result = await call('session.info', session_id=sid)
            assert result['model'] == 'switched'
            after = await turn(sid, 'AFTER_SWITCH')
            assert peer.requests[-1]['model'] == 'switched'
            assert peer.requests[0]['model'] == 'original'
            agent = GatewayACPAgent()
            agent._gateway = client
            agent._snapshots[sid] = after
            fork = await agent.fork_session(cwd=(await call('session.info', session_id=sid))['cwd'], session_id=sid)
            child = fork.session_id
            branched = await call('session.resume', session_id=child)
            assert branched['messages'] == after['messages']
            await turn(child, 'ONLY_BRANCH')
            parent = await call('session.resume', session_id=sid)
            assert 'ONLY_BRANCH' not in json.dumps(parent['messages'])
            assert peer.requests[-1]['model'] == 'switched'
            assert 'ORIGINAL_HISTORY' in json.dumps(peer.requests[-1]['messages'])
            return params, result
    try:
        with daemon(root, home, env, barrier=False) as (_, desc):
            params, result = asyncio.run(run(desc))
        async def retry(desc):
            async with websocket(home, desc) as ws:
                info = await rpc(ws, 'session.info', session_id=params['session_id'])
                assert info['result']['model'] == 'switched'
        with daemon(root, home, env, barrier=False) as (_, desc):
            asyncio.run(retry(desc))
    finally:
        peer.release.set()
        peer.shutdown()
        peer.server_close()
        thread.join(timeout=5)


@pytest.mark.asyncio
async def test_input_queued_while_a_model_receipt_commits_still_runs(tmp_path, monkeypatch):
    """A drain that runs while the model receipt commits off-loop sees the new stored policy beside
    the old live one and pauses; publishing the policy must wake it, or the input stays queued
    until another submit or a restart."""
    import time
    from types import SimpleNamespace
    from gateway import run
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import SessionAuthority
    from gateway.session_contract import Submission
    from gateway.session_controls import AuthorityConnection
    from gateway.session_local import create_local_session
    import hermes_cli.model_switch as model_switch
    import hermes_state_runtime as rt
    monkeypatch.setattr(run, '_load_gateway_config', lambda *a: {})
    monkeypatch.setattr(model_switch, 'switch_model', lambda **k: model_switch.ModelSwitchResult(
        success=True, new_model='switched', target_provider='custom', base_url='http://127.0.0.1:9/v1'))
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    db = store._db
    executed = []

    async def handle(event):
        executed.append(event.text)
        return 'ok'
    runner = SimpleNamespace(session_store=store, _session_db=db, adapters={}, _draining=False,
                             _evict_cached_agent=lambda route: None, _handle_message=handle,
                             _resolve_session_agent_runtime=lambda **k: ('frozen', {}))
    runner._adapter_for_source = lambda source: runner.adapters.get(source.platform)
    authority = SessionAuthority(runner, profile_id='owned', instance_id='owner', db=db,
                                 epoch=rt.begin_runtime_epoch(db, instance_id='owner'))
    owner = AuthorityConnection(authority, object(), {'user_id': 'human'})
    ref = create_local_session(authority, owner.actor, dict(request_id='m', source='cli', cwd=str(tmp_path),
                                                            model='frozen', toolsets=[]))
    # A contended receipt transaction: the commit thread holds it open while the loop runs.
    entered = threading.Event()
    db._conn.create_function('hold_receipt', 1, lambda s: (entered.set(), time.sleep(s))[1] or 0)
    db._execute_write(lambda c: c.execute("CREATE TRIGGER hold_receipt AFTER INSERT ON state_meta "
                                          "WHEN NEW.key LIKE 'gateway.mutation.v1.%' BEGIN SELECT hold_receipt(0.4); END"))
    snap = db.get_session(ref.session_id)
    try:
        mutation = asyncio.create_task(owner.dispatch({'id': 1, 'method': 'session.mutate', 'params': {
            'session_id': ref.session_id, 'request_id': 'switch', 'expected_revision': snap['runtime_revision'],
            'expected_generation': snap['runtime_generation'], 'operation': 'model', 'payload': {'model': 'switched'}}}))
        await asyncio.to_thread(entered.wait, 5)
        queued = await authority.submit(owner.actor, Submission('during-switch', ref, {'text': 'FOLLOWER'}, 'queue'))
        assert (await mutation)['result']['model'] == 'switched'
        async with asyncio.timeout(10):
            while rt.get_session_admission(db, admission_id=queued.admission_id)['status'] != 'terminal':
                await asyncio.sleep(0.05)
        assert executed == ['FOLLOWER']
    finally:
        await owner.close()
        store.close_all_db_handles()

