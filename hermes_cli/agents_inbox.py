"""``hermes agents inbox`` implementation.

Parser lives in ``hermes_cli/subcommands/agents.py``; this module holds the
behaviour, mirroring the ``approvals`` subcommand's parser/implementation
split.

All state manipulation delegates to ``tools.cross_session_transport`` — this
module formats, it does not decide. In particular ``approve_held`` returns a
row to ``pending`` rather than delivering it, so the recipient's own drain
still applies its current inbound policy.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List

from tools.cross_session_transport import (
    STATUS_HELD,
    approve_held,
    deny_held,
    expire_held_messages,
    list_inbox,
)

# Body preview length in the human-readable listing. The full body is
# available via --json. A held message is untrusted content from another
# agent, so the listing shows enough to decide on and no more.
_PREVIEW_CHARS = 160


def _age(created_at: Any) -> str:
    try:
        seconds = max(0.0, time.time() - float(created_at))
    except (TypeError, ValueError):
        return "unknown"
    if seconds < 90:
        return f"{int(seconds)}s ago"
    if seconds < 5400:
        return f"{int(seconds // 60)}m ago"
    return f"{seconds / 3600:.1f}h ago"


def _preview(body: Any) -> str:
    text = " ".join(str(body or "").split())
    if len(text) <= _PREVIEW_CHARS:
        return text
    return text[:_PREVIEW_CHARS] + "…"


def _format_rows(rows: List[Dict[str, Any]], *, held_only: bool) -> str:
    if not rows:
        scope = "held for approval" if held_only else "in the inbox"
        return (
            f"No cross-session messages {scope}.\n"
            "Messages appear here when a session's inbound policy is `hold` "
            "(config.yaml: cross_session.inbound)."
        )
    lines = []
    for row in rows:
        header = (
            f"[{row.get('id')}] from {row.get('from_name') or row.get('from_session_id')} "
            f"→ {row.get('to_session_id')}  ({row.get('status')}, {_age(row.get('created_at'))}"
        )
        hop = row.get("hop_count") or 0
        if hop:
            header += f", hop {hop}"
        header += ")"
        lines.append(header)
        lines.append(f"    {_preview(row.get('body'))}")
    if held_only:
        lines.append("")
        lines.append(
            "Approve with `hermes agents inbox --approve <id>` (returns it to "
            "the recipient's pending queue), or deny with `--deny <id>`."
        )
    return "\n".join(lines)


def agents_command(args) -> int:
    """Handle ``hermes agents inbox``. Returns a process exit status."""
    sub = getattr(args, "agents_command", None)
    if sub not in (None, "inbox"):
        print(f"unknown agents subcommand: {sub}")
        return 2

    as_json = bool(getattr(args, "json", False))

    approve_id = getattr(args, "approve_id", None)
    deny_id = getattr(args, "deny_id", None)

    if approve_id is not None and deny_id is not None:
        print("--approve and --deny are mutually exclusive.")
        return 2

    if approve_id is not None:
        ok = approve_held(approve_id)
        if as_json:
            print(json.dumps({"action": "approve", "id": approve_id, "ok": ok}))
        elif ok:
            print(
                f"Message {approve_id} approved and returned to the pending "
                f"queue. The recipient session delivers it at its next drain "
                f"checkpoint; it is not delivered by this command."
            )
        else:
            print(
                f"Message {approve_id} was not approved — no message with that "
                f"id is currently held (it may have been denied, delivered, or "
                f"expired)."
            )
        return 0 if ok else 1

    if deny_id is not None:
        ok = deny_held(deny_id)
        if as_json:
            print(json.dumps({"action": "deny", "id": deny_id, "ok": ok}))
        elif ok:
            print(f"Message {deny_id} denied. It will never be delivered.")
        else:
            print(
                f"Message {deny_id} was not denied — no message with that id "
                f"is currently held."
            )
        return 0 if ok else 1

    # Default: listing. Sweep expiries first so the listing never offers an
    # already-stale hold for approval.
    expire_held_messages()

    show_all = bool(getattr(args, "all", False))
    rows = list_inbox(
        session_id=getattr(args, "session_id", None),
        status=None if show_all else STATUS_HELD,
    )
    if as_json:
        print(json.dumps(rows, indent=2, default=str))
    else:
        print(_format_rows(rows, held_only=not show_all))
    return 0
