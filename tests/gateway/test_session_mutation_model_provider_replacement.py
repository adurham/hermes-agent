"""A model pick replaces the picked provider's frozen entry whole: real picker/config/runtime and a
loopback wire (never a vendor), plus a real gateway process for the withdrawn-key case."""
import asyncio
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from tests.gateway.fixtures.local_recovery_probe import child_env, daemon, rpc, websocket
from tests.waiting import async_wait_until

from tests.gateway.test_session_mutation_model import _loopback

@contextmanager
def peers():
    a, at, au = _loopback()
    b, bt, bu = _loopback()
    try:
        yield a, au, b, bu
    finally:
        for p, t in [(a, at), (b, bt)]:
            p.release.set()
            p.shutdown()
            p.server_close()
            t.join(timeout=5)


async def pick(tmp_path, monkeypatch, before, after, provider, *, request=None):
    from gateway.session_mutation_model import prepare_model
    from gateway.session_policy import build_policy, bind_launch_key, restore_policy
    from gateway.run_turn_prepare import _resolve_policy_agent_runtime
    from hermes_state import SessionDB
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    (tmp_path / 'config.yaml').write_text(json.dumps(before))
    private = {}
    policy = build_policy({'cwd': str(tmp_path), 'model': 'original', **(request or {})},
                          deepcopy(before), private_secrets=private)
    with SessionDB(tmp_path / 'state.db') as db:
        authority = SimpleNamespace(instance_id='review', epoch=1, profile_id='review', db=db)
        runner = SimpleNamespace(session_authority=authority)
        authority.runner = runner
        policy = bind_launch_key(authority, 'sid', policy, None, config_secrets=private)
        runner._resolve_session_agent_runtime = lambda **k: _resolve_policy_agent_runtime(runner, policy, fallback=False)
        (tmp_path / 'config.yaml').write_text(json.dumps(after))
        prepared = {'snapshot': {'receipt': {'session_id': 'sid', 'policy': asdict(policy)}}}
        committed = await prepare_model(authority, SimpleNamespace(source=None, route='r'),
                                        {'model': 'switched', 'provider': provider}, prepared)
        new = restore_policy(committed['policy'])
        config = new.config(authority)
        _, runtime = _resolve_policy_agent_runtime(runner, new, fallback=False)
        # A real HTTP post using the runtime fields the next agent client receives.
        response = await asyncio.to_thread(httpx.post, runtime['base_url'].rstrip('/') + '/chat/completions',
                              headers={'Authorization': f"Bearer {runtime['api_key']}"},
                              json={'model':'switched','messages':[{'role':'user','content':'REVIEW'}]}, timeout=10)
        assert response.status_code == 200
        # A cold owner (no in-memory secrets) recovers the same route from the profile's own paths.
        authority._local_config_secrets = {}
        _, cold = _resolve_policy_agent_runtime(runner, new, fallback=False)
        assert (cold['base_url'], cold['api_key']) == (runtime['base_url'], runtime['api_key'])
        return config, runtime


@pytest.mark.asyncio
@pytest.mark.parametrize('replacement', ['removed', 'rotated', 'key_env', 'headers_removed'])
async def test_named_provider_replacement_does_not_keep_withdrawn_secrets(tmp_path, monkeypatch, replacement):
    with peers() as (_a, au, b, bu):
        old_entry = {'base_url': au, 'api_key':'old-review-key', 'models': {'original':{}, 'switched':{}}}
        new_entry = {'base_url': bu, 'models': {'original':{}, 'switched':{}}}
        expected = 'no-key-required'
        if replacement == 'rotated':
            new_entry['api_key'] = expected = 'new-review-key'
        if replacement == 'key_env':
            monkeypatch.setenv('REVIEW_NEW_KEY', 'env-review-key')
            new_entry['key_env'] = 'REVIEW_NEW_KEY'
            expected = 'env-review-key'
        if replacement == 'headers_removed':
            old_entry['extra_headers'] = {'X-Review-Secret': 'removed-header'}
        before = {'model': {'provider':'reviewprovider', 'default':'original'}, 'providers': {'reviewprovider':old_entry}}
        after = {**before, 'providers': {'reviewprovider':new_entry}}
        config, runtime = await pick(tmp_path, monkeypatch, before, after, 'reviewprovider')
        assert runtime['base_url'].rstrip('/') == bu
        assert b.bearers == [f'Bearer {expected}']
        assert config['providers']['reviewprovider'].get('extra_headers') is None


@pytest.mark.asyncio
async def test_legacy_provider_respelling_picks_new_endpoint(tmp_path, monkeypatch):
    with peers() as (a, au, b, bu):
        before = {'model': {'provider':'custom:my-provider', 'default':'original'},
                  'custom_providers':[{'name':'My Provider','base_url':au,'api_key':'same-review-key'}]}
        after = {'model':before['model'],
                 'custom_providers':[{'name':'my-provider','base_url':bu,'api_key':'same-review-key'}]}
        _, runtime = await pick(tmp_path, monkeypatch, before, after, 'custom:my-provider')
        assert b.bearers == ['Bearer same-review-key'] and not a.bearers
        assert runtime['base_url'].rstrip('/') == bu


@pytest.mark.asyncio
@pytest.mark.parametrize('bypass', ['ignore_user_config', 'safe_mode'])
async def test_config_only_pick_does_not_import_profile_provider(tmp_path, monkeypatch, bypass):
    from hermes_state_runtime import RuntimeStoreError
    with peers() as (_a, au, b, bu):
        before = {'model': {'provider':'custom', 'base_url':au,'default':'original'}}
        after = {**before, 'providers': {'reviewprovider':{'base_url':bu,'api_key':'profile-only-key'}}}
        try:
            await pick(tmp_path, monkeypatch, before, after, 'reviewprovider',
                       request={bypass: True, 'provider': 'custom', 'base_url': au})
        except RuntimeStoreError as exc:
            assert str(exc) == 'model_resolution_failed'
        assert not b.bearers, 'ignore_user_config session imported profile-only provider'


@pytest.mark.asyncio
async def test_appended_legacy_provider_keeps_inline_key_after_cold_restore(tmp_path, monkeypatch):
    with peers() as (a, au, b, bu):
        before = {'model': {'provider':'custom:old-review', 'default':'original'},
                  'custom_providers':[{'name':'old-review','base_url':au,'api_key':'old-review-key'}]}
        after = {'model':before['model'],
                 'custom_providers':[{'name':'new-review','base_url':bu,'api_key':'new-review-key'}]}
        _, runtime = await pick(tmp_path, monkeypatch, before, after, 'custom:new-review')
        assert b.bearers == ['Bearer new-review-key'] and not a.bearers
        assert runtime['base_url'].rstrip('/') == bu


@pytest.mark.platforms('linux')
def test_removed_provider_key_is_not_sent_to_retargeted_endpoint(tmp_path):
    root = Path(__file__).resolve().parents[2]
    home, user = tmp_path / 'state', tmp_path / 'user'
    home.mkdir(); user.mkdir()
    with peers() as (_origin, origin_url, picked, picked_url):
        cfg = {'gateway': {'multiplex_profiles':False},
               'model': {'provider':'custom', 'default':'original', 'base_url':origin_url},
               'providers': {'loop': {'base_url':origin_url, 'api_key':'withdrawn-review-key',
                                      'models':{'original':{}, 'switched':{}}}},
               'auxiliary': {'title_generation':{'enabled':False}}, 'platform_toolsets':{'cli':[]}}
        (home / 'config.yaml').write_text(json.dumps(cfg))
        (home / 'models_dev_cache.json').write_text(json.dumps({'custom': {'id':'custom','models':{
            name:{'id':name,'name':name,'limit':{'context':100000,'output':4000}} for name in ('original','switched')}}}))
        env = child_env()
        env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home),
                   XDG_CONFIG_HOME=str(user / '.config'), HERMES_GATEWAY_LOCK_DIR=str(home / 'locks'),
                   PYTHONPATH=str(root), PYTHONUNBUFFERED='1',
                   OPENAI_API_KEY='original-only-key', OPENAI_BASE_URL=origin_url)
        async def run(desc):
            async with websocket(home, desc) as ws:
                async def call(method, **params):
                    result = await rpc(ws, method, **params)
                    assert 'result' in result, result
                    return result['result']
                async def turn(sid, marker):
                    await call('prompt.submit', session_id=sid, input_id=marker, text=marker)
                    def terminal():
                        with sqlite3.connect(home / 'state.db') as db:
                            row = db.execute('SELECT status FROM session_admissions WHERE request_id=?', (marker,)).fetchone()
                        return row and row[0] == 'terminal'
                    await async_wait_until(terminal, timeout=30, what='real gateway turn terminal')
                    return await call('session.resume', session_id=sid)
                sid = (await call('session.create', source='gui', request_id='review-create',toolsets=[]))['session_id']
                before = await turn(sid, 'BEFORE')
                (home / 'config.yaml').write_text(json.dumps({**cfg, 'providers':{'loop':{
                    'base_url':picked_url,'models':{'switched':{}}}}}))
                result = await call('session.mutate', session_id=sid, request_id='review-pick', operation='model',
                                   payload={'model':'switched','provider':'loop'}, expected_revision=before['revision'],
                                   expected_generation=before['execution_generation'])
                assert result['model'] == 'switched'
                await turn(sid, 'AFTER')
        with daemon(root, home, env, barrier=False) as (_, desc):
            asyncio.run(run(desc))
        assert picked.bearers and all(value == 'Bearer no-key-required' for value in picked.bearers), picked.bearers
