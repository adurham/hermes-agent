"""Per-session buffer for proposed memory entries.

Persisted to ``$HERMES_HOME/memory_extraction_buffer.json`` so that:
  - A crash mid-session doesn't lose proposals (they're confirmed at
    session end).
  - Multiple turns within the same session accumulate into one buffer.
  - The buffer file is small (~KB), human-readable, and safe to delete
    if it ever gets confused.

Buffer schema (top-level dict, keyed by session_id):

    {
      "<session_id>": {
        "session_id": "<session_id>",
        "started_at": "<ISO>",
        "updated_at": "<ISO>",
        "entries": [
          {
            "content": "...",
            "category": "...",
            "tags": "...",
            "rationale": "...",
            "source": "per_turn" | "pre_compress",
            "added_at": "<ISO>"
          }
        ]
      }
    }

A single file holds buffers for any sessions in flight. Old session
buffers (>7 days, or in a TERMINAL state) are pruned automatically.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


_BUFFER_FILENAME = "memory_extraction_buffer.json"
_PRUNE_AFTER_DAYS = 7

_lock = threading.Lock()


def _buffer_path() -> Path:
    """Resolve the buffer file path; lazy so HERMES_HOME profile changes work."""
    from hermes_constants import get_hermes_home
    return get_hermes_home() / _BUFFER_FILENAME


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _load() -> Dict[str, Any]:
    """Load the buffer file, returning an empty dict on any error."""
    path = _buffer_path()
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            return {}
        data = json.loads(text)
        if not isinstance(data, dict):
            return {}
        return data
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("memory extraction buffer unreadable, starting fresh: %s", e)
        return {}


def _save(data: Dict[str, Any]) -> None:
    """Atomically write the buffer file."""
    path = _buffer_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), suffix=".tmp", prefix=".mexbuf_",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _prune_stale(data: Dict[str, Any]) -> Dict[str, Any]:
    """Drop buffers older than _PRUNE_AFTER_DAYS that haven't been touched."""
    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=_PRUNE_AFTER_DAYS)
    pruned: Dict[str, Any] = {}
    for sid, sess in data.items():
        if not isinstance(sess, dict):
            continue
        try:
            updated = _dt.datetime.fromisoformat(sess.get("updated_at", ""))
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=_dt.timezone.utc)
            if updated < cutoff:
                continue
        except ValueError:
            continue
        pruned[sid] = sess
    return pruned


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def append(
    session_id: str,
    entries: List[Dict[str, Any]],
    *,
    source: str,
) -> int:
    """Append proposals to the session's buffer. Returns count appended."""
    if not session_id or not entries:
        return 0
    with _lock:
        data = _load()
        sess = data.get(session_id)
        now = _now_iso()
        if sess is None:
            sess = {
                "session_id": session_id,
                "started_at": now,
                "updated_at": now,
                "entries": [],
            }
            data[session_id] = sess
        existing_contents = {e.get("content") for e in sess["entries"]}
        appended = 0
        for entry in entries:
            content = entry.get("content", "")
            if not content or content in existing_contents:
                continue
            sess["entries"].append({
                "content": content,
                "category": entry.get("category", "general"),
                "tags": entry.get("tags", ""),
                "rationale": entry.get("rationale", ""),
                "source": source,
                "added_at": now,
            })
            existing_contents.add(content)
            appended += 1
        sess["updated_at"] = now
        # Opportunistic prune
        data = _prune_stale(data)
        _save(data)
        return appended


def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    """Return the buffer for one session, or None."""
    if not session_id:
        return None
    with _lock:
        data = _load()
        return data.get(session_id)


def get_session_entries(session_id: str) -> List[Dict[str, Any]]:
    """Return just the entries list for one session (or empty)."""
    sess = get_session(session_id)
    if sess is None:
        return []
    return list(sess.get("entries", []))


def clear_session(session_id: str) -> int:
    """Drop one session's buffer. Returns number of entries dropped."""
    if not session_id:
        return 0
    with _lock:
        data = _load()
        sess = data.pop(session_id, None)
        if sess is None:
            return 0
        _save(data)
        return len(sess.get("entries", []))


def replace_session_entries(
    session_id: str,
    entries: List[Dict[str, Any]],
) -> None:
    """Replace one session's buffer entries (used after session-end reconciliation)."""
    if not session_id:
        return
    with _lock:
        data = _load()
        sess = data.get(session_id)
        now = _now_iso()
        if sess is None:
            sess = {
                "session_id": session_id,
                "started_at": now,
                "updated_at": now,
                "entries": [],
            }
            data[session_id] = sess
        sess["entries"] = list(entries)
        sess["updated_at"] = now
        _save(data)


def all_sessions() -> List[str]:
    """List session ids that have a buffer (for debug / cleanup commands)."""
    with _lock:
        return sorted(_load().keys())
