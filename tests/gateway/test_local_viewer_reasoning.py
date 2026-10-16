"""Reasoning a local viewer's turn sends Ink and Desktop: the live stream by default, the final reasoning on
``message.complete``, and never the messaging platforms' reasoning block in the reply text."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway.config import Platform
from tests.gateway.test_local_viewer_events_turn import _turn


@pytest.mark.parametrize(('source', 'config', 'flag', 'streams'), [
    ('tui', {}, None, True),  # unset: the launch's display.show_reasoning, on by default
    ('gui', {}, None, True),
    ('gui', {'display': {'show_reasoning': False}}, None, False),
    ('gui', {}, False, False),  # /reasoning hide or config.set from a viewer wins
    ('tui', {'display': {'show_reasoning': False}}, True, True),
])
def test_a_local_turn_streams_reasoning_unless_the_session_or_its_config_hides_it(tmp_path, source, config, flag,
                                                                                    streams):
    from gateway import local_viewer_events
    from gateway.session_policy import build_policy, policy_scope
    published = []
    live = SimpleNamespace(tool_progress_mode=None, **({} if flag is None else {'show_reasoning': flag}))
    turn = _turn(Platform.LOCAL, published, live=live)
    agent = SimpleNamespace(tool_gen_callback=None, _print_fn=None, thinking_callback=None,
                            reaction_callback=None, reasoning_callback=None)
    with policy_scope(build_policy({'source': source, 'cwd': str(tmp_path), 'model': 'fixture'}, config)):
        local_viewer_events.wire(turn, agent)
        if agent.reasoning_callback is not None:
            agent.reasoning_callback('step 1')
    assert (('reasoning.delta', {'text': 'step 1'}) in published) is streams


def test_completion_carries_the_turns_reasoning_only_to_local_viewers():
    from gateway.session_settlement_recovery import completion_payload
    from tui_gateway.contracts.registry import EVENTS
    row = {'admission_id': 'a1', 'request_id': 'r1'}
    local = completion_payload(row, {'outcome': 'completed'}, 'answer', {'result': {
        'local_viewer': True, 'last_reasoning': '  weigh both options  '}})
    assert local['reasoning'] == 'weigh both options'
    EVENTS['message.complete'].payload.model_validate({'text': 'answer', 'reasoning': local['reasoning']})
    messaging = completion_payload(row, {'outcome': 'completed'}, 'answer', {'result': {
        'last_reasoning': 'weigh both options'}})
    assert 'reasoning' not in messaging
    blank = completion_payload(row, {'outcome': 'completed'}, 'answer', {'result': {
        'local_viewer': True, 'last_reasoning': '   '}})
    assert 'reasoning' not in blank
