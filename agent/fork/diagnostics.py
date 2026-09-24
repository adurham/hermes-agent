"""Per-turn diagnostics (fork-only).

Fork-only helpers that don't fit elsewhere:

* ``tools_signature``          — stable short hash of the current
  ``agent.tools[]`` for cache-flush diagnostics.

The xAI entitlement hint (``decorate_xai_entitlement_error``) was
retired 2026-09-24: upstream's ``agent.api_error_summary`` now ships the
same feature and wins the AIAgent MRO.
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
