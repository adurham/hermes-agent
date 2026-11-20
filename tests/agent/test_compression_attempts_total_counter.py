"""Behavior contract for ``sessions.compression_attempts_total``.

Root cause this guards against (see FORK.md, dated entry for this fix):
``compression_fallback_streak`` and ``compression_ineffective_count`` are
anti-thrash FAILURE counters. Both read 0 in two completely different
situations — "compression never had reason to fire" and "compression fired
and succeeded on every single boundary" — so neither one (nor their
combination) can answer the operational question "did compression ever run
for this session?". That ambiguity produced a false "compression never
fires for subagent sessions" bug report against sessions whose real
per-request token usage never crossed the configured threshold in the first
place.

``compression_attempts_total`` is the missing POSITIVE signal: it must
increment on every real completed compaction boundary — deterministic
fallback, feasibility-skip, and full-LLM-summary alike, since all three are
genuine boundaries that ``record_completed_compaction()`` is called for —
and it must never increment when compression never fires. The two anti-
thrash counters staying at 0 must be fully independent of this counter, in
both directions.

The counter lives on the storage interface's lineage repository
(``compression_attempts_total`` / ``increment_compression_attempts_total``);
the compressor reaches it through its ``persistence`` handle, exactly like
every other durable per-session value it writes.
"""
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.context_compressor import ContextCompressor
from hermes_state import SessionDB
from persistence.conversation.sessions import NewSession
from persistence_assembly import open_persistence, profile_scope


@pytest.fixture
def store(tmp_path):
    handle = open_persistence(profile_scope(home=tmp_path), eager=True)
    yield handle
    handle.close()


@pytest.fixture
def session_db(tmp_path, store):
    """The ``SessionDB``-shaped door the compressor also holds, on the same file."""
    db = SessionDB(db_path=tmp_path / "state.db")
    yield db
    db.close()


def _compressor(session_db, store, session_id: str) -> ContextCompressor:
    with patch(
        "agent.context_compressor.get_model_context_length",
        return_value=100_000,
    ):
        cc = ContextCompressor(
            model="test/model",
            threshold_percent=0.85,
            protect_first_n=2,
            protect_last_n=2,
            quiet_mode=True,
        )
    # The persistence handle is what routes the durable writes to the store's lineage repository.
    cc.bind_session_state(session_db, session_id, persistence=store)
    return cc


def _new_session(store, session_id: str, source: str = "cli") -> None:
    store.sessions.create(NewSession(session_id=session_id, source=source))


class TestCompressionAttemptsTotalIsAPositiveSignal:
    def test_starts_at_zero_for_a_fresh_session(self, store):
        _new_session(store, "s1")
        assert store.lineage.compression_attempts_total("s1") == 0

    def test_real_boundary_increments_the_counter(self, store, session_db):
        _new_session(store, "s1", "subagent")
        cc = _compressor(session_db, store, "s1")

        cc.record_completed_compaction(used_fallback=False, feasibility_skip=False)

        assert store.lineage.compression_attempts_total("s1") == 1

    def test_fallback_boundary_also_increments(self, store, session_db):
        """A deterministic-fallback boundary is still a real completed
        compaction — it must count toward attempts_total even though it
        also increments the separate fallback-streak failure counter."""
        _new_session(store, "s1")
        cc = _compressor(session_db, store, "s1")

        cc.record_completed_compaction(used_fallback=True, feasibility_skip=False)

        assert store.lineage.compression_attempts_total("s1") == 1
        assert store.lineage.compression_fallback_streak("s1") == 1

    def test_feasibility_skip_boundary_also_increments(self, store, session_db):
        """A feasibility-skip boundary is streak-NEUTRAL for the anti-thrash
        fallback streak but is still a real completed boundary — it must
        count toward attempts_total."""
        _new_session(store, "s1")
        cc = _compressor(session_db, store, "s1")

        cc.record_completed_compaction(used_fallback=False, feasibility_skip=True)

        assert store.lineage.compression_attempts_total("s1") == 1
        assert store.lineage.compression_fallback_streak("s1") == 0

    def test_never_firing_leaves_the_counter_at_zero(self, store, session_db):
        """The counterexample to the false bug report: a session that never
        crosses the threshold must show attempts_total == 0, matching the
        two anti-thrash counters — all three agree when compression is
        legitimately idle."""
        _new_session(store, "s1", "subagent")
        _compressor(session_db, store, "s1")

        # Never call record_completed_compaction — simulates a session whose
        # real usage never reached threshold_tokens.

        assert store.lineage.compression_attempts_total("s1") == 0
        assert store.lineage.compression_fallback_streak("s1") == 0
        assert store.lineage.compression_ineffective_count("s1") == 0

    def test_multiple_successful_boundaries_disambiguate_from_never_fired(self, store, session_db):
        """The exact scenario the two old counters cannot distinguish:
        compression fired 3 times and succeeded every time (fallback streak
        resets to 0 on success, ineffective count never trips). Before this
        fix, that session and a session where compression never fired were
        indistinguishable from state.db alone."""
        _new_session(store, "s1", "subagent")
        cc = _compressor(session_db, store, "s1")

        for _ in range(3):
            cc.record_completed_compaction(used_fallback=False, feasibility_skip=False)
            # A real provider reading well under threshold clears the
            # anti-thrash counters, mirroring a healthy compaction.
            cc.update_from_response({"prompt_tokens": 100})

        assert store.lineage.compression_attempts_total("s1") == 3
        assert store.lineage.compression_fallback_streak("s1") == 0
        assert store.lineage.compression_ineffective_count("s1") == 0

    def test_increment_helper_is_atomic_and_session_scoped(self, store):
        _new_session(store, "hot")
        _new_session(store, "cold")

        store.lineage.increment_compression_attempts_total("hot")
        store.lineage.increment_compression_attempts_total("hot")
        store.lineage.increment_compression_attempts_total("cold")

        assert store.lineage.compression_attempts_total("hot") == 2
        assert store.lineage.compression_attempts_total("cold") == 1

    def test_missing_session_id_is_a_safe_no_op(self, store):
        store.lineage.increment_compression_attempts_total("")
        assert store.lineage.compression_attempts_total("") == 0
        assert store.lineage.compression_attempts_total("nonexistent") == 0
