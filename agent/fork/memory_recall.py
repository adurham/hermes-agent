"""Memory-recall reminder feature (fork-only).

Mirrors ``agent.fork.skill_recall``: after every Nth tool call (or
immediately when the user explicitly invokes memory directives), inject
a one-line nudge into the tool result asking the agent to call
``memory(action='recall', query=...)`` against the warm-tier store.

Why this exists: hot-tier memory is char-capped, warm-tier memory is
unbounded but only consulted when the agent decides to recall — which
rarely happens during the moment that matters most, hypothesis
formation mid-investigation. This reminder closes the gap by surfacing
the warm tier at predictable intervals.

See ``agent.fork.skill_recall`` for the proven shape this mirrors and
``docs/plans/2026-05-19-memory-recall-reminder-and-session-pin.md`` for
the full design.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)


# Stopwords stripped from query-candidate extraction. Short, common
# English words that carry no retrieval signal. The list is deliberately
# small — we keep proper nouns and 6+ char content words regardless.
_STOPWORDS = frozenset({
    "the", "and", "but", "for", "with", "from", "this", "that",
    "these", "those", "are", "was", "were", "have", "has", "had",
    "will", "would", "could", "should", "can", "may", "might", "must",
    "into", "onto", "upon", "about", "after", "before", "during",
    "what", "when", "where", "which", "who", "why", "how", "let",
    "lets", "let's", "you", "your", "yours", "they", "them", "their",
    "ours", "him", "her", "his", "hers", "its", "our", "any", "all",
    "some", "such", "than", "then", "now", "here", "there", "very",
    "much", "many", "more", "most", "less", "least", "just", "only",
    "also", "even", "still", "yet", "back", "well", "good", "bad",
    "ok", "okay", "yes", "no", "not", "nor", "off", "out", "over",
    "under", "again", "once", "twice",
})

# Explicit memory-directive triggers that fire the reminder immediately
# regardless of the cooldown counter. Matched case-insensitively.
# We allow up to ~40 chars of filler between the verb and the temporal
# marker so phrasings like "we did this NEC investigation before" still
# trigger (a tight ``we did this before`` pattern misses real cases).
_DIRECTIVE_PATTERNS = (
    re.compile(r"\bremember\b", re.IGNORECASE),
    re.compile(
        r"\bwe (did|saw|worked|hit|handled|investigated|debugged|tried|covered|"
        r"learned|discovered|noticed|found|talked\s+about|discussed)\b"
        r"(?:[^.?!\n]{0,80}?)\b(before|earlier|previously|last\s+time)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(prior|past|previous|earlier|last)\s+"
        r"(session|investigation|case|work|debug|conversation|chat)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(forget|never\s+mind|nevermind)\b", re.IGNORECASE),
    re.compile(r"\blike\s+(last|previously|before)\b", re.IGNORECASE),
)

# Tokens to extract from text. We use three patterns merged:
#   * Proper nouns: capitalized word (≥3 chars)
#   * Long lowercase words: 6+ chars
#   * Code-like tokens: 2+ uppercase letters followed by optional digits
# All matches are deduplicated downstream.
_TOKEN_RE = re.compile(
    r"\b("
    r"[A-Z][a-zA-Z]{2,}"           # ProperNoun
    r"|[A-Z]{2,}(?:-\d+)?"          # AIDEV-72, CDN, NEC
    r"|[a-z]{6,}"                  # investigating, regression
    r")\b"
)


# ---------------------------------------------------------------------------
# Query extraction — local, no LLM call
# ---------------------------------------------------------------------------


def extract_query_candidate(
    user_msg: str,
    recent_tool_args: Optional[Sequence[Any]] = None,
    max_tokens: int = 5,
) -> Optional[str]:
    """Extract a short FTS5-friendly query candidate from a user message
    plus optional recent tool args.

    Returns an `` OR ``-joined query of up to ``max_tokens`` distinct
    tokens, or ``None`` if nothing meaningful can be extracted.

    Tokens are deduplicated (case-insensitive). We prefer message
    content over tool args (proper nouns in the user's own framing
    typically carry more signal than file paths or command tokens), but
    fall back to tool args when the message is terse.
    """
    if not user_msg or not user_msg.strip():
        user_msg = ""

    candidates: List[str] = []
    seen_lower: set[str] = set()

    def _consume(text: str) -> None:
        for m in _TOKEN_RE.finditer(text or ""):
            tok = m.group(1)
            if tok.lower() in _STOPWORDS:
                continue
            key = tok.lower()
            if key in seen_lower:
                continue
            seen_lower.add(key)
            candidates.append(tok)
            if len(candidates) >= max_tokens:
                return

    _consume(user_msg)

    # If we still have room, scan tool args. Coerce dict/list values to
    # strings — we just want lexical tokens out of whatever's there.
    if len(candidates) < max_tokens and recent_tool_args:
        for arg in recent_tool_args:
            if len(candidates) >= max_tokens:
                break
            if arg is None:
                continue
            if isinstance(arg, (str, bytes)):
                _consume(arg if isinstance(arg, str) else arg.decode("utf-8", "ignore"))
            elif isinstance(arg, dict):
                for v in arg.values():
                    if len(candidates) >= max_tokens:
                        break
                    if isinstance(v, str):
                        _consume(v)
            elif isinstance(arg, (list, tuple)):
                for v in arg:
                    if len(candidates) >= max_tokens:
                        break
                    if isinstance(v, str):
                        _consume(v)

    if not candidates:
        return None

    return " OR ".join(candidates)


# ---------------------------------------------------------------------------
# Warm-tier access — wrapped so tests can stub these out cheaply.
# ---------------------------------------------------------------------------


def _get_warm_count() -> int:
    """Return the warm-tier indexed-fact count, or 0 if unavailable."""
    try:
        from tools.memory_warm import get_warm_store
        return int(get_warm_store().count())
    except Exception:
        return 0


def _run_warm_recall(query: str, top_k: int) -> List[Dict[str, Any]]:
    """Run a warm-tier recall query and return the result rows.

    Best-effort: a failure (DB locked, FTS5 missing, etc.) returns an
    empty list so the reminder never crashes the agent loop.
    """
    try:
        from tools.memory_warm import get_warm_store
        return get_warm_store().recall(query=query, top_k=int(top_k))
    except Exception as e:
        logger.debug("memory-recall reminder: warm recall failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Reminder firing
# ---------------------------------------------------------------------------


def _has_explicit_directive(user_msg: str) -> bool:
    if not user_msg:
        return False
    return any(p.search(user_msg) for p in _DIRECTIVE_PATTERNS)


def _format_hint_message(
    interval: int,
    query: str,
    auto_results: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Format the one-line reminder string injected into the tool result."""
    base = (
        f"\n\n[memory-recall reminder] You haven't consulted warm memory "
        f"in {interval} turns."
    )
    if auto_results:
        first = auto_results[0]
        # Truncate the preview content to ~140 chars so the reminder
        # stays one short paragraph.
        preview = (first.get("content") or "")[:140].rstrip()
        if len(first.get("content") or "") > 140:
            preview += "..."
        trust = first.get("trust_score", 0.5)
        return (
            base
            + f" Auto-recall for query \"{query}\" returned "
            + f"{len(auto_results)} relevant fact(s). "
            + f"Top: [fact {first.get('fact_id')}, trust {trust}, \"{preview}\"]. "
            + "Call memory(action='recall', query='...') for more, "
            + "or memory(action='read', fact_id=N) for full text. "
            + "If a fact applies to this whole session, "
            + "memory(action='pin', fact_id=N) keeps it in your prompt."
        )
    # Hint mode — point the agent at the recall call directly.
    return (
        base
        + " If your current investigation touches a topic you've worked on "
        + f"before, call memory(action='recall', query='{query}') — "
        + "keyword search across all indexed warm facts. "
        + f"This reminder fires every {interval} tool calls and is cheap "
        + "to act on."
    )


def maybe_memory_recall_hint(agent, function_name: str) -> Optional[str]:
    """Return a one-line memory-recall reminder, or ``None``.

    Fires when ALL these hold:
      * ``_memory_recall_reminder_interval > 0``
      * warm tier is non-empty
      * the last user message is at least ``_memory_recall_min_user_chars``
        long (filters out one-liner Q&A), OR the message contains an
        explicit memory directive ("remember", "we did this before", ...)
      * the counter has reached the interval (unless directive bypass)
      * a meaningful query candidate can be extracted from the user
        message + recent tool args

    Increments the counter on every call. Resets to 0 when the reminder
    actually fires.

    ``function_name`` is currently unused (every tool call ticks the
    counter), but is accepted so callers can match the
    ``_maybe_skill_recall_hint`` signature and we keep the option of
    filtering on tool type later without changing the call site.
    """
    interval = getattr(agent, "_memory_recall_reminder_interval", 0)
    if interval <= 0:
        return None

    # We treat the same tools the skill-recall reminder treats as "this
    # tool call counts" — but for memory we use ALL tool calls, because
    # memory is relevant for hypothesis formation regardless of tool
    # type. ``function_name`` accepted for future filtering.
    _ = function_name  # silence linters

    last_msg = getattr(agent, "_last_user_message", "") or ""
    min_chars = int(getattr(agent, "_memory_recall_min_user_chars", 200))
    has_directive = _has_explicit_directive(last_msg)

    # Short-message gate (unless directive present).
    if not has_directive and len(last_msg) < min_chars:
        return None

    # Warm tier must exist.
    if _get_warm_count() <= 0:
        return None

    # Tick the counter. Fire only when we've reached the interval
    # (or on directive bypass).
    agent._turns_since_memory_recall = getattr(
        agent, "_turns_since_memory_recall", 0
    ) + 1
    if not has_directive and agent._turns_since_memory_recall < interval:
        return None

    # Extract a query candidate. If we can't, skip THIS turn but pull
    # the counter back by one so we can fire next turn with real signal.
    recent_args = getattr(agent, "_recent_tool_args", None) or []
    query = extract_query_candidate(last_msg, recent_args)
    if not query:
        agent._turns_since_memory_recall = max(
            0, agent._turns_since_memory_recall - 1,
        )
        return None

    # Build the reminder.
    mode = getattr(agent, "_memory_recall_reminder_mode", "auto")
    top_k = int(getattr(agent, "_memory_recall_auto_top_k", 3))

    if mode == "auto":
        results = _run_warm_recall(query=query, top_k=top_k)
        if results:
            agent._turns_since_memory_recall = 0
            return _format_hint_message(interval, query, auto_results=results)
        # Empty recall → fall back to hint mode rather than emitting
        # garbage "0 results" text. Reset counter; the agent saw a recall
        # was attempted and found nothing — it can decide what to do.
        agent._turns_since_memory_recall = 0
        return (
            f"\n\n[memory-recall reminder] Auto-recall for query \"{query}\" "
            "returned 0 hits in warm tier. Try different keywords with "
            "memory(action='recall', query='...') or skip this turn."
        )

    # hint mode — just text, no DB hit.
    agent._turns_since_memory_recall = 0
    return _format_hint_message(interval, query)


# ---------------------------------------------------------------------------
# Voluntary recall — counter reset hook called from memory_tool
# ---------------------------------------------------------------------------


def record_voluntary_recall(agent) -> None:
    """Reset the reminder counter when the agent invokes recall voluntarily.

    Best-effort: no-op if the agent wasn't built with the feature
    attributes (subagent, test harness, gateway side-call, etc.).
    """
    try:
        agent._turns_since_memory_recall = 0
    except Exception:
        # Defensive: never break a recall call on counter-reset errors.
        pass


# ---------------------------------------------------------------------------
# init_state — wired in from agent/agent_init.py
# ---------------------------------------------------------------------------


def init_state(agent) -> None:
    """Initialize per-agent state for the memory-recall reminder.

    Called once from ``agent.agent_init.init_agent``. The interval and
    mode are overridden later by ``init_agent`` from
    ``agent.memory.recall_reminder_*`` config keys.
    """
    agent._memory_recall_reminder_interval = 8
    agent._memory_recall_reminder_mode = "auto"
    agent._memory_recall_auto_top_k = 3
    agent._memory_recall_min_user_chars = 200
    agent._turns_since_memory_recall = 0
    agent._last_user_message = ""
    # Sliding window of the last few tool-call argument dicts. Capped
    # at 3 — we only look back a few tool calls for query extraction.
    # Real wiring in tool_executor / agent_runtime_helpers maintains
    # this list; tests can pass a plain list.
    agent._recent_tool_args = []
