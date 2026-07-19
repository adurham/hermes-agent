#!/usr/bin/env python3
"""Warm-tier memory backend for the unified `memory` tool.

The hot tier (`MemoryStore` in `tools/memory_tool.py`) is bounded, frozen-
snapshot, file-backed, and always-loaded into the system prompt. The warm
tier is the opposite: unbounded, mutable, SQLite + FTS5 backed, NEVER
directly injected into the prompt. The agent reaches into it on demand
via `memory(action="recall", query="...")`.

This module is a thin wrapper around `plugins/memory/holographic/store.py`
that:
  - Renames the class to ``WarmStore`` to avoid the naming collision with
    ``tools.memory_tool.MemoryStore``.
  - Exposes a stable, opinionated API for the unified memory tool
    (add / recall / recall_related / list / promote / demote / remove).
  - Tags every entry with ``tier="warm"`` semantics. Promotion to hot
    tier is delegated to the caller (we just hand back the entry text).
  - Does NOT register tool schemas, does NOT use the MemoryProvider
    plumbing — those are for external/swappable backends. The warm tier
    is internal infrastructure of the unified memory tool.

Lazy singleton: the SQLite connection is created on first use, then
reused for the lifetime of the process. ``get_warm_store()`` is the
entry point; pass an explicit ``db_path`` only in tests.

Threading: holographic's MemoryStore uses an internal RLock, so
concurrent calls from multiple threads (e.g. background recall thread +
foreground tool call) are safe.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Lazy-imported holographic store. Done lazily so:
#   (1) memory_tool.py import doesn't pull SQLite cost when memory is disabled,
#   (2) test isolation works (test fixture can substitute a fresh DB),
#   (3) numpy import (used for HRR if available) is deferred.
_HoloMemoryStore = None


def _load_holo() -> type:
    """Import and return ``plugins.memory.holographic.store.MemoryStore``."""
    global _HoloMemoryStore
    if _HoloMemoryStore is None:
        from plugins.memory.holographic.store import MemoryStore as _MS
        _HoloMemoryStore = _MS
    return _HoloMemoryStore


# ---------------------------------------------------------------------------
# WarmStore — the public API used by tools/memory_tool.py
# ---------------------------------------------------------------------------

class WarmStore:
    """Searchable, unbounded warm-tier memory backed by SQLite + FTS5.

    Wraps holographic's MemoryStore. The wrapper is intentionally small —
    most logic lives in the underlying store.
    """

    # Legal default category. Holographic's schema defaults to 'general';
    # we keep that for compatibility but expose it here for tests / migration.
    DEFAULT_CATEGORY: str = "general"

    def __init__(self, db_path: Optional[str | Path] = None) -> None:
        cls = _load_holo()
        # Holographic's MemoryStore handles its own path-defaulting via
        # hermes_constants.get_hermes_home() / "memory_store.db" when
        # db_path is None — so we pass through.
        self._inner = cls(db_path=str(db_path) if db_path else None)
        self.db_path = self._inner.db_path

    # -- Writes -------------------------------------------------------------

    def add(
        self,
        content: str,
        category: str = DEFAULT_CATEGORY,
        tags: str = "",
    ) -> Dict[str, Any]:
        """Add a fact to warm memory.

        Returns a dict with ``fact_id`` and a status (``"created"`` or
        ``"existing"`` if the content already existed).
        """
        content = (content or "").strip()
        if not content:
            return {"success": False, "error": "Content cannot be empty."}

        # Detect whether this content already exists before insert (the
        # underlying ``add_fact`` returns the existing id silently on
        # duplicate, which we want to surface to the caller).
        existing_id = self._inner._conn.execute(  # type: ignore[attr-defined]
            "SELECT fact_id FROM facts WHERE content = ?", (content,)
        ).fetchone()

        try:
            fact_id = self._inner.add_fact(content=content, category=category, tags=tags)
        except sqlite3.OperationalError as e:
            return {"success": False, "error": f"warm-tier write failed: {e}"}

        status = "existing" if existing_id else "created"
        return {"success": True, "fact_id": int(fact_id), "status": status}

    def update(
        self,
        fact_id: int,
        content: Optional[str] = None,
        tags: Optional[str] = None,
        category: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update an existing fact. Returns success + updated row."""
        ok = self._inner.update_fact(
            fact_id=fact_id,
            content=content,
            tags=tags,
            category=category,
        )
        if not ok:
            return {"success": False, "error": f"No warm fact with id {fact_id}."}
        return {"success": True, "fact_id": fact_id}

    def remove(self, fact_id: int) -> Dict[str, Any]:
        """Delete a warm fact by id."""
        ok = self._inner.remove_fact(fact_id=fact_id)
        if not ok:
            return {"success": False, "error": f"No warm fact with id {fact_id}."}
        return {"success": True, "fact_id": fact_id, "status": "removed"}

    def record_feedback(self, fact_id: int, helpful: bool) -> Dict[str, Any]:
        """Record helpful/unhelpful feedback. Used to train trust scores."""
        try:
            r = self._inner.record_feedback(fact_id=fact_id, helpful=helpful)
            r["success"] = True
            return r
        except KeyError:
            return {"success": False, "error": f"No warm fact with id {fact_id}."}

    # -- Reads --------------------------------------------------------------

    def recall(
        self,
        query: str,
        top_k: int = 5,
        category: Optional[str] = None,
        min_trust: float = 0.0,
    ) -> List[Dict[str, Any]]:
        """Search warm memory for facts matching ``query``.

        Backed by FTS5 BM25. Returns at most ``top_k`` rows ordered by
        relevance then trust score. ``category`` filters to a single
        category if set. ``min_trust`` defaults to 0.0 (no filtering) so
        newly-added facts (with default 0.5 trust) and even decayed
        facts can be retrieved — let BM25 do the ranking.
        """
        query = (query or "").strip()
        if not query:
            return []

        top_k = max(1, min(int(top_k), 25))
        # FTS5 has reserved syntax (AND/OR/NOT, parens, quotes). For a
        # natural-language query the agent passes in, we want substring-
        # ish matching, not boolean-search semantics. The simplest robust
        # transform: wrap the whole query in double quotes to make it a
        # phrase, but only if it doesn't already contain FTS5 syntax.
        clean_query = self._sanitize_fts_query(query)

        rows = self._inner.search_facts(
            query=clean_query,
            category=category,
            min_trust=min_trust,
            limit=top_k,
        )
        _record_recall_for_auto_feedback(rows)
        return rows

    def recall_related(
        self,
        seed: str,
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        """Find facts related to a seed string by tag/keyword overlap.

        Phase 1 implementation: split the seed on whitespace and OR the
        tokens through FTS5. Quick, no semantic similarity, but better
        than nothing for "what else does this remind me of."

        Future enhancement (Phase 4): use HRR similarity if numpy is
        available — the underlying store already computes vectors per
        fact, we just don't expose the query path yet.
        """
        seed = (seed or "").strip()
        if not seed:
            return []

        tokens = [t for t in seed.split() if len(t) >= 3]
        if not tokens:
            return []

        # OR the tokens together. FTS5 syntax: "foo" OR "bar" OR "baz".
        query = " OR ".join(f'"{self._escape_fts_phrase(t)}"' for t in tokens[:8])
        rows = self._inner.search_facts(query=query, limit=max(1, min(int(top_k), 25)))
        _record_recall_for_auto_feedback(rows)
        return rows

    def list_facts(
        self,
        category: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Browse warm facts ordered by trust score descending."""
        return self._inner.list_facts(category=category, limit=max(1, min(int(limit), 200)))

    def get(self, fact_id: int) -> Optional[Dict[str, Any]]:
        """Fetch a single fact by id, or None."""
        row = self._inner._conn.execute(  # type: ignore[attr-defined]
            """
            SELECT fact_id, content, category, tags, trust_score,
                   retrieval_count, helpful_count, created_at, updated_at
            FROM facts WHERE fact_id = ?
            """,
            (fact_id,),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def count(self) -> int:
        """Return the total number of facts indexed."""
        row = self._inner._conn.execute(  # type: ignore[attr-defined]
            "SELECT COUNT(*) AS n FROM facts"
        ).fetchone()
        return int(row["n"]) if row else 0

    # -- FTS5 query sanitization -------------------------------------------

    @staticmethod
    def _sanitize_fts_query(query: str) -> str:
        """Convert a natural-language query into a robust FTS5 expression.

        FTS5's default tokenizer treats most input as a series of unquoted
        tokens implicitly AND-ed together. That breaks on punctuation
        (the ``!`` in "wasn't!", a trailing ``?``, parentheses) and on
        reserved keywords used as content (``OR``, ``AND``, ``NOT``).

        Strategy:
          1. If the query already contains FTS5 syntax (double quotes,
             explicit AND/OR/NOT in caps, parens, ``*`` for prefix),
             trust the caller and pass through.
          2. Otherwise, split on whitespace, drop tokens that are pure
             punctuation, and AND the tokens together as quoted phrases.
             This makes ``"docker networking"`` (lowercase) into
             ``"docker" AND "networking"`` — robust against punctuation.
        """
        # Already-structured FTS5 query: pass through.
        if any(tok in query for tok in ('"', '*', '(', ')')):
            return query
        # Look for explicit boolean operators (case-sensitive in FTS5).
        for op in (" AND ", " OR ", " NOT "):
            if op in query:
                return query

        # Tokenize on whitespace, drop pure-punctuation tokens, escape quotes.
        tokens: List[str] = []
        for raw in query.split():
            cleaned = raw.strip(".,;:!?()[]{}\"'`")
            if not cleaned:
                continue
            tokens.append(WarmStore._escape_fts_phrase(cleaned))
        if not tokens:
            return ""
        return " AND ".join(f'"{t}"' for t in tokens)

    @staticmethod
    def _escape_fts_phrase(token: str) -> str:
        """Escape a token for inclusion in a quoted FTS5 phrase.

        Inside a quoted phrase, the only special character is the double
        quote itself (FTS5 doesn't recognize backslash escapes — instead
        a literal ``"`` is written as ``""``).
        """
        return token.replace('"', '""')

    # -- Lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        try:
            self._inner.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Auto-feedback bridge — fires after every recall to stash results in the
# per-session window (see ``tools/memory_auto_feedback``). No-op when the
# feature is disabled in config; failures are swallowed so audit issues
# can never break a recall call.
# ---------------------------------------------------------------------------


def _record_recall_for_auto_feedback(rows: List[Dict[str, Any]]) -> None:
    """Tell the auto-feedback layer about recall results, if it's enabled.

    Best-effort: import + dispatch are both wrapped in try/except.
    Session id is read from a contextvar set by ``run_agent.py`` at turn
    start — when no session is bound (subagent, test, gateway side-call),
    this returns immediately without doing any work.
    """
    if not rows:
        return
    try:
        from tools.memory_auto_feedback.audit import (
            current_session_id,
            record_recall,
        )
        session_id = current_session_id()
        if not session_id:
            return
        record_recall(session_id, rows)
    except Exception:
        # Audit must NEVER break recall. Swallow everything.
        pass


# ---------------------------------------------------------------------------
# Module-level singleton (lazy)
# ---------------------------------------------------------------------------

_warm_singleton: Optional[WarmStore] = None
_singleton_lock = threading.Lock()


def get_warm_store(db_path: Optional[str | Path] = None) -> WarmStore:
    """Return the process-wide WarmStore singleton, creating it on first use.

    Pass ``db_path`` only in tests — production code should let it default
    to ``$HERMES_HOME/memory_store.db``.
    """
    global _warm_singleton
    with _singleton_lock:
        if _warm_singleton is None or db_path is not None:
            if _warm_singleton is not None and db_path is not None:
                # Test path: explicit override — close the old singleton.
                try:
                    _warm_singleton.close()
                except Exception:
                    pass
            try:
                _warm_singleton = WarmStore(db_path=db_path)
            except Exception as e:
                logger.warning("Warm-tier memory unavailable: %s", e)
                raise
        return _warm_singleton


def reset_warm_store_for_testing() -> None:
    """Test-only: drop the singleton so the next get_warm_store() rebuilds it."""
    global _warm_singleton
    with _singleton_lock:
        if _warm_singleton is not None:
            try:
                _warm_singleton.close()
            except Exception:
                pass
            _warm_singleton = None
