"""Local-viewer turn events the classic CLI renders through its own callbacks (CALLMAP W1).

On the session authority the agent runs in the gateway, so every callback main's HermesCLI
passed to its in-process AIAgent must reach the viewer as an execution event: the reply-box
segment boundary, the tool-generating line, every ``_print_fn`` status line and the tool
duration. A messaging route must not see any of it.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.run_turn_runner import TurnRunner


def _turn(platform, published, *, live=None):
    turn = object.__new__(TurnRunner)
    turn._ctx = SimpleNamespace(
        source=SimpleNamespace(platform=platform, chat_id='chat'),
        mute_notification_reply=False, scheduled_heartbeat=False,
        streaming_tts_consumer_holder=[None], stream_consumer_holder=[None],
        interim_assistant_messages_enabled=False, user_config={},
        resolve_display_setting=lambda *a: None, _run_still_current=lambda: True,
        _voice_ack_guild=[None], _native_slack_task_cards=False, progress_mode='all',
        session_id='s', progress_queue=None, _live_status_adapter=None, _live_status_mode='off',
        log_queue=None,
    )
    turn._runner = SimpleNamespace(config=None, _delivery_adapter_for=lambda source: None)
    live = live or SimpleNamespace(tool_progress_mode=None)
    authority = SimpleNamespace(sessions={'s': live})
    turn._approval_owner = (authority, 's', 1)
    turn._publish_execution = lambda kind, payload: published.append((kind, payload)) or True
    turn._publish_api_tool = lambda *a, **k: None
    return turn


def _delta_cb(turn):
    _consumer, delta_cb, _interim, _want = turn._setup_stream_consumer('cli')
    return delta_cb


def test_local_viewer_gets_the_segment_boundary_as_null_delta():
    published = []
    delta_cb = _delta_cb(_turn(Platform.LOCAL, published))
    delta_cb('hello')
    delta_cb(None)
    assert published == [('message.delta', {'text': 'hello'}), ('message.delta', {'text': None})]


def test_messaging_route_never_publishes_the_boundary():
    published = []
    delta_cb = _delta_cb(_turn(Platform.TELEGRAM, published))
    delta_cb('hello')
    delta_cb(None)
    assert published == [('message.delta', {'text': 'hello'})]


def test_turn_sinks_publish_and_restore_on_the_shared_agent():
    from gateway import local_viewer_events
    published = []
    turn = _turn(Platform.LOCAL, published)
    sentinel = object()
    agent = SimpleNamespace(tool_gen_callback=sentinel, _print_fn=None)
    previous = local_viewer_events.wire(turn, agent)
    agent.tool_gen_callback('terminal')
    agent._print_fn('  ✓ [1/2]', 'done')
    assert published == [('tool.generating', {'name': 'terminal'}),
                         ('agent.print', {'text': '  ✓ [1/2] done'})]
    local_viewer_events.restore(agent, previous)
    assert agent.tool_gen_callback is sentinel and agent._print_fn is None


def test_status_lines_the_agent_prints_reach_the_viewer():
    """``_safe_print`` (subagent completion, compaction, errors) routes through ``_print_fn``."""
    from agent.status_output import StatusOutputMixin
    from gateway import local_viewer_events
    published = []
    agent = type('A', (StatusOutputMixin,), {})()
    agent._print_fn = None
    local_viewer_events.wire(_turn(Platform.LOCAL, published), agent)
    agent._safe_print('🗜️ compacted')
    assert published == [('agent.print', {'text': '🗜️ compacted'})]


@pytest.mark.parametrize('platform', [Platform.LOCAL, Platform.TELEGRAM])
def test_tool_complete_carries_duration_only_for_local_viewers(platform):
    published = []
    turn = _turn(platform, published)
    turn.combined_tool_start_callback('c1', 'read_file', {'path': 'x'})
    turn.combined_tool_complete_callback('c1', 'read_file', {'path': 'x'}, '{"content": "hi"}')
    complete = next(payload for kind, payload in published if kind == 'tool.complete')
    if platform == Platform.LOCAL:
        assert isinstance(complete['duration_s'], float) and complete['duration_s'] >= 0
    else:
        assert 'duration_s' not in complete


def test_wire_contract_accepts_the_boundary_frame():
    from tui_gateway.contracts.registry import EVENTS
    EVENTS['message.delta'].payload.model_validate({'text': None})
    EVENTS['tool.generating'].payload.model_validate({'name': 'terminal'})
    EVENTS['agent.print'].payload.model_validate({'text': 'line'})
    EVENTS['tool.complete'].payload.model_validate({'tool_id': 'c', 'name': 'n', 'duration_s': 0.1})


def test_thinking_and_reaction_publish_and_reasoning_waits_for_the_session_flag():
    from gateway import local_viewer_events
    published = []
    live = SimpleNamespace(tool_progress_mode=None)
    turn = _turn(Platform.LOCAL, published, live=live)
    agent = SimpleNamespace(tool_gen_callback=None, _print_fn=None, thinking_callback=None,
                            reaction_callback=None, reasoning_callback=None)
    previous = local_viewer_events.wire(turn, agent)
    agent.thinking_callback('(◕‿◕) pondering...')
    agent.thinking_callback('')
    agent.reaction_callback('vibe')
    # No sink unless the viewer asked: a reasoning callback changes the provider request.
    assert agent.reasoning_callback is None
    local_viewer_events.restore(agent, previous)
    assert (agent.thinking_callback, agent.reaction_callback) == (None, None)
    live.show_reasoning = True
    local_viewer_events.wire(turn, agent)
    agent.reasoning_callback('step 1')
    assert published == [('thinking.delta', {'text': '(◕‿◕) pondering...'}), ('thinking.delta', {'text': ''}),
                         ('reaction', {'kind': 'vibe'}), ('reasoning.delta', {'text': 'step 1'})]


@pytest.mark.parametrize('classic', [True, False])
def test_progress_projects_reasoning_available_and_moa(monkeypatch, classic):
    from gateway import session_cli_turn
    monkeypatch.setattr(session_cli_turn, 'classic_cli_turn', lambda turn: classic)
    published = []
    live = SimpleNamespace(tool_progress_mode=None)
    turn = _turn(Platform.LOCAL, published, live=live)
    turn.progress_callback('reasoning.available', '_thinking', 'because', None)
    turn.progress_callback('moa.reference', 'gpt-x', 'ref answer', None, moa_index=1, moa_count=2)
    turn.progress_callback('moa.progress', 'gpt-x', None, None, moa_refs_done=1, moa_refs_total=2)
    turn.progress_callback('moa.phase', 'agg', None, None, moa_phase='aggregator', moa_refs_done=2, moa_refs_total=2)
    turn.progress_callback('moa.aggregating', 'agg', None, None, moa_ref_count=2)
    moa = [('moa.reference', {'label': 'gpt-x', 'text': 'ref answer', 'index': 1, 'count': 2}),
           ('moa.progress', {'label': 'gpt-x', 'refs_done': 1, 'refs_total': 2}),
           ('moa.phase', {'aggregator': 'agg', 'phase': 'aggregator', 'refs_done': 2, 'refs_total': 2}),
           ('moa.aggregating', {'aggregator': 'agg'})]
    # main's CLI renders MoA references whatever its reasoning display; reasoning.available only when shown.
    assert published == (moa if classic else [])
    published.clear()
    live.show_reasoning = True
    turn.progress_callback('reasoning.available', '_thinking', 'because', None)
    assert published == [('reasoning.available', {'text': 'because'})]
    from tui_gateway.contracts.registry import EVENTS
    for kind, payload in moa:
        EVENTS[kind].payload.model_validate(payload)


def test_messaging_progress_never_publishes_reasoning():
    published = []
    turn = _turn(Platform.TELEGRAM, published, live=SimpleNamespace(tool_progress_mode=None, show_reasoning=True))
    turn.progress_callback('reasoning.available', '_thinking', 'because', None)
    turn.progress_callback('moa.aggregating', 'agg', None, None)
    assert published == []


@pytest.mark.asyncio
async def test_show_reasoning_is_a_session_scoped_config_key(tmp_path, monkeypatch):
    from gateway.session_controls import AuthorityConnection
    from tests.gateway.test_surface_admission import _authority

    async def answer(event):
        return 'ok'
    authority = await _authority(tmp_path, monkeypatch, answer)
    connection = AuthorityConnection(authority, object(), {'user_id': 'owner'})
    try:
        await connection.dispatch({'id': 1, 'method': 'session.resume', 'params': {'session_id': 's'}})
        get = {'id': 2, 'method': 'config.get', 'params': {'session_id': 's', 'key': 'show_reasoning'}}
        assert (await connection.dispatch(get))['result']['value'] == '0'
        set_on = await connection.dispatch({'id': 3, 'method': 'config.set', 'params': {
            'session_id': 's', 'key': 'show_reasoning', 'value': 'on'}})
        assert set_on['result'] == {'key': 'show_reasoning', 'value': '1', 'scope': 'session'}, set_on
        assert authority.sessions['s'].show_reasoning is True
        bad = await connection.dispatch({'id': 4, 'method': 'config.set', 'params': {
            'session_id': 's', 'key': 'show_reasoning', 'value': 'loud'}})
        assert bad['error']['message'] == 'invalid_params', bad
    finally:
        await connection.close()
