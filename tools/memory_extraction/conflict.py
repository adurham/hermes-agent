"""Conflict resolution between proposed and existing warm-tier facts.

Workflow:
  1. ``classify(content)`` runs an FTS5 search for similar facts.
  2. If no matches → verdict = NEW immediately (no LLM call).
  3. Otherwise: one LLM classification call → DUPLICATE / REFINEMENT /
     CONTRADICTION / NEW.
  4. Caller dispatches based on verdict:
       - DUPLICATE → drop the proposal, bump retrieval count on existing
       - REFINEMENT → update existing fact's content with merged_content
       - CONTRADICTION → surface to user (or skip auto-commit; flag for confirm UI)
       - NEW → store as a fresh fact
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ConflictVerdict:
    verdict: str  # DUPLICATE | REFINEMENT | CONTRADICTION | NEW
    matched_id: Optional[int] = None
    matched_content: Optional[str] = None
    rationale: str = ""
    merged_content: Optional[str] = None
    candidates: List[Dict[str, Any]] = field(default_factory=list)


def classify(
    content: str,
    *,
    warm_store: Any = None,
    llm_caller: Any = None,
) -> ConflictVerdict:
    """Classify a proposed entry against existing warm-tier content.

    Args:
        content: the new proposed fact text
        warm_store: optional WarmStore instance (default: lazy-load singleton)
        llm_caller: optional callable for LLM dispatch — for testing.
            Default: ``extractor._call_extraction_llm``. Accepts
            ``system: str, user: str, max_tokens: int -> str``.

    Returns a ConflictVerdict. Failures degrade gracefully to NEW.
    """
    content = (content or "").strip()
    if not content:
        return ConflictVerdict(verdict="NEW", rationale="empty content")

    # Step 1: FTS5 lookup
    if warm_store is None:
        try:
            from tools.memory_warm import get_warm_store
            warm_store = get_warm_store()
        except Exception as e:
            logger.warning("conflict classify: warm store unavailable: %s", e)
            return ConflictVerdict(verdict="NEW", rationale="warm store unavailable")

    # Use recall_related (OR-semantics on whitespace tokens) instead of
    # recall (AND-semantics): for conflict detection we want any meaningful
    # token overlap, not all-tokens-must-match. A strict recall would miss
    # paraphrased duplicates ("TDS uses cdsdb" vs "cdsdb is the TDS storage").
    candidates: List[Dict[str, Any]] = []
    try:
        candidates = warm_store.recall_related(content, top_k=5)
    except Exception as e:
        logger.debug("conflict classify: recall_related failed: %s", e)
        candidates = []

    if not candidates:
        return ConflictVerdict(verdict="NEW", rationale="no FTS5 matches")

    # Step 2: LLM classification
    if llm_caller is None:
        from tools.memory_extraction.extractor import _call_extraction_llm
        llm_caller = _call_extraction_llm

    from tools.memory_extraction import prompts

    try:
        response_text = llm_caller(
            system=prompts.CONFLICT_SYSTEM,
            user=prompts.conflict_user(content, candidates),
            max_tokens=400,
        )
    except Exception as e:
        # Best effort — if classification fails, default to NEW (write the
        # fact rather than risk dropping it). User can dedup later.
        logger.debug("conflict classify: LLM call failed: %s", e)
        return ConflictVerdict(
            verdict="NEW",
            rationale=f"LLM classify failed: {e}",
            candidates=candidates,
        )

    parsed = prompts.parse_conflict_response(response_text)
    if parsed is None:
        return ConflictVerdict(
            verdict="NEW",
            rationale="LLM response unparseable",
            candidates=candidates,
        )

    matched_id = parsed.get("matched_id")
    matched_content = None
    if matched_id is not None:
        for c in candidates:
            if c.get("fact_id") == matched_id:
                matched_content = c.get("content")
                break

    return ConflictVerdict(
        verdict=parsed["verdict"],
        matched_id=matched_id if isinstance(matched_id, int) else None,
        matched_content=matched_content,
        rationale=parsed.get("rationale", ""),
        merged_content=parsed.get("merged_content"),
        candidates=candidates,
    )


def apply_verdict(
    verdict: ConflictVerdict,
    proposal: Dict[str, Any],
    *,
    warm_store: Any = None,
    auto_commit: bool = False,
) -> Dict[str, Any]:
    """Apply a verdict to the warm tier.

    Returns a dict describing what happened:
        {action: <str>, fact_id: <int>, contradiction_pair: <dict?>}

    If auto_commit=False (default), CONTRADICTION verdicts are returned
    UNCOMMITTED so the user can resolve via the confirm UI. NEW / DUPLICATE
    / REFINEMENT auto-commit.
    """
    if warm_store is None:
        from tools.memory_warm import get_warm_store
        warm_store = get_warm_store()

    content = proposal.get("content", "").strip()
    category = proposal.get("category") or "general"
    tags = proposal.get("tags") or ""

    if verdict.verdict == "DUPLICATE":
        # No write needed — bump retrieval count to favor it on future recall.
        if verdict.matched_id is not None:
            try:
                warm_store.recall(content, top_k=1)  # increments retrieval_count
            except Exception:
                pass
        return {
            "action": "deduplicated",
            "fact_id": verdict.matched_id,
            "rationale": verdict.rationale,
        }

    if verdict.verdict == "REFINEMENT":
        merged = verdict.merged_content or content
        if verdict.matched_id is not None:
            warm_store.update(
                fact_id=verdict.matched_id,
                content=merged,
                tags=tags,
                category=category,
            )
            return {
                "action": "refined",
                "fact_id": verdict.matched_id,
                "merged_content": merged,
                "rationale": verdict.rationale,
            }
        # Matched id missing — fall through to NEW write
        verdict.verdict = "NEW"

    if verdict.verdict == "CONTRADICTION":
        if not auto_commit:
            return {
                "action": "contradiction_pending",
                "fact_id": None,
                "matched_id": verdict.matched_id,
                "matched_content": verdict.matched_content,
                "proposed_content": content,
                "rationale": verdict.rationale,
            }
        # auto_commit=True: write the new fact AND tag the existing one as
        # superseded. We do that by appending a "[superseded]" prefix to its
        # content; the user can clean up later.
        result = warm_store.add(content=content, category=category, tags=tags)
        if verdict.matched_id is not None and verdict.matched_content:
            try:
                warm_store.update(
                    fact_id=verdict.matched_id,
                    content=f"[superseded by fact {result['fact_id']}] {verdict.matched_content}",
                )
            except Exception:
                pass
        return {
            "action": "superseded",
            "fact_id": result.get("fact_id"),
            "superseded_id": verdict.matched_id,
            "rationale": verdict.rationale,
        }

    # NEW
    result = warm_store.add(content=content, category=category, tags=tags)
    return {
        "action": "stored" if result.get("status") == "created" else "duplicate_on_unique_index",
        "fact_id": result.get("fact_id"),
        "rationale": verdict.rationale,
    }
