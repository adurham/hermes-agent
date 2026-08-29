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
"""
from pathlib import Path
from unittest.mock import patch

from agent.context_compressor import ContextCompressor
from hermes_state import SessionDB


def _compressor(db: SessionDB, session_id: str) -> ContextCompressor:
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
    cc.bind_session_state(db, session_id)
    return cc


def _db(tmp_path: Path) -> SessionDB:
    return SessionDB(db_path=tmp_path / "state.db")


class TestCompressionAttemptsTotalIsAPositiveSignal:
    def test_starts_at_zero_for_a_fresh_session(self, tmp_path):
        db = _db(tmp_path)
        db.create_session("s1", source="cli")
        assert db.get_compression_attempts_total("s1") == 0

    def test_real_boundary_increments_the_counter(self, tmp_path):
        db = _db(tmp_path)
        db.create_session("s1", source="subagent")
        cc = _compressor(db, "s1")

        cc.record_completed_compaction(used_fallback=False, feasibility_skip=False)

        assert db.get_compression_attempts_total("s1") == 1

    def test_fallback_boundary_also_increments(self, tmp_path):
        """A deterministic-fallback boundary is still a real completed
        compaction — it must count toward attempts_total even though it
        also increments the separate fallback-streak failure counter."""
        db = _db(tmp_path)
        db.create_session("s1", source="cli")
        cc = _compressor(db, "s1")

        cc.record_completed_compaction(used_fallback=True, feasibility_skip=False)

        assert db.get_compression_attempts_total("s1") == 1
        assert db.get_compression_fallback_streak("s1") == 1

    def test_feasibility_skip_boundary_also_increments(self, tmp_path):
        """A feasibility-skip boundary is streak-NEUTRAL for the anti-thrash
        fallback streak but is still a real completed boundary — it must
        count toward attempts_total."""
        db = _db(tmp_path)
        db.create_session("s1", source="cli")
        cc = _compressor(db, "s1")

        cc.record_completed_compaction(used_fallback=False, feasibility_skip=True)

        assert db.get_compression_attempts_total("s1") == 1
        assert db.get_compression_fallback_streak("s1") == 0

    def test_never_firing_leaves_the_counter_at_zero(self, tmp_path):
        """The counterexample to the false bug report: a session that never
        crosses the threshold must show attempts_total == 0, matching the
        two anti-thrash counters — all three agree when compression is
        legitimately idle."""
        db = _db(tmp_path)
        db.create_session("s1", source="subagent")
        cc = _compressor(db, "s1")

        # Never call record_completed_compaction — simulates a session whose
        # real usage never reached threshold_tokens.

        assert db.get_compression_attempts_total("s1") == 0
        assert db.get_compression_fallback_streak("s1") == 0
        assert db.get_compression_ineffective_count("s1") == 0

    def test_multiple_successful_boundaries_disambiguate_from_never_fired(self, tmp_path):
        """The exact scenario the two old counters cannot distinguish:
        compression fired 3 times and succeeded every time (fallback streak
        resets to 0 on success, ineffective count never trips). Before this
        fix, that session and a session where compression never fired were
        indistinguishable from state.db alone."""
        db = _db(tmp_path)
        db.create_session("s1", source="subagent")
        cc = _compressor(db, "s1")

        for _ in range(3):
            cc.record_completed_compaction(used_fallback=False, feasibility_skip=False)
            # A real provider reading well under threshold clears the
            # anti-thrash counters, mirroring a healthy compaction.
            cc.update_from_response({"prompt_tokens": 100})

        assert db.get_compression_attempts_total("s1") == 3
        assert db.get_compression_fallback_streak("s1") == 0
        assert db.get_compression_ineffective_count("s1") == 0

    def test_increment_helper_is_atomic_and_session_scoped(self, tmp_path):
        db = _db(tmp_path)
        db.create_session("hot", source="cli")
        db.create_session("cold", source="cli")

        db.increment_compression_attempts_total("hot")
        db.increment_compression_attempts_total("hot")
        db.increment_compression_attempts_total("cold")

        assert db.get_compression_attempts_total("hot") == 2
        assert db.get_compression_attempts_total("cold") == 1

    def test_missing_session_id_is_a_safe_no_op(self, tmp_path):
        db = _db(tmp_path)
        db.increment_compression_attempts_total("")
        assert db.get_compression_attempts_total("") == 0
        assert db.get_compression_attempts_total("nonexistent") == 0
