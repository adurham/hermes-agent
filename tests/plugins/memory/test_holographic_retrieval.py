"""Tests for FactRetriever FTS5 query sanitization.

These tests cover the fix where raw natural-language queries passed to
FTS5 MATCH were AND-joined by default, dropping recall to zero on any
multi-word prose query. The sanitizer drops stopwords and OR-joins the
remaining content tokens as phrase literals.
"""
from __future__ import annotations

import pytest

pytest.importorskip("numpy")  # retrieval module imports numpy indirectly

from plugins.memory.holographic.retrieval import FactRetriever
from plugins.memory.holographic.store import MemoryStore


# ---------------------------------------------------------------------------
# _sanitize_fts_query — unit tests (no DB required)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "query,expected_tokens",
    [
        # stopwords dropped
        ("what happened with the deployment rollback", {"happened", "deployment", "rollback"}),
        # single content word passes through
        ("compaction", {"compaction"}),
        # all stopwords → falls back to raw
        ("the and of", None),  # None = sentinel for fallback-to-raw
        # empty string → empty output
        ("", ""),
        # FTS5 operator characters stripped. The hyphen in "length-probe"
        # must become a token *separator*, not silently deleted: FTS5's own
        # unicode61 tokenizer treats '-' as a word boundary, so a stored
        # value like "length-probe" is indexed as two tokens ("length",
        # "probe"). Gluing them into one token ("lengthprobe") here would
        # produce a query that can never match the index (see
        # plugins/memory/holographic/retrieval.py::_sanitize_fts_query).
        ("context: length-probe", {"context", "length", "probe"}),
        # trailing punctuation stripped by tokenizer
        ("hello, world!", {"hello", "world"}),
    ],
)
def test_sanitize_fts_query_extracts_content_tokens(query, expected_tokens):
    result = FactRetriever._sanitize_fts_query(query)

    if expected_tokens == "":
        assert result == ""
        return

    if expected_tokens is None:
        # Pathological case: all stopwords — should fall back to raw query
        assert result == query
        return

    # OR-joined phrase literals: `"tok1" OR "tok2" OR ...`
    # Extract the tokens between quotes, order-independent.
    import re
    matches = re.findall(r'"([^"]+)"', result)
    assert set(matches) == expected_tokens, f"got {result!r}"


# ---------------------------------------------------------------------------
# Integration test — actually run _fts_candidates against an in-memory DB
# ---------------------------------------------------------------------------

@pytest.fixture
def retriever_with_facts(tmp_path):
    """MemoryStore seeded with a few facts for retrieval tests."""
    db_path = tmp_path / "test_facts.db"
    store = MemoryStore(str(db_path))
    store.add_fact(
        content="The Thursday deployment rollback failed because of stale migration state.",
        category="project",
    )
    store.add_fact(
        content="Compaction settings tuned to 0.85 threshold.",
        category="tool",
    )
    store.add_fact(
        content="Venice.ai advertises availableContextTokens inside model_spec.",
        category="tool",
    )
    retriever = FactRetriever(store=store)
    yield retriever
    store.close()


def test_prefetch_recovers_prose_query(retriever_with_facts):
    """A natural-language query should now match the relevant fact.

    Before the sanitizer fix, 'what happened with the deployment rollback'
    returned zero hits because FTS5 required every token to co-occur.
    """
    results = retriever_with_facts.search(
        "what happened with the deployment rollback"
    )
    assert len(results) >= 1
    # The top hit should be the deployment rollback fact
    assert "deployment rollback" in results[0]["content"].lower()




# ---------------------------------------------------------------------------
# Loop-invariant encode hoists (perf) — search/probe/related must encode
# constant vectors ONCE per call, not once per candidate/row.
# encode_text/encode_atom are deterministic (SHA-256 counter blocks), so the
# hoisted vectors are bit-identical to the per-iteration values they replace.
# ---------------------------------------------------------------------------

from plugins.memory.holographic import holographic as hrr


def test_encode_functions_are_deterministic():
    """Soundness premise of the hoists: same input -> identical vector."""
    import numpy as np

    assert np.array_equal(hrr.encode_text("deploy target", 1024),
                          hrr.encode_text("deploy target", 1024))
    assert np.array_equal(hrr.encode_atom("__hrr_role_content__", 1024),
                          hrr.encode_atom("__hrr_role_content__", 1024))












# ---------------------------------------------------------------------------
# HRR query must be ROLE-BOUND to match encode_fact's storage role.
#
# encode_fact stores the content signal as bind(text, ROLE_CONTENT); comparing a
# raw encode_text(query) against a fact vector therefore compares across roles and
# scores ~noise (measured on a real 2.3k-fact store: the source fact of its own
# 8-word query ranked ~N/2, i.e. chance; role-bound: rank 3).
# ---------------------------------------------------------------------------

_TARGET_CONTENT = ("kestrel mercury zephyr falcon tundra quartz nimbus cobalt "
                   "harbor lantern")
_QUERY = "kestrel mercury zephyr falcon tundra quartz nimbus cobalt"
_UNRELATED = [
    "sable prism meadow thistle ember gale hollow ivory juniper kelp",
    "onyx vellum mirth lumen cobalt harbor lantern kestrel prism sable",
    "quartz thistle nimbus juniper gale meadow ember ivory kelp hollow",
    "ivory lantern prism sable mirth lumen onyx vellum gale ember thistle",
]


def test_search_hrr_term_ranks_the_matching_fact_first_when_fts_and_jaccard_are_tied(tmp_path):
    """With FTS rank and Jaccard held constant, the HRR term must pick the
    fact that actually contains the query's content.

    Old behavior: the query was compared unbound, so the HRR term was noise and
    this assertion failed. This pins the role-bound comparison.
    """
    store = MemoryStore(str(tmp_path / "role_binding.db"))
    try:
        target_id = store.add_fact(content=_TARGET_CONTENT, category="c")
        for content in _UNRELATED:
            store.add_fact(content=content, category="c")

        retriever = FactRetriever(store=store)

        # Control the two competing terms so only the HRR term decides the order:
        # identical fts_rank and identical jaccard for every candidate.
        original_fts_candidates = retriever._fts_candidates
        original_jaccard = retriever._jaccard_similarity
        try:
            def flat_candidates(query, category=None, min_trust=0.0, limit=10):
                rows = original_fts_candidates(query, category, 0.0, 999)
                for row in rows:
                    row["fts_rank"] = 1.0
                return rows

            retriever._fts_candidates = flat_candidates
            retriever._jaccard_similarity = lambda a, b: 0.5  # typer: ignore

            results = retriever.search(_QUERY, category="c", min_trust=0.0, limit=len(_UNRELATED) + 1)
        finally:
            retriever._fts_candidates = original_fts_candidates
            retriever._jaccard_similarity = original_jaccard

        assert [r["fact_id"] for r in results][0] == target_id, (
            "HRR term did not rank the matching fact first: "
            f"{[r['fact_id'] for r in results]} (target={target_id})"
        )
    finally:
        store.close()


def test_search_hrr_score_is_informative_not_neutral(tmp_path):
    """The scoring term itself must separate a matching fact from an unrelated
    one — the old cross-role comparison did not (both ~0.5 after shifting)."""
    import numpy as np

    store = MemoryStore(str(tmp_path / "role_binding_score.db"))
    try:
        target_id = store.add_fact(content=_TARGET_CONTENT, category="c")
        other_id = store.add_fact(content=_UNRELATED[0], category="c")
        retriever = FactRetriever(store=store)

        def hrr_term(fact_id):
            row = store._conn.execute("SELECT hrr_vector FROM facts WHERE fact_id = ?", (fact_id,)).fetchone()
            return hrr.similarity(hrr.bind(hrr.encode_text(_QUERY, 1024),
                                           hrr.encode_atom(hrr.ROLE_CONTENT, 1024)),
                                  hrr.bytes_to_phases(bytes(row["hrr_vector"]), dim=1024))

        assert hrr_term(target_id) > hrr_term(other_id), (
            f"matching fact scored {hrr_term(target_id):.4f}, unrelated {hrr_term(other_id):.4f}"
        )
        assert hrr_term(target_id) > 0.1, "matching fact should be clearly above noise"
        assert abs(np.mean([hrr_term(other_id)])) < 0.1, "unrelated fact should sit near zero"
    finally:
        store.close()
