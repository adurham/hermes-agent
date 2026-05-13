"""Tests for tools/memory_auto_feedback — Phase 3 automatic warm-tier feedback.

Covers:
  * fingerprinting (distinctive vs stop-word tokens, n-gram sliding)
  * record_recall populates the per-session window
  * on_turn_end matches fingerprints against assistant text and upvotes
  * asymmetric — never auto-downvotes
  * once-per-session dedup
  * window ages out after recall_window_turns
  * flush_session drops state
  * disabled-by-default config gate
  * contextvar binding via set_session / current_session_id
  * WarmStore.recall / recall_related hook fires only when feature is on
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from tools.memory_auto_feedback import audit as maf
from tools.memory_warm import (
    get_warm_store,
    reset_warm_store_for_testing,
)


# =========================================================================
# Fixtures
# =========================================================================


@pytest.fixture()
def isolated_hermes_home(tmp_path, monkeypatch):
    """Point HERMES_HOME at tmp so warm DB lands in isolation."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import hermes_constants
    if hasattr(hermes_constants, "_HERMES_HOME_CACHE"):
        hermes_constants._HERMES_HOME_CACHE = None
    yield tmp_path
    reset_warm_store_for_testing()
    if hasattr(hermes_constants, "_HERMES_HOME_CACHE"):
        hermes_constants._HERMES_HOME_CACHE = None


@pytest.fixture()
def warm(isolated_hermes_home):
    reset_warm_store_for_testing()
    s = get_warm_store(db_path=isolated_hermes_home / "warm.db")
    yield s
    reset_warm_store_for_testing()


@pytest.fixture()
def reset_audit_state():
    """Drop all in-memory audit state before AND after each test."""
    maf._reset_state_for_testing()
    yield
    maf._reset_state_for_testing()


@pytest.fixture()
def enabled_config():
    """Patch _get_config() to return enabled feature with default values."""
    cfg = {
        "enabled": True,
        "recall_window_turns": 3,
        "min_fingerprint_words": 4,
        "max_facts_per_session": 200,
    }
    with patch.object(maf, "_get_config", return_value=cfg):
        yield cfg


@pytest.fixture()
def disabled_config():
    """Patch _get_config() to return the disabled default."""
    cfg = {
        "enabled": False,
        "recall_window_turns": 3,
        "min_fingerprint_words": 4,
        "max_facts_per_session": 200,
    }
    with patch.object(maf, "_get_config", return_value=cfg):
        yield cfg


# =========================================================================
# Fingerprinting
# =========================================================================


class TestFingerprintFact:
    def test_distinctive_identifier_terms_qualify(self):
        # PLAT-15800 and CDN have digits / uppercase, BWT is uppercase,
        # SD-WAN has dash + uppercase. All count as distinctive.
        fps = maf.fingerprint_fact(
            "PLAT-15800: BWT counter includes CDN bytes that don't "
            "traverse the SD-WAN tunnel.",
            min_words=4,
        )
        assert len(fps) >= 1
        # First fingerprint should start with the most distinctive token.
        assert fps[0].startswith("plat-15800")

    def test_stopword_only_yields_empty(self):
        # All words are stopwords + short.
        fps = maf.fingerprint_fact("the a and or of to in")
        assert fps == ()

    def test_short_content_below_min_words_returns_empty(self):
        # Only two distinctive tokens; can't build a 4-word fingerprint.
        fps = maf.fingerprint_fact("PLAT-15800 affects.", min_words=4)
        assert fps == ()

    def test_lowercased_in_output(self):
        fps = maf.fingerprint_fact(
            "Salesforce LaborSubCategory MUST be Support-Platform always",
            min_words=4,
        )
        assert all(fp == fp.lower() for fp in fps)

    def test_max_three_fingerprints_by_default(self):
        # Long content with many distinctive tokens — max_fp clips at 3.
        text = ("PLAT-15800 fixes BWT counter Issue-1234 in HERMES-5678 "
                "release-2026 milestone-2027 sprint-XYZ deliverable-ABC")
        fps = maf.fingerprint_fact(text, min_words=4)
        assert len(fps) <= 3

    def test_deduplicates_identical_fingerprints(self):
        # Same 4 distinctive words appear twice in a row -> only first kept.
        text = ("MCP Hermes Polaris config "
                "MCP Hermes Polaris config")
        fps = maf.fingerprint_fact(text, min_words=4)
        # Should produce distinct fingerprints, not duplicates.
        assert len(fps) == len(set(fps))


class TestIsDistinctive:
    def test_uppercase_internal_qualifies(self):
        assert maf._is_distinctive("TaaS")
        assert maf._is_distinctive("MCP")

    def test_digit_qualifies(self):
        assert maf._is_distinctive("PLAT-15800")
        assert maf._is_distinctive("v1.2.3")

    def test_long_lowercase_word_qualifies(self):
        assert maf._is_distinctive("salesforce")
        assert maf._is_distinctive("kubernetes")

    def test_short_word_skipped(self):
        assert not maf._is_distinctive("foo")
        assert not maf._is_distinctive("the")

    def test_stopword_skipped_even_if_long(self):
        # "should" is in the stopword set
        assert not maf._is_distinctive("should")


# =========================================================================
# record_recall / window state
# =========================================================================


class TestRecordRecall:
    def test_no_op_when_disabled(self, disabled_config, reset_audit_state):
        rows = [{"fact_id": 7, "content": "PLAT-15800 BWT counter CDN bytes"}]
        maf.record_recall("sess-1", rows)
        assert maf._snapshot_window("sess-1") == []

    def test_records_when_enabled(self, enabled_config, reset_audit_state):
        rows = [{
            "fact_id": 7,
            "content": "PLAT-15800 BWT counter includes CDN bytes",
        }]
        maf.record_recall("sess-1", rows)
        snap = maf._snapshot_window("sess-1")
        assert len(snap) == 1
        assert snap[0]["fact_id"] == 7
        assert snap[0]["turn_age"] == 0
        assert snap[0]["fingerprints"]

    def test_skips_fact_with_no_distinctive_content(
        self, enabled_config, reset_audit_state,
    ):
        # All stopwords -> no fingerprint -> not recorded.
        rows = [{
            "fact_id": 9,
            "content": "the a and or of to in on at",
        }]
        maf.record_recall("sess-1", rows)
        assert maf._snapshot_window("sess-1") == []

    def test_skips_when_session_id_empty(
        self, enabled_config, reset_audit_state,
    ):
        rows = [{"fact_id": 7, "content": "PLAT-15800 BWT counter CDN bytes"}]
        maf.record_recall("", rows)
        maf.record_recall(None, rows)
        assert maf._snapshot_window("") == []

    def test_refreshes_age_on_re_recall(
        self, enabled_config, reset_audit_state,
    ):
        rows = [{
            "fact_id": 7,
            "content": "PLAT-15800 BWT counter includes CDN bytes",
        }]
        maf.record_recall("sess-1", rows)
        # Manually age it
        with maf._get_lock("sess-1"):
            maf._session_windows["sess-1"][0].turn_age = 2
        # Re-recall the same fact: should reset age to 0, not duplicate.
        maf.record_recall("sess-1", rows)
        snap = maf._snapshot_window("sess-1")
        assert len(snap) == 1
        assert snap[0]["turn_age"] == 0

    def test_multiple_facts_one_session(
        self, enabled_config, reset_audit_state,
    ):
        rows = [
            {"fact_id": 1, "content": "PLAT-15800 BWT counter CDN bytes"},
            {"fact_id": 2, "content": "Tanium MCP Hermes Polaris config"},
        ]
        maf.record_recall("sess-1", rows)
        snap = maf._snapshot_window("sess-1")
        assert {e["fact_id"] for e in snap} == {1, 2}

    def test_handles_bad_fact_id_gracefully(
        self, enabled_config, reset_audit_state,
    ):
        rows = [
            {"fact_id": None, "content": "PLAT-15800 BWT counter CDN bytes"},
            {"fact_id": "not-an-int", "content": "More distinctive content here"},
            {"fact_id": 5, "content": "MCP Hermes Polaris config gateway"},
        ]
        maf.record_recall("sess-1", rows)
        snap = maf._snapshot_window("sess-1")
        # Only the valid fact_id=5 should land.
        assert [e["fact_id"] for e in snap] == [5]


# =========================================================================
# on_turn_end — fingerprint match + upvote
# =========================================================================


class TestOnTurnEnd:
    def test_credits_matched_fact(self, enabled_config, reset_audit_state, warm):
        # Seed warm with a real fact whose fingerprints we'll cite.
        r = warm.add(
            content="PLAT-15800 BWT counter includes CDN bytes that don't "
                    "traverse the SD-WAN tunnel.",
            category="tanium",
        )
        fid = r["fact_id"]
        rows = [warm.get(fid)]
        maf.record_recall("sess-1", rows)

        assistant_text = (
            "Looking at the bandwidth metric, "
            "plat-15800 bwt counter includes cdn bytes — that's the inflation."
        )
        summary = maf.on_turn_end("sess-1", assistant_text)

        assert summary["upvoted"] == 1
        assert summary["fact_ids"] == [fid]
        # Check the warm-tier side: helpful_count should be 1, trust > 0.5.
        row = warm.get(fid)
        assert row["helpful_count"] == 1
        assert row["trust_score"] > 0.5

    def test_no_credit_when_assistant_doesnt_cite(
        self, enabled_config, reset_audit_state, warm,
    ):
        r = warm.add(
            content="PLAT-15800 BWT counter includes CDN bytes from the edge.",
            category="tanium",
        )
        fid = r["fact_id"]
        maf.record_recall("sess-1", [warm.get(fid)])

        # Assistant talks about something else.
        summary = maf.on_turn_end(
            "sess-1",
            "The user asked about Kubernetes pod scheduling — let me check.",
        )
        assert summary["upvoted"] == 0
        # helpful_count must still be zero (asymmetric: no auto-downvote).
        row = warm.get(fid)
        assert row["helpful_count"] == 0
        assert row["trust_score"] == 0.5

    def test_does_not_double_credit_same_session(
        self, enabled_config, reset_audit_state, warm,
    ):
        r = warm.add(
            content="MCP Hermes Polaris gateway configuration default",
            category="hermes",
        )
        fid = r["fact_id"]
        maf.record_recall("sess-1", [warm.get(fid)])

        text = "Citing mcp hermes polaris gateway configuration here."
        s1 = maf.on_turn_end("sess-1", text)
        # Re-record + re-audit same turn; second on_turn_end should NOT
        # upvote again.
        maf.record_recall("sess-1", [warm.get(fid)])
        s2 = maf.on_turn_end("sess-1", text)

        assert s1["upvoted"] == 1
        assert s2["upvoted"] == 0
        row = warm.get(fid)
        assert row["helpful_count"] == 1

    def test_disabled_feature_is_no_op(
        self, disabled_config, reset_audit_state, warm,
    ):
        # Pre-seed the window directly by temporarily enabling
        with patch.object(maf, "_get_config", return_value={
            "enabled": True, "recall_window_turns": 3,
            "min_fingerprint_words": 4, "max_facts_per_session": 200,
        }):
            r = warm.add(
                content="PLAT-15800 BWT counter includes CDN bytes",
                category="tanium",
            )
            fid = r["fact_id"]
            maf.record_recall("sess-1", [warm.get(fid)])

        # Now feature is disabled (the disabled_config fixture's patch is active).
        summary = maf.on_turn_end(
            "sess-1",
            "Citing plat-15800 bwt counter includes cdn here.",
        )
        assert summary["upvoted"] == 0
        row = warm.get(fid)
        assert row["helpful_count"] == 0

    def test_window_ages_out(self, enabled_config, reset_audit_state, warm):
        r = warm.add(
            content="PLAT-15800 BWT counter includes CDN bytes always",
            category="tanium",
        )
        fid = r["fact_id"]
        maf.record_recall("sess-1", [warm.get(fid)])

        # 3 unrelated turns; default window_turns is 3.
        for _ in range(3):
            maf.on_turn_end("sess-1", "unrelated turn output")

        # After 3 unrelated turns, the entry has been aged 3 times. The
        # window survivors filter keeps entries with turn_age <= 3, so
        # one more aging tick should evict it. Confirm by snapshotting
        # then running a 4th aging.
        snap = maf._snapshot_window("sess-1")
        assert len(snap) == 1
        maf.on_turn_end("sess-1", "another unrelated turn")
        snap = maf._snapshot_window("sess-1")
        assert snap == []

    def test_safe_with_empty_inputs(
        self, enabled_config, reset_audit_state,
    ):
        # No exceptions, returns zero-summary.
        s1 = maf.on_turn_end("", "anything")
        s2 = maf.on_turn_end("sess", "")
        s3 = maf.on_turn_end("sess-no-window", "some text")
        for s in (s1, s2, s3):
            assert s["upvoted"] == 0

    def test_only_upvotes_never_downvotes(
        self, enabled_config, reset_audit_state, warm,
    ):
        r = warm.add(
            content="PLAT-15800 BWT counter includes CDN bytes from edge",
            category="tanium",
        )
        fid = r["fact_id"]
        maf.record_recall("sess-1", [warm.get(fid)])
        # Assistant fully ignores the fact across many turns.
        for _ in range(5):
            maf.on_turn_end("sess-1", "nothing relevant here at all.")
        # Trust must NOT go below 0.5 from auto-feedback alone.
        # (The window expires before turn 5; the test asserts no penalty.)
        row = warm.get(fid)
        assert row["trust_score"] == 0.5
        assert row["helpful_count"] == 0


# =========================================================================
# flush_session
# =========================================================================


class TestFlushSession:
    def test_drops_window_and_credited(
        self, enabled_config, reset_audit_state,
    ):
        rows = [{
            "fact_id": 1,
            "content": "Distinctive PLAT-15800 content here forever",
        }]
        maf.record_recall("sess-X", rows)
        # Manually mark credited so we can verify both maps cleared.
        maf._credited["sess-X"] = {1}

        maf.flush_session("sess-X")

        assert maf._snapshot_window("sess-X") == []
        assert "sess-X" not in maf._credited

    def test_idempotent_on_unknown_session(
        self, enabled_config, reset_audit_state,
    ):
        # No exception when called for a session we never saw.
        maf.flush_session("never-existed")
        maf.flush_session("")
        maf.flush_session(None)


# =========================================================================
# Context binding + WarmStore integration
# =========================================================================


class TestSetSession:
    def test_round_trip(self, reset_audit_state):
        maf.set_session("sid-123")
        assert maf.current_session_id() == "sid-123"
        maf.set_session(None)
        assert maf.current_session_id() is None


class TestWarmStoreHook:
    def test_recall_records_when_session_bound_and_enabled(
        self, enabled_config, reset_audit_state, warm,
    ):
        warm.add(
            content="Distinctive PLAT-15800 BWT counter CDN bytes content",
            category="tanium",
        )

        maf.set_session("sid-hook")
        try:
            results = warm.recall("PLAT-15800")
        finally:
            maf.set_session(None)

        # The recall should have stashed the result in the audit window.
        snap = maf._snapshot_window("sid-hook")
        assert len(snap) == len(results) == 1

    def test_recall_no_session_is_no_op(
        self, enabled_config, reset_audit_state, warm,
    ):
        warm.add(
            content="Distinctive PLAT-15800 BWT counter CDN bytes content",
            category="tanium",
        )
        # No set_session call -> contextvar default None -> no record.
        warm.recall("PLAT-15800")
        # Window for the empty-string session id should be empty.
        assert maf._snapshot_window("") == []

    def test_recall_with_session_but_disabled_is_no_op(
        self, disabled_config, reset_audit_state, warm,
    ):
        warm.add(
            content="Distinctive PLAT-15800 BWT counter CDN bytes content",
            category="tanium",
        )
        maf.set_session("sid-disabled")
        try:
            warm.recall("PLAT-15800")
        finally:
            maf.set_session(None)

        # Feature disabled: record_recall returns early.
        assert maf._snapshot_window("sid-disabled") == []

    def test_recall_related_records(
        self, enabled_config, reset_audit_state, warm,
    ):
        warm.add(
            content="Distinctive PLAT-15800 BWT counter CDN bytes content",
            category="tanium",
        )

        maf.set_session("sid-related")
        try:
            warm.recall_related("PLAT-15800 BWT")
        finally:
            maf.set_session(None)

        snap = maf._snapshot_window("sid-related")
        assert len(snap) >= 1
