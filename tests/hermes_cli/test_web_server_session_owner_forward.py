"""A standalone dashboard's mutations must reach the live gateway owner (#138915).

While a gateway serves the same home, the dashboard process is not the store owner, so its
offline mutation path can never win the exclusive-maintenance reservation: forward the
revision-fenced ``session.mutate`` verb to the owner over the control socket instead, and
keep the offline store only for an owner-less home.
"""
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException


def _standalone_request(monkeypatch, tmp_path):
    """An authenticated principal on a standalone dashboard (no composed gateway)."""
    monkeypatch.setattr('hermes_state._default_db_path', lambda: str(tmp_path / 'state.db'))
    return SimpleNamespace(
        state=SimpleNamespace(native_http_principal=None,
                              session=SimpleNamespace(user_id='user-a', provider='oidc',
                                                      issuer='https://idp.example')),
        app=SimpleNamespace(state=SimpleNamespace(auth_required=True, session_authority=None,
                                                  gateway_runner=None)))


def _discover(monkeypatch, endpoint, seen=None):
    def fake_discover(home, timeout=2.0):
        if seen is not None:
            seen['home'] = home
        return SimpleNamespace(endpoint=endpoint)
    monkeypatch.setattr('hermes_cli.gateway_runtime.discover_gateway_endpoint', fake_discover)


@pytest.mark.asyncio
async def test_standalone_mutation_forwards_to_the_live_owner(tmp_path, monkeypatch):
    from hermes_cli import web_server_sessions

    request = _standalone_request(monkeypatch, tmp_path)
    seen = {}
    calls = {}

    class FakeClient:
        async def rpc(self, method, **params):
            calls['rpc'] = (method, params)
            return {'session_id': params['session_id'], 'operation': 'delete', 'revision': 1}

    @asynccontextmanager
    async def fake_connect(endpoint=None, subject=None):
        calls['endpoint'] = endpoint
        calls['subject'] = subject
        yield FakeClient()

    offline = []
    monkeypatch.setattr(web_server_sessions, '_mutate_offline_store',
                        lambda profile, actor, params: offline.append(params))
    _discover(monkeypatch, 'owner-endpoint', seen)
    monkeypatch.setattr('hermes_cli.gateway_client.connect_gateway', fake_connect)

    result = await web_server_sessions._mutate_session_request(
        request, None, 'session-1', request_id='req-1', expected_revision=0,
        operation='delete', payload={}, expected_generation=0)

    assert result == {'ok': True, 'session_id': 'session-1', 'operation': 'delete', 'revision': 1}
    # Discovery resolved the selected home's owner; the offline store never opened.
    assert seen['home'] == tmp_path.resolve()
    assert calls['endpoint'] == 'owner-endpoint'
    # The ticket names the dashboard's authenticated principal — the socket UID would be
    # refused by authorize_history on any row that stores an owner.
    assert calls['subject'] == 'auth:v1:["oidc","https://idp.example","user-a"]'
    method, params = calls['rpc']
    assert method == 'session.mutate'
    assert params == {'session_id': 'session-1', 'request_id': 'req-1', 'expected_revision': 0,
                      'operation': 'delete', 'payload': {}, 'expected_generation': 0}
    assert offline == []


@pytest.mark.asyncio
async def test_standalone_mutation_falls_back_to_offline_without_an_owner(tmp_path, monkeypatch):
    from hermes_cli import web_server_sessions

    request = _standalone_request(monkeypatch, tmp_path)
    _discover(monkeypatch, None)
    offline = []

    def fake_offline(profile, actor, params):
        offline.append(params)
        return {'session_id': params['session_id'], 'operation': 'rename', 'revision': 2}

    monkeypatch.setattr(web_server_sessions, '_mutate_offline_store', fake_offline)

    result = await web_server_sessions._mutate_session_request(
        request, None, 'session-2', request_id='req-2', expected_revision=1,
        operation='rename', payload={'title': 'renamed'})

    assert result == {'ok': True, 'session_id': 'session-2', 'operation': 'rename', 'revision': 2}
    assert len(offline) == 1
    assert offline[0]['operation'] == 'rename'


@pytest.mark.asyncio
async def test_owner_dial_failure_surfaces_a_retryable_error(tmp_path, monkeypatch):
    """A dial that dies on an endpoint discovery just identified is not owner-less (a slow first
    dial, not a stale endpoint): the offline reservation would only lose to that same owner and
    answer with the bare 409 this forward exists to remove, so a retryable 503 is the honest
    verdict — a later retry rediscovers and takes the offline store once the owner is really gone."""
    from hermes_cli import web_server_sessions

    request = _standalone_request(monkeypatch, tmp_path)
    _discover(monkeypatch, 'owner-endpoint')

    @asynccontextmanager
    async def dying_connect(endpoint=None, subject=None):
        raise TimeoutError('first dial outlived its budget')
        yield  # pragma: no cover

    offline = []
    monkeypatch.setattr(web_server_sessions, '_mutate_offline_store',
                        lambda profile, actor, params: offline.append(params))
    monkeypatch.setattr('hermes_cli.gateway_client.connect_gateway', dying_connect)

    with pytest.raises(HTTPException) as exc:
        await web_server_sessions._mutate_session_request(
            request, None, 'session-3', request_id='req-3', expected_revision=0,
            operation='archive', payload={})
    assert exc.value.status_code == 503
    assert exc.value.detail == 'owner_unreachable'
    assert offline == []


@pytest.mark.asyncio
async def test_owner_refusal_maps_to_the_matching_http_status(tmp_path, monkeypatch):
    from hermes_cli.gateway_client import GatewayClientError
    from hermes_cli import web_server_sessions

    request = _standalone_request(monkeypatch, tmp_path)
    _discover(monkeypatch, 'owner-endpoint')

    @asynccontextmanager
    async def refusing_connect(endpoint=None, subject=None):
        class Refuser:
            async def rpc(self, method, **params):
                raise GatewayClientError('permission_denied')
        yield Refuser()

    monkeypatch.setattr('hermes_cli.gateway_client.connect_gateway', refusing_connect)
    with pytest.raises(HTTPException) as exc:
        await web_server_sessions._mutate_session_request(
            request, None, 'session-4', request_id='req-4', expected_revision=0,
            operation='delete', payload={}, expected_generation=0)
    assert exc.value.status_code == 403
    assert exc.value.detail == 'permission_denied'


@pytest.mark.asyncio
async def test_selected_profile_ticket_is_minted_on_that_profiles_own_socket(tmp_path, monkeypatch):
    """``?profile=work`` where ``work`` runs its own gateway: discovery finds work's endpoint
    (``control_home`` unset, it answers on its own socket), so the ticket must be minted on
    work's control socket — not the dashboard launch home's, which has no owner (503) or is
    another profile's owner (never mint one profile's ticket from another's socket)."""
    from hermes_cli import web_server_sessions
    from hermes_cli.gateway_client import GatewayClientError
    from hermes_cli.gateway_runtime import GatewayEndpoint

    launch = tmp_path / 'launch'
    work = tmp_path / 'launch' / 'profiles' / 'work'
    work.mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(launch))
    monkeypatch.setattr('hermes_cli.web_server_cron._cron_profile_home', lambda name: (name, work))
    endpoint = GatewayEndpoint(profile_id=str(work), instance_id='i-work', authority_epoch=1,
                               runtime_protocol=1, api_origin='http://127.0.0.1:1', supervisor='none',
                               capabilities=frozenset({'session-authority-v1'}))
    seen = {}
    _discover(monkeypatch, endpoint, seen)
    minted = {}

    def fake_ticket(home, ep, *, purpose='interactive', scope=None, subject=None):
        from hermes_cli.gateway_runtime import control_home_for
        minted['socket_home'] = control_home_for(home, ep)
        minted['profile_id'] = ep.profile_id
        raise GatewayClientError('stop_after_mint')

    monkeypatch.setattr('hermes_cli.gateway_client._session_ticket', fake_ticket)
    actor = SimpleNamespace(subject='auth:v1:["basic","","alice"]')
    with pytest.raises(GatewayClientError):
        await web_server_sessions._mutate_via_gateway_owner('work', actor, dict(
            session_id='s', request_id='r', expected_revision=0, operation='sidebar', payload={}))
    assert seen['home'] == work
    assert minted == {'socket_home': work, 'profile_id': str(work)}


@pytest.mark.asyncio
async def test_a_multiplexed_profile_keeps_the_multiplexers_socket(tmp_path, monkeypatch):
    """A secondary served by the default multiplexer has no socket of its own: its discovered
    ``control_home`` (the multiplexer root) stays the ticket socket."""
    from hermes_cli import web_server_sessions
    from hermes_cli.gateway_client import GatewayClientError
    from hermes_cli.gateway_runtime import GatewayEndpoint

    root = tmp_path / 'root'
    work = root / 'profiles' / 'work'
    work.mkdir(parents=True)
    monkeypatch.setattr('hermes_cli.web_server_cron._cron_profile_home', lambda name: (name, work))
    endpoint = GatewayEndpoint(profile_id=str(work), instance_id='i-root', authority_epoch=1,
                               runtime_protocol=1, api_origin='http://127.0.0.1:1', supervisor='none',
                               capabilities=frozenset({'session-authority-v1'}), control_home=str(root))
    _discover(monkeypatch, endpoint)
    minted = {}

    def fake_ticket(home, ep, *, purpose='interactive', scope=None, subject=None):
        from hermes_cli.gateway_runtime import control_home_for
        minted['socket_home'] = control_home_for(home, ep)
        raise GatewayClientError('stop_after_mint')

    monkeypatch.setattr('hermes_cli.gateway_client._session_ticket', fake_ticket)
    with pytest.raises(GatewayClientError):
        await web_server_sessions._mutate_via_gateway_owner('work', SimpleNamespace(subject='x'), dict(
            session_id='s', request_id='r', expected_revision=0, operation='sidebar', payload={}))
    assert minted['socket_home'] == root


@pytest.mark.asyncio
@pytest.mark.parametrize('reason, status', [('storage_unavailable', 503), ('runtime_draining', 503),
                                            ('not_found', 404), ('stale_revision', 409)])
async def test_a_forwarded_refusal_keeps_the_direct_paths_status(tmp_path, monkeypatch, reason, status):
    """The owner sends ``storage_unavailable`` for a sqlite failure; forwarded, it must stay the
    503 the direct path answers, not read as a 409 session conflict."""
    from hermes_cli.gateway_client import GatewayClientError
    from hermes_cli import web_server_sessions

    request = _standalone_request(monkeypatch, tmp_path)
    _discover(monkeypatch, 'owner-endpoint')

    @asynccontextmanager
    async def refusing_connect(endpoint=None, subject=None):
        class Refuser:
            async def rpc(self, method, **params):
                raise GatewayClientError(reason)
        yield Refuser()

    monkeypatch.setattr('hermes_cli.gateway_client.connect_gateway', refusing_connect)
    with pytest.raises(HTTPException) as exc:
        await web_server_sessions._mutate_session_request(
            request, None, 'session-5', request_id='req-5', expected_revision=0,
            operation='archive', payload={})
    assert (exc.value.status_code, exc.value.detail) == (status, reason)
