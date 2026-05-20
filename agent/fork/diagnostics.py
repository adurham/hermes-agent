"""Per-turn diagnostics + provider-specific error hints (fork-only).

Three small helpers that don't share state but are all forked-only
features that don't fit elsewhere:

* ``record_usage_history``     — append per-API-call usage records to
  ``agent._usage_history`` so post-mortems can spot a cache flush
  (cache_read drops to ~0 while msg_count keeps climbing) without
  needing the bloaty ``HERMES_DUMP_REQUESTS`` capture.

* ``tools_signature``          — stable short hash of the current
  ``agent.tools[]`` for cache-flush diagnostics in the usage history.

* ``decorate_xai_entitlement_error`` — append a neutral hint when
  xAI's OAuth surface returns the "entitlement denied" 403, pointing
  users at the most common cause (SuperGrok tier mismatch) without
  accusing the subscriber.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional, Tuple


def record_usage_history(agent, canonical_usage) -> None:
    """Append one per-turn usage record to ``agent._usage_history``.

    Record shape: ``{ts, input, cache_read, cache_write, output,
    msg_count, tools_count, tools_hash}``. ~80 bytes serialized — a
    2000-turn session adds ~160KB to the session log file. Persisted
    as part of the session JSON so post-mortems can spot a cache
    flush without HERMES_DUMP_REQUESTS bodies on disk.
    """
    try:
        record = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "input": int(getattr(canonical_usage, "input_tokens", 0) or 0),
            "cache_read": int(getattr(canonical_usage, "cache_read_tokens", 0) or 0),
            "cache_write": int(getattr(canonical_usage, "cache_write_tokens", 0) or 0),
            "output": int(getattr(canonical_usage, "output_tokens", 0) or 0),
            "msg_count": len(agent._session_messages or []),
            "tools_count": len(agent.tools or []),
            "tools_hash": agent._tools_signature(),
        }
        agent._usage_history.append(record)
        if len(agent._usage_history) > agent._usage_history_cap:
            # Keep tail — the recent window is what's useful for
            # diagnosing the current session's behavior.
            drop = len(agent._usage_history) - agent._usage_history_cap
            del agent._usage_history[:drop]
    except Exception as e:
        if getattr(agent, "verbose_logging", False):
            logging.debug("Failed to record usage history: %s", e)

# Tool names that count as a "risky operation" for the skill-recall
# reminder. Tick the counter when one of these runs; when it hits the
# configured interval, the NEXT tool result gets a one-line nudge
# asking the agent to re-check skill_pitfalls for the loaded skills.


def tools_signature(agent) -> str:
    """Stable short hash of the current tools[] for cache-flush diagnostics.

    Cached behind ``id(agent.tools), len(agent.tools)`` so the hash is only
    recomputed when tools[] is replaced or grows — adding a new tool
    appends, so the length changes; ToolSearch reloading the same set
    keeps the same hash.
    """
    tools = agent.tools or []
    key = (id(tools), len(tools))
    if agent._tools_hash_cache and agent._tools_hash_cache[0] == key:
        return agent._tools_hash_cache[1]
    try:
        blob = json.dumps(tools, sort_keys=True, default=str).encode("utf-8")
    except Exception:
        blob = repr(tools).encode("utf-8", errors="replace")
    digest = hashlib.sha256(blob).hexdigest()[:8]
    agent._tools_hash_cache = (key, digest)
    return digest


def decorate_xai_entitlement_error(detail: str) -> str:
    """Append a neutral hint when xAI's OAuth surface returns the
    permission-denied 403.

    xAI's ``/v1/responses`` endpoint replies to several distinct failure
    modes with the SAME body::

        {"code": "The caller does not have permission to execute the
         specified operation", "error": "You have either run out of
         available resources or do not have an active Grok subscription.
         Manage subscriptions at https://grok.com/?_s=usage or subscribe
         at https://grok.com/supergrok"}

    That body covers several real causes we cannot distinguish without
    more info from xAI.  The most common (and least obvious) one is
    that **X Premium+ does NOT include API access** — only standalone
    SuperGrok subscribers can use Hermes against xai-oauth.  Lots of
    users see Grok in their X app, assume it works here too, and hit
    this 403 with no idea why.  Lead the hint with that.

    Other possible causes:
      * No Grok subscription at all
      * SuperGrok tier doesn't include the requested model (e.g.
        grok-4.3 may need a higher tier)
      * Monthly quota exhausted (the ``?_s=usage`` URL hints at this)

    Surface the raw xAI text verbatim and point at
    https://grok.com/?_s=usage where the user can see WHICH applies.

    Matched once per detail string — won't double-decorate if the
    upstream already concatenated the same text.
    """
    if not detail:
        return detail
    lower = detail.lower()
    is_entitlement = (
        "do not have an active grok subscription" in lower
        or ("out of available resources" in lower and "grok" in lower)
        or ("does not have permission" in lower and "grok" in lower)
    )
    if not is_entitlement:
        return detail
    hint = (
        " — xAI rejected this OAuth account. NOTE: X Premium+ does NOT "
        "include xAI API access — only standalone SuperGrok subscribers "
        "can use this provider. Other possible causes: no Grok "
        "subscription, your tier doesn't include this model, or your "
        "quota is exhausted. Check https://grok.com/?_s=usage to see "
        "which, or run `/model` to switch providers."
    )
    # Idempotency: detect prior decoration by a substring unique to the
    # hint (not present in xAI's own body text).
    if "X Premium+ does NOT include" in detail:
        return detail
    return f"{detail}{hint}"
