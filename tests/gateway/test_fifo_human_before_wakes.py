"""A person's queued message is never stuck behind internal process-completion wakes (#141822).

Live shape: a local session's turn runs long; background processes finish and each completion is
admitted (``automation:<route>``) into the same durable FIFO the person's typed messages use. The
user Ctrl+C's the turn and types again: the new message lands at the BACK of the FIFO, behind every
wake, and each wake runs as its own model turn first. From the user's side the queue "survives the
cancel" and the new message "doesn't flush it". Expected: the claim takes the oldest queued human
input first; wakes keep their own FIFO order and run once no person is waiting.

Real SessionStore SQLite and the real drain; the turn executor is a stand-in.
"""
import queue
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore
from gateway.session_authority import LiveSession, initialize_session_authority
from gateway.session_contract import SessionRef, Submission
from gateway.session_controls import AuthorityConnection
from hermes_state_runtime import (admit_session_input, claim_session_input, list_session_admissions,
                                  settle_session_input)
from tests.waiting import async_wait_until

ROUTE = 'agent:main:telegram:dm:wake'


class Peer:
    def __init__(self):
        self.frames = queue.Queue()

    def write(self, frame):
        self.frames.put(frame)
        return True


def _wake(db, epoch, name):
    return admit_session_input(db, epoch=epoch, principal_id='automation:' + ROUTE, session_id='s',
                               request_id=f'["completion","{name}",1]',
                               payload={'text': f'[IMPORTANT: Background process {name} completed]'})['admission_id']


@pytest.mark.asyncio
async def test_message_typed_after_cancel_runs_before_queued_process_wakes(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    executed = []

    async def answer(event):
        from gateway.session_results import execution_result
        executed.append(event.text)
        execution_result.get()['result'] = {'final_response': 'ok', 'completed': True, 'messages': []}
        return 'ok'

    runner = SimpleNamespace(_session_db=store._db, session_store=store, _draining=False,
                             _handle_message=answer, _adapter_for_source=lambda source: None)
    db = store._db
    db.create_session('s', source='telegram')
    authority = await initialize_session_authority(runner, profile_id='p', instance_id='owner')
    authority.sessions['s'] = LiveSession(SessionSource(platform=Platform.TELEGRAM, chat_id='wake'), ROUTE)
    ref = SessionRef('p', 's')
    actor = AuthorityConnection(authority, Peer(), {'user_id': 'human'}).actor
    authority._schedule = lambda ref: None  # hold the drain: the long turn is "running"
    try:
        long_turn = await authority.submit(actor, Submission('long', ref, {'text': 'LONG'}, 'queue'))
        claimed = claim_session_input(db, epoch=authority.epoch, session_id='s')
        assert claimed['admission_id'] == long_turn.admission_id
        wakes = [_wake(db, authority.epoch, name) for name in ('proc_a', 'proc_b')]
        # Ctrl+C: the running turn settles interrupted.
        settle_session_input(db, epoch=authority.epoch, admission_id=claimed['admission_id'],
                             generation=claimed['generation'], outcome='interrupted')
        # The person types again after the cancel.
        typed = await authority.submit(actor, Submission('typed', ref, {'text': 'TYPED'}, 'queue'))
        del authority._schedule
        authority._schedule(ref)
        live = authority.sessions['s']
        await async_wait_until(lambda: live.task is not None and live.task.done(), what='drain finished')
        assert executed[0] == 'TYPED', f'the typed message waited behind internal wakes: {executed}'
        assert executed[1:] == ['[IMPORTANT: Background process proc_a completed]',
                                '[IMPORTANT: Background process proc_b completed]']  # wakes keep FIFO
        statuses = {r['admission_id']: (r['status'], r['outcome'])
                    for r in list_session_admissions(db, session_id='s', pending_only=False)}
        assert statuses[typed.admission_id] == ('terminal', 'completed')
        assert all(statuses[w] == ('terminal', 'completed') for w in wakes)  # nothing stranded
    finally:
        store.close_all_db_handles()


def test_claim_picks_oldest_human_row_over_older_wakes(tmp_path):
    from hermes_state import SessionDB
    from hermes_state_runtime import begin_runtime_epoch
    db = SessionDB(tmp_path / 'state.db')
    try:
        db.create_session('s', source='telegram')
        epoch = begin_runtime_epoch(db, instance_id='x')
        _wake(db, epoch, 'proc_a')
        human = admit_session_input(db, epoch=epoch, principal_id='human', session_id='s',
                                    request_id='h', payload={'text': 'H'})['admission_id']
        assert claim_session_input(db, epoch=epoch, session_id='s')['admission_id'] == human
    finally:
        db.close()
