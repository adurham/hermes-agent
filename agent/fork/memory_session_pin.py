"""Session-pin feature (fork-only).

Keeps selected warm-tier facts visible in the system prompt for the
duration of the current session. Sits between hot tier (always
loaded, char-capped, permanent) and warm tier (searchable, invisible
unless queried).

Why this exists: mid-investigation, the agent often realizes a single
warm fact applies to the WHOLE current session but doesn't belong in
hot tier (too specific, too short-lived). Re-querying warm every turn
is unreliable. Session-pin is the deliberate "keep this visible for
now" signal.

See ``docs/plans/2026-05-19-memory-recall-reminder-and-session-pin.md``
for the full design rationale.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Defaults. ``init_state`` writes these onto the agent so users can
# override via config without changing the module.
_DEFAULT_MAX_COUNT = 5
_DEFAULT_MAX_CHARS = 2000


# ---------------------------------------------------------------------------
# Warm-store lookup — wrapped so tests can stub it.
# ---------------------------------------------------------------------------


def _fetch_warm_fact(fact_id: int) -> Optional[Dict[str, Any]]:
    """Return the warm-tier row for ``fact_id`` or ``None``.

    Best-effort: any DB / import failure returns None and the caller
    surfaces a "fact not found" error.
    """
    try:
        from tools.memory_warm import get_warm_store
        return get_warm_store().get(int(fact_id))
    except Exception as e:
        logger.debug("session-pin: warm fetch failed for %s: %s", fact_id, e)
        return None


# ---------------------------------------------------------------------------
# pin / unpin / list
# ---------------------------------------------------------------------------


def _total_pinned_chars(agent) -> int:
    """Sum of pinned-fact content lengths on the agent."""
    pinned = getattr(agent, "_session_pinned_facts", {}) or {}
    return sum(len(row.get("content", "") or "") for row in pinned.values())


def pin_fact(agent, fact_id: int) -> Dict[str, Any]:
    """Pin a warm fact to the session prompt.

    Returns a status dict with ``success`` and either ``message`` or
    ``error``. Enforces the per-agent count and char caps. When the
    count cap is reached, evicts the oldest pin (LRU on insertion
    order) to make room.

    Idempotent — pinning an already-pinned fact returns success with a
    "already pinned" message and does NOT touch eviction order.
    """
    try:
        fact_id = int(fact_id)
    except (TypeError, ValueError):
        return {"success": False, "error": "fact_id must be an integer."}

    pinned: Optional[Dict[int, Dict[str, Any]]] = getattr(
        agent, "_session_pinned_facts", None,
    )
    if pinned is None:
        # Defensive: agent wasn't built with the feature wired.
        return {
            "success": False,
            "error": (
                "Session-pin not available on this agent. "
                "Feature requires the AIAgent runtime (not subagent/test stubs)."
            ),
        }

    # Idempotent pin.
    if fact_id in pinned:
        return {
            "success": True,
            "message": f"Fact {fact_id} is already pinned to this session.",
            "fact_id": fact_id,
        }

    row = _fetch_warm_fact(fact_id)
    if row is None:
        return {
            "success": False,
            "error": f"No warm fact with id {fact_id}.",
        }

    # Char-cap check FIRST — an oversized pin should be refused, not
    # silently evict something else to make room for it.
    max_chars = int(getattr(agent, "_session_pin_max_chars", _DEFAULT_MAX_CHARS))
    incoming = len(row.get("content", "") or "")
    if incoming > max_chars:
        return {
            "success": False,
            "error": (
                f"Fact {fact_id} is {incoming:,} chars; "
                f"exceeds session_pin_max_chars cap ({max_chars:,}). "
                "Reduce the cap with agent.memory.session_pin_max_chars "
                "or split the fact."
            ),
        }

    # Char-budget check across all pinned facts.
    if _total_pinned_chars(agent) + incoming > max_chars:
        # We can choose to evict oldest pins to make room — same LRU
        # logic as count-based eviction. Walk in insertion order until
        # the new fact fits OR we'd evict everything.
        evicted: List[int] = []
        for evict_id in list(pinned.keys()):
            if _total_pinned_chars(agent) + incoming <= max_chars:
                break
            pinned.pop(evict_id, None)
            evicted.append(evict_id)
        if _total_pinned_chars(agent) + incoming > max_chars:
            # Even after evicting everything we still don't fit — refuse.
            return {
                "success": False,
                "error": (
                    f"Fact {fact_id} ({incoming:,} chars) cannot fit in "
                    f"the session_pin_max_chars budget ({max_chars:,})."
                ),
            }
    else:
        evicted = []

    # Count-cap check — evict oldest until we have room.
    max_count = int(getattr(agent, "_session_pin_max_count", _DEFAULT_MAX_COUNT))
    while len(pinned) >= max_count and pinned:
        oldest_id = next(iter(pinned))
        pinned.pop(oldest_id, None)
        evicted.append(oldest_id)

    pinned[fact_id] = {
        "fact_id": fact_id,
        "content": row.get("content", ""),
        "category": row.get("category"),
        "trust_score": row.get("trust_score"),
        "tags": row.get("tags"),
    }

    msg = f"Pinned fact {fact_id} to this session."
    out: Dict[str, Any] = {
        "success": True,
        "fact_id": fact_id,
        "message": msg,
        "pinned_count": len(pinned),
    }
    if evicted:
        out["evicted"] = evicted
        out["message"] += f" Evicted {len(evicted)} older pin(s) to make room."
    return out


def unpin_fact(agent, fact_id: int) -> Dict[str, Any]:
    """Remove a session pin.

    Soft no-op when the fact isn't pinned — returns success=False with
    a clear error rather than crashing.
    """
    try:
        fact_id = int(fact_id)
    except (TypeError, ValueError):
        return {"success": False, "error": "fact_id must be an integer."}

    pinned: Optional[Dict[int, Dict[str, Any]]] = getattr(
        agent, "_session_pinned_facts", None,
    )
    if not pinned or fact_id not in pinned:
        return {
            "success": False,
            "error": f"Fact {fact_id} is not pinned to this session.",
        }

    pinned.pop(fact_id, None)
    return {
        "success": True,
        "fact_id": fact_id,
        "message": f"Unpinned fact {fact_id} from this session.",
        "pinned_count": len(pinned),
    }


def list_pinned(agent) -> Dict[str, Any]:
    """Return the list of currently pinned facts in pin order."""
    pinned = getattr(agent, "_session_pinned_facts", None) or {}
    rows = list(pinned.values())
    return {
        "success": True,
        "count": len(rows),
        "pinned": rows,
        "total_chars": _total_pinned_chars(agent),
        "max_count": int(
            getattr(agent, "_session_pin_max_count", _DEFAULT_MAX_COUNT)
        ),
        "max_chars": int(
            getattr(agent, "_session_pin_max_chars", _DEFAULT_MAX_CHARS)
        ),
    }


# ---------------------------------------------------------------------------
# System-prompt rendering
# ---------------------------------------------------------------------------


def render_pinned_block(agent) -> Optional[str]:
    """Render the pinned-facts block for the system prompt.

    Returns ``None`` when no facts are pinned or the agent wasn't built
    with the feature wired (test stubs, subagents).
    """
    pinned = getattr(agent, "_session_pinned_facts", None)
    if not pinned:
        return None

    total = _total_pinned_chars(agent)
    separator = "═" * 46
    header = (
        f"SESSION-PINNED FACTS ({len(pinned)} pinned, {total:,} chars)"
    )
    body_lines: List[str] = []
    for fact_id, row in pinned.items():
        content = (row.get("content") or "").strip()
        trust = row.get("trust_score")
        trust_str = f" trust={trust}" if trust is not None else ""
        body_lines.append(f"[fact {fact_id}{trust_str}] {content}")

    note = (
        "These facts are pinned for THIS session only. Apply them when "
        "reasoning. To unpin, call memory(action='unpin', fact_id=N)."
    )
    body = "\n§\n".join(body_lines)
    return f"{separator}\n{header}\n{separator}\n{body}\n\n{note}"


# ---------------------------------------------------------------------------
# init_state — wired from agent/agent_init.py
# ---------------------------------------------------------------------------


def init_state(agent) -> None:
    """Initialize per-agent state for the session-pin feature."""
    agent._session_pinned_facts = {}
    agent._session_pin_max_count = _DEFAULT_MAX_COUNT
    agent._session_pin_max_chars = _DEFAULT_MAX_CHARS
