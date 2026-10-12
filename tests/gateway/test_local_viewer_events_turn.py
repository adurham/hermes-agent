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
        session_id='s', progress_queue=None,
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
