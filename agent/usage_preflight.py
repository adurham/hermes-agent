"""Per-turn usage preflight: does the primary's plan have room for this turn *before* we ask?

Hermes' fallback chain is reactive — the turn discovers a spent allowance by being refused, which
costs one 402 per turn and a cold prompt-cache re-read on the fallback. A usage-window plan
publishes its own gauges, so the same answer is available before the request.

``fallback.preflight_usage`` (FORK, default off) asks the Portal at turn start and routes the turn
accordingly:

* on the primary, allowance spent -> bind the configured chain now (one notice, then quiet);
* on the chain, allowance still spent -> stay there, without a restore attempt and without the
  "primary restored" / "fallback" notice churn every turn;
* on the chain, allowance back -> let the normal ``restore_primary_runtime`` put the primary back.

It re-asks every turn, so the primary returns on its own as soon as the window resets.

The decision is the refusal path's own rule (``agent.plan_limits.blocking_gauge``):

* a free model is never blocked;
* a full all-scope gauge (day / week / month) blocks every paid model;
* a full top-tier gauge blocks only a model the catalog marks top tier.

An unmarked model is *not* preemptively switched on a top-tier gauge: a wrong switch spends
another provider's quota and takes the turn off the model the operator chose, while the reactive
402 path still catches that case after one request. The preflight only skips a request it can
prove is doomed. Nothing here raises or blocks a turn: an unavailable snapshot is "no opinion".
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# One lookup per turn is the contract; this only absorbs a second prologue in the same turn.
_LOOKUP_DEBOUNCE_SECONDS = 5.0


def usage_preflight_enabled() -> bool:
    """``fallback.preflight_usage`` — ask the plan before the request instead of after a refusal."""
    try:
        from hermes_cli.config import cfg_get, load_config_readonly
        return bool(cfg_get(load_config_readonly(), "fallback", "preflight_usage", default=False))
    except Exception:  # health: allow BLE001 -- a config read must never change a turn's route
        logger.debug("fallback.preflight_usage unreadable; keeping the request-first path", exc_info=True)
        return False


def _is_nous_route(provider: Any, base_url: Any) -> bool:
    if str(provider or "").strip().lower() == "nous":
        return True
    if not base_url:
        return False
    try:
        from utils import base_url_host_matches
        return base_url_host_matches(str(base_url), "nousresearch.com")
    except Exception:
        return False


def _account_usage_window(agent: Any) -> Any:
    """The Portal's ``usage_window`` block, freshly looked up (never from a stale cache).

    ``get_nous_portal_account_info`` keeps its own 60 s cache; ``force_fresh`` bypasses it so the
    answer is current for this turn. Returns None on any failure / non-window account.
    """
    from hermes_cli.nous_account import cached_nous_portal_account_info, get_nous_portal_account_info
    now = time.monotonic()
    last = float(getattr(agent, "_usage_preflight_at", 0.0) or 0.0)
    info = cached_nous_portal_account_info()
    if info is None or (now - last) >= _LOOKUP_DEBOUNCE_SECONDS:
        agent._usage_preflight_at = now
        info = get_nous_portal_account_info(force_fresh=True)
    return getattr(info, "usage_window", None)


def _catalog(base_url: str, *, model: str, need_marks: bool) -> dict:
    """This profile's Nous catalog rows. One fetch only when a tier mark is needed to decide."""
    from agent.plan_limits import cached_pricing
    pricing = cached_pricing(base_url)
    if need_marks and model and model not in (pricing or {}):
        try:
            from hermes_cli.models_pricing import get_pricing_for_provider
            pricing = get_pricing_for_provider("nous") or pricing
        except Exception:
            logger.debug("usage preflight: catalog fetch failed", exc_info=True)
    return pricing or {}


def blocked_gauge(agent: Any, *, provider: Any, model: Any, base_url: Any) -> Optional[Any]:
    """The full gauge that stops *model* on *provider* right now, or None.

    None means "go ahead": room in every allowance that applies, an allowance whose reset has
    already passed (a portal snapshot can lag the clock), or no usable snapshot.
    """
    if not _is_nous_route(provider, base_url):
        return None
    window = _account_usage_window(agent)
    if window is None:
        return None
    from agent.credits_tracker import _window_seed_fields
    fields = _window_seed_fields(window)
    if not fields:
        return None
    from agent.usage_gauges import SCOPE_TOP_TIER
    gauges = tuple(fields.get("gauges") or ())
    logger.debug("usage preflight: consulted for %s on %s (%d gauge(s): %s)", model, provider,
                 len(gauges), ",".join(f"{g.id}={g.used_bp/100:.1f}%" for g in gauges))
    full = [g for g in gauges if getattr(g, "full", False)]
    if not full:
        return None
    model = str(model or "")
    pricing = _catalog(str(base_url or ""), model=model,
                       need_marks=any(g.scope == SCOPE_TOP_TIER for g in full))
    from agent.plan_limits import blocking_gauge as _blocking_gauge, is_free, is_top_tier
    gauge = _blocking_gauge(model, gauges, free=is_free(model, pricing), top_tier=is_top_tier(model, pricing))
    logger.debug("usage preflight: %s on %s -> %s", model, provider,
                 f"blocked by {getattr(gauge, 'id', '?')}" if gauge is not None else "room")
    if gauge is None:
        return None
    resets_at_ms = getattr(gauge, "resets_at_ms", None)
    if isinstance(resets_at_ms, int) and resets_at_ms > 0 and resets_at_ms / 1000 <= time.time():
        # The allowance the portal still calls full has already rolled over; the first request
        # after a reset is how the server's own clock reaches us.
        return None
    return gauge


def usage_notice(agent: Any, gauge: Any) -> str:
    """The operator-facing line for a preflight switch, naming the allowance and its reset."""
    from agent.credits_tracker import format_window_reset
    from agent.usage_gauges import gauge_limit_name
    chain = tuple(getattr(agent, "_fallback_chain", ()) or ())
    first = chain[0] if chain else {}
    model = str(first.get("model") or "the fallback model")
    provider = str(first.get("provider") or "the fallback provider")
    return (f"⚠️ Nous {gauge_limit_name(gauge)} allowance is used up "
            f"(resets {format_window_reset(getattr(gauge, 'resets_at_ms', None))}); "
            f"continuing this turn on {model} via {provider}.")


def preflight_usage_route(agent: Any) -> bool:
    """Fix the turn's route from the plan before any request. True means "keep the fallback".

    Called at turn start *instead of* the unconditional primary restore: returning True suppresses
    that restore (the allowance is still spent, so restoring would only re-marshal the context to
    a backend that will refuse it). False means the normal restore should run.
    """
    try:
        if not usage_preflight_enabled():
            return False
        chain = tuple(getattr(agent, "_fallback_chain", ()) or ())
        primary = dict(getattr(agent, "_primary_runtime", None) or {})
        on_chain = bool(getattr(agent, "_fallback_activated", False)) and bool(primary)
        if on_chain:
            provider, model, base_url = primary.get("provider"), primary.get("model"), primary.get("base_url")
        else:
            provider, model, base_url = agent.provider, agent.model, agent.base_url
            if not chain:
                return False  # nothing to route to
        gauge = blocked_gauge(agent, provider=provider, model=model, base_url=base_url)
        if gauge is None:
            return False  # room on the primary: let the normal restore run
        if on_chain:
            logger.info("usage preflight: %s/%s still has no room (%s); staying on the fallback",
                        provider, model, getattr(gauge, "id", "?"))
            return True
        from agent.chat_completion_helpers import try_activate_fallback
        from agent.error_classifier import FailoverReason
        resets_at = getattr(gauge, "resets_at_ms", None)
        switched = bool(try_activate_fallback(
            agent, reason=FailoverReason.billing,
            reset_at=(resets_at / 1000) if isinstance(resets_at, int) and resets_at > 0 else None,
            notice_override=usage_notice(agent, gauge),
        ))
        if switched:
            logger.info("usage preflight: %s/%s has no room (%s full); turn moved to the fallback chain",
                        provider, model, getattr(gauge, "id", "?"))
        return switched
    except Exception:
        logger.debug("usage preflight failed; the turn keeps its primary route", exc_info=True)
        return False
