"""FTS storage-layout drift: one structural answer shared by the stamp, the update notice and doctor.

Regression for #129927. The ``fts_storage_version`` stamp was written by a base-index realign that never
looked at the trigram vtable, doctor trusted that stamp, and the shape check only knew two historical
layouts, so the next layout bump would be stamped current while still stale.
"""

from __future__ import annotations

import contextlib
import io
import os
import pathlib
import sqlite3

import pytest

import hermes_state_common
from hermes_state import SessionDB


def _require_trigram(db: SessionDB) -> None:
    if not db._trigram_available:
        db.close()
        pytest.skip("trigram tokenizer unavailable in this SQLite build")


def _install_v1_trigram(db: SessionDB) -> None:
    """The trigram layout `optimize-storage` produced between #65798 and #101729 (projects ``tool_calls``)."""
    db._conn.executescript(
        """
        DROP TRIGGER messages_fts_trigram_insert;
        DROP TRIGGER messages_fts_trigram_delete;
        DROP TRIGGER messages_fts_trigram_update;
        DROP TABLE messages_fts_trigram;
        DROP VIEW messages_fts_trigram_src;
        CREATE VIEW messages_fts_trigram_src AS
            SELECT id, role, content, tool_name, tool_calls FROM messages WHERE role <> 'tool';
        CREATE VIRTUAL TABLE messages_fts_trigram USING fts5(
            content, tool_name, tool_calls,
            content='messages_fts_trigram_src', content_rowid='id', tokenize='trigram'
        );
        INSERT INTO messages_fts_trigram(messages_fts_trigram) VALUES('rebuild');
        """
    )


def _install_raw_messages_base(db: SessionDB) -> None:
    """The pre-v3 base index: external-content over raw ``messages`` (realigned on the next open)."""
    names = [r[0] for r in db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name IN "
        "('messages_fts_insert','messages_fts_delete','messages_fts_update')")]
    db._conn.executescript(
        "".join(f"DROP TRIGGER {n};" for n in names)
        + """
        DROP TABLE messages_fts;
        CREATE VIRTUAL TABLE messages_fts USING fts5(
            content, tool_name, tool_calls, content='messages', content_rowid='id'
        );
        INSERT INTO messages_fts(messages_fts) VALUES('rebuild');
        """
    )


def _stamp(db_path: pathlib.Path):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT value FROM state_meta WHERE key = 'fts_storage_version'").fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _seed(db: SessionDB) -> None:
    db.create_session("cli", source="cli")
    db.append_message("cli", role="user", content="deploy the payment service")
    db.append_message(
        "cli", role="assistant", content="running",
        tool_calls=[{"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}],
    )


@pytest.fixture
def v1_trigram_db(tmp_path):
    """tek's state: optimized at #65798 (v1 trigram, raw-messages base), then reopened by current code."""
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    _require_trigram(db)
    _seed(db)
    _install_v1_trigram(db)
    _install_raw_messages_base(db)
    db._conn.execute("UPDATE state_meta SET value = '1' WHERE key = 'fts_storage_version'")
    db._conn.commit()
    db.close()
    SessionDB(db_path=db_path).close()  # the gateway/CLI open that realigns only the base index
    return db_path


def test_realign_does_not_stamp_current_while_the_trigram_is_stale(v1_trigram_db):
    assert _stamp(v1_trigram_db) != str(hermes_state_common.FTS_STORAGE_VERSION)


def test_doctor_offers_optimize_storage_for_a_stale_trigram(v1_trigram_db, monkeypatch):
    from hermes_cli import doctor_state
    from hermes_state_dbfile import collect_state_db_stats

    stats = collect_state_db_stats(v1_trigram_db)
    monkeypatch.setattr(doctor_state, "STATE_DB_SIZE_WARN_BYTES", 0)
    rows = doctor_state._render_state_db_stats(stats)
    assert any("hermes sessions optimize-storage" in detail for _, _, detail in rows)


def test_fresh_store_is_stamped_on_creation_and_doctor_stays_quiet(tmp_path, monkeypatch):
    from hermes_cli import doctor_state
    from hermes_state_dbfile import collect_state_db_stats

    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    _seed(db)
    db.close()
    assert _stamp(db_path) == str(hermes_state_common.FTS_STORAGE_VERSION)
    monkeypatch.setattr(doctor_state, "STATE_DB_SIZE_WARN_BYTES", 0)
    rows = doctor_state._render_state_db_stats(collect_state_db_stats(db_path))
    assert not any("hermes sessions optimize-storage" in detail for _, _, detail in rows)


def _notice_for(db_path: pathlib.Path, monkeypatch) -> str:
    """Run the `hermes update` notice against *db_path* with the size gate satisfied for state.db only."""
    from hermes_cli import update_cmd_maint
    import hermes_constants

    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: db_path.parent)
    real_stat = pathlib.Path.stat

    def _big(path, *args, **kwargs):
        st = real_stat(path, *args, **kwargs)
        if path.name == "state.db":
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid, st.st_gid,
                                   10 * 1024 ** 3, st.st_atime, st.st_mtime, st.st_ctime))
        return st

    monkeypatch.setattr(pathlib.Path, "stat", _big)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        update_cmd_maint._print_fts_optimize_available_notice()
    return buf.getvalue()


def test_notice_for_a_stale_trigram_does_not_claim_the_legacy_reclaim(v1_trigram_db, monkeypatch):
    out = _notice_for(v1_trigram_db, monkeypatch)
    assert "hermes sessions optimize-storage" in out
    assert "60%" not in out and "old storage layout" not in out.lower()


def test_notice_for_a_legacy_inline_store_keeps_the_reclaim_estimate(tmp_path, monkeypatch):
    from hermes_state_common import LEGACY_FTS_SQL

    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    _seed(db)
    names = [r[0] for r in db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'messages_fts%'")]
    db._conn.executescript(
        "".join(f"DROP TRIGGER {n};" for n in names)
        + "DROP TABLE IF EXISTS messages_fts_cjk; DROP TABLE IF EXISTS messages_fts_trigram;"
        + "DROP VIEW IF EXISTS messages_fts_trigram_src; DROP TABLE messages_fts;"
        + LEGACY_FTS_SQL
    )
    db._conn.commit()
    db.close()
    assert "60%" in _notice_for(db_path, monkeypatch)


def test_a_layout_change_the_code_has_not_shipped_yet_is_reported_as_drift(tmp_path, monkeypatch):
    """The next bump (any DDL change) must surface without someone adding a per-version predicate."""
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    _seed(db)
    db.close()
    assert _stamp(db_path) == str(hermes_state_common.FTS_STORAGE_VERSION)

    import hermes_state_fts
    bumped = hermes_state_common.FTS_SQL.replace(
        "content_rowid='id'\n);", "content_rowid='id',\n    tokenize='porter unicode61'\n);", 1)
    assert bumped != hermes_state_common.FTS_SQL
    monkeypatch.setattr(hermes_state_fts, "FTS_SQL", bumped)

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        drift = hermes_state_fts.fts_layout_drift(conn.cursor())
    finally:
        conn.close()
    assert "messages_fts" in drift
