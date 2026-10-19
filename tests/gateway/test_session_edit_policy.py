"""ACP-7: an ACP session's edit-approval policy (Default / Accept Edits / Don't Ask) is owner data.

Main's in-process ACP kept the mode on its own session (``acp_adapter/server.py`` ``_MODES``). On the
shared owner it is a field of the session's frozen LocalSessionPolicy, written by a revision-fenced
``session.mutate edit_policy``, read back by every viewer's ``session.info``, restored after an owner
restart, and enforced where the edit runs (``gateway/session_local_editor.py``): sensitive paths ask.
"""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest


def _runner(store):
    runner = SimpleNamespace(session_store=store, _session_db=store._db, adapters={}, _draining=False,
                             _evict_cached_agent=lambda route: None, _cached_agent_for=lambda route: None,
                             _set_session_reasoning_override=lambda route, value: None,
                             _handle_message=None, _resolve_session_agent_runtime=lambda **k: ('frozen', {}))
    runner._adapter_for_source = lambda source: runner.adapters.get(source.platform)
    return runner


def _mutation(db, sid, request_id, payload, operation='edit_policy'):
    snap = db.get_session(sid)
    return {'id': request_id, 'method': 'session.mutate', 'params': {
        'session_id': sid, 'request_id': request_id, 'operation': operation, 'payload': payload,
        'expected_revision': snap['runtime_revision'], 'expected_generation': snap['runtime_generation']}}


async def _info(connection, sid):
    reply = await connection.dispatch({'id': 'i', 'method': 'session.info', 'params': {'session_id': sid}})
    assert 'result' in reply, reply
    return reply['result']


@pytest.mark.asyncio
async def test_edit_policy_is_a_durable_session_setting_every_viewer_reads(tmp_path, monkeypatch):
    from gateway import run
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import initialize_session_authority
    from gateway.session_controls import AuthorityConnection
    from gateway.session_policy import policy_for_source
    monkeypatch.setattr(run, '_load_gateway_config', lambda *a: {})
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    db = store._db
    first = await initialize_session_authority(_runner(store), profile_id='owned', instance_id='first')
    editor = AuthorityConnection(first, object(), {'user_id': 'human'})
    other = AuthorityConnection(first, object(), {'user_id': 'human'})
    try:
        created = await editor.dispatch({'id': 1, 'method': 'session.create', 'params': {
            'request_id': 'acp', 'source': 'acp', 'cwd': str(tmp_path), 'model': 'frozen',
            'editor': {'mcp_servers': [], 'edit_approval_policy': 'ask'}}})
        sid = created['result']['session_id']
        gui = (await editor.dispatch({'id': 2, 'method': 'session.create', 'params': {
            'request_id': 'gui', 'source': 'gui', 'cwd': str(tmp_path), 'model': 'frozen'}}))['result']['session_id']
        # A client that never sets it keeps main's default: every edit asks.
        assert (await _info(editor, sid))['edit_approval_policy'] == 'ask'
        live = first.sessions[sid]
        assert policy_for_source(first.runner, live.source).edit_approval_policy == 'ask'

        applied = await editor.dispatch(_mutation(db, sid, 'm1', {'policy': 'workspace_session'}))
        assert applied['result']['edit_approval_policy'] == 'workspace_session', applied
        # The policy the next turn executes under, and what a second attached surface reads.
        assert policy_for_source(first.runner, live.source).edit_approval_policy == 'workspace_session'
        assert (await _info(other, sid))['edit_approval_policy'] == 'workspace_session'

        bad = await editor.dispatch(_mutation(db, sid, 'm2', {'policy': 'yolo'}))
        assert bad['error']['data']['reason'] == 'invalid_params', bad
        # Only an ACP session has an editor edit policy; any other surface's edits never ask an editor.
        refused = await editor.dispatch(_mutation(db, gui, 'm3', {'policy': 'session'}))
        assert refused['error']['data']['reason'] == 'invalid_params', refused
    finally:
        await editor.close()
        await other.close()

    # An owner restart restores the session's policy from its stored receipt.
    cold = await initialize_session_authority(_runner(store), profile_id='owned', instance_id='restarted')
    viewer = AuthorityConnection(cold, object(), {'user_id': 'human'})
    try:
        resumed = await viewer.dispatch({'id': 3, 'method': 'session.resume', 'params': {'session_id': sid}})
        assert 'result' in resumed, resumed
        assert policy_for_source(cold.runner, cold.sessions[sid].source).edit_approval_policy == 'workspace_session'
        assert (await _info(viewer, sid))['edit_approval_policy'] == 'workspace_session'
        back = await viewer.dispatch(_mutation(db, sid, 'm4', {'policy': 'ask'}))
        assert back['result']['edit_approval_policy'] == 'ask', back
    finally:
        await viewer.close()
        store.close_all_db_handles()


def test_a_policy_stored_before_the_field_restores_as_ask_with_its_identity_unchanged(tmp_path):
    """A receipt main minted has no ``edit_approval_policy``: it restores as ``ask`` and its credential
    reference (a whole-policy hash) still matches, so no stored session loses its launch secrets."""
    from dataclasses import asdict
    from gateway.session_policy import build_policy, restore_policy
    from gateway.session_policy_credentials import policy_identity
    policy = build_policy({'source': 'acp', 'cwd': str(tmp_path), 'model': 'fixture'}, {})
    stored = asdict(policy)
    stored.pop('edit_approval_policy')
    old = restore_policy(stored)
    assert old.edit_approval_policy == 'ask'
    assert policy_identity(old) == policy_identity(policy)
    assert policy_identity(replace(policy, edit_approval_policy='session')) != policy_identity(policy)
    with pytest.raises(Exception):
        restore_policy({**stored, 'edit_approval_policy': 'everything'})


@pytest.mark.asyncio
@pytest.mark.parametrize('name, policy, asks', [
    ('edit.txt', 'ask', True),                    # Default: every edit asks
    ('edit.txt', 'workspace_session', False),     # Accept Edits: a workspace edit runs
    ('.env', 'workspace_session', True),          # ... a sensitive path still asks
    ('edit.txt', 'session', False),               # Don't Ask
    ('.git/config', 'session', True),             # ... a sensitive path still asks
])
async def test_the_owners_edit_guard_enforces_the_sessions_policy(tmp_path, name, policy, asks):
    from acp_adapter.edit_approval import maybe_require_edit_approval
    from gateway.session_policy import build_policy, policy_scope
    from tools.approval import register_gateway_notify, unregister_gateway_notify
    from tools.approval_context import reset_current_session_key, set_current_session_key

    route = f'edit-policy-{policy}-{name}'
    asked = []

    def notify(data):
        # The editor's consent prompt; denied here, so an asked edit never writes.
        asked.append(data)
        import threading
        from tests.waiting import wait_until
        from tools.approval import resolve_gateway_approval
        # The waiter enqueues its entry right after this notify returns; deny it once it is there.
        threading.Thread(target=lambda: wait_until(lambda: resolve_gateway_approval(route, 'deny'), timeout=10,
                                                   what='the edit approval to deny'), daemon=True).start()

    register_gateway_notify(route, notify)
    frozen = replace(build_policy({'source': 'acp', 'cwd': str(tmp_path), 'model': 'fixture'}, {}),
                     edit_approval_policy=policy)
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)

    def guard():
        token = set_current_session_key(route)
        try:
            with policy_scope(frozen):
                return maybe_require_edit_approval('write_file', {'path': str(path), 'content': 'new'})
        finally:
            reset_current_session_key(token)
    try:
        result = await asyncio.wait_for(asyncio.to_thread(guard), 10)
    finally:
        unregister_gateway_notify(route)
    assert bool(asked) is asks, (policy, name, asked)
    assert (result is None) is (not asks), result
