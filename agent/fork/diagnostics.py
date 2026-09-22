"""Per-turn diagnostics + provider-specific error hints (fork-only).

Two small helpers that don't share state but are both fork-only
features that don't fit elsewhere:

* ``tools_signature``          — stable short hash of the current
  ``agent.tools[]`` for cache-flush diagnostics.

* ``decorate_xai_entitlement_error`` — append a neutral hint when
  xAI's OAuth surface returns the "entitlement denied" 403, pointing
  users at the most common cause (SuperGrok tier mismatch) without
  accusing the subscriber.
"""

from __future__ import annotations

import hashlib
import json


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


def init_state(agent) -> None:
    """Initialize fork instance state for per-turn diagnostics.

    Called once from ``agent.agent_init.init_agent``.  Sets:

    * ``agent._strip_cache_on_overload`` — opt-in flag for stripping cache
      breakpoints on retry when an overloaded_error fires.
    * ``agent._tools_hash_cache``        — memoized (id, hex) tuple of the
      most-recently-hashed tools[].  Cleared when tools change.
    """
    agent._strip_cache_on_overload = False
    agent._tools_hash_cache = None
