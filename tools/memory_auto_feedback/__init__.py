"""Automatic warm-tier feedback layer.

Trains warm-tier ``trust_score`` without requiring the agent to remember
to call ``memory(action="feedback", ...)`` after every recall.

Mechanism (asymmetric, upvote-only):

  1. ``record_recall(session_id, results)`` is called from
     ``WarmStore.recall`` and ``WarmStore.recall_related`` whenever a
     session id has been bound via ``set_session``. It stashes the
     returned fact_ids + small content fingerprints in a per-session
     sliding window.

  2. ``on_turn_end(session_id, assistant_text)`` runs from
     ``run_agent.py`` right after ``memory_extraction.on_turn_end``.
     It walks the live window for this session, fingerprints the
     assistant text, and for every fact whose fingerprint shows up in
     the assistant text within ``recall_window_turns`` turns (default
     3), it calls ``WarmStore.record_feedback(fact_id, helpful=True)``.

  3. Asymmetric — NEVER auto-downvotes. Silence != unhelpful. The
     negative direction stays manual: a fact that is misleading or
     stale still needs an explicit
     ``memory(action="feedback", helpful=False)`` call.

Config (``~/.hermes/config.yaml``)::

    memory:
        auto_feedback: true            # default false; opt-in
        recall_window_turns: 3         # turns a recall stays "live"
        min_fingerprint_words: 4       # min words for a distinctive fp
        max_facts_per_session: 200     # hard cap on the per-session window

Public API (called from run_agent.py / tools/memory_warm.py)::

    set_session(session_id)
    record_recall(session_id, results)
    on_turn_end(session_id, assistant_text)
    flush_session(session_id)
    is_enabled()

Import-light: no LLM calls, no SQLite queries beyond the single
``record_feedback`` write per matched fact. Failures degrade gracefully;
every entry point is wrapped in try/except so warm recall never breaks
because audit failed.

Design notes:
  * The fingerprint is a distinctive run of content-words from the
    recalled fact. We don't use ``fact_id`` directly — the assistant
    rarely echoes raw ids, but it does paraphrase or quote distinctive
    phrases from facts it actually consults.
  * One upvote per fact per session — a session-scoped "already
    credited" set prevents double-counting when the same fact is
    recalled twice.
  * Uses ``contextvars`` instead of threading session_id through every
    call site. Avoids touching ``tools/memory_tool.py``'s arg surface
    or the two memory-bypass blocks in ``run_agent.py`` (the recurring
    bug surface documented in
    ``software-development/hermes-agent-internals/references/memory-tool-bypass-dispatch.md``).
"""

from __future__ import annotations

from tools.memory_auto_feedback.audit import (
    flush_session,
    is_enabled,
    on_turn_end,
    record_recall,
    set_session,
)

__all__ = [
    "flush_session",
    "is_enabled",
    "on_turn_end",
    "record_recall",
    "set_session",
]
