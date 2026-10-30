"""Drain extraction proposals stranded by a deferred (gateway shutdown) session end.

A gateway shutdown sets ``agent._defer_memory_extraction`` so the process is not
force-killed by the shutdown watchdog while session-end LLM passes run; the buffer
is kept "for the next session end of the same session_id". A session id only ends
once, so that next end never arrives, and ``buffer._PRUNE_AFTER_DAYS`` (7) then
deletes the proposals silently — the exact opposite of transparent memory
management.

This sweep is the missing second half: at gateway boot (and on a slow cadence) it
commits buffered proposals whose session is no longer live, through the same
``conflict.classify`` -> ``conflict.apply_verdict`` path the non-interactive
session-end commit uses. Bounded per call, best-effort, and a no-op unless
``auxiliary.memory_extraction.auto_commit_session_end`` is on (the same gate the
session-end path honours), so a review-oriented config keeps its proposals.

Runs inside the caller's profile scope: ``buffer``, ``state.db`` and the warm store
all resolve through ``HERMES_HOME`` at call time.
"""
from __future__ import annotations

import datetime as _dt
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

# A session still writing (a turn in flight, a review pass) must never be drained
# out from under itself. state.db's own evidence is authoritative; the updated_at
# guard covers a session whose row has not been stamped yet.
DEFAULT_MIN_IDLE_SECONDS = 600
DEFAULT_MAX_SESSIONS = 8

_COMMITTED_ACTIONS = frozenset({"stored", "refined", "deduplicated", "superseded"})


def _live_session_ids(home: Path, idle_seconds: int) -> set[str]:
    """Session ids that are open AND recently active — never sweep those."""
    db_path = home / "state.db"
    if not db_path.exists():
        return set()
    cutoff = time.time() - idle_seconds
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            rows = conn.execute(
                "SELECT id FROM sessions WHERE ended_at IS NULL "
                "AND coalesce(last_activity_at, started_at) > ?",
                (cutoff,),
            ).fetchall()
        finally:
            conn.close()
        return {str(row[0]) for row in rows}
    except sqlite3.Error as exc:
        # Without the live set, sweeping could yank a buffer out from under a live
        # session; fail closed and try again on the next cadence.
        logger.debug("memory orphan sweep: live-session probe failed: %s", exc)
        return None  # type: ignore[return-value]


def _updated_at(record: dict[str, Any]) -> _dt.datetime | None:
    try:
        parsed = _dt.datetime.fromisoformat(str(record.get("updated_at") or ""))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=_dt.UTC)


def sweep_orphaned_buffers(
    *,
    max_sessions: int = DEFAULT_MAX_SESSIONS,
    min_idle_seconds: int = DEFAULT_MIN_IDLE_SECONDS,
) -> dict[str, Any]:
    """Commit proposals from buffers whose session is gone; returns a summary dict."""
    from hermes_constants import get_hermes_home
    from tools.memory_extraction import buffer as _buffer
    from tools.memory_extraction import conflict as _conflict
    from tools.memory_extraction import extractor

    summary: dict[str, Any] = {
        "sessions": 0, "entries": 0, "committed": 0, "skipped": 0, "errors": 0,
    }
    try:
        if not extractor.is_enabled():
            summary["reason"] = "memory.auto_extract off"
            return summary
        if not extractor._get_extraction_config().get("auto_commit_session_end"):
            summary["reason"] = "auto_commit_session_end off"
            return summary
    except Exception as exc:
        summary["reason"] = f"config unavailable: {exc}"
        return summary

    home = Path(get_hermes_home())
    live = _live_session_ids(home, min_idle_seconds)
    if live is None:
        summary["reason"] = "live-session probe unavailable"
        return summary

    now = _dt.datetime.now(_dt.UTC)
    candidates: list[tuple[_dt.datetime, str, list[dict[str, Any]]]] = []
    for sid in _buffer.all_sessions():
        if sid in live:
            continue
        record = _buffer.get_session(sid) or {}
        entries = [e for e in (record.get("entries") or []) if (e.get("content") or "").strip()]
        if not entries:
            continue
        updated = _updated_at(record)
        if updated is not None and (now - updated).total_seconds() < min_idle_seconds:
            continue  # fresh: the owning turn may still be running
        candidates.append((updated or _dt.datetime.min.replace(tzinfo=_dt.UTC), sid, entries))

    candidates.sort(key=lambda item: item[0])
    for _, sid, entries in candidates[:max_sessions]:
        summary["sessions"] += 1
        for entry in entries:
            summary["entries"] += 1
            proposal = {
                "content": entry["content"].strip(),
                "category": entry.get("category") or "general",
                "tags": entry.get("tags") or "",
                "rationale": entry.get("rationale") or "",
            }
            try:
                verdict = _conflict.classify(proposal["content"])
                if getattr(verdict, "verdict", "") == "DUPLICATE":
                    summary["skipped"] += 1
                    continue
                outcome = _conflict.apply_verdict(verdict, proposal, auto_commit=False)
                if outcome.get("action") in _COMMITTED_ACTIONS:
                    summary["committed"] += 1
                else:
                    summary["skipped"] += 1
            except Exception as exc:
                summary["errors"] += 1
                logger.debug("memory orphan sweep: proposal failed: %s", exc)
        # Cleared only after the pass: a re-run must not duplicate what landed.
        _buffer.clear_session(sid)

    if summary["sessions"]:
        logger.info(
            "memory orphan sweep: drained %d buffer(s), %d committed, %d skipped, %d error(s)",
            summary["sessions"], summary["committed"], summary["skipped"], summary["errors"],
        )
    return summary
