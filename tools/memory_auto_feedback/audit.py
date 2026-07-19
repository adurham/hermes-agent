"""Implementation for tools.memory_auto_feedback.

See the package docstring in ``__init__.py`` for the overall design.

State (per-process, in-memory):
  * ``_session_windows[session_id]`` is a deque of ``RecallEntry`` —
    fact_id + fingerprint + age (turns since recall). Ages out after
    ``recall_window_turns``.
  * ``_credited[session_id]`` is a set of fact_ids already upvoted in
    this session. Prevents double-counting.
  * ``_session_ctx`` is a ``ContextVar`` so the warm store can know
    the current session_id without arg-threading.

Threading: the deque + set are protected by a per-session ``RLock``.
Concurrent recall from a background thread + foreground tool call is
safe (matches ``WarmStore``'s own locking guarantee).
"""

from __future__ import annotations

import contextvars
import logging
import re
import threading
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _get_config() -> Dict[str, Any]:
    """Return the ``memory.auto_feedback*`` config slice with defaults applied.

    Defaults: feature OFF. Opt-in via ``memory.auto_feedback: true``.
    """
    try:
        from hermes_cli.config_io import get_config

        cfg = get_config() or {}
    except Exception:
        cfg = {}
    mem = cfg.get("memory", {}) if isinstance(cfg, dict) else {}
    return {
        "enabled": bool(mem.get("auto_feedback", False)),
        "recall_window_turns": int(mem.get("recall_window_turns", 3) or 3),
        "min_fingerprint_words": int(mem.get("min_fingerprint_words", 4) or 4),
        "max_facts_per_session": int(mem.get("max_facts_per_session", 200) or 200),
    }


def is_enabled() -> bool:
    """Cheap check — returns True only when ``memory.auto_feedback: true``.

    Wrapped in try/except so a malformed config never blocks the warm
    recall path.
    """
    try:
        return bool(_get_config()["enabled"])
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Per-session state
# ---------------------------------------------------------------------------


@dataclass
class _RecallEntry:
    fact_id: int
    fingerprints: Tuple[str, ...]
    turn_age: int  # 0 = recalled this turn; increments on each on_turn_end


_session_windows: Dict[str, Deque[_RecallEntry]] = {}
_credited: Dict[str, Set[int]] = {}
_session_locks: Dict[str, threading.RLock] = {}
_state_lock = threading.RLock()

# Set by run_agent.py at turn start; read by WarmStore.recall() so the
# warm store doesn't need session_id as an arg.
_session_ctx: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "memory_auto_feedback_session", default=None,
)


def _get_lock(session_id: str) -> threading.RLock:
    with _state_lock:
        lock = _session_locks.get(session_id)
        if lock is None:
            lock = threading.RLock()
            _session_locks[session_id] = lock
        return lock


def set_session(session_id: Optional[str]) -> None:
    """Bind the current session_id to the context for downstream recall calls.

    Call at turn start. After this, ``WarmStore.recall`` will tag any
    results it returns with this session_id via ``record_recall``.

    Pass ``None`` to clear the binding (e.g. between sessions in the
    same process).
    """
    try:
        _session_ctx.set(session_id or None)
    except Exception:
        pass


def current_session_id() -> Optional[str]:
    """Return the contextvar-bound session id, or None."""
    try:
        return _session_ctx.get()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Fingerprinting — pick distinctive content words from a fact
# ---------------------------------------------------------------------------


# Words too common to count as a "distinctive citation" — keep tight,
# expand only when false positives surface in real use.
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "at",
    "for", "with", "from", "by", "as", "is", "are", "was", "were", "be",
    "been", "being", "this", "that", "these", "those", "it", "its", "if",
    "then", "than", "so", "not", "no", "yes", "do", "does", "did", "has",
    "have", "had", "can", "could", "will", "would", "should", "may",
    "might", "must", "i", "you", "he", "she", "they", "we", "them",
    "his", "her", "their", "our", "your", "my", "me", "us", "him",
})

# A "content word" is something the assistant would only repeat if it
# actually consulted the fact: a token that contains a digit or an
# uppercase letter (likely an identifier / version / ticket / proper
# noun), or any non-stopword 5+ char word.
_WORD_RE = re.compile(r"\b[A-Za-z0-9_./:-]+\b")


def _tokenize(text: str) -> List[str]:
    """Return whitespace-normalized tokens, preserving case + identifiers."""
    if not text:
        return []
    return _WORD_RE.findall(text)


def _is_distinctive(token: str) -> bool:
    """Return True if ``token`` is rare enough to count as evidence of citation.

    Rules:
      * Contains a digit OR uppercase letter (identifier/version/proper noun)
      * Or: non-stopword 5+ char lowercase word
      * Excludes pure single chars and stopwords.
    """
    if len(token) < 2:
        return False
    low = token.lower()
    if low in _STOPWORDS:
        return False
    # Identifiers: anything with digits, dots, slashes, colons, underscores
    if any(c.isdigit() for c in token) or any(c in token for c in "._/:-"):
        return True
    # Uppercase: PLAT-15800, JS, MCP, TaaS — but skip first-word-of-sentence
    # capitalization by requiring at least one INTERNAL uppercase OR a
    # mid-word uppercase (rough heuristic).
    if token[1:] != token[1:].lower():
        return True
    # Plain word — must be 5+ chars and not a stopword.
    return len(token) >= 5


def fingerprint_fact(content: str, min_words: int = 4, max_fp: int = 3) -> Tuple[str, ...]:
    """Compute up to ``max_fp`` distinctive n-gram fingerprints for a fact.

    A fingerprint is a normalized run of ``min_words`` consecutive
    distinctive tokens. Returns a tuple of lowercased fingerprint
    strings.

    Example::

        >>> fingerprint_fact("PLAT-15800: BWT counter includes CDN bytes "
        ...                  "that don't traverse the SD-WAN tunnel.")
        ('plat-15800 bwt counter includes', 'bwt counter includes cdn',
         'counter includes cdn bytes')

    The match check is substring-based: if ANY of these fingerprints
    appears in the assistant's response (lowercased), we count the fact
    as cited.
    """
    tokens = _tokenize(content or "")
    distinctive = [t for t in tokens if _is_distinctive(t)]
    if len(distinctive) < min_words:
        return ()

    fps: List[str] = []
    seen: Set[str] = set()
    # Slide a window of min_words across the distinctive-token stream.
    for i in range(len(distinctive) - min_words + 1):
        window = distinctive[i : i + min_words]
        fp = " ".join(window).lower()
        if fp in seen:
            continue
        seen.add(fp)
        fps.append(fp)
        if len(fps) >= max_fp:
            break
    return tuple(fps)


# ---------------------------------------------------------------------------
# record_recall — called from WarmStore.recall / recall_related
# ---------------------------------------------------------------------------


def record_recall(
    session_id: Optional[str],
    results: List[Dict[str, Any]],
) -> None:
    """Stash recall results in the session window.

    No-op when:
      * ``memory.auto_feedback`` is disabled
      * ``session_id`` is falsy (no current session bound)
      * ``results`` is empty / non-list
    """
    if not session_id or not results:
        return
    try:
        cfg = _get_config()
        if not cfg["enabled"]:
            return
        min_fp_words = cfg["min_fingerprint_words"]
        cap = cfg["max_facts_per_session"]

        new_entries: List[_RecallEntry] = []
        for row in results:
            try:
                fid = int(row.get("fact_id"))
            except (TypeError, ValueError):
                continue
            content = row.get("content") or ""
            fps = fingerprint_fact(content, min_words=min_fp_words)
            if not fps:
                # Fact has no distinctive content to fingerprint — can't
                # credit a citation reliably. Skip.
                continue
            new_entries.append(_RecallEntry(
                fact_id=fid, fingerprints=fps, turn_age=0,
            ))
        if not new_entries:
            return

        lock = _get_lock(session_id)
        with lock:
            window = _session_windows.get(session_id)
            if window is None:
                window = deque(maxlen=cap)
                _session_windows[session_id] = window
            # Note: ``_session_windows[session_id]`` was created with a
            # bounded maxlen; if we exceed it, oldest entries fall off.
            already = {e.fact_id for e in window}
            for entry in new_entries:
                if entry.fact_id in already:
                    # Refresh the existing entry's age to 0 (it's "live"
                    # again) rather than appending a dupe.
                    for i, e in enumerate(window):
                        if e.fact_id == entry.fact_id:
                            window[i] = _RecallEntry(
                                fact_id=e.fact_id,
                                fingerprints=entry.fingerprints,
                                turn_age=0,
                            )
                            break
                else:
                    window.append(entry)
                    already.add(entry.fact_id)
    except Exception as e:
        # Best-effort — never break recall.
        logger.debug("auto_feedback.record_recall failed: %s", e)


# ---------------------------------------------------------------------------
# on_turn_end — fingerprint match + upvote
# ---------------------------------------------------------------------------


def on_turn_end(session_id: Optional[str], assistant_text: Optional[str]) -> Dict[str, Any]:
    """Audit the recall window for ``session_id`` against ``assistant_text``.

    For every fact whose fingerprint appears (case-insensitive substring)
    in the assistant text, call ``WarmStore.record_feedback(fact_id,
    helpful=True)`` once per session.

    Returns a summary dict::

        {
          "session_id": str,
          "checked": int,        # facts in the live window
          "upvoted": int,        # how many fired record_feedback
          "fact_ids": List[int], # the upvoted fact_ids (for debug)
        }

    Always safe to call. Returns the zero-summary on any failure or
    when the feature is disabled.
    """
    summary: Dict[str, Any] = {
        "session_id": session_id or "",
        "checked": 0,
        "upvoted": 0,
        "fact_ids": [],
    }
    if not session_id or not assistant_text:
        return summary
    try:
        cfg = _get_config()
        if not cfg["enabled"]:
            return summary
        window_turns = cfg["recall_window_turns"]

        lock = _get_lock(session_id)
        with lock:
            window = _session_windows.get(session_id)
            if not window:
                return summary
            credited = _credited.setdefault(session_id, set())
            summary["checked"] = len(window)

            haystack = assistant_text.lower()
            to_credit: List[int] = []
            for entry in list(window):
                if entry.fact_id in credited:
                    continue
                if any(fp in haystack for fp in entry.fingerprints):
                    to_credit.append(entry.fact_id)

            # Fire feedback after we're done iterating the window (the
            # warm store call may take a few ms and we don't want to
            # hold the lock that long).
        if to_credit:
            try:
                from tools.memory_warm import get_warm_store

                warm = get_warm_store()
            except Exception:
                warm = None
            if warm is not None:
                with lock:
                    credited = _credited.setdefault(session_id, set())
                    for fid in to_credit:
                        if fid in credited:
                            continue
                        try:
                            warm.record_feedback(fid, helpful=True)
                            credited.add(fid)
                            summary["upvoted"] += 1
                            summary["fact_ids"].append(fid)
                        except Exception as e:
                            logger.debug(
                                "auto_feedback record_feedback(%s) failed: %s",
                                fid, e,
                            )

        # Age out the window after the audit. Entries older than
        # window_turns are dropped.
        with lock:
            window = _session_windows.get(session_id)
            if window:
                survivors: List[_RecallEntry] = []
                for entry in window:
                    entry.turn_age += 1
                    if entry.turn_age <= window_turns:
                        survivors.append(entry)
                # Rebuild with the same maxlen.
                _session_windows[session_id] = deque(
                    survivors, maxlen=cfg["max_facts_per_session"],
                )

    except Exception as e:
        logger.debug("auto_feedback.on_turn_end failed: %s", e)
    return summary


# ---------------------------------------------------------------------------
# flush_session — called on /reset, session boundary
# ---------------------------------------------------------------------------


def flush_session(session_id: Optional[str]) -> None:
    """Drop all per-session state for ``session_id``.

    Idempotent. Used on /reset and at session end so a long-running
    process doesn't grow unbounded.
    """
    if not session_id:
        return
    try:
        with _state_lock:
            _session_windows.pop(session_id, None)
            _credited.pop(session_id, None)
            _session_locks.pop(session_id, None)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Testing helpers — not part of the public API
# ---------------------------------------------------------------------------


def _reset_state_for_testing() -> None:
    """Drop all session state — for unit tests only."""
    with _state_lock:
        _session_windows.clear()
        _credited.clear()
        _session_locks.clear()


def _snapshot_window(session_id: str) -> List[Dict[str, Any]]:
    """Return a snapshot of the current recall window for ``session_id``.

    For tests + debugging. Read-only.
    """
    lock = _get_lock(session_id)
    with lock:
        window = _session_windows.get(session_id)
        if not window:
            return []
        return [
            {
                "fact_id": e.fact_id,
                "fingerprints": list(e.fingerprints),
                "turn_age": e.turn_age,
            }
            for e in window
        ]
