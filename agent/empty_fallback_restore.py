"""Bounded mid-run primary restore after an EMPTY-response fallback.

Fallback is normally turn-scoped: ``restore_primary_runtime`` runs only at turn start. A
delegated child IS one turn, so a child whose primary had a transient empty streak would
otherwise finish its whole run on the (weaker) fallback model. An empty streak is not a
provider-failure class (429/5xx/auth keep turn-start-only restore), so after an
empty-caused fallback the primary is re-tried at iteration boundaries:

* the boundary immediately after activation is skipped (the fallback serves at least one
  request); the next boundary restores the primary and the next real request is the probe
  (no separate health check);
* a probe answered with real output (a landed tool round) confirms the primary — stay;
* a probe that empties again re-falls-back through the ordinary ladder; the next restore
  then waits 2, 4, 8, ... (cap 16) boundaries; a restore that ``restore_primary_runtime``
  refuses (cooldown, entitlement, rebuild failure) counts as a failed cycle too;
* at most :data:`MAX_RESTORE_ATTEMPTS` restores per turn.

All state is per turn (``agent/turn_context._PER_TURN_RESET_STATE``); this module reuses
``restore_primary_runtime`` as-is and never mutates message history.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

EMPTY_RESPONSE_CAUSE = "empty_response"
MAX_RESTORE_ATTEMPTS = 4
FIRST_FAILED_SKIP = 2
MAX_SKIP = 16
# Boundaries skipped right after an activation so the fallback answers at least once.
_POST_ACTIVATION_SKIP = 1

# Per-turn agent attributes (defaults mirrored in turn_context._PER_TURN_RESET_STATE).
ATTR_LAST_CAUSE = "_last_fallback_cause"
ATTR_ATTEMPTS = "_empty_restore_attempts"
ATTR_SKIP_REMAINING = "_empty_restore_skip_remaining"
ATTR_NEXT_SKIP = "_empty_restore_next_skip"
ATTR_PROBE_ACTIVE = "_empty_restore_probe_active"

PER_TURN_DEFAULTS = (
    (ATTR_LAST_CAUSE, None),
    (ATTR_ATTEMPTS, 0),
    (ATTR_SKIP_REMAINING, 0),
    (ATTR_NEXT_SKIP, FIRST_FAILED_SKIP),
    (ATTR_PROBE_ACTIVE, False),
)


def _int(agent: Any, name: str, default: int) -> int:
    value = getattr(agent, name, default)
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _fail_cycle(agent: Any) -> None:
    """A restore cycle failed: wait the current back-off, then double it (capped)."""
    skip = max(FIRST_FAILED_SKIP, _int(agent, ATTR_NEXT_SKIP, FIRST_FAILED_SKIP))
    setattr(agent, ATTR_SKIP_REMAINING, skip)
    setattr(agent, ATTR_NEXT_SKIP, min(skip * 2, MAX_SKIP))
    setattr(agent, ATTR_PROBE_ACTIVE, False)


def note_empty_fallback_activated(agent: Any) -> None:
    """Called by the empty-response ladder right after a successful fallback activation."""
    # try_activate_fallback already stamped the cause from _fallback_pending_cause; the
    # ladder knows it authoritatively, so pin it (robust to a substituted activator).
    setattr(agent, ATTR_LAST_CAUSE, EMPTY_RESPONSE_CAUSE)
    if getattr(agent, ATTR_PROBE_ACTIVE, False) is True:
        # The restored primary emptied out again: failed cycle → back off before re-probing.
        _fail_cycle(agent)
        logger.info(
            "Restored primary emptied again — staying on fallback for %d iteration(s) "
            "before the next restore attempt", _int(agent, ATTR_SKIP_REMAINING, 0),
        )
        return
    setattr(agent, ATTR_SKIP_REMAINING, _POST_ACTIVATION_SKIP)


def note_primary_response_ok(agent: Any) -> None:
    """A real (non-empty) response arrived; a restored primary under probe is confirmed."""
    if getattr(agent, ATTR_PROBE_ACTIVE, False) is True and not getattr(agent, "_fallback_activated", False):
        setattr(agent, ATTR_PROBE_ACTIVE, False)
        logger.info("Restored primary answered — staying on %s (%s)",
                    getattr(agent, "model", "?"), getattr(agent, "provider", "?"))


def maybe_restore_primary_mid_run(agent: Any) -> bool:
    """At an iteration boundary (before the request is built): restore the primary when the
    active fallback was caused by an empty-response streak and the back-off allows. Returns
    True only when the primary runtime was actually restored (the caller must then re-sync
    its system prompt from ``agent._cached_system_prompt``)."""
    if getattr(agent, "_fallback_activated", False) is not True:
        return False
    if getattr(agent, ATTR_LAST_CAUSE, None) != EMPTY_RESPONSE_CAUSE:
        return False  # 429/5xx/auth/etc.: turn-start-only restore
    attempts = _int(agent, ATTR_ATTEMPTS, 0)
    if attempts >= MAX_RESTORE_ATTEMPTS:
        return False
    skip = _int(agent, ATTR_SKIP_REMAINING, 0)
    if skip > 0:
        setattr(agent, ATTR_SKIP_REMAINING, skip - 1)
        return False
    setattr(agent, ATTR_ATTEMPTS, attempts + 1)
    fallback_label = f"{getattr(agent, 'model', '?')} ({getattr(agent, 'provider', '?')})"
    try:
        restored = bool(agent._restore_primary_runtime())
    except Exception:  # noqa: BLE001 — a restore probe must never break the loop
        logger.warning("Mid-run primary restore raised", exc_info=True)
        restored = False
    if not restored:
        _fail_cycle(agent)
        logger.info(
            "Mid-run primary restore %d/%d declined — staying on fallback %s for %d iteration(s)",
            attempts + 1, MAX_RESTORE_ATTEMPTS, fallback_label, _int(agent, ATTR_SKIP_REMAINING, 0),
        )
        return False
    setattr(agent, ATTR_PROBE_ACTIVE, True)
    setattr(agent, ATTR_LAST_CAUSE, None)
    # New provider, new streak: the fallback's in-flight empty count must not shorten the
    # restored primary's budget.
    agent._empty_content_retries = 0
    logger.info(
        "Mid-run primary restore %d/%d after empty-response fallback: %s (%s) replaces %s",
        attempts + 1, MAX_RESTORE_ATTEMPTS, getattr(agent, "model", "?"),
        getattr(agent, "provider", "?"), fallback_label,
    )
    return True
