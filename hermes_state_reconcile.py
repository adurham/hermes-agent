"""Additive state.db schema drift: detection, the one-shot browse reconcile, and explicit repair.

Drift (a declared table/column missing) is not corruption. Browsing catches an unowned store up
once, additive DDL only (:func:`reconcile_drift_for_browse`); ``hermes sessions repair`` reconciles
a protected snapshot so failed DDL never leaves the store partly upgraded or escalates into FTS
surgery (:func:`repair_declarative_schema`). Neither runs while a gateway owns the store.
"""
import contextlib
import logging
import sqlite3
import threading
from contextlib import closing
from pathlib import Path


def schema_drift_reason(path: Path):
    """Return a missing declared table/column, not a corruption diagnosis."""
    from hermes_state_schema import schema_read_probe_statements
    from hermes_cli.sqlite_safe_read import connect_tracked

    with closing(connect_tracked(path.resolve().as_uri() + '?mode=ro', uri=True, tracking_path=path)) as conn:
        try:
            for statement in schema_read_probe_statements():
                conn.execute(statement).fetchone()
        except sqlite3.OperationalError as exc:
            from hermes_state_repair import _schema_not_built
            if _schema_not_built(exc):
                return str(exc)
            raise
    return None


def _reconcile_snapshot(path, guard):
    from hermes_state_repair import (
        _copy_database_snapshot, _db_opens_cleanly, _repair_conn,
        _repair_scratch_space_error, _unlink_db_triple,
    )
    from hermes_state_schema import SessionSchemaMixin, reconcile_state_schema

    scratch = path.with_name(path.name + '.reconcile-scratch')
    if error := _repair_scratch_space_error(path):
        raise RuntimeError(error)
    if error := _unlink_db_triple(scratch):
        raise RuntimeError(error)
    try:
        _copy_database_snapshot(path, scratch, source_connection=guard)
        with _repair_conn(scratch) as conn:
            _require_history_tables(conn)
            # Old indexed columns must exist before the canonical DDL can create
            # their indexes. Reuse the column owner; the final probe fails closed
            # if an additive change is not possible. Missing tables are created below.
            object.__new__(SessionSchemaMixin)._reconcile_columns(conn.cursor())
            reconcile_state_schema(conn)
            conn.commit()
        if reason := schema_drift_reason(scratch):
            raise RuntimeError(f'schema reconciliation incomplete: {reason}')
        if reason := _db_opens_cleanly(scratch):
            raise RuntimeError(f'reconciled snapshot is unhealthy: {reason}')
        _copy_database_snapshot(scratch, path, destination_connection=guard)
    finally:
        if error := _unlink_db_triple(scratch):
            import logging
            logging.getLogger(__name__).warning('Could not remove reconciliation snapshot: %s', error)


def _require_history_tables(conn):
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if {'sessions', 'messages'} - tables:
        raise RuntimeError('canonical history tables are missing; use sessions recover, not additive reconciliation')


def _reconcile_locked(path, backup, report):
    from hermes_state_repair import (
        _backup_db_file, _db_opens_cleanly, _exclusive_repair_db_guard,
        _live_writer_holds_db,
    )

    if _live_writer_holds_db(path):
        raise RuntimeError('a live writer still holds state.db; stop it and retry')
    from hermes_cli.sqlite_safe_read import connect_tracked
    with closing(connect_tracked(path.resolve().as_uri() + '?mode=ro', uri=True, tracking_path=path)) as conn:
        _require_history_tables(conn)
    if schema_drift_reason(path) is None:
        report.update(repaired=True, strategy='already_healthy')
        return
    # Additive repair is not a substitute for corruption recovery.
    if reason := _db_opens_cleanly(path):
        raise RuntimeError(reason)
    if backup:
        bpath, error = _backup_db_file(path)
        if bpath is None:
            raise RuntimeError(f'pre-repair backup refused: {error}')
        report['backup_path'] = str(bpath)
    with _exclusive_repair_db_guard(path) as (guard, error):
        if guard is None:
            raise RuntimeError(f'could not acquire exclusive state.db repair ownership: {error}')
        _reconcile_snapshot(path, guard)
        report.update(repaired=True, strategy='reconcile_declarative_schema')


# Browse-time reconcile (#140315). A store whose tables/columns lag SCHEMA_SQL is not corrupt:
# a writable open would catch it up, but an idle profile with no gateway never gets one, so every
# read-only surface (sidebar, /api/sessions/<id>/messages?profile=) lost its whole history.
# Catch it up ONCE, additive DDL only, in one short transaction, and only while holding that
# profile's gateway runtime lock. When a gateway owns the store it is the only writer: refuse
# by name with the remedy instead of becoming a second one.
#
# The full integrity scan is read-only and scales with file size, so it runs BEFORE the lock, on its
# own budget (BROWSE_INTEGRITY_BUDGET_S); a gateway booting meanwhile simply takes the lock. The
# scan is tied to the file's (size, mtime) signature: if the store changed by the time the lock is
# held, the scan no longer vouches for it and nothing is written. Under the lock only the cheap drift
# re-check and the DDL run, cut off by BROWSE_RECONCILE_BUDGET_S (a sqlite progress handler; the DDL
# rolls back). Busy waiting does not run the progress handler, so each connection's busy timeout is
# capped at the remaining budget. Gateway boot waits this budget out (gateway.run_bootstrap).
BROWSE_RECONCILE_BUDGET_S = 3.0
BROWSE_INTEGRITY_BUDGET_S = 20.0
_BUDGET_PROGRESS_OPS = 1000


class DeclarativeDriftError(RuntimeError):
    """The store lags SCHEMA_SQL (missing table/column) and browsing could not catch it up."""

    def __init__(self, detail: str, *, owned: bool = False):
        super().__init__(detail)
        self.owned = owned


def is_declarative_drift_error(exc: BaseException) -> bool:
    """``no such table/column`` from a schema read probe: drift, never corruption."""
    from hermes_state_repair import _schema_not_built
    return isinstance(exc, sqlite3.OperationalError) and _schema_not_built(exc)


def _owner_remedy(home: Path) -> tuple[str, str]:
    from gateway.status import _profile_label_for_home  # never raises: None when unknown
    label = _profile_label_for_home(home)
    if label and label != 'default':
        return label, f'hermes -p {label} gateway restart'
    return label or str(home), 'hermes gateway restart'


class BrowseBudgetExceeded(RuntimeError):
    """The browse reconcile could not finish inside BROWSE_RECONCILE_BUDGET_S; nothing was written."""


class _Budget:
    """One monotonic deadline for everything a browse does while holding ``gateway.lock``."""

    def __init__(self, seconds: float):
        import time
        self._clock = time.monotonic
        self.deadline = self._clock() + seconds
        self.expired = False

    def remaining(self) -> float:
        return max(self.deadline - self._clock(), 0.0)

    def busy_timeout(self) -> float:
        return max(self.remaining(), 0.05)

    def arm(self, conn) -> None:
        def progress():
            if self._clock() >= self.deadline:
                self.expired = True
                return 1  # sqlite raises OperationalError('interrupted'); the caller rolls back
            return 0
        conn.set_progress_handler(progress, _BUDGET_PROGRESS_OPS)

    def check(self) -> None:
        if self.expired or self.remaining() <= 0:
            self.expired = True
            raise BrowseBudgetExceeded('browse schema catch-up exceeded its time budget')


def _apply_additive_schema(path: Path, budget: _Budget | None = None) -> None:
    """Missing SCHEMA_SQL tables and columns, in ONE ``BEGIN IMMEDIATE`` transaction.

    No data migration, no FTS work, no schema_version stamp (that stays the owner's: the
    version gates data migrations this path does not run). Every statement is additive and
    idempotent. The declared shape is verified inside the transaction before COMMIT: a column
    the shared reconciler could not add (it logs and continues) or an expired budget rolls the
    store back to exactly what it was, never a partial upgrade."""
    from hermes_cli.sqlite_safe_read import connect_tracked
    from hermes_state_common import SCHEMA_SQL
    from hermes_state_repair import _reapply_durability_barriers, _schema_not_built
    from hermes_state_schema import SessionSchemaMixin, schema_read_probe_statements

    budget = budget or _Budget(BROWSE_RECONCILE_BUDGET_S)
    budget.check()
    with closing(connect_tracked(path, tracking_path=path, timeout=budget.busy_timeout(),
                                 isolation_level=None)) as conn:
        _reapply_durability_barriers(conn)
        budget.arm(conn)
        conn.execute('BEGIN IMMEDIATE')
        try:
            cursor = conn.cursor()
            shim = object.__new__(SessionSchemaMixin)
            # Columns first: SCHEMA_SQL indexes may name columns an old table lacks.
            shim._reconcile_columns(cursor)
            SessionSchemaMixin._execute_ddl_script_transactional(cursor, SCHEMA_SQL)
            shim._reconcile_columns(cursor)  # tables the script just created are complete; no-op
            # Postcondition, still inside the transaction: every declared table/column resolves.
            for statement in schema_read_probe_statements():
                try:
                    cursor.execute(statement).fetchone()
                except sqlite3.OperationalError as exc:
                    if _schema_not_built(exc):
                        raise DeclarativeDriftError(str(exc)) from exc
                    raise
            budget.check()
            conn.execute('COMMIT')
        except BaseException as exc:
            with contextlib.suppress(sqlite3.Error):
                conn.execute('ROLLBACK')
            if budget.expired and isinstance(exc, sqlite3.OperationalError):
                raise BrowseBudgetExceeded('browse schema catch-up exceeded its time budget') from exc
            raise


BROWSE_RECONCILE_PURPOSE = 'browse_schema_reconcile'
_BROWSE_SIBLING_POLL_S = 0.1


class CorruptStoreError(sqlite3.DatabaseError):
    """The lagging store is also damaged: browsing must not write DDL into it. Worded like sqlite's
    own malformed-image error so every caller maps it to the corrupt-store status (``hermes doctor``)."""

    def __init__(self, detail: str):
        super().__init__(f'database disk image is malformed (integrity_check: {detail})')


def _integrity_problem(path: Path, budget: _Budget):
    """``PRAGMA integrity_check`` on a read-only connection under *budget*: None when ``ok``, else the
    first problems. Full integrity_check, not quick_check: quick_check skips index-vs-table content,
    so an index with the right entry count but the wrong keys passes it. Raises
    :class:`BrowseBudgetExceeded` when the scan cannot finish in time (fail closed: no write)."""
    from hermes_cli.sqlite_safe_read import connect_tracked
    budget.check()
    try:
        with closing(connect_tracked(path.resolve().as_uri() + '?mode=ro', uri=True, tracking_path=path,
                                     timeout=budget.busy_timeout())) as conn:
            budget.arm(conn)
            rows = conn.execute('PRAGMA integrity_check(3)').fetchall()
    except sqlite3.DatabaseError as exc:
        if budget.expired:
            raise BrowseBudgetExceeded('integrity check exceeded the browse time budget') from exc
        from hermes_state_errors import is_malformed_db_error
        if is_malformed_db_error(exc):
            return str(exc)
        raise
    budget.check()
    problems = [str(r[0]) for r in rows if r and str(r[0]).lower() != 'ok']
    return '; '.join(problems[:3]) or None


def _wait_for_sibling_reconcile(path: Path, home: Path):
    """A sibling browse (another thread or process) holds the lock for its own reconcile: wait at
    at most the sibling's own execution budget. None once the store is current, else the remaining drift."""
    import time
    from gateway.runtime_ownership import live_runtime_owner
    deadline = time.monotonic() + BROWSE_RECONCILE_BUDGET_S + 0.5
    while True:
        if (reason := schema_drift_reason(path)) is None:
            return None
        if time.monotonic() >= deadline or live_runtime_owner(home) is not None:
            return reason
        time.sleep(_BROWSE_SIBLING_POLL_S)


def _store_signature(path: Path):
    """(size, mtime_ns) of the main file and its WAL: any committed write changes one of them."""
    out = []
    for p in (path, path.with_name(path.name + '-wal')):
        try:
            st = p.stat()
            out.append((st.st_size, st.st_mtime_ns))
        except FileNotFoundError:
            out.append(None)
    return tuple(out)


_scan_guard = threading.Lock()
_scan_locks: dict[str, threading.Lock] = {}
# Signatures whose integrity scan could not finish inside BROWSE_INTEGRITY_BUDGET_S. A sidebar polls
# every few seconds; rescanning a multi-GB file on each poll only burns I/O for the same answer.
_scan_too_slow: dict[str, tuple] = {}


def _path_scan_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _scan_guard:
        return _scan_locks.setdefault(key, threading.Lock())


def reconcile_drift_for_browse(path: Path) -> bool:
    """Catch a lagging store up for a read-only browse; True when DDL ran, False when already current.

    Raises :class:`DeclarativeDriftError` (``owned=True`` when a running gateway owns the store)
    with a user-facing detail naming the drift and the remedy, and :class:`CorruptStoreError`
    when the lagging store is also damaged. Never touches a store missing its history tables:
    recreating them empty would hide lost sessions."""
    from gateway.runtime_ownership import OwnershipConflict, exclusive_maintenance, live_runtime_owner
    from hermes_state_health import STORAGE_CORRUPT, mark_storage_corrupt, storage_corrupt_reason, storage_state

    reason = schema_drift_reason(path)
    if reason is None:
        return False
    from hermes_cli.sqlite_safe_read import connect_tracked
    with closing(connect_tracked(path.resolve().as_uri() + '?mode=ro', uri=True, tracking_path=path)) as conn:
        try:
            _require_history_tables(conn)
        except RuntimeError as exc:
            raise DeclarativeDriftError(
                f'Session store is missing its history tables ({reason}); this is not upgrade drift. '
                'Run `hermes sessions recover`.') from exc
    home = path.parent
    label, remedy = _owner_remedy(home)

    def owned(drift):
        return DeclarativeDriftError(
            f"Session store for profile '{label}' is behind this Hermes version ({drift}), and a "
            f'running gateway owns it, so browsing will not write to it. Restart that gateway '
            f'(`{remedy}`) to bring the store up to date.', owned=True)

    def too_slow(what):
        return DeclarativeDriftError(
            f"Session store for profile '{label}' is behind this Hermes version ({reason}) and could "
            f'not be {what} while browsing; nothing was changed and the list was not cleared. Retry, '
            f"or start this profile's gateway (`{remedy}`) or run `hermes sessions repair` to bring "
            'it up to date.')

    # A gateway already owns it: never scan or write, refuse by name at once. Probe the flock itself
    # (a record left by a released holder still names a live PID), holding it for no work at all.
    try:
        with exclusive_maintenance([home], purpose=BROWSE_RECONCILE_PURPOSE):
            pass
    except OwnershipConflict as exc:
        if live_runtime_owner(home) is not None:
            raise owned(reason) from exc
    # Already latched corrupt in this process: do not rescan a damaged file on every poll.
    if storage_state(path) == STORAGE_CORRUPT:
        raise CorruptStoreError(storage_corrupt_reason(path) or 'latched corrupt')
    key = str(path.resolve())
    with _path_scan_lock(path):  # one scan per store per process; concurrent polls wait for it
        if schema_drift_reason(path) is None:
            return False
        signature = _store_signature(path)
        if _scan_too_slow.get(key) == signature:
            raise too_slow(f'verified within {BROWSE_INTEGRITY_BUDGET_S:.0f}s')
        # Outside gateway.lock: read-only, may be slow on a large store, never blocks a gateway boot.
        try:
            problem = _integrity_problem(path, _Budget(BROWSE_INTEGRITY_BUDGET_S))
        except BrowseBudgetExceeded as exc:
            _scan_too_slow[key] = signature
            raise too_slow(f'verified within {BROWSE_INTEGRITY_BUDGET_S:.0f}s') from exc
        if problem:
            # A failing column probe hides damage elsewhere in the file: additive DDL is not
            # corruption recovery, so never write into a store that is not verified clean.
            mark_storage_corrupt(path, problem)
            raise CorruptStoreError(problem)
        try:
            with exclusive_maintenance([home], purpose=BROWSE_RECONCILE_PURPOSE):
                budget = _Budget(BROWSE_RECONCILE_BUDGET_S)
                # Re-check under the lock: a gateway that started and released between probes
                # (or a sibling browse) may already have caught the store up.
                if schema_drift_reason(path) is None:
                    return False
                if _store_signature(path) != signature:
                    # Written since the scan (a gateway that came and went): the scan no longer
                    # vouches for these bytes. Refuse; the next browse scans the new state.
                    raise DeclarativeDriftError(
                        f"Session store for profile '{label}' changed while browsing verified it "
                        f'({reason}); nothing was changed. Retry in a moment; the list was not cleared.')
                try:
                    _apply_additive_schema(path, budget)
                except DeclarativeDriftError as exc:
                    raise DeclarativeDriftError(
                        f"Session store for profile '{label}' is behind this Hermes version and could not "
                        f'be caught up ({exc}); nothing was changed. Run `hermes sessions repair` for that '
                        'profile.') from exc
        except BrowseBudgetExceeded as exc:
            raise too_slow(f'caught up within {BROWSE_RECONCILE_BUDGET_S:.0f}s') from exc
        except OwnershipConflict as exc:
            if live_runtime_owner(home) is not None:
                raise owned(reason) from exc
            # No gateway: the holder is a sibling browse's own reconcile (or other short maintenance).
            if (pending := _wait_for_sibling_reconcile(path, home)) is None:
                return False
            if live_runtime_owner(home) is not None:
                raise owned(pending) from exc
            raise DeclarativeDriftError(
                f"Session store for profile '{label}' is being brought up to date by another Hermes "
                f'process ({pending}). Retry in a moment; the list was not cleared.') from exc
    if after := schema_drift_reason(path):
        raise DeclarativeDriftError(
            f"Session store for profile '{label}' is behind this Hermes version and could not be "
            f'caught up ({after}). Run `hermes sessions repair` for that profile.')
    logging.getLogger(__name__).warning(
        'state.db at %s lagged SCHEMA_SQL (%s); added the missing tables/columns for browsing', path, reason)
    return True


def repair_declarative_schema(path: Path, *, backup: bool = True):
    """Reuse repair's snapshot/SQLite exclusion beneath the runtime owner lock."""
    from gateway.runtime_ownership import OwnershipConflict, exclusive_maintenance
    from hermes_state_repair import _cross_process_repair_lock

    report = {'repaired': False, 'strategy': None, 'backup_path': None, 'error': None}
    try:
        with exclusive_maintenance([path.parent]):
            with _cross_process_repair_lock(path) as holding:
                if not holding:
                    raise RuntimeError('could not obtain the state.db repair lock')
                _reconcile_locked(path, backup, report)
    except (OwnershipConflict, OSError, sqlite3.Error, RuntimeError, TimeoutError) as exc:
        report['error'] = str(exc)
    return report
