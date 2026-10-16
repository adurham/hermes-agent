"""A gateway-owned session runs with ``provider_routing`` from its own
profile's config as of its creation, as a new ``hermes chat`` process read it once at startup on
pre-merge main (CFG-6). The daemon-start snapshot on the runner never reaches it; a managed
worker already builds from the same frozen config."""
from types import SimpleNamespace

import pytest

LAUNCH = {'model': 'm', 'provider': 'custom', 'base_url': 'http://127.0.0.1:9/v1'}


class _Built(Exception):
    pass


def test_in_process_turn_routes_with_the_sessions_own_provider_routing(tmp_path, monkeypatch):
    """The routing the in-process turn hands its agent is the session's, not the runner's
    daemon-start ``_provider_routing``."""
    from gateway import session_policy
    from gateway.config import Platform
    from gateway.run_turn_runner import TurnRunner
    from gateway.session_policy import build_policy
    policy = build_policy({'cwd': str(tmp_path), **LAUNCH}, {'provider_routing': {'only': ['Fresh'], 'sort': 'price'}})
    monkeypatch.setattr(session_policy, 'policy_for_source', lambda runner, source: policy)
    runner = SimpleNamespace(
        _provider_routing={'only': ['DaemonStart']}, _peek_session_state=lambda key: None,
        session_authority=SimpleNamespace(profile_id=str(tmp_path)),
        _resolve_session_agent_runtime=lambda **kw: ('m', {'provider': 'custom'}),
        _resolve_turn_agent_config=lambda message, model, runtime: {'model': model, 'runtime': runtime})
    turn = TurnRunner.__new__(TurnRunner)
    turn._runner = runner
    turn._ctx = SimpleNamespace(source=SimpleNamespace(platform=Platform.LOCAL, chat_id='c'), session_key='k',
                                user_config={}, message='hi', inbound_message_id=None, enabled_toolsets=[])
    turn._combined_ephemeral_prompt = lambda: None
    turn._setup_stream_consumer = lambda platform_key: (None, None, None, False)

    def built(route, platform_key, ephemeral, max_iterations, reasoning, pr):
        raise _Built(pr)
    turn._resolve_turn_agent = built
    with pytest.raises(_Built) as caught:
        turn._run_sync_scoped()
    assert caught.value.args[0] == {'only': ['Fresh'], 'sort': 'price'}


def test_bg_side_agent_uses_the_sessions_provider_routing(tmp_path):
    """``/bg`` through its handler: the side agent it constructs routes with the session's
    ``provider_routing``, not the runner's daemon-start copy."""
    import asyncio
    import run_agent
    from gateway import session_tui_side
    from gateway.session_contract import SessionRef
    from tests.gateway.test_local_create_env_refs import _owner
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.yaml').write_text(CONFIG + 'provider_routing: {only: [Fresh], sort: price}\n')
    built = {}

    class Agent:
        def __init__(self, **kwargs):
            built.update(kwargs)

        def run_conversation(self, **kwargs):
            return {'final_response': 'done'}

        def close(self):
            pass

    async def probe(monkeypatch):
        monkeypatch.setattr(run_agent, 'AIAgent', Agent)
        owner, connection = await _owner(home, monkeypatch)
        owner.runner._provider_routing = {'only': ['DaemonStart']}
        owner.runner._peek_session_state = lambda key: None
        owner.runner._adapter_for_source = lambda source: owner.runner.adapters.get(source.platform)
        try:
            ref, _policy = _create(owner, connection, home, 'bg')
            connection.subscriptions[ref.session_id] = 'sub'
            reply = await session_tui_side.background(connection, SessionRef(owner.profile_id, ref.session_id),
                                                      {'session_id': ref.session_id, 'text': 'BG'})
            assert reply['task_id'].startswith('bg_')
            for thread in list(session_tui_side._RUNNING):
                thread.join(30)
        finally:
            connection.subscriptions.clear()
            await connection.close()
            owner.db.close()
    with pytest.MonkeyPatch.context() as monkeypatch:
        asyncio.run(probe(monkeypatch))
    assert (built['providers_allowed'], built['provider_sort']) == (['Fresh'], 'price'), built


CONFIG = ('platform_toolsets: {cli: []}\n'
          'model: {provider: custom, default: m, base_url: "http://127.0.0.1:9/v1"}\n')


def _create(owner, connection, home, request_id):
    from gateway.session_local import create_local_session
    from gateway.session_policy import restore_policy
    from hermes_state_local import local_receipt
    ref = create_local_session(owner, connection.actor, {'request_id': request_id, 'source': 'cli',
                                                         'cwd': str(home), 'toolsets': []})
    return ref, restore_policy(local_receipt(owner.db, ref.session_id)['policy'])
