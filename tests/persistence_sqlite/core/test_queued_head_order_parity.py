"""One people-before-wakes order, written in SQL and in Python, around one admission store.

``claim_session_input`` takes the SQL head (``QUEUED_HEAD_ORDER_SQL``); a preclaim read takes the
listing's ``head`` order, or sorts a ``seq`` listing by its Python twin (``queued_head_key``). If the
two disagree on any row, the claim refuses (``False``) or the caller validates a row that is not the
one that runs. Nothing else asserts they agree, so this does.

An automation wake is a principal under ``automation:<route>``; a person's input is everything else.
A person waiting must never sit behind a wake (#141822): people FIFO first, then wakes FIFO.
"""
from __future__ import annotations

import itertools
import sqlite3

import pytest

from persistence.authority.vocabulary import AUTOMATION_PRINCIPAL_PREFIX, queued_head_key
from persistence.conversation.sessions import NewSession
from persistence.sqlite.authority._admission_store import QUEUED_HEAD_CLASS_SQL, QUEUED_HEAD_ORDER_SQL

SESSION = 'sess-queued-head'
EPOCH_INSTANCE = 'queued-head'

# People, internal wakes, and principals that only LOOK like one or the other: a case variant
# (SQLite LIKE is ASCII case-insensitive, GLOB and str.startswith are not), the bare prefix, a near miss
# without the colon, a prefix in the middle, and a GLOB/LIKE metacharacter in the id.
PRINCIPALS = [
    'messaging:telegram:42', 'api', 'cron', 'msgraph:route',
    'automation:agent:main:local:dm:x', 'automation:', 'automation:%',
    'Automation:agent:main', 'AUTOMATION:x', 'automation_x', 'automationx', 'x:automation:y',
    'automation*', 'automation:[a]',
]


def _scratch(rows=()):
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    # Same columns the rule reads; seq NOT unique here so key ties are exercised too.
    conn.execute('CREATE TABLE q (admission_id TEXT, principal_id TEXT NOT NULL, seq INTEGER NOT NULL)')
    conn.executemany('INSERT INTO q VALUES (?,?,?)', rows)
    return conn


def _sql_keys(conn):
    return {r['admission_id']: (bool(r['k']), r['seq'])
            for r in conn.execute('SELECT admission_id, seq, ' + QUEUED_HEAD_CLASS_SQL + ' AS k FROM q')}


def test_every_principal_gets_the_same_class_in_sql_and_python():
    """Row-level parity: the strongest form — if every row's key agrees, every head agrees."""
    conn = _scratch([(f'a{i}', p, i) for i, p in enumerate(PRINCIPALS)])
    py = {r['admission_id']: queued_head_key(dict(r)) for r in conn.execute('SELECT * FROM q')}
    assert _sql_keys(conn) == py


@pytest.mark.parametrize('size', [2, 3, 4])
def test_sql_head_equals_python_head_on_every_mixed_queue(size):
    """Exhaustive small queues: every principal mix, every seq assignment including ties.

    On a key tie (same class, same seq) neither side defines an order, so the heads must be tied
    rows: same key. With a unique seq (the real schema) the head row is identical.
    """
    pool = ['messaging:a', 'automation:w', 'Automation:w', 'api']
    conn = _scratch()
    checked = 0
    for principals in itertools.product(pool, repeat=size):
        for seqs in itertools.product(range(1, size + 1), repeat=size):
            rows = [(f'r{i}', p, s) for i, (p, s) in enumerate(zip(principals, seqs))]
            conn.execute('DELETE FROM q')
            conn.executemany('INSERT INTO q VALUES (?,?,?)', rows)
            sql_head = conn.execute('SELECT * FROM q ORDER BY ' + QUEUED_HEAD_ORDER_SQL + ' LIMIT 1').fetchone()
            listing = [dict(r) for r in conn.execute('SELECT * FROM q ORDER BY seq')]
            py_head = min(listing, key=queued_head_key)
            assert queued_head_key(dict(sql_head)) == queued_head_key(py_head), rows
            if len(set(seqs)) == size:
                assert sql_head['admission_id'] == py_head['admission_id'], rows
            checked += 1
    assert checked == len(pool) ** size * size ** size


def test_the_automation_prefix_holds_no_glob_metacharacters():
    """``QUEUED_HEAD_ORDER_SQL`` splices the prefix into a GLOB pattern unescaped: a ``*``, ``?`` or ``[``
    in it would silently change which rows sort as internal wakes (and break parity with startswith)."""
    assert AUTOMATION_PRINCIPAL_PREFIX and not set(AUTOMATION_PRINCIPAL_PREFIX) & set('*?[')
    assert QUEUED_HEAD_CLASS_SQL == f"(principal_id GLOB '{AUTOMATION_PRINCIPAL_PREFIX}*')"
    assert QUEUED_HEAD_ORDER_SQL == QUEUED_HEAD_CLASS_SQL + ', seq'


def _store(backend, scope_a):
    store = backend.open(scope_a)
    store.sessions.create(NewSession(session_id=SESSION, source='contract'))
    store.ready()  # the holder is open before the test's first verb
    return store, store.runtime.begin_epoch(instance_id=EPOCH_INSTANCE)


def _admit(store, epoch, principal_id, request_id):
    record, created = store.admissions.admit(epoch=epoch, principal_id=principal_id, session_id=SESSION,
                                             request_id=request_id, payload={'text': principal_id})
    assert created
    return record


def _queued_head(store):
    """The head the way a drain's preclaim read takes it: the first queued row of the head order."""
    ordered = store.admissions.list(session_id=SESSION, order='head')
    return next((row for row in ordered if row.status == 'queued'), None)


def test_the_public_listing_and_claim_take_the_same_head(backend, scope_a):
    """End to end on the real schema: admit an interleaved queue, then repeatedly take the head the
    head-ordered listing names and claim it with ``head=`` pinned. A disagreement shows up as ``claim``
    returning ``False`` (the head moved) or claiming another row."""
    store, epoch = _store(backend, scope_a)
    order = ['automation:wake1', 'messaging:alice', 'Automation:spoof', 'automation:wake2',
             'api', 'automation_x', 'messaging:bob', 'automation:wake3']
    for i, principal in enumerate(order):
        _admit(store, epoch, principal, f'req-{i}')
    # Row-level parity through the public reads: the head order is the seq listing sorted by the twin.
    assert ([row.admission_id for row in store.admissions.list(session_id=SESSION, order='head')]
            == [row.admission_id
                for row in sorted(store.admissions.list(session_id=SESSION), key=queued_head_key)])
    claimed = []
    while True:
        first = _queued_head(store)
        row = store.admissions.claim(epoch=epoch, session_id=SESSION,
                                     head=first.admission_id if first is not None else None)
        if first is None:
            assert row is None
            break
        assert row is not False, f'claim took a row other than the listed head {first.principal_id!r}'
        assert row.admission_id == first.admission_id
        claimed.append(row.principal_id)
        store.admissions.settle(epoch=epoch, admission_id=row.admission_id,
                                generation=row.generation, outcome='completed')
    # People (anything not exactly ``automation:``-prefixed) FIFO first, then wakes FIFO.
    assert claimed == ['messaging:alice', 'Automation:spoof', 'api', 'automation_x', 'messaging:bob',
                       'automation:wake1', 'automation:wake2', 'automation:wake3']


def test_a_person_queued_behind_a_wake_is_claimed_first(backend, scope_a):
    """The reported shape: a completion lands during a long turn, then the person types. People first."""
    store, epoch = _store(backend, scope_a)
    wake = _admit(store, epoch, 'automation:agent:main:local:dm:x', 'wake')
    person = _admit(store, epoch, 'messaging:telegram:42', 'message')
    claimed = store.admissions.claim(epoch=epoch, session_id=SESSION)
    assert claimed.admission_id == person.admission_id != wake.admission_id


@pytest.mark.parametrize('principal', ['Automation:x', 'AUTOMATION:x', 'automation_x', 'automationx',
                                       'x:automation:y', 'automation*'])
def test_a_near_miss_principal_is_a_person_not_a_wake(backend, scope_a, principal):
    """GLOB, not LIKE: a case variant or a metacharacter in the id is a person, so an older wake waits."""
    store, epoch = _store(backend, scope_a)
    _admit(store, epoch, 'automation:wake', 'wake')
    person = _admit(store, epoch, principal, 'message')
    assert store.admissions.claim(epoch=epoch, session_id=SESSION).admission_id == person.admission_id


def test_a_bracketed_id_under_the_prefix_is_still_a_wake(backend, scope_a):
    """The other side of the metacharacter case: a person-looking id that DOES start with the prefix is
    a wake (``automation:[a]`` starts with ``automation:``), so an earlier person's input still wins."""
    store, epoch = _store(backend, scope_a)
    wake = _admit(store, epoch, 'automation:[a]', 'wake')
    person = _admit(store, epoch, 'api', 'message')
    assert store.admissions.claim(epoch=epoch, session_id=SESSION).admission_id == person.admission_id
    assert queued_head_key(wake) == (True, wake.seq)
