"""Primary rate-limit cooldown arming, the anti-flap primary-restore hold, and per-session
model rejection markers, shared by the fallback walk (chat_completion_helpers) and
restore_primary_runtime (agent_runtime_helpers)."""
import logging
import math
import time

from agent.error_classifier import FailoverReason

logger = logging.getLogger(__name__)

_RATE_LIMIT_FAILOVER_REASONS = frozenset({FailoverReason.rate_limit, FailoverReason.billing, FailoverReason.upstream_rate_limit})


def _provider_reset_delay(reset_at) -> float | None:
    """Seconds until the provider-declared reset, or None when missing/invalid/expired."""
    from agent.credential_pool import _parse_absolute_timestamp
    parsed = _parse_absolute_timestamp(reset_at)
    delay = parsed - time.time() if parsed is not None else None
    if delay is not None and math.isfinite(delay) and delay > 0:
        return delay
    return None


def switch_deferred_by_reset(agent, reason: "FailoverReason | None", reset_at) -> bool:
    """Opt-in ``fallback.min_switch_reset_seconds`` (default 0 = off, #117484): when the primary's
    rate limit reopens sooner than N seconds, switching model mid-task costs more than waiting, so
    the fallback walk is skipped and the retry loop's own backoff rides out the window. Only for
    rate-limit failovers leaving the primary with a valid future ``reset_at``."""
    if reason not in _RATE_LIMIT_FAILOVER_REASONS or getattr(agent, "_fallback_activated", False):
        return False
    try:
        from hermes_cli.config import load_config
        threshold = float((load_config() or {}).get("fallback", {}).get("min_switch_reset_seconds") or 0)
    except Exception:
        return False
    if threshold <= 0:
        return False
    delay = _provider_reset_delay(reset_at)
    if delay is None or delay >= threshold:
        return False
    logging.info("Rate limit resets in %.0f s (< fallback.min_switch_reset_seconds=%.0f): staying on the primary", delay, threshold)
    return True


def _arm_rate_limit_cooldown(
    agent, reason: "FailoverReason | None", reset_at=None,
) -> int | None:
    """Arm the primary cooldown until the provider reset, or use exponential backoff.

    ``reset_at`` is an absolute wall-clock timestamp while ``_rate_limited_until`` is monotonic;
    convert through a duration so wall-clock epoch values never enter the monotonic comparison.
    Missing, invalid, or expired provider resets retain the 60s → 2m → ... → 4h fallback.
    Only arm when leaving the primary: chain-switching from an active fallback means the primary
    was not the failing source. Return the armed cooldown in seconds, or None when not armed.
    """
    if reason not in _RATE_LIMIT_FAILOVER_REASONS:
        return None
    current_provider = (getattr(agent, "provider", "") or "").strip().lower()
    primary_provider = ((agent._primary_runtime or {}).get("provider") or "").strip().lower()
    if getattr(agent, "_fallback_activated", False) and not (primary_provider and current_provider == primary_provider):
        return None
    backoff_count = getattr(agent, "_rate_limit_backoff_count", 0)
    agent._rate_limit_backoff_count = backoff_count + 1
    provider_delay = _provider_reset_delay(reset_at)
    if provider_delay is not None:
        backoff_seconds = math.ceil(provider_delay)
        source = "provider reset"
    else:
        backoff_seconds = min(60 * (2 ** backoff_count), 14400)
        source = "exponential fallback"
    agent._rate_limited_until = time.monotonic() + backoff_seconds
    logging.info(
        "Rate-limit backoff level %d: cooldown %d s (%.1f min, backoff#%d, %s)",
        backoff_count, backoff_seconds, backoff_seconds / 60, backoff_count + 1, source,
    )
    return backoff_seconds


def _mark_entitlement_rejected_model(agent, api_error) -> bool:
    """Record a Codex ChatGPT-account 400 that rejects the current model for this account.

    Pool rotation runs first (recover_with_credential_pool benches (credential, model) and
    moves to the next entitled entry, #71970); this runs only once no pool entry is left for
    the model, so the (provider, model) pair is treated as dead for the session: the fallback
    walk skips it and restore_primary_runtime stops switching back — otherwise every turn
    re-fails on the primary, announces an unverified "Primary model restored", and oscillates
    forever (#106475).
    """
    if getattr(api_error, "status_code", None) != 400:
        return False
    from agent.error_classifier import CODEX_ACCOUNT_MODEL_ENTITLEMENT_MARKER
    haystack = str(getattr(api_error, "message", "") or api_error).lower()
    if CODEX_ACCOUNT_MODEL_ENTITLEMENT_MARKER not in haystack:
        return False
    provider = str(getattr(agent, "provider", "") or "").strip().lower()
    model = str(getattr(agent, "model", "") or "").strip()
    if not provider or not model:
        return False
    pool = getattr(agent, "_credential_pool", None)
    if pool is not None and pool.has_available(model=model):
        return False  # another pool entry is still eligible for this model; rotation owns it
    rejected = getattr(agent, "_entitlement_rejected_models", None)
    if rejected is None:
        rejected = agent._entitlement_rejected_models = set()
    if (provider, model) in rejected:
        return True
    rejected.add((provider, model))
    logger.warning(
        "Model entitlement rejection: this account is not entitled to %s via %s; "
        "treating it as unavailable for this session",
        model, provider,
    )
    agent._buffer_diagnostic_status(
        f"🚫 This account is not entitled to {model} via {provider}; it will be skipped "
        "until restart. Switch to an entitled model via /model or `hermes model`."
    )
    return True


def _is_entitlement_rejected(agent, provider: str, model: str) -> bool:
    """True when (provider, model) — as configured or normalized — was rejected as unentitled
    for this account (see _mark_entitlement_rejected_model)."""
    rejected = getattr(agent, "_entitlement_rejected_models", None) or ()
    if not rejected:
        return False
    if (provider, model) in rejected:
        return True
    from hermes_cli.model_normalize import normalize_model_for_provider
    return (provider, normalize_model_for_provider(model, provider)) in rejected


# ── Anti-flap hold: space out failed primary-restore probes ──────────────────────────────
# Fallback is turn-scoped: restore_primary_runtime swaps the primary back in at every turn
# start. When the primary is still broken, that restore is a failed PROBE — the turn
# re-marshals the full context to the primary (seconds for fast 5xx classes, minutes for
# slow-failing subscription-SDK routes), then falls back and re-uploads the whole context
# COLD to the fallback cache. Repeated across turns this is the observed fallback↔primary
# flap: every flip costs both providers' prompt caches plus a failed retry sequence, while
# re-probing an unchanged outage cannot succeed any sooner than a delayed probe.
#
# Trigger: a fallback activation while the primary is still _primary_unproven_since_restore
# (set by every successful restore, cleared by the first completed response ON the primary).
# A fresh primary failure (no recent restore) keeps the legacy fast re-probe. Hold: base 10
# min, doubling per consecutive flap (cap 2 h); the counter restarts only once the primary
# was actually seen working again (>= _PRIMARY_RESTORE_HOLD_RESET_COMPLETIONS completed
# primary responses, or _PRIMARY_RESTORE_HOLD_RESET_HEALTHY_S of a healthy primary run) —
# never on wall time alone. Rate-limit reasons keep their own longer provider-reset cooldown;
# a fallback-chain failover (the fallback itself died) lifts the hold so the primary probe is
# not stranded behind it. The hold is process-wide as well: a gateway hosts many sessions and
# a provider-wide outage should not be re-probed by every session simultaneously.
_PRIMARY_RESTORE_HOLD_BASE_S = 600.0
_PRIMARY_RESTORE_HOLD_MAX_S = 7200.0
_PRIMARY_RESTORE_HOLD_RESET_COMPLETIONS = 3
_PRIMARY_RESTORE_HOLD_RESET_HEALTHY_S = 1800.0
_PROCESS_RESTORE_HOLD_UNTIL: dict = {}


def _primary_hold_key(agent) -> tuple:
    rt = getattr(agent, "_primary_runtime", None) or {}
    return (str(rt.get("provider") or "").strip().lower(), str(rt.get("model") or "").strip())


def primary_restore_hold_remaining(agent) -> float:
    """Seconds until the next primary restore probe is allowed (0 = not held)."""
    now = time.monotonic()
    agent_until = float(getattr(agent, "_primary_restore_hold_until", 0.0) or 0.0)
    key = _primary_hold_key(agent)
    process_until = _PROCESS_RESTORE_HOLD_UNTIL.get(key, 0.0) if (key[0] or key[1]) else 0.0
    return max(0.0, max(agent_until, process_until) - now)


def _arm_primary_restore_hold(agent, reason, was_fallback_active: bool = False) -> float | None:
    """Arm the anti-flap hold after a fallback activation; returns held seconds or None.

    Only arms when the activation happened while the primary was still unproven since its
    last restore (i.e. a restore probe just failed) — a fresh failure on a previously
    healthy primary keeps the legacy per-turn re-probe. Skips rate-limit reasons (their own
    cooldown already gates the restore). A chain failover away from an already active
    fallback means the primary was not the failing source: lift any hold instead of arming.
    """
    current_provider = (getattr(agent, "provider", "") or "").strip().lower()
    primary_provider = ((getattr(agent, "_primary_runtime", None) or {}).get("provider") or "").strip().lower()
    if was_fallback_active and not (primary_provider and current_provider == primary_provider):
        if float(getattr(agent, "_primary_restore_hold_until", 0.0) or 0.0) > time.monotonic():
            logging.info(
                "Primary restore hold lifted: the fallback chain itself is failing over "
                "(the primary was not the failing source)")
            agent._primary_restore_hold_until = 0.0
        return None
    if reason in _RATE_LIMIT_FAILOVER_REASONS:
        return None
    if not getattr(agent, "_primary_unproven_since_restore", False):
        return None  # fresh primary failure: keep the legacy fast re-probe next turn
    now = time.monotonic()
    completions = int(getattr(agent, "_primary_completions_since_flap", 0) or 0)
    run_started = float(getattr(agent, "_primary_run_started_at", 0.0) or 0.0)
    healthy_run = bool(run_started) and (now - run_started) >= _PRIMARY_RESTORE_HOLD_RESET_HEALTHY_S
    if completions >= _PRIMARY_RESTORE_HOLD_RESET_COMPLETIONS or healthy_run:
        count = 0  # the primary demonstrably worked since the last flap — restart the ladder
    else:
        count = int(getattr(agent, "_primary_restore_hold_count", 0) or 0)
    count += 1
    hold = min(_PRIMARY_RESTORE_HOLD_BASE_S * (2 ** (count - 1)), _PRIMARY_RESTORE_HOLD_MAX_S)
    agent._primary_restore_hold_count = count
    agent._primary_restore_hold_until = max(
        float(getattr(agent, "_primary_restore_hold_until", 0.0) or 0.0), now + hold)
    agent._primary_completions_since_flap = 0
    agent._primary_run_started_at = 0.0
    key = _primary_hold_key(agent)
    if key[0] or key[1]:
        for expired in [k for k, until in _PROCESS_RESTORE_HOLD_UNTIL.items() if until <= now]:
            _PROCESS_RESTORE_HOLD_UNTIL.pop(expired, None)
        _PROCESS_RESTORE_HOLD_UNTIL[key] = max(_PROCESS_RESTORE_HOLD_UNTIL.get(key, 0.0), now + hold)
    logging.info(
        "Primary restore hold armed: flap #%d, next probe of %s/%s in %.0f s "
        "(post-restore fallback — the restore probe failed)",
        count, key[0] or "?", key[1] or "?", hold,
    )
    return hold


def note_primary_completion(agent) -> None:
    """A response completed ON the primary (no fallback active): the primary is proven since
    its last restore, and this feeds the hold-counter restart rule (see
    _arm_primary_restore_hold). Model comparison is normalization-aware; an unparseable pair
    counts as a completion (proving the primary is the safe direction)."""
    if getattr(agent, "_fallback_activated", False):
        return
    rt = getattr(agent, "_primary_runtime", None) or {}
    p_prov = str(rt.get("provider") or "").strip().lower()
    p_model = str(rt.get("model") or "").strip()
    if not p_prov and not p_model:
        return
    cur_prov = str(getattr(agent, "provider", "") or "").strip().lower()
    cur_model = str(getattr(agent, "model", "") or "").strip()
    if p_prov and cur_prov and cur_prov != p_prov:
        return
    if p_model and cur_model and cur_model != p_model:
        try:
            from hermes_cli.model_normalize import normalize_model_for_provider
            ref = cur_prov or p_prov
            if normalize_model_for_provider(cur_model, ref) != normalize_model_for_provider(p_model, ref):
                return
        except Exception:
            pass  # unparseable pair: treat as a completion rather than never proving the primary
    agent._primary_unproven_since_restore = False
    completions = int(getattr(agent, "_primary_completions_since_flap", 0) or 0) + 1
    agent._primary_completions_since_flap = completions
    if completions == 1:
        agent._primary_run_started_at = time.monotonic()


def clear_primary_restore_hold(agent) -> None:
    """Explicit user model switch: drop the anti-flap hold entirely (agent + process map)."""
    agent._primary_restore_hold_until = 0.0
    agent._primary_restore_hold_count = 0
    agent._primary_unproven_since_restore = False
    agent._primary_completions_since_flap = 0
    agent._primary_run_started_at = 0.0
    key = _primary_hold_key(agent)
    if key[0] or key[1]:
        _PROCESS_RESTORE_HOLD_UNTIL.pop(key, None)
