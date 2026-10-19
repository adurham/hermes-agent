"""#134889 F1: ``agent.restart_drain_timeout`` stays 0, and a turn a restart interrupts says so.

With the 0 s default a restart or stop interrupts in-flight turns at once. The admission settles
``interrupted`` (never replayed: ``tests/gateway/test_journey_new_session_queued_work.py`` restart
case, ``tests/gateway/test_prompt_queued_transfer.py`` owner-restart case), and its
``message.complete`` carries a named notice in ``warning``, the slot Ink and Desktop already render.
"""
import asyncio
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
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


def _publish(outcome, captured=None):
    stream = _Stream()
    live = SimpleNamespace(controls=SimpleNamespace(snapshot=lambda *a: None), event_stream=stream)
    row = {'admission_id': 'adm-1', 'request_id': 'sub-1'}
    _publish_completion(SimpleNamespace(runner=None), live, SimpleNamespace(session_id='s'), row,
                        {'outcome': outcome}, 'partial reply', captured)
    (frame,) = stream.frames
    return frame


def _cause(message, **extra):
    return {'result': {'interrupted': True, 'interrupt_message': message, **extra}}


@pytest.mark.parametrize(('cause', 'notice'), [('Gateway restarting', RESTART_INTERRUPT_NOTICE),
                                               ('Gateway shutting down', SHUTDOWN_INTERRUPT_NOTICE)])
def test_turn_interrupted_by_the_owner_stop_carries_a_named_notice(cause, notice):
    frame = _publish('interrupted', _cause(cause))
    assert frame['status'] == 'interrupted' and frame['warning'] == notice
    assert 'will not be run again' in notice
    EVENTS['message.complete'].payload.model_validate({k: frame[k] for k in ('text', 'status', 'warning')})


def test_other_endings_get_no_restart_notice():
    # A completed turn, a user Stop (no recorded cause, or its own) and a turn with no result.
    assert 'warning' not in _publish('completed', _cause('Gateway restarting'))
    assert 'warning' not in _publish('interrupted', {'result': {'interrupted': True}})
    assert 'warning' not in _publish('interrupted', _cause('User requested stop'))
    assert 'warning' not in _publish('interrupted')


def test_a_turn_warning_is_not_overwritten():
    captured = _cause('Gateway restarting', local_viewer=True, warning='own warning')
    assert _publish('interrupted', captured)['warning'] == 'own warning'


def test_classic_cli_view_prints_the_restart_notice_for_an_interrupted_turn(capsys):
    """The attached classic CLI (``hermes chat``) prints the notice on stderr; a completed turn's
    warning stays unprinted there, as before."""
    from hermes_cli.gateway_chat_view import GatewayChatView

    view = GatewayChatView(object(), {'stored_session_id': 'sid'})
    view._complete('a1', {'text': '', 'status': 'interrupted', 'warning': RESTART_INTERRUPT_NOTICE})
    view._complete('a2', {'text': 'done', 'status': 'complete', 'warning': 'other warning'})
    err = capsys.readouterr().err
    assert RESTART_INTERRUPT_NOTICE in err and 'other warning' not in err


@pytest.mark.asyncio
async def test_user_stop_is_not_relabelled_when_shutdown_starts_during_settlement(tmp_path):
    """A user Stop whose settlement a competing SQLite writer delays until after shutdown began is
    still the user's Stop: the notice follows the turn's recorded cause, not the runner's flags."""
    from gateway.session_authority import LiveSession, SessionAuthority
    from gateway.session_contract import SessionRef
    from gateway.session_results import admission_result
    from gateway.session_settlement_recovery import commit_and_publish
    from hermes_state import SessionDB
    from hermes_state_runtime import admit_session_input, begin_runtime_epoch, claim_session_input

    runner = SimpleNamespace(_draining=False, _running=True, _restart_requested=False)
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        db.create_session('s', source='test')
        epoch = begin_runtime_epoch(db, instance_id='owner')
        authority = SessionAuthority(runner, profile_id='owned', instance_id='owner', db=db, epoch=epoch)
        live = authority.sessions['s'] = LiveSession(None, 'route')
        ref = SessionRef('owned', 's')
        admit_session_input(db, epoch=epoch, principal_id='person', session_id='s', request_id='r',
                            payload={'text': 'please work'})
        row = claim_session_input(db, epoch=epoch, session_id='s')
        authority._stamp_claim(ref, live, row)
        frames = []
        live.event_stream.observers.add(frames.append)
        captured = {'result': {'final_response': '', 'messages': [], 'interrupted': True,
                               'completed': False, 'interrupt_message': 'User requested stop'}}
        reached = threading.Event()
        settlement = {}
        lock = sqlite3.connect(tmp_path / 'state.db')
        lock.execute('BEGIN IMMEDIATE')

        def trace(frame, event, arg):
            if event == 'call' and frame.f_code.co_name == 'settle_session_input':
                reached.set()
            return trace

        def commit():
            sys.settrace(trace)
            try:
                commit_and_publish(authority, live, ref, row, '', 'interrupted', captured, settlement)
            finally:
                sys.settrace(None)

        with ThreadPoolExecutor(max_workers=1) as pool:
            task = pool.submit(commit)
            try:
                assert await asyncio.to_thread(reached.wait, 10), 'settlement not reached'
                assert not task.done(), 'competing SQLite writer should block settlement'
                runner._draining = True
                runner._running = False
                lock.rollback()
                await asyncio.wrap_future(task)
            finally:
                lock.close()
        complete = [f['params']['payload'] for f in frames if f['params']['type'] == 'message.complete']
        persisted = admission_result(db, row['admission_id'])
        assert len(complete) == 1
        assert persisted['result']['interrupt_message'] == 'User requested stop'
        assert 'warning' not in complete[0], complete[0]
