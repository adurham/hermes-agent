"""``session.usage`` / ``session.stats``: the session-cumulative counters main's HermesCLI read off its
in-process agent (status bar, /usage, exit summary), published from the gateway (wave A2, CALLMAP W3)."""
from __future__ import annotations

from collections import deque
from types import SimpleNamespace

import pytest

from gateway.config import Platform

FIELDS = {'input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens', 'prompt_tokens',
          'completion_tokens', 'total_tokens', 'reasoning_tokens', 'api_calls', 'last_prompt_tokens',
          'context_tokens', 'context_length', 'config_context_length', 'context_source', 'compression_count',
          'avg_latency_s', 'tokens_per_s', 'last_turn_tokens', 'rate_limit', 'active_subagents', 'model',
          'provider', 'fallback_model'}


def _agent():
    compressor = SimpleNamespace(last_prompt_tokens=900, context_length=128000, _config_context_length=128000,
                                 compression_count=1)
    return SimpleNamespace(
        model='fake-model', provider='custom', session_id='sid',
        session_input_tokens=100, session_output_tokens=40, session_cache_read_tokens=10,
        session_cache_write_tokens=5, session_prompt_tokens=115, session_completion_tokens=40,
        session_total_tokens=155, session_reasoning_tokens=3, session_api_calls=2,
        context_compressor=compressor, _api_latency_history=deque([1.0, 3.0]),
        _api_output_history=deque([10, 30]), get_rate_limit_state=lambda: None,
        _session_messages=[], _turn_base_usage_anchor=None)


def test_usage_stats_is_exactly_the_frozen_field_list():
    from gateway.session_usage import usage_stats
    stats = usage_stats(_agent())
    assert set(stats) == FIELDS
    assert stats['input_tokens'] == 100 and stats['api_calls'] == 2 and stats['total_tokens'] == 155
    assert stats['avg_latency_s'] == 2.0 and stats['tokens_per_s'] == 10.0
    assert stats['context_length'] == 128000 and stats['compression_count'] == 1
    assert stats['rate_limit'] is None and stats['fallback_model'] is None
    from tui_gateway.contracts.registry import EVENTS
    EVENTS['session.usage'].payload.model_validate(stats)


def _turn(platform, published, agent):
    from gateway.run_turn_runner import TurnRunner
    turn = object.__new__(TurnRunner)
    turn._ctx = SimpleNamespace(source=SimpleNamespace(platform=platform, chat_id='chat'), _step_callback_sync=None,
                                _run_still_current=lambda: True, agent_holder=[agent],
                                _hooks_ref=SimpleNamespace(loaded_hooks=[]))
    turn._runner = SimpleNamespace()
    turn._approval_owner = (SimpleNamespace(sessions={'s': SimpleNamespace()}), 's', 1)
    turn._publish_execution = lambda kind, payload: published.append((kind, payload)) or True
    return turn


@pytest.mark.parametrize('platform', [Platform.LOCAL, Platform.TELEGRAM])
def test_each_api_step_publishes_usage_only_for_local_viewers(platform):
    published = []
    turn = _turn(platform, published, _agent())
    turn._step_callback_sync(2, [])
    kinds = [kind for kind, _ in published]
    if platform == Platform.LOCAL:
        usage = next(payload for kind, payload in published if kind == 'session.usage')
        assert set(usage) == FIELDS and usage['api_calls'] == 2
    else:
        assert 'session.usage' not in kinds


@pytest.mark.asyncio
async def test_session_stats_returns_the_same_dict(tmp_path, monkeypatch):
    from gateway.session_controls import AuthorityConnection
    from tests.gateway.test_surface_admission import _authority
    from tui_gateway.contracts.registry import CANONICAL_METHODS

    async def answer(event):
        return 'ok'
    authority = await _authority(tmp_path, monkeypatch, answer)
    agent = _agent()
    authority.runner._cached_agent_for = lambda route: agent
    connection = AuthorityConnection(authority, object(), {'user_id': 'owner'})
    try:
        await connection.dispatch({'id': 1, 'method': 'session.resume', 'params': {'session_id': 's'}})
        reply = await connection.dispatch({'id': 2, 'method': 'session.stats', 'params': {'session_id': 's'}})
        assert 'result' in reply, reply
        from gateway.session_usage import usage_stats
        assert reply['result'] == usage_stats(agent)
        CANONICAL_METHODS['session.stats'].result.model_validate(reply['result'])
    finally:
        await connection.close()
