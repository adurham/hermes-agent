"""Display visibility of the rewritten copies an in-place compaction inserts.

In-place compaction archives the summarized rows (``active=0, compacted=1``). They stay
display-visible on purpose, so the transcript keeps the full history. The compressor also carries
some messages forward in a rewritten form: a tool result pruned or demoted to a one-line stub keeps
its ``message_uid`` and timestamp but not its body. That copy's display key differs from the archived
original's, so both painted and the user saw the same tool result twice (once full, once as the
stub). The stub exists for the model's context only, so it is written ``model_only`` when its full
original stays visible. The archived body is the one the transcript shows.
"""

from __future__ import annotations

from typing import Any

from agent.context_compressor import MODEL_ONLY_DISPLAY_METADATA_KEY
from hermes_state_common import _sql_json_extract

_ORIGINAL_VISIBLE_SQL = f"COALESCE({_sql_json_extract('o.display_metadata', '$.' + MODEL_ONLY_DISPLAY_METADATA_KEY)}, 0) = 0"


def _next_message_id(conn: Any) -> int:
    """The id the next inserted row receives (rows are never deleted below MAX(id) inside the commit)."""
    return int(conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM messages").fetchone()[0])


def hide_rewritten_tool_copies(db: Any, conn: Any, session_id: str, first_new_id: int) -> None:
    """Mark the tool rows inserted at or after *first_new_id* model-only when an archived row with the
    same ``message_uid`` but a different body is still display-visible. A copy whose original was
    rewound (``compacted=0``) stays visible, because it is then the only copy the transcript has."""
    rows = conn.execute(
        "SELECT c.id, c.display_metadata FROM messages c WHERE c.session_id = ? AND c.id >= ? "
        "AND c.active = 1 AND c.role = 'tool' AND c.message_uid IS NOT NULL AND EXISTS ("
        "SELECT 1 FROM messages o WHERE o.session_id = c.session_id AND o.message_uid = c.message_uid "
        f"AND o.id < c.id AND o.active = 0 AND o.compacted = 1 AND o.content IS NOT c.content AND {_ORIGINAL_VISIBLE_SQL})",
        (session_id, int(first_new_id))).fetchall()
    updates = []
    for row in rows:
        meta = db._decode_display_metadata(row["display_metadata"]) or {}
        if not meta.get(MODEL_ONLY_DISPLAY_METADATA_KEY):
            updates.append((db._encode_display_metadata({**meta, MODEL_ONLY_DISPLAY_METADATA_KEY: True}), row["id"]))
    if updates:
        conn.executemany("UPDATE messages SET display_metadata = ? WHERE id = ?", updates)
