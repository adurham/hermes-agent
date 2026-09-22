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

MIGRATION (2026-09, owner-approved architecture decision, NOT a bug fix):
the holographic plugin is now the warm tier's backend of record for BOTH
storage and retrieval. Reads used to run through a fork-added
``MemoryStore.search_facts`` (raw FTS5 rank -> trust ordering) and a
``recall_related`` that its own docstring admitted was a placeholder
("Phase 1: OR the tokens through FTS5 ... Future enhancement (Phase 4):
use HRR similarity"). Both now delegate to upstream's ``FactRetriever``:

  - ``recall``        -> ``FactRetriever.search``  (limit*3 FTS5 candidates,
    reranked by weighted Jaccard + HRR vector cosine + trust, with optional
    temporal decay) instead of bare BM25 order.
  - ``recall_related`` -> ``FactRetriever.related`` (vector-space structural
    adjacency) — the Phase 4 that was never written, already shipped
    upstream.

``search_facts`` is gone from the shared plugin tree; the one behavior it
had that upstream's retriever lacks — bumping ``retrieval_count`` — is now
an explicit ``bump_retrieval_counts`` call made HERE, by the warm tier, on
the rows actually handed to a caller. That keeps retrieval accounting tied
to a deliberate recall (which ``tools/memory_extraction/conflict.py`` relies
on to break duplicate ties) rather than to every ranking pass.

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


def _holo_plugin_config() -> Dict[str, Any]:
    """The holographic plugin's own config section, or ``{}``.

    Read through the same key the plugin reads (``plugins.hermes-memory-store``) so
    warm-tier retrieval tuning and the registered-provider path share one set of
    knobs. Any failure degrades to defaults — retrieval must never hard-fail on config.
    """
    try:
        from hermes_cli.config import cfg_get, load_config_readonly
        return cfg_get(load_config_readonly(), "plugins", "hermes-memory-store", default={}) or {}
    except Exception:
        return {}


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
        self._retriever_obj = None

    @property
    def _retriever(self):
        """Upstream ``FactRetriever`` over this store, built on first read.

        Built lazily (not in ``__init__``) so a write-only caller — the hot-tier
        audit's demote sink, memory_extraction's committer — never pays for the
        retrieval import. Config comes from the holographic plugin's own section so
        the warm tier and a registered ``memory.provider: holographic`` rank facts
        identically instead of drifting apart.
        """
        if self._retriever_obj is None:
            from plugins.memory.holographic.retrieval import FactRetriever
            cfg = _holo_plugin_config()
            self._retriever_obj = FactRetriever(
                store=self._inner,
                hrr_dim=int(cfg.get("hrr_dim", 1024)),
                hrr_weight=float(cfg.get("hrr_weight", 0.3)),
                temporal_decay_half_life=int(cfg.get("temporal_decay_half_life", 0)),
            )
        return self._retriever_obj

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
        existing = self._inner.find_fact_id_by_content(content)

        try:
            fact_id = self._inner.add_fact(content=content, category=category, tags=tags)
        except sqlite3.OperationalError as e:
            return {"success": False, "error": f"warm-tier write failed: {e}"}

        status = "existing" if existing is not None else "created"
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

        Backed by upstream's ``FactRetriever.search``: FTS5 pulls ``top_k * 3``
        candidates, then they are reranked by weighted Jaccard overlap + HRR vector
        cosine + trust score (and optional temporal decay). Returns at most ``top_k``
        rows. ``category`` filters to a single category if set. ``min_trust``
        defaults to 0.0 (no filtering) so newly-added facts (default 0.5 trust) and
        even decayed facts stay retrievable — let the ranker decide.

        Query sanitization lives in ``FactRetriever._sanitize_fts_query`` (drops
        stopwords, strips FTS5 operators, splits hyphenated codes like
        ``PLAT-15800`` so they tokenize), so the raw query passes straight through.
        """
        query = (query or "").strip()
        if not query:
            return []

        top_k = max(1, min(int(top_k), 25))
        rows = self._retriever.search(
            query=query,
            category=category,
            min_trust=min_trust,
            limit=top_k,
        )
        self._after_recall(rows)
        return rows

    def recall_related(
        self,
        seed: str,
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        """Find facts structurally related to a seed string.

        Delegates to upstream's ``FactRetriever.related``, which unbinds the seed's
        vector from each fact vector and keeps the facts where the seed plays a
        structural role — genuine "what else does this remind me of" adjacency rather
        than the token-OR keyword approximation this method used to do. Without numpy
        the retriever falls back to ``search`` on its own, so behavior degrades to
        keyword overlap instead of failing.
        """
        seed = (seed or "").strip()
        if not seed:
            return []

        # Single chars can't carry structural signal, and the pre-migration contract
        # (asserted by tests) is an empty list rather than a scan of the whole store.
        if not [t for t in seed.split() if len(t) >= 3]:
            return []

        rows = self._retriever.related(seed, limit=max(1, min(int(top_k), 25)))
        self._after_recall(rows)
        return rows

    def _after_recall(self, rows: List[Dict[str, Any]]) -> None:
        """Retrieval bookkeeping for rows we actually handed back to a caller.

        ``FactRetriever`` deliberately doesn't touch ``retrieval_count`` (it ranks;
        it doesn't decide that a fact was used). The warm tier does, because a
        ``recall`` IS a deliberate use — and ``memory_extraction/conflict.py`` reads
        the counter to favor an existing fact over a near-duplicate proposal.
        """
        if not rows:
            return
        try:
            ids = [int(r["fact_id"]) for r in rows if r.get("fact_id") is not None]
            self._inner.bump_retrieval_counts(ids)
            for row in rows:  # keep returned dicts consistent with the persisted count
                if row.get("retrieval_count") is not None:
                    row["retrieval_count"] = int(row["retrieval_count"]) + 1
        except Exception as e:
            logger.debug("warm recall bookkeeping failed: %s", e)
        _record_recall_for_auto_feedback(rows)

    def list_facts(
        self,
        category: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Browse warm facts ordered by trust score descending."""
        return self._inner.list_facts(category=category, limit=max(1, min(int(limit), 200)))

    def get(self, fact_id: int) -> Optional[Dict[str, Any]]:
        """Fetch a single fact by id, or None."""
        return self._inner.get_fact(int(fact_id))

    def count(self) -> int:
        """Return the total number of facts indexed."""
        return self._inner.count_facts()

    # -- Lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        # Drop the retriever first: it holds a reference to the store, and a
        # rebuilt singleton must not resurrect a retriever over a closed handle.
        self._retriever_obj = None
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
