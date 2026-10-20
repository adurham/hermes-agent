"""Owner-reserved delegated children of a worker execution (#141793): the owner mints every child,
caps them, scopes their writes to their own row, and retires them with their parent."""
import os
from functools import partial

import psutil
import pytest

from gateway.session_worker_child import reserve_child_execution
from hermes_state import SessionDB
import hermes_state_runtime as rt


def _reserve(db, epoch, sequence, *, limits=(2, 1), refused=None, execution_id='worker'):
    handler = partial(reserve_child_execution, epoch=epoch, execution_id=execution_id,
                      identity={'profile_id': 'home', 'pid': os.getpid(), 'birth': psutil.Process().create_time()},
                      principal='local', limits=limits, refused=refused)
    return rt.apply_worker_receipt(db, epoch=epoch, execution_id=execution_id, session_id='parent', generation=0,
                                   sequence=sequence, operation='child.reserve',
                                   payload={'secret': f'child-secret-{sequence:08d}'}, handlers={'child.reserve': handler})


def _child_append(db, epoch, child, sequence, content, session_id=None):
    return rt.mutate_worker_execution(db, epoch=epoch, execution_id=child['execution_id'],
        session_id=session_id or child['session_id'], generation=0, sequence=sequence,
        operation='transcript.append', payload={'messages': [{'role': 'assistant', 'content': content}]})


@pytest.fixture
def ledger(tmp_path):
    db = SessionDB(db_path=tmp_path / 'state.db')
    db.create_session('parent', source='kanban')
    db.create_session('other', source='cli')
    epoch = rt.begin_runtime_epoch(db, instance_id='boot')
    rt.register_worker_execution(db, epoch=epoch, execution_id='worker', session_id='parent', generation=0,
                                 kind='compute', adoption_secret='private-fixture')
    yield db, epoch
    db.close()


def test_owner_mints_caps_and_scopes_children_and_retires_them_with_the_parent(ledger):
    db, epoch = ledger
    first = _reserve(db, epoch, 1)['child']
    second = _reserve(db, epoch, 2)['child']
    # Concurrency and draining caps are receipts, so the parent's journal never sticks.
    assert _reserve(db, epoch, 3) == {'refused': 'delegation_capacity'}
    assert _reserve(db, epoch, 4, refused='runtime_draining') == {'refused': 'runtime_draining'}
    row = db.get_session(first['session_id'])
    assert (row['source'], row['parent_session_id']) == ('subagent', 'parent')
    with db._read_ctx() as conn:
        kinds = conn.execute("SELECT kind,status FROM worker_executions WHERE execution_id!='worker'").fetchall()
    assert [tuple(r) for r in kinds] == [('child', 'running')] * 2
    # A child writes its own row only.
    _child_append(db, epoch, first, 1, 'CHILD_OUTPUT')
    with pytest.raises(rt.RuntimeStoreError, match='permission_denied'):
        _child_append(db, epoch, first, 2, 'escape', session_id='parent')
    assert [m['content'] for m in db.get_messages(first['session_id'])] == ['CHILD_OUTPUT']
    assert db.get_messages('parent') == []
    db.create_session(first['session_id'] + '-x', source='subagent')  # unrelated row, untouched by retirement
    # The parent's finish retires every live child: it can no longer write and its row is ended.
    rt.mutate_worker_execution(db, epoch=epoch, execution_id='worker', session_id='parent', generation=0,
                               sequence=5, operation='execution.finish', payload={})
    with pytest.raises(rt.RuntimeStoreError, match='stale_generation'):
        _child_append(db, epoch, second, 1, 'late')
    with db._read_ctx() as conn:
        assert {r[0] for r in conn.execute("SELECT status FROM worker_executions")} == {'terminal'}
    assert db.get_session(second['session_id'])['end_reason'] == 'agent_close'
    assert db.get_session(first['session_id'] + '-x')['ended_at'] is None


def test_owner_restart_retires_in_flight_children_instead_of_recovering_them(ledger):
    db, epoch = ledger
    rt.adopt_worker_execution(db, epoch=epoch, execution_id='worker', session_id='parent', generation=0,
                              adoption_secret='private-fixture')
    assert _reserve(db, epoch, 1, limits=(1, 1), execution_id='worker')['child']
    with db._read_ctx() as conn:
        child_eid, child_sid = conn.execute("SELECT execution_id,session_id FROM worker_executions WHERE kind='child'").fetchone()
    assert _reserve(db, epoch, 2, limits=(1, 1)) == {'refused': 'delegation_capacity'}
    replacement = rt.begin_runtime_epoch(db, instance_id='restart')
    rt.recover_session_inputs(db, epoch=replacement)
    with db._read_ctx() as conn:
        states = dict(conn.execute('SELECT execution_id,status FROM worker_executions').fetchall())
    # The root is the recoverable unknown; its child died with the worker and is never adoptable.
    assert states == {'worker': 'unknown', child_eid: 'terminal'}
    assert db.get_session(child_sid)['end_reason'] == 'agent_close'
    with pytest.raises(rt.RuntimeStoreError, match='stale_generation|stale_epoch'):
        _child_append(db, replacement, {'execution_id': child_eid, 'session_id': child_sid}, 1, 'late')
