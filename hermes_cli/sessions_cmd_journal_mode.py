"""`hermes sessions set-journal-mode delete|wal` — the offline self-service path for #100896.

``database.journal_mode: delete`` can never self-apply on a store that is already WAL:
``apply_wal_with_fallback`` deliberately never live-downgrades (#68545 — other gateway/cron/worker connections
may hold uncheckpointed WAL commits). Before this command the only escape hatch was an undocumented hand-run
``PRAGMA journal_mode=DELETE``. This runs it under the same admission the maintenance paths use: refuse while
ANY foreign process holds the file or a sidecar (``foreign_state_db_holders``), flip without waiting out openers
(``_set_journal_mode_no_wait``), then verify the header bytes 18/19 SQLite writes for the mode.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

# SQLite file header: bytes 18 (write version) / 19 (read version) are 1 for rollback-journal, 2 for WAL.
_HEADER_VERSION_BYTES = {"delete": (1, 1), "wal": (2, 2)}


def _header_mode(db_path: Path) -> str:
    """Journal mode as the on-disk header reports it (no SQLite connection is open in this process here)."""
    fd = os.open(db_path, os.O_RDONLY)
    try:
        head = os.pread(fd, 20, 0)
    finally:
        os.close(fd)
    if len(head) < 20 or head[:16] != b"SQLite format 3\x00":
        return "not-a-database"
    return {(1, 1): "delete", (2, 2): "wal"}.get((head[18], head[19]), f"unknown({head[18]}/{head[19]})")


def cmd_set_journal_mode(args) -> int:
    from hermes_state import DEFAULT_DB_PATH
    from hermes_state_holders import describe_holder_pid, foreign_state_db_holders
    from hermes_state_wal import _set_journal_mode_no_wait, is_sqlite_wal_reset_vulnerable, resolve_journal_mode

    target = args.mode
    db_path = Path(getattr(args, "db", None) or DEFAULT_DB_PATH)
    if not db_path.exists():
        print(f"No database at {db_path} (nothing to convert).")
        return 1
    current = _header_mode(db_path)
    if current == target:
        print(f"✓ {db_path} is already journal_mode={target}.")
        return 0
    if target == "wal" and is_sqlite_wal_reset_vulnerable():
        print(f"✗ Refusing to enable WAL: the linked SQLite {sqlite3.sqlite_version} has the WAL-reset bug "
              "(https://sqlite.org/wal.html#walresetbug). Upgrade to 3.51.3+ first.")
        return 1
    holders = foreign_state_db_holders(db_path)
    if holders:
        print(f"✗ Refusing to change the journal mode of {db_path}: other processes hold it open "
              "(a live switch would destroy their uncheckpointed commits). Stop them and re-run:")
        for pid, target_path in holders:
            print(f"    pid {pid} — {describe_holder_pid(pid) if pid > 0 else 'scan'}: {target_path}")
        return 1
    # timeout=0: any opener that appeared between the scan and the flip makes the pragma fail with
    # 'database is locked' instead of sneaking the switch between a writer's transactions.
    conn = sqlite3.connect(str(db_path), timeout=0.0, isolation_level=None)
    try:
        try:
            reported = _set_journal_mode_no_wait(conn, target.upper())
        except sqlite3.OperationalError as exc:
            print(f"✗ journal_mode={target.upper()} refused: {exc} (another opener appeared; nothing was changed)")
            return 1
    finally:
        conn.close()
    after = _header_mode(db_path)
    if after != target:
        print(f"✗ SQLite reported journal_mode={reported or '?'} but the file header reads {after}; not converted.")
        return 1
    print(f"✓ {db_path}: journal_mode {current} → {after} (header verified).")
    configured = resolve_journal_mode()
    if configured != target:
        print(f"  Note: config.yaml has database.journal_mode: {configured} — the next open would switch the file "
              f"back. Set `database.journal_mode: {target}` in config.yaml to keep it.")
    return 0
