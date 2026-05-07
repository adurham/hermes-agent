"""Auto-extraction layer for warm-tier memory (Phase 2).

Watches conversation turns and proposes new warm-tier memory entries via
bounded LLM calls (per-turn, pre-compress, session-end). Conflict-checks
against existing warm facts and routes the verdict to confirm/auto-commit.

Design:
  * Bounded contexts everywhere — NEVER feed the full session to the
    extraction LLM. Per-turn slices are 2-10K tokens; pre-compress
    piggybacks on the existing compression call; session-end runs over
    the post-compression remainder only.
  * Lazy and best-effort — every entry point is wrapped in try/except;
    extraction failures must NEVER block the agent loop.
  * Anthropic-only — uses the existing ``auxiliary_client.call_llm``
    routing chain. Default model: ``claude-haiku-4-5``. User-overridable
    via ``auxiliary.memory_extraction.model`` / ``.provider``.
  * Per-session JSON buffer at ``$HERMES_HOME/memory_extraction_buffer.json``
    so a crash mid-session doesn't lose proposals.
  * No new tool surface — proposals land in the warm-tier SQLite DB
    via the existing WarmStore.

Public API (called from run_agent.py / cli.py):
  * ``on_turn_end(session_id, user_msg, assistant_msg)`` — per-turn extraction
  * ``on_pre_compress(session_id, messages)`` — extract before compression discards messages
  * ``on_session_end(session_id, messages, *, interactive=False)`` — final pass with confirm UI
  * ``flush_buffer(session_id)`` — drop the per-session buffer (called on /reset)
  * ``is_enabled()`` — config check; True when memory.auto_extract is on

This module is import-light: it only loads heavy deps (LLM client, JSON
schemas) on first call. Importing the module costs ~10ms.
"""

from __future__ import annotations

from tools.memory_extraction.extractor import (
    flush_buffer,
    is_enabled,
    on_pre_compress,
    on_session_end,
    on_turn_end,
)

__all__ = [
    "flush_buffer",
    "is_enabled",
    "on_pre_compress",
    "on_session_end",
    "on_turn_end",
]
