"""``QUEUED_HEAD_ORDER_SQL`` and ``queued_head_key`` are one ordering rule in two languages.

``claim_session_input`` claims the SQL head; ``session_authority._claim_next`` validates the
Python head (``min(queued, key=queued_head_key)``) in its preclaim checks and passes that id as
``head=``. If the two disagree on any row, the claim refuses (``False``) or the checks validate a
row that is not the one that runs. Nothing else asserts they agree, so this does.
"""
import itertools
import sqlite3

import pytest

from hermes_state_runtime import QUEUED_HEAD_ORDER_SQL, queued_head_key

# People, internal wakes, and principals that only LOOK like one or the other: a case variant
# (SQLite LIKE is ASCII case-insensitive, str.startswith is not), the bare prefix, a near miss
# without the colon, a prefix in the middle, and a GLOB/LIKE metacharacter in the id.
PRINCIPALS = [
    'messaging:telegram:42', 'api', 'cron', 'msgraph:route',
    'automation:agent:main:local:dm:x', 'automation:', 'automation:%',
    'Automation:agent:main', 'AUTOMATION:x', 'automation_x', 'automationx', 'x:automation:y',
    'automation*', 'automation:[a]',
]


def _scratch(rows):
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    # Same columns the rule reads; seq NOT unique here so key ties are exercised too.
    conn.execute('CREATE TABLE q (admission_id TEXT, principal_id TEXT NOT NULL, seq INTEGER NOT NULL)')
    conn.executemany('INSERT INTO q VALUES (?,?,?)', rows)
    return conn


def _sql_keys(conn):
    return {r['admission_id']: (bool(r['k']), r['seq'])
            for r in conn.execute('SELECT admission_id, seq, ' + QUEUED_HEAD_ORDER_SQL.split(',')[0]
                                  + ' AS k FROM q')}


def test_every_principal_gets_the_same_class_in_sql_and_python():
    """Row-level parity: the strongest form — if every row's key agrees, every head agrees."""
    conn = _scratch([(f'a{i}', p, i) for i, p in enumerate(PRINCIPALS)])
    py = {r['admission_id']: queued_head_key(dict(r)) for r in conn.execute('SELECT * FROM q')}
    assert _sql_keys(conn) == py


@pytest.mark.parametrize('size', [2, 3, 4])
def test_sql_head_equals_python_head_on_every_mixed_queue(size):
    """Exhaustive small queues: every principal mix, every seq assignment including ties.

    On a key tie (same class, same seq) neither side defines an order, so the heads must be tied
    rows: same key. With unique seq (the real schema) the head row is identical.
    """
    pool = ['messaging:a', 'automation:w', 'Automation:w', 'api']
    checked = 0
    for principals in itertools.product(pool, repeat=size):
        for seqs in itertools.product(range(1, size + 1), repeat=size):
            rows = [(f'r{i}', p, s) for i, (p, s) in enumerate(zip(principals, seqs))]
            conn = _scratch(rows)
            sql_head = conn.execute('SELECT * FROM q ORDER BY ' + QUEUED_HEAD_ORDER_SQL + ' LIMIT 1').fetchone()
            listing = [dict(r) for r in conn.execute('SELECT * FROM q ORDER BY seq')]
            py_head = min(listing, key=queued_head_key)
            assert queued_head_key(dict(sql_head)) == queued_head_key(py_head), rows
            if len(set(seqs)) == size:
                assert sql_head['admission_id'] == py_head['admission_id'], rows
            checked += 1
    assert checked == len(pool) ** size * size ** size


def test_real_store_claims_the_head_session_authority_validates(tmp_path):
    """End to end on the real schema: admit an interleaved queue, then repeatedly take the Python
    head the way ``_claim_next`` does and claim it with ``head=`` pinned. A disagreement shows up as
    ``claim_session_input`` returning ``False`` (head moved) or claiming another row."""
    from hermes_state import SessionDB
    from hermes_state_runtime import (
        admit_session_input, begin_runtime_epoch, claim_session_input, list_session_admissions,
        settle_session_input,
    )

    db = SessionDB(tmp_path / 'state.db')
    try:
        db.create_session('s', 'cli')
        epoch = begin_runtime_epoch(db, instance_id='parity')
        order = ['automation:wake1', 'messaging:alice', 'Automation:spoof', 'automation:wake2',
                 'api', 'automation_x', 'messaging:bob', 'automation:wake3']
        for i, principal in enumerate(order):
            admit_session_input(db, epoch=epoch, principal_id=principal, session_id='s',
                                request_id=f'req-{i}', payload={'text': principal})
        claimed = []
        while True:
            queued = [r for r in list_session_admissions(db, session_id='s') if r['status'] == 'queued']
            first = min(queued, key=queued_head_key, default=None)
            row = claim_session_input(db, epoch=epoch, session_id='s',
                                      head=first['admission_id'] if first else None)
            if first is None:
                assert row is None
                break
            assert row is not False, f'SQL head moved off the Python head {first["principal_id"]!r}'
            assert isinstance(row, dict)
            assert row['admission_id'] == first['admission_id']
            claimed.append(row['principal_id'])
            settle_session_input(db, epoch=epoch, admission_id=row['admission_id'],
                                 generation=row['generation'], outcome='completed')
        # People (anything not exactly ``automation:``-prefixed) FIFO first, then wakes FIFO.
        assert claimed == ['messaging:alice', 'Automation:spoof', 'api', 'automation_x', 'messaging:bob',
                           'automation:wake1', 'automation:wake2', 'automation:wake3']
    finally:
        db.close()
