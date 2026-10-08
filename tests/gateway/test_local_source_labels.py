"""A canonical local session keeps the source label it was created with, and human pickers hide the
non-conversation labels (``tool`` integrations) as on the classic surfaces."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.session_controls import AuthorityConnection


async def _authority(tmp_path, monkeypatch):
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import initialize_session_authority
    import gateway.run as run

    monkeypatch.setattr(run, '_load_gateway_config', lambda: {'model': {'default': 'fixture'}, 'platform_toolsets': {'cli': []}})
    monkeypatch.setattr(run, '_resolve_gateway_model', lambda config: 'fixture')
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    runner = run.GatewayRunner.__new__(run.GatewayRunner)
    runner.adapters, runner.session_store, runner._session_db, runner._draining = {}, store, store._db, False
    authority = await initialize_session_authority(runner, profile_id=str(Path(store._db.db_path).parent), instance_id='test')
    authority._schedule = lambda ref: None
    return authority, store


async def _create(conn, authority, store, source, request_id):
    conn_cwd = Path(authority.db.db_path).parent
    created = await conn.dispatch({'id': request_id, 'method': 'session.create',
                                   'params': {'request_id': request_id, 'source': source, 'cwd': str(conn_cwd)}})
    assert 'result' in created, created
    sid = created['result']['stored_session_id']
    live = authority.sessions[sid]
    # Every turn refreshes the routing peer; the creation label must survive it.
    store._record_gateway_session_peer(sid, live.route, live.source)
    return sid


@pytest.mark.asyncio
async def test_source_tool_is_admitted_stored_and_hidden_from_pickers(tmp_path, monkeypatch):
    authority, store = await _authority(tmp_path, monkeypatch)
    conn = AuthorityConnection(authority, SimpleNamespace(write=lambda frame: None), {'user_id': 'owner'})
    described = await conn.dispatch({'id': 0, 'method': 'runtime.describe', 'params': {}})
    assert 'tool' in described['result']['session_create']['sources']
    chat = await _create(conn, authority, store, 'cli', 'chat')
    tool = await _create(conn, authority, store, 'tool', 'integration')
    assert authority.db.get_session(tool)['source'] == 'tool'
    assert authority.sessions[tool].source.platform.value == 'local'
    listed = await conn.dispatch({'id': 9, 'method': 'session.list', 'params': {}})
    assert [row['id'] for row in listed['result']['sessions']] == [chat]
    await conn.close()
    authority.db.close()

