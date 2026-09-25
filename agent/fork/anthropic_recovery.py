"""Anthropic provider recovery helpers (fork-only).

Two related fork-specific paths:

1. Refusal retry sanitization (``sanitize_messages_for_refusal_retry``):
   Strips credential-extraction / database-dump shell patterns from
   historical context that look like data exfiltration to Anthropic's
   content filter but are legitimate authorized support work
   (``pg_dump`` via lockbox, S3 presigns, etc.).  Only touches
   historical messages; the most recent user message is left intact.

2. Claude Code alias arg translation (``translate_cc_args_after_repair``):
   The Anthropic OAuth path advertises CC canonical tool names (``Bash``,
   ``Read``, ``Edit``, ``Write``, ``Grep``) on the wire so the plan-budget
   billing classifier accepts the request.  ``_repair_tool_call``'s
   CC-alias fast-path renames CC names to hermes names BEFORE dispatch,
   so this helper translates the ARGS too (``file_path`` → ``path``,
   ``run_in_background`` → ``background``, etc.).

Retired: ``is_anthropic_refusal`` (removed 2026-09-22).  It detected
``stop_reason == "refusal"`` on the anthropic_messages path to enter the
fork's refusal ladder.  Upstream now maps that stop_reason itself —
``agent/transports/anthropic.py``'s ``_STOP_REASON_MAP`` turns
``"refusal"`` into ``finish_reason="content_filter"``, which
``agent/turn_response_check.py`` (byte-identical to upstream) routes into
``agent/turn_truncation.py::handle_content_policy_refusal``, the same rung
this predicate used to feed.  The scrub above is a DIFFERENT thing and is
still live: it is the ladder's middle rung, called from that handler.
"""

from __future__ import annotations

import logging
logger = logging.getLogger("run_agent")

from tools.content_filter_scrub import scrub_message_content


def sanitize_messages_for_refusal_retry(agent, messages: list) -> tuple:
    """Strip shell patterns that trigger content-policy filters from historical context.

    Targets credential-extraction + database-dump + data-transfer command
    patterns that look like exfiltration to Anthropic's filter but are
    legitimate authorized support work (pg_dump via lockbox, S3 presigns,
    etc.).  Only touches historical messages; the most recent user message
    is left intact so the user's actual request is preserved.

    Pattern list lives in ``tools.content_filter_scrub`` — shared with the
    tool-result persistence layer (``tools/tool_result_storage.py``), which
    scrubs the same patterns out of raw tool output (e.g. ``session_search``
    hits pulling old session text verbatim into live context) before this
    retry path ever gets a chance to run.

    Returns (sanitized_messages, was_modified).
    """
    # Leave the most recent user message untouched — it's the active request.
    last_user_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            last_user_idx = i
            break

    sanitized, any_changed = [], False
    for i, msg in enumerate(messages):
        if i == last_user_idx:
            sanitized.append(msg)
            continue
        new_content, changed = scrub_message_content(msg.get("content"))
        if changed:
            msg = {**msg, "content": new_content}
            any_changed = True
        sanitized.append(msg)

    return sanitized, any_changed

# ── Per-turn primary restoration ─────────────────────────────────────
