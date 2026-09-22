"""FORK: the salvage lane must recognise a fork-shaped store, and the fork's own
column reconcile must not swallow lock contention.

Two independent regressions from the v2026.9.14 merge, both in the
"a fork store is silently mishandled" class:

1. ``hermes_cli.session_schema_history`` is upstream's source of truth for the
   physical column layouts ``hermes sessions recover`` maps salvaged cells by
   NAME against. The fork ALTER-ADDs its own columns
   (``hermes_state.FORK_TABLE_COLUMNS``), so a real fork store's physical order
   was in NO candidate layout -- while its WIDTH matched many upstream ones.
   Recovery therefore mapped a fork store to the wrong column names instead of
   rejecting it, silently moving cell values between columns.

2. ``SessionDB._reconcile_columns``' fork pass logged every ``OperationalError``
   at DEBUG. A busy/locked ALTER left the store permanently half-reconciled
   ("no such column" on every read) because the lock-patience wrapper never
   learned init had failed. Upstream's own pass re-raises on locked/busy.
"""

from __future__ import annotations

import sqlite3

import pytest

import hermes_state
import hermes_state_schema
from hermes_state import FORK_TABLE_COLUMNS, SessionDB
from hermes_cli.session_schema_history import (
    FORK_COLUMN_ARRIVALS,
    SCHEMA_HISTORY,
    current_declared_columns,
    reachable_physical_layouts,
)

FORK_TABLES = sorted(FORK_TABLE_COLUMNS)


def _physical_columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")'))


@pytest.fixture
def fork_store(tmp_path, monkeypatch) -> sqlite3.Connection:
    """A real fork-schema store, opened through the real SessionDB."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "s_fork_recovery_probe"
    db.create_session(session_id, source="cli", model="claude-opus-5", cwd=str(tmp_path))
    db.append_message(session_id, "user", content="hello fork store")
    db.append_message(
        session_id, "assistant", content="hi there",
        anthropic_content_blocks=[{"type": "text", "text": "hi there"}],
        finish_reason="end_turn", token_count=12,
    )
    db.increment_compression_attempts_total(session_id)
    return sqlite3.connect(str(tmp_path / "state.db"))


# ── 1. salvage layout recognition ───────────────────────────────────────


@pytest.mark.parametrize("table", FORK_TABLES)
def test_fork_columns_are_declared_in_the_schema_history(table: str) -> None:
    """Every fork column on an upstream table needs a FORK_COLUMN_ARRIVALS entry,
    or its layouts are unreachable and recovery maps the store by width alone."""
    assert table in SCHEMA_HISTORY, f"{table} is not salvage-mapped; drop it from this test"
    declared = {column for column, _index in FORK_COLUMN_ARRIVALS.get(table, ())}
    missing = sorted(set(FORK_TABLE_COLUMNS[table]) - declared)
    assert not missing, (
        f"FORK_TABLE_COLUMNS[{table!r}] adds {missing} but FORK_COLUMN_ARRIVALS does not "
        f"declare them: hermes sessions recover will not recognise a fork store's layout "
        f"and will map its cells to same-width UPSTREAM column names instead. Add "
        f"(column, <newest event index for this table>) to FORK_COLUMN_ARRIVALS."
    )


@pytest.mark.parametrize("table", FORK_TABLES)
def test_live_fork_layout_is_a_reachable_salvage_candidate(
    fork_store: sqlite3.Connection, table: str
) -> None:
    """The layout a real SessionDB open actually produces must be in the
    candidate set the salvage lane walks -- by name, not by width."""
    live = _physical_columns(fork_store, table)
    width = len(live)
    # Prune by width: the full graph is large and only same-width states matter here.
    candidates = [
        layout for layout in reachable_physical_layouts(table, lambda lay, _f: len(lay) <= width)
        if len(layout) == width
    ]
    assert live in candidates, (
        f"the live fork {table} layout (width {width}) is not a reachable candidate, but "
        f"{len(candidates)} wrong same-width layouts are -- recovery would map this store "
        f"to one of those instead of to its real columns"
    )


@pytest.mark.parametrize("table", FORK_TABLES)
def test_fork_layout_carries_the_fork_column_last(
    fork_store: sqlite3.Connection, table: str
) -> None:
    """ALTER TABLE ADD COLUMN appends, so the fork's columns sit after every
    upstream one -- the assumption FORK_COLUMN_ARRIVALS encodes."""
    live = _physical_columns(fork_store, table)
    upstream = current_declared_columns(table)
    assert live[: len(upstream)] == upstream, (
        f"{table}: the fork store's first {len(upstream)} columns no longer match the "
        f"upstream declared order; FORK_COLUMN_ARRIVALS' append model is invalid"
    )
    assert set(live[len(upstream):]) == set(FORK_TABLE_COLUMNS[table])


def test_salvage_maps_a_torn_fork_message_row_to_its_real_columns(
    fork_store: sqlite3.Connection,
) -> None:
    """End-to-end: a torn (mostly-NULL) fork ``messages`` record must resolve to
    its REAL column names. Before the FORK_COLUMN_ARRIVALS fix this mapped six
    positions wrong, e.g. anthropic_content_blocks -> display_order.
    """
    from hermes_cli.session_lost_and_found import (
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


# ── 2. fork column reconcile must not swallow lock contention ───────────


def _reconcile_under_write_lock(tmp_path, monkeypatch):
    """Run ONLY the fork column pass against a WAL store whose write lock is held
    by another connection. Returns the raised exception (or None).

    WAL is what Hermes actually uses: reads still succeed, so the PRAGMA probe
    passes and the failure lands exactly on the fork ALTER -- the real-world shape.
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

        class _Shim(SessionDB):
            def __init__(self) -> None:  # no real open; only the reconcile is under test
                pass

        # Isolate the fork pass from the FK heal, FORK_SCHEMA_SQL and upstream's own pass.
        monkeypatch.setattr(hermes_state, "FORK_SCHEMA_SQL", "-- noop\n")
        monkeypatch.setattr(
            hermes_state_schema.SessionSchemaMixin, "_reconcile_columns",
            lambda self, cursor: None,
        )
        try:
            _Shim()._reconcile_columns(victim.cursor())
        except sqlite3.OperationalError as exc:
            return exc, victim
        return None, victim
    finally:
        with sqlite3.connect(":memory:"):
            pass
        try:
            blocker.rollback()
        finally:
            blocker.close()


def test_locked_fork_column_alter_propagates(tmp_path, monkeypatch) -> None:
    """A locked/busy ALTER must RE-RAISE so the lock-patience wrapper retries init.
    Swallowing it left the store half-reconciled forever."""
    raised, victim = _reconcile_under_write_lock(tmp_path, monkeypatch)
    try:
        assert raised is not None, (
            "the fork column pass swallowed a genuine write-lock failure; the store is "
            "now half-reconciled and every read of the fork column raises 'no such column'"
        )
        assert "locked" in str(raised).lower() or "busy" in str(raised).lower()
        # The store really is behind: that is why the caller must be told.
        live = _physical_columns(victim, "messages")
        assert "anthropic_content_blocks" not in live
    finally:
        victim.close()


def test_duplicate_fork_column_is_still_swallowed(tmp_path, monkeypatch) -> None:
    """The race a sibling process wins is benign and must stay quiet -- only
    lock/busy escalates."""
    path = tmp_path / "dup.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT);"
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT);"
    )
    conn.commit()

    class _Shim(SessionDB):
        def __init__(self) -> None:
            pass

    monkeypatch.setattr(hermes_state, "FORK_SCHEMA_SQL", "-- noop\n")
    monkeypatch.setattr(
        hermes_state_schema.SessionSchemaMixin, "_reconcile_columns",
        lambda self, cursor: None,
    )

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
        _Shim()._reconcile_columns(_DuplicateOnAlterCursor(conn.cursor()))  # type: ignore[arg-type]
    finally:
        conn.close()


def test_non_lock_fork_column_failure_is_loud_but_not_fatal(tmp_path, monkeypatch, caplog) -> None:
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

    class _Shim(SessionDB):
        def __init__(self) -> None:
            pass

    monkeypatch.setattr(hermes_state, "FORK_SCHEMA_SQL", "-- noop\n")
    monkeypatch.setattr(
        hermes_state_schema.SessionSchemaMixin, "_reconcile_columns",
        lambda self, cursor: None,
    )
    # A NOT NULL column with no default is exactly the "schema mistake" case.
    monkeypatch.setattr(
        hermes_state, "FORK_TABLE_COLUMNS",
        {"messages": {"bad_fork_col": "TEXT NOT NULL"}},
    )
    try:
        with caplog.at_level(logging.WARNING):
            _Shim()._reconcile_columns(conn.cursor())  # must not raise
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("bad_fork_col" in r.getMessage() for r in warnings), (
            f"expected a WARNING naming the failed column, got "
            f"{[r.getMessage() for r in warnings]}"
        )
    finally:
        conn.close()
