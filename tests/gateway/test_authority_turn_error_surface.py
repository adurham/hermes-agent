"""A turn that fails on the session authority completes with the same ``error_surface`` the TUI
path sends, so the Desktop renders the failure's own card (free tier at capacity, rate limit)
instead of the generic one."""
import io
from types import SimpleNamespace

import pytest

from gateway.config import Platform

_FREE_TIER_FULL = {'free_tier': {'kind': 'at_capacity', 'message': 'The free tier is full right now.'},
                   'failure_reason': 'rate_limit', 'failure_retryable': True, 'error': 'HTTP 429'}
_RATE_LIMITED = {'failure_reason': 'rate_limit', 'failure_retryable': True, 'failure_resets_at': 1900000000.0,
                 'error': 'HTTP 429: slow down'}


def _authority_turn(platform, failure):
    """Run one failed turn through the authority's in-process TurnRunner: ``(response, captured)``."""
    from gateway.run_turn_runner import TurnRunner
    from gateway.session import SessionSource
    from gateway.session_results import execution_result
    from gateway.turn_context import TurnContext
    from tests.gateway.test_local_viewer_events_submit import _gateway_runner, _RecordingAgent

    class _FailingAgent(_RecordingAgent):
        def run_conversation(self, message, **kwargs):
            return {'final_response': '', 'messages': [], 'failed': True, 'completed': False,
                    'provider': 'nous', 'model': 'free-model', **failure}

    ctx = TurnContext(
        source=SessionSource(platform=platform, chat_id='sid', user_id='u'),
        message='hi', history=[], session_id='sid', session_key='key', user_config={},
        AIAgent=_FailingAgent, resolve_display_setting=lambda *_a: False,
        _run_still_current=lambda: True, _hooks_ref=SimpleNamespace(loaded_hooks=False))
    captured = {}
    token = execution_result.set(captured)
    try:
        turn = TurnRunner(_gateway_runner(), ctx)
        turn._approval_owner = (SimpleNamespace(sessions={'sid': SimpleNamespace(tool_progress_mode=None)},
                                                profile_id='p', adopt_agent=lambda *a: None), 'sid', 1)
        delivered = turn.run_sync()
    finally:
        execution_result.reset(token)
    return delivered['final_response'], captured


def _complete(response, captured):
    from gateway.session_results import prepare_result
    from gateway.session_settlement_recovery import completion_payload
    row = {'admission_id': 'a1', 'request_id': 'r1', 'payload': {'text': 'hi'}}
    outcome, _stored = prepare_result(row, response, 'completed', captured)
    return completion_payload(row, {'outcome': outcome}, response, captured)


@pytest.mark.parametrize('platform', [Platform.LOCAL, Platform.TELEGRAM])
def test_free_tier_at_capacity_turn_completes_with_its_own_surface(platform):
    complete = _complete(*_authority_turn(platform, _FREE_TIER_FULL))
    assert complete['status'] == 'error'
    assert complete['error_surface']['code'] == 'free_tier_at_capacity'
    assert complete['error_surface']['message'] == 'The free tier is full right now.'


def test_rate_limited_turn_completes_with_rate_limit_and_reset_time():
    from tui_gateway.contracts.registry import EVENTS

    complete = _complete(*_authority_turn(Platform.LOCAL, _RATE_LIMITED))
    surface = complete['error_surface']
    assert (surface['code'], surface['retryable'], surface['resets_at']) == ('rate_limit', True, 1900000000.0)
    EVENTS['message.complete'].payload.model_validate(
        {k: v for k, v in complete.items() if k not in ('content', 'admission_id', 'outcome')})


def test_a_managed_worker_turn_carries_the_free_tier_failure_to_the_owner():
    from agent.managed_worker import accept_result, encode_frame, read_frame, result_frame

    raw = {'final_response': '', 'failed': True, 'completed': False, 'provider': 'nous', 'model': 'free-model',
           'messages': [], **_FREE_TIER_FULL}
    wire = read_frame(io.BytesIO(encode_frame({'type': 'result', 'result': result_frame(raw)})))
    accepted, usage = accept_result(wire['result'])
    # The owner's projection of a finished worker turn (``session_managed_worker``).
    complete = _complete('', {'result': {**accepted, 'local_viewer': True}, 'usage': usage})
    assert complete['error_surface']['code'] == 'free_tier_at_capacity'


def test_a_successful_turn_completes_without_a_surface():
    complete = _complete('done', {'result': {'final_response': 'done', 'messages': []}, 'usage': {}})
    assert complete['status'] == 'complete' and 'error_surface' not in complete
