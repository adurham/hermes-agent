"""Per-turn diagnostics (fork-only).

Fork-only per-turn state (see ``init_state``).

The xAI entitlement hint (``decorate_xai_entitlement_error``) was
retired 2026-09-24: upstream's ``agent.api_error_summary`` now ships the
same feature and wins the AIAgent MRO.
"""

from __future__ import annotations


def init_state(agent) -> None:
    """Initialize fork instance state for per-turn diagnostics.

    Called once from ``agent.agent_init.init_agent``.  Sets:

    * ``agent._strip_cache_on_overload`` — opt-in flag for stripping cache
      breakpoints on retry when an overloaded_error fires (config key:
      ``agent.strip_cache_on_overload``; see cli.py defaults and
      cli-config.yaml.example).
    * ``agent._strip_cache_for_overload`` — one-shot request flag: armed by the
      overloaded-error handler (turn_recovery.route_classified_error) and
      consumed by the next request build (turn_api_request.build_api_request).
    """
    agent._strip_cache_on_overload = False
    agent._strip_cache_for_overload = False
