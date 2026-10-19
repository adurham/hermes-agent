"""#134889 F1: ``agent.restart_drain_timeout`` stays 0, and a turn a restart interrupts says so.

With the 0 s default a restart or stop interrupts in-flight turns at once. The admission settles
``interrupted`` (never replayed: ``tests/gateway/test_journey_new_session_queued_work.py`` restart
case, ``tests/gateway/test_prompt_queued_transfer.py`` owner-restart case), and its
``message.complete`` carries a named notice in ``warning``, the slot Ink and Desktop already render.
"""
from types import SimpleNamespace

import pytest

from gateway.session_settlement_recovery import (
    RESTART_INTERRUPT_NOTICE,
    SHUTDOWN_INTERRUPT_NOTICE,
    _publish_completion,
)
from tui_gateway.contracts.registry import EVENTS


def test_restart_drain_timeout_default_stays_zero():
    from gateway.restart import DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT
    from hermes_cli.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG['agent']['restart_drain_timeout'] == 0
    assert DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT == 0.0


class _Stream:
    def __init__(self):
        self.frames = []

    def publish(self, session_id, payload):
        self.frames.append(payload)


def _publish(runner, outcome, captured=None):
    stream = _Stream()
    live = SimpleNamespace(controls=SimpleNamespace(snapshot=lambda *a: None), event_stream=stream)
    row = {'admission_id': 'adm-1', 'request_id': 'sub-1'}
    _publish_completion(SimpleNamespace(runner=runner), live, SimpleNamespace(session_id='s'), row,
                        {'outcome': outcome}, 'partial reply', captured)
    (frame,) = stream.frames
    return frame


def _runner(*, restart, stopping=True):
    return SimpleNamespace(_draining=True, _running=not stopping, _restart_requested=restart)


@pytest.mark.parametrize(('restart', 'notice'), [(True, RESTART_INTERRUPT_NOTICE), (False, SHUTDOWN_INTERRUPT_NOTICE)])
def test_turn_interrupted_by_the_owner_stop_carries_a_named_notice(restart, notice):
    frame = _publish(_runner(restart=restart), 'interrupted')
    assert frame['status'] == 'interrupted' and frame['warning'] == notice
    assert 'will not be run again' in notice
    EVENTS['message.complete'].payload.model_validate({k: frame[k] for k in ('text', 'status', 'warning')})


def test_other_endings_get_no_restart_notice():
    # Completed during the drain, a user Stop in a restart's after-turn wait (still running),
    # and an ordinary interrupt with no restart all stay as they were.
    assert 'warning' not in _publish(_runner(restart=True), 'completed')
    assert 'warning' not in _publish(_runner(restart=True, stopping=False), 'interrupted')
    assert 'warning' not in _publish(SimpleNamespace(_draining=False, _running=True, _restart_requested=False),
                                     'interrupted')


def test_a_turn_warning_is_not_overwritten():
    captured = {'result': {'local_viewer': True, 'warning': 'own warning'}}
    assert _publish(_runner(restart=True), 'interrupted', captured)['warning'] == 'own warning'


def test_classic_cli_view_prints_the_restart_notice_for_an_interrupted_turn(capsys):
    """The attached classic CLI (``hermes chat``) prints the notice on stderr; a completed turn's
    warning stays unprinted there, as before."""
    from hermes_cli.gateway_chat_view import GatewayChatView

    view = GatewayChatView(object(), {'stored_session_id': 'sid'})
    view._complete('a1', {'text': '', 'status': 'interrupted', 'warning': RESTART_INTERRUPT_NOTICE})
    view._complete('a2', {'text': 'done', 'status': 'complete', 'warning': 'other warning'})
    err = capsys.readouterr().err
    assert RESTART_INTERRUPT_NOTICE in err and 'other warning' not in err
