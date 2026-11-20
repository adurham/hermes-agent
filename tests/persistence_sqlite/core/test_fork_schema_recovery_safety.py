"""FORK: the salvage lane must recognise a fork-shaped store, and the column
reconciler must not swallow lock contention.

Two independent regressions from the v2026.9.14 merge, both in the
"a fork store is silently mishandled" class:

1. ``persistence.sqlite.admin._schema_history`` is upstream's source of truth for the
   physical column layouts ``hermes sessions recover`` maps salvaged cells by
   NAME against. The fork's own columns (``FORK_TABLE_COLUMNS``) are declared in
   ``SCHEMA_SQL`` now, but every store a fork release wrote BEFORE that
   reconciliation has them appended by ``ALTER TABLE ADD COLUMN`` -- a layout the
   pure-upstream replay can never produce. Without the ``FORK_COLUMN_ARRIVALS``
   variants, recovery mapped such a store to a same-width upstream layout instead
   of rejecting it, silently moving cell values between columns.

2. The column reconciler logged every non-lock ``OperationalError`` at DEBUG. A
   busy/locked ALTER must re-raise so the lock-patience wrapper retries init;
   swallowing it left the store permanently half-reconciled ("no such column" on
   every read).
"""

from __future__ import annotations

import sqlite3

import pytest

from persistence.conversation.messages import NewMessage
from persistence.conversation.sessions import NewSession
from persistence.sqlite import _schema as schema
from persistence.sqlite.admin._schema_history import (
    FORK_COLUMN_ARRIVALS,
    FORK_TABLE_COLUMNS,
    SCHEMA_HISTORY,
    current_declared_columns,
    reachable_physical_layouts,
    upstream_declared_snapshots,
)
from persistence_assembly import open_persistence, profile_scope

FORK_TABLES = sorted(FORK_TABLE_COLUMNS)


def _physical_columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")'))


def _is_reachable(table: str, live: tuple[str, ...]) -> bool:
    """Whether a chain of declared snapshots reaches exactly *live*.

    The walk appends columns, so a candidate that already names a different column at any position
    can never become *live*: pruning on that keeps the walk to the layout's own prefixes instead of
    materialising the whole (exponential) graph.
    """
    def accept(layout: tuple[str, ...], _first_new: int) -> bool:
        return len(layout) <= len(live) and layout == live[: len(layout)]
    return any(layout == live for layout in reachable_physical_layouts(table, accept))


@pytest.fixture
def fork_store(tmp_path) -> sqlite3.Connection:
    """A real store written through the real persistence handle, read back raw."""
    handle = open_persistence(profile_scope(home=tmp_path), eager=True)
    try:
        handle.sessions.create(NewSession(session_id="s_fork_recovery_probe", source="cli",
                                          model="claude-opus-5", cwd=str(tmp_path)))
        handle.messages.append("s_fork_recovery_probe", NewMessage(
            role="user", content="hello fork store"))
        handle.messages.append("s_fork_recovery_probe", NewMessage(
            role="assistant", content="hi there", finish_reason="end_turn", token_count=12,
            anthropic_content_blocks=[{"type": "text", "text": "hi there"}]))
        handle.lineage.increment_compression_attempts_total("s_fork_recovery_probe")
    finally:
        handle.close()
    return sqlite3.connect(str(tmp_path / "state.db"))


def _store_at_previous_snapshot(path, table: str) -> sqlite3.Connection:
    """A store shaped like the release before the fork columns were declared in ``SCHEMA_SQL``.

    That is the layout every fork store written before this merge physically has once the fork's
    own reconcile appended its columns: the previous declared snapshot, then the fork columns.
    """
    columns = upstream_declared_snapshots(table)[-2]
    quoted = ", ".join('"' + column + '" TEXT' for column in columns)
    conn = sqlite3.connect(str(path))
    conn.execute(f'CREATE TABLE "{table}" ({quoted})')
    conn.commit()
    return conn


# ── 1. salvage layout recognition ───────────────────────────────────────


@pytest.mark.parametrize("table", FORK_TABLES)
def test_fork_columns_are_declared_in_the_schema_history(table: str) -> None:
    """Every fork column on an upstream table needs a FORK_COLUMN_ARRIVALS entry,
    or the layouts of the stores the fork wrote before this merge are unreachable
    and recovery maps them by width alone."""
    assert table in SCHEMA_HISTORY, f"{table} is not salvage-mapped; drop it from this test"
    declared = {column for column, _index in FORK_COLUMN_ARRIVALS.get(table, ())}
    missing = sorted(set(FORK_TABLE_COLUMNS[table]) - declared)
    assert not missing, (
        f"FORK_TABLE_COLUMNS[{table!r}] adds {missing} but FORK_COLUMN_ARRIVALS does not "
        f"declare them: hermes sessions recover will not recognise such a store's layout "
        f"and will map its cells to same-width UPSTREAM column names instead. Add "
        f"(column, <newest event index for this table>) to FORK_COLUMN_ARRIVALS."
    )


@pytest.mark.parametrize("table", FORK_TABLES)
def test_live_fork_layout_is_a_reachable_salvage_candidate(
    fork_store: sqlite3.Connection, table: str
) -> None:
    """The layout a real store open actually produces must be in the
    candidate set the salvage lane walks -- by name, not by width."""
    live = _physical_columns(fork_store, table)
    assert _is_reachable(table, live), (
        f"the live {table} layout (width {len(live)}) is not reachable from any chain of "
        f"declared snapshots -- recovery would map this store by width alone instead of to "
        f"its real columns"
    )


@pytest.mark.parametrize("table", FORK_TABLES)
def test_an_upgraded_pre_merge_fork_layout_is_a_reachable_salvage_candidate(
    tmp_path, table: str
) -> None:
    """A store created before the fork columns were declared, then reconciled onto
    them, must also resolve by name: its physical order is the older snapshot with
    the fork columns appended, which only the FORK_COLUMN_ARRIVALS variants cover."""
    conn = _store_at_previous_snapshot(tmp_path / f"upgraded_{table}.db", table)
    try:
        schema._reconcile_columns(conn.cursor(), tmp_path)
        conn.commit()
        live = _physical_columns(conn, table)
    finally:
        conn.close()
    assert _is_reachable(table, live), (
        f"the reconciled {table} layout (width {len(live)}) is not a reachable salvage candidate; "
        f"FORK_COLUMN_ARRIVALS no longer covers a store created before the fork columns were "
        f"declared"
    )


@pytest.mark.parametrize("table", FORK_TABLES)
def test_an_upgraded_store_carries_the_fork_columns_last(tmp_path, table: str) -> None:
    """ALTER TABLE ADD COLUMN appends, so a store upgraded from before the declaration
    has the fork's columns after every upstream one -- the assumption
    FORK_COLUMN_ARRIVALS encodes."""
    conn = _store_at_previous_snapshot(tmp_path / f"appended_{table}.db", table)
    try:
        schema._reconcile_columns(conn.cursor(), tmp_path)
        conn.commit()
        live = _physical_columns(conn, table)
    finally:
        conn.close()
    previous = upstream_declared_snapshots(table)[-2]
    assert live[: len(previous)] == previous, (
        f"{table}: the reconciled store's first {len(previous)} columns no longer match the "
        f"pre-fork declared order; FORK_COLUMN_ARRIVALS' append model is invalid"
    )
    assert set(live[len(previous):]) == set(FORK_TABLE_COLUMNS[table])
    # Today's declaration declares the same columns, in its own order, after the same prefix.
    assert set(current_declared_columns(table)[len(previous):]) == set(FORK_TABLE_COLUMNS[table])


def test_salvage_maps_a_torn_fork_message_row_to_its_real_columns(
    fork_store: sqlite3.Connection,
) -> None:
    """End-to-end: a torn (mostly-NULL) fork ``messages`` record must resolve to
    its REAL column names. Before the FORK_COLUMN_ARRIVALS fix this mapped six
    positions wrong, e.g. anthropic_content_blocks -> display_order.
    """
    from persistence.sqlite.admin._lost_and_found import (
        LayoutEvidence, _declared_types, infer_physical_layouts,
    )

    columns = _physical_columns(fork_store, "messages")
    quoted = ", ".join(f'"{c}"' for c in columns)
    rows = list(fork_store.execute(f"SELECT {quoted} FROM messages"))
    assert rows, "fixture wrote no messages"

    evidence = LayoutEvidence("messages")
    for row in rows:
        # Realistic salvage shape: identity + a few leading cells survive, rest NULL.
        evidence.add(tuple(value if index < 6 else None for index, value in enumerate(row)))

    mapping = infer_physical_layouts(evidence, _declared_types(fork_store, "messages")).get(
        len(columns)
    )
    assert mapping is not None, "no layout inferred for the fork width"
    misnamed = [
        (index, columns[index], mapping[index])
        for index in range(len(columns))
        if mapping[index] is not None and mapping[index] != columns[index]
    ]
    assert not misnamed, (
        f"recovery mapped fork cells to the wrong columns: {misnamed} -- each entry is "
        f"(position, real column, column recovery would have written it to)"
    )
    # The fork column itself must resolve by name or stay unresolved, never be renamed.
    fork_index = columns.index("anthropic_content_blocks")
    assert mapping[fork_index] in (None, "anthropic_content_blocks")


# ── 2. the reconciler must not swallow lock contention ──────────────────


def _only_fork_columns(monkeypatch, columns: dict[str, str] | None = None) -> None:
    """Point the reconciler at the fork's columns alone, as the fork's own pass used to be.

    The real entry point reconciles every SCHEMA_SQL column; injecting the expected set keeps these
    tests about the reconciler's ERROR CLASSIFICATION (duplicate vs locked vs broken DDL) instead of
    about the other columns a three-column fixture table lacks.
    """
    expected = {"messages": columns or {"anthropic_content_blocks": "TEXT"}}
    monkeypatch.setattr(schema, "_parse_schema_columns", lambda _sql, _home: expected)


def _reconcile_under_write_lock(tmp_path, monkeypatch):
    """Run the column pass against a WAL store whose write lock is held by another
    connection. Returns the raised exception (or None).

    WAL is what Hermes actually uses: reads still succeed, so the PRAGMA probe
    passes and the failure lands exactly on the ALTER -- the real-world shape.
    """
    path = tmp_path / "locked.db"
    seed = sqlite3.connect(str(path))
    seed.execute("PRAGMA journal_mode=WAL")
    seed.executescript(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT);"
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT);"
    )
    seed.commit()
    seed.close()

    blocker = sqlite3.connect(str(path), timeout=0.1, isolation_level=None)
    victim = sqlite3.connect(str(path), timeout=0.1)
    try:
        blocker.execute("PRAGMA journal_mode=WAL")
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute("INSERT INTO sessions (id, source) VALUES ('x', 'cli')")

        _only_fork_columns(monkeypatch)
        try:
            schema._reconcile_columns(victim.cursor(), tmp_path)
        except sqlite3.OperationalError as exc:
            return exc, victim
        return None, victim
    finally:
        try:
            blocker.rollback()
        finally:
            blocker.close()


def test_locked_column_alter_propagates(tmp_path, monkeypatch) -> None:
    """A locked/busy ALTER must RE-RAISE so the lock-patience wrapper retries init.
    Swallowing it left the store half-reconciled forever."""
    raised, victim = _reconcile_under_write_lock(tmp_path, monkeypatch)
    try:
        assert raised is not None, (
            "the column pass swallowed a genuine write-lock failure; the store is "
            "now half-reconciled and every read of the fork column raises 'no such column'"
        )
        assert "locked" in str(raised).lower() or "busy" in str(raised).lower()
        # The store really is behind: that is why the caller must be told.
        live = _physical_columns(victim, "messages")
        assert "anthropic_content_blocks" not in live
    finally:
        victim.close()


def test_duplicate_column_is_still_swallowed(tmp_path, monkeypatch) -> None:
    """The race a sibling process wins is benign and must stay quiet -- only
    lock/busy escalates."""
    path = tmp_path / "dup.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT);"
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT);"
    )
    conn.commit()
    _only_fork_columns(monkeypatch)

    class _DuplicateOnAlterCursor:
        """sqlite3.Cursor is an immutable C type, so wrap rather than patch it."""

        def __init__(self, cursor: sqlite3.Cursor) -> None:
            self._cursor = cursor

        def execute(self, sql, *args):
            if "ADD COLUMN" in sql:
                raise sqlite3.OperationalError(
                    "duplicate column name: anthropic_content_blocks"
                )
            return self._cursor.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._cursor, name)

    try:
        # Must not raise: a duplicate column means a sibling already did the work.
        schema._reconcile_columns(_DuplicateOnAlterCursor(conn.cursor()), tmp_path)  # type: ignore[arg-type]
    finally:
        conn.close()


def test_non_lock_column_failure_is_loud_but_not_fatal(tmp_path, monkeypatch, caplog) -> None:
    """A schema mistake (bad column type) must warn, not raise -- matching
    upstream's classification. Previously it was DEBUG-only and invisible."""
    import logging

    path = tmp_path / "badtype.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT);"
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT);"
    )
    # SQLite permits ADD COLUMN ... NOT NULL (no default) on an EMPTY table, so the
    # store must hold a row for this to be the schema mistake we want to exercise.
    conn.execute("INSERT INTO messages (session_id, role) VALUES ('s', 'user')")
    conn.commit()
    # A NOT NULL column with no default is exactly the "schema mistake" case.
    _only_fork_columns(monkeypatch, {"bad_fork_col": "TEXT NOT NULL"})
    try:
        with caplog.at_level(logging.WARNING):
            schema._reconcile_columns(conn.cursor(), tmp_path)  # must not raise
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("bad_fork_col" in r.getMessage() for r in warnings), (
            f"expected a WARNING naming the failed column, got "
            f"{[r.getMessage() for r in warnings]}"
        )
    finally:
        conn.close()
