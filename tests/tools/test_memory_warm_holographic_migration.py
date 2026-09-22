"""Behavior contracts for the warm-tier -> holographic-provider migration.

The warm tier used to carry its own retrieval implementation (a fork-added
``MemoryStore.search_facts`` plus a token-OR ``recall_related``). It now reads
through upstream's ``FactRetriever``, and the holographic provider is the single
backend of record for both storage and retrieval.

These tests assert the RELATIONSHIPS that migration has to preserve -- not the
ranking numbers, which are upstream's to tune:

  * the fork's bespoke read path is really gone (no silent fallback),
  * a fact written through the warm tier is readable through the *provider's*
    own tool surface over the same DB (i.e. no data migration is needed),
  * retrieval accounting is tied to a deliberate recall, NOT to ranking, so an
    always-on prefetch can't inflate the counter conflict-detection reads,
  * hyphenated identifiers still tokenize (``PLAT-15800``),
  * the mutual-exclusion guard closes the model-facing surface but never the
    internal one.

Every test runs against an on-disk SQLite DB under ``tmp_path``. The suite-wide
conftest already redirects HERMES_HOME/HOME to per-test tempdirs, and every
store here is constructed with an EXPLICIT db_path, so nothing can reach the
real ``$HERMES_HOME/memory_store.db``.
"""

from __future__ import annotations

import json

import pytest

from plugins.memory.holographic.retrieval import FactRetriever
from plugins.memory.holographic.store import MemoryStore as HoloStore
from tools import memory_warm as mw
from tools.memory_warm import get_warm_store, reset_warm_store_for_testing

_FACT = "Escalation PLAT-15800 tracks the TDS query queue overflow returning 503"


@pytest.fixture()
def warm(tmp_path):
    """Fresh WarmStore singleton at an explicit tmp_path DB."""
    reset_warm_store_for_testing()
    store = get_warm_store(db_path=tmp_path / "warm.db")
    yield store
    reset_warm_store_for_testing()


class TestBespokeReadPathRetired:
    def test_store_no_longer_carries_the_fork_search_method(self, warm):
        """``search_facts`` was the fork's parallel retrieval path; it must be gone.

        If it comes back, the warm tier has two ranking implementations again and
        whichever one `recall` happens to call becomes an accident.
        """
        assert not hasattr(warm._inner, "search_facts")

    def test_recall_runs_through_the_upstream_retriever(self, warm):
        warm.add(_FACT, category="project")
        assert isinstance(warm._retriever, FactRetriever)
        assert warm._retriever.store is warm._inner

    def test_retriever_is_shared_not_rebuilt_per_call(self, warm):
        """One retriever per store: rebuilding per call would re-encode role atoms."""
        warm.add(_FACT)
        assert warm._retriever is warm._retriever

    def test_recall_still_finds_a_plain_match(self, warm):
        warm.add(_FACT, category="project")
        warm.add("Salesforce writes routed through a local sync script")
        hits = warm.recall("TDS query queue")
        assert [h["content"] for h in hits] == [_FACT]

    def test_recall_scores_are_exposed_for_ranking(self, warm):
        """The retriever ranks; a scoreless row means we fell back to raw FTS order."""
        warm.add(_FACT)
        assert "score" in warm.recall("TDS query queue")[0]

    def test_recall_never_leaks_the_raw_vector_blob(self, warm):
        """Callers JSON-serialize these rows; an hrr_vector blob would break that."""
        warm.add(_FACT)
        assert "hrr_vector" not in warm.recall("TDS query queue")[0]


class TestNoDataMigrationNeeded:
    def test_warm_write_is_readable_through_the_provider_surface(self, tmp_path):
        """The migration's central claim: same DB, same rows, no conversion step.

        Write via the warm tier, then read back through the holographic PROVIDER's
        own ``fact_store`` tool over the same file.
        """
        from plugins.memory.holographic import HolographicMemoryProvider

        db = tmp_path / "shared.db"
        reset_warm_store_for_testing()
        try:
            warm = get_warm_store(db_path=db)
            fact_id = warm.add(_FACT, category="project", tags="tanium")["fact_id"]
            trust_before = warm.get(fact_id)["trust_score"]

            provider = HolographicMemoryProvider(config={"db_path": str(db)})
            provider.initialize(session_id="migration-test")
            try:
                out = json.loads(provider.handle_tool_call(
                    "fact_store", {"action": "search", "query": "TDS query queue", "min_trust": 0.0},
                ))
                found = [r for r in out["results"] if r["fact_id"] == fact_id]
                assert found, "warm-written fact was invisible to the provider read path"
                assert found[0]["trust_score"] == trust_before
                assert found[0]["content"] == _FACT
            finally:
                provider.shutdown()
        finally:
            reset_warm_store_for_testing()


class TestRetrievalAccounting:
    def test_deliberate_recall_bumps_the_counter(self, warm):
        fact_id = warm.add(_FACT)["fact_id"]
        assert warm.get(fact_id)["retrieval_count"] == 0
        warm.recall("TDS query queue")
        assert warm.get(fact_id)["retrieval_count"] == 1
        warm.recall("query queue overflow")
        assert warm.get(fact_id)["retrieval_count"] == 2

    def test_returned_rows_agree_with_the_persisted_counter(self, warm):
        """A row saying 0 while the DB says 1 would make callers double-count."""
        fact_id = warm.add(_FACT)["fact_id"]
        row = warm.recall("TDS query queue")[0]
        assert row["retrieval_count"] == warm.get(fact_id)["retrieval_count"]

    def test_bare_ranking_does_not_bump_the_counter(self, warm):
        """Ranking is not usage.

        ``MemoryManager.prefetch_all`` ranks on every non-trivial turn. If the
        retriever bumped the counter, `memory_extraction/conflict.py` -- which reads
        retrieval_count to favor an existing fact over a near-duplicate -- would see
        a number that grows with turn count instead of with actual recalls.
        """
        fact_id = warm.add(_FACT)["fact_id"]
        before = warm.get(fact_id)["retrieval_count"]
        warm._retriever.search("TDS query queue", min_trust=0.0, limit=5)
        assert warm.get(fact_id)["retrieval_count"] == before

    def test_bump_is_idempotent_on_an_empty_id_list(self, warm):
        assert warm._inner.bump_retrieval_counts([]) == 0


class TestHyphenTokenization:
    """A real bug fix that must survive the migration: ``_FTS_OPERATORS`` DELETES
    '-', so without the hyphen split "PLAT-15800" becomes an unmatchable
    "plat15800" while FTS5 indexed the content as two tokens."""

    def test_hyphenated_code_is_split_not_glued(self):
        assert FactRetriever._sanitize_fts_query("PLAT-15800") == '"plat" OR "15800"'

    def test_hyphenated_code_is_actually_retrievable(self, warm):
        fact_id = warm.add(_FACT, category="project")["fact_id"]
        assert any(h["fact_id"] == fact_id for h in warm.recall("PLAT-15800"))

    def test_glued_form_does_not_match(self, warm):
        """Guards the regression direction: if '-' were merely deleted again, the
        query token would look like this and match nothing."""
        warm.add(_FACT, category="project")
        assert warm.recall("plat15800") == []


class TestRecallRelated:
    def test_related_delegates_to_the_retriever(self, warm):
        warm.add(_FACT, category="project")
        warm.add("Tanium Reporting historical collection failures", category="project")
        assert warm.recall_related("Tanium TDS query", top_k=5)

    @pytest.mark.parametrize("seed", ["", "   ", "a", "ab x"])
    def test_seeds_too_thin_to_carry_signal_return_empty(self, warm, seed):
        warm.add(_FACT)
        assert warm.recall_related(seed) == []


class TestMutualExclusionGuard:
    """With ``memory.provider: holographic`` both paths address the same rows, so
    the model-facing warm surface withdraws -- but internal plumbing must not."""

    @pytest.fixture()
    def provider_registered(self, monkeypatch):
        monkeypatch.setattr(mw, "holographic_provider_is_registered", lambda: True)

    @pytest.mark.parametrize("configured,expected", [
        ("holographic", True),
        ("Holographic", True),   # config values aren't case-normalized for us
        ("  holographic  ", True),
        ("honcho", False),
        ("hindsight", False),
        ("", False),
        (None, False),
    ])
    def test_predicate_reads_memory_provider_from_config(self, monkeypatch, configured, expected):
        """Exercises the REAL config read, not a monkeypatched stand-in.

        Without this, every guard test below could pass while the predicate itself
        was hardwired to False -- i.e. the guard silently absent in production.
        """
        monkeypatch.setattr(
            "hermes_cli.config.load_config_readonly",
            lambda *a, **k: {"memory": {"provider": configured}},
        )
        assert mw.holographic_provider_is_registered() is expected

    def test_predicate_is_false_when_config_is_unreadable(self, monkeypatch):
        """A broken config must leave the warm tier WORKING, not lock the user out
        of their own memory."""
        def _boom(*a, **k):
            raise RuntimeError("config unreadable")

        monkeypatch.setattr("hermes_cli.config.load_config_readonly", _boom)
        assert mw.holographic_provider_is_registered() is False

    def test_default_config_keeps_the_warm_surface_open(self, warm):
        from tools.memory_tool import _get_warm_store_or_error

        store, err = _get_warm_store_or_error()
        assert err is None and store is not None

    def test_registered_provider_closes_the_model_facing_surface(self, warm, provider_registered):
        from tools.memory_tool import _get_warm_store_or_error

        store, err = _get_warm_store_or_error()
        assert store is None
        payload = json.loads(err)
        assert payload["success"] is False
        # The refusal has to name the replacement, or the model just retries.
        assert "fact_store" in payload["error"]

    def test_registered_provider_drops_the_duplicate_prompt_block(self, warm, provider_registered):
        from tools.memory_tool import _format_warm_status

        warm.add(_FACT)
        assert _format_warm_status() is None

    def test_registered_provider_silences_the_pull_nudge(self, warm, provider_registered):
        """The provider pushes via prefetch_all, and the action the nudge asks for
        is refused -- so nudging would be advice the model cannot act on."""
        from agent.fork import memory_recall

        warm.add(_FACT)
        assert memory_recall._get_warm_count() == 0

    def test_internal_callers_bypass_the_guard(self, warm, provider_registered):
        """hot-tier-audit demote, extraction, session-pin and auto-feedback are this
        process's own plumbing over shared rows, not a rival surface."""
        from agent.fork import memory_session_pin

        fact_id = get_warm_store().add(
            content="demoted under the guard",
            category="demoted-stale-path",
            tags="hot-tier-audit,auto-demoted",
        )["fact_id"]
        assert warm.get(fact_id) is not None
        assert memory_session_pin._fetch_warm_fact(fact_id)["fact_id"] == fact_id


class TestStorePrimitives:
    """The warm tier now asks the store for these instead of hand-rolling SQL
    against ``_inner._conn``."""

    def test_get_fact_round_trips_and_misses_cleanly(self, tmp_path):
        store = HoloStore(db_path=str(tmp_path / "p.db"))
        try:
            fact_id = store.add_fact(_FACT, category="project")
            assert store.get_fact(fact_id)["content"] == _FACT
            assert store.get_fact(999999) is None
        finally:
            store.close()

    def test_count_facts_tracks_writes_and_removals(self, tmp_path):
        store = HoloStore(db_path=str(tmp_path / "c.db"))
        try:
            assert store.count_facts() == 0
            fact_id = store.add_fact(_FACT)
            store.add_fact("a second unrelated fact about linting")
            assert store.count_facts() == 2
            store.remove_fact(fact_id)
            assert store.count_facts() == 1
        finally:
            store.close()

    def test_find_fact_id_by_content_distinguishes_insert_from_duplicate(self, tmp_path):
        """``content`` is UNIQUE and ``add_fact`` silently returns the existing id;
        this is how the warm tier reports created-vs-existing."""
        store = HoloStore(db_path=str(tmp_path / "d.db"))
        try:
            assert store.find_fact_id_by_content(_FACT) is None
            fact_id = store.add_fact(_FACT)
            assert store.find_fact_id_by_content(_FACT) == fact_id
            assert store.add_fact(_FACT) == fact_id
        finally:
            store.close()

    def test_warm_add_reports_created_then_existing(self, warm):
        first = warm.add(_FACT)
        second = warm.add(_FACT)
        assert first["status"] == "created"
        assert second["status"] == "existing"
        assert second["fact_id"] == first["fact_id"]
