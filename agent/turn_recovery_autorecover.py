"""Bounded post-exhaustion auto-recovery ladder for a pre-delivery turn (#85426, #107307).

Runs from ``settle_unrecovered_error`` once ``max_retries`` is spent AND the fallback chain has
nothing left to move to (fallback stays first). While the provider is only *temporarily* away
(5xx, overloaded/529, connect/read timeouts) and no answer text has reached the user yet, the
turn parks with a visible countdown instead of ending in "API failed after N retries", then
re-enters the ordinary retry loop. Cycles: ``agent.auto_recovery_cycles`` (default 5) with a
jittered 15/30/60/60/60 s schedule; a provider ``Retry-After`` wins up to 120 s. Non-retryable
classes (auth, format, content policy, billing, entitlement) never enter, and an interrupt
cancels the wait cleanly. Overload-class errors use this same schedule — there is no separate
overload backoff path.

Identical-failure circuit breaker: the ladder replays the SAME request, so a provider crash-looping
its own model runner reproduces the same failure every cycle. ``note_recovery_failure`` fingerprints
each failed attempt ``(provider, base_url, model, error class, received_token)`` on the turn's
``TurnRetryState``; when the same fingerprint fails ``agent.auto_recovery_identical_failure_limit``
times in a row (default 2) within a 30-minute window, the ladder stops and ``crash_loop_result``
surfaces a terminal error instead of parking the turn through more silent waits. A progress-making
attempt (any token/chunk received) or a different error class never counts.
"""

from __future__ import annotations

import logging
import time
from contextlib import suppress
from typing import Any, Dict, Optional

from agent.error_classifier import FailoverReason

logger = logging.getLogger("agent.conversation_loop")

# Transient transport verdicts: the provider is expected back. Everything else (auth, format,
# content policy, billing, entitlement, overflow, model-not-found ...) is deterministic for this
# request and stays out.
_LADDER_REASONS = frozenset({FailoverReason.overloaded, FailoverReason.server_error, FailoverReason.timeout})

_LADDER_BASE_DELAY_S = 15.0
_LADDER_CAP_S = 60.0
# A provider that names its own cooldown knows better than the schedule, within reason.
_RETRY_AFTER_CAP_S = 120.0

# Identical-failure circuit breaker. The ladder replays the SAME request, so a provider that
# crash-loops its own model runner reproduces the same failure every cycle while the attempts only
# hang and burn wall time (a 2026-10-16 outage spent 3 h 18 m this way). After this many consecutive
# IDENTICAL failures — same provider/base_url/model/error class and no token received — inside this
# window, the ladder stops and reports the crash-loop rather than replaying the request again.
_IDENTICAL_FAILURE_WINDOW_S = 1800.0
_DEFAULT_IDENTICAL_FAILURE_LIMIT = 2

# How the user stops the wait on this surface; the ladder text must say so plainly.
_STOP_HINTS = {
    "cli": "press Esc to stop",
    "tui": "press Esc to stop",
    "desktop": "press Esc to stop",
    "api_server": "cancel the request to stop",
    "cron": "",
}
_DEFAULT_STOP_HINT = "send /stop to cancel"


def auto_recovery_cycles(agent: Any) -> int:
    """Configured ``agent.auto_recovery_cycles`` (0 disables the ladder)."""
    return max(int(getattr(agent, "_auto_recovery_cycles", 0) or 0), 0)


def auto_recovery_identical_failure_limit(agent: Any) -> int:
    """Configured ``agent.auto_recovery_identical_failure_limit`` (0 disables the breaker)."""
    default = _DEFAULT_IDENTICAL_FAILURE_LIMIT
    try:
        return max(int(getattr(agent, "_auto_recovery_identical_failure_limit", default)), 0)
    except (TypeError, ValueError):
        return default


def attempt_received_any_token(agent: Any) -> bool:
    """True when the failed attempt received ANY token/chunk: a first stream chunk landed, or visible
    text was emitted. Such an attempt is never part of an identical-failure streak."""
    if getattr(agent, "_last_api_first_chunk_at", None) is not None:
        return True
    streamed = getattr(agent, "_current_streamed_assistant_text", "") or ""
    return bool(streamed.strip())


def failure_fingerprint(agent: Any, classified: Any, *, received_token: bool) -> tuple:
    """``(provider, base_url, model, error class, received_token)`` for one failed attempt. The error
    class is the classifier's normalized ``reason`` value, so two failures only match when the
    provider AND the failure mode are identical."""
    reason = getattr(classified, "reason", None)
    reason_value = getattr(reason, "value", None) or (str(reason) if reason is not None else "unknown")
    return (
        str(getattr(agent, "provider", "") or ""),
        str(getattr(agent, "base_url", "") or ""),
        str(getattr(agent, "model", "") or ""),
        str(reason_value),
        bool(received_token),
    )


def note_recovery_failure(agent: Any, classified: Any, _retry: Any, *, now: Optional[float] = None) -> int:
    """Record one failed API attempt against the ladder's circuit breaker; return the number of
    consecutive IDENTICAL failures. Any progress (tokens/chunks received) breaks the streak, as does a
    different fingerprint; a gap older than ``_IDENTICAL_FAILURE_WINDOW_S`` since the last identical
    failure restarts it. Stored on ``TurnRetryState`` so it survives every ladder cycle."""
    if _retry is None:
        return 0
    _now = time.time() if now is None else float(now)
    received_token = attempt_received_any_token(agent)
    fingerprint = failure_fingerprint(agent, classified, received_token=received_token)
    if received_token:
        # A progress-making attempt is never an identical failure: reset outright.
        _retry.auto_recovery_failure_fingerprint = None
        _retry.auto_recovery_identical_failures = 0
        _retry.auto_recovery_identical_failure_at = _now
        return 0
    last_at = float(getattr(_retry, "auto_recovery_identical_failure_at", 0.0) or 0.0)
    same = (
        getattr(_retry, "auto_recovery_failure_fingerprint", None) == fingerprint
        and (_now - last_at) <= _IDENTICAL_FAILURE_WINDOW_S
    )
    if same:
        _retry.auto_recovery_identical_failures = int(
            getattr(_retry, "auto_recovery_identical_failures", 0) or 0
        ) + 1
    else:
        _retry.auto_recovery_failure_fingerprint = fingerprint
        _retry.auto_recovery_identical_failures = 1
    _retry.auto_recovery_identical_failure_at = _now
    return int(_retry.auto_recovery_identical_failures)


def identical_failure_limit_reached(agent: Any, _retry: Any) -> bool:
    """True when the ladder must stop: the same fingerprint has failed ``limit`` times in a row."""
    limit = auto_recovery_identical_failure_limit(agent)
    if limit <= 0 or _retry is None:
        return False
    return int(getattr(_retry, "auto_recovery_identical_failures", 0) or 0) >= limit


def crash_loop_message(count: int, last_error: str) -> str:
    """Terminal copy for a tripped breaker: names the crash-loop, the identical-failure count and the
    last error, and tells the user auto-recovery was stopped and they can retry manually."""
    return (
        "🛑 Auto-recovery stopped: the provider appears to be crash-looping.\n\n"
        f"The same request failed {count} times in a row with no response progress "
        f"(provider, endpoint and model unchanged). Retrying an identical request will not help.\n\n"
        f"Last error: {last_error}\n\n"
        "Wait for the provider to become healthy, then retry manually."
    )


def crash_loop_result(
    agent: Any, classified: Any, api_error: Any, *, messages: Any, conversation_history: Any,
    api_call_count: int, count: int,
) -> dict[str, Any]:
    """Terminal turn result for a tripped breaker. Mirrors the failed-turn shape the other terminal
    paths build (``final_response`` / ``messages`` / ``api_calls`` / ``failed`` / ``failure_reason``)
    and adds ``auto_recovery_stopped`` so callers can distinguish it from a plain exhaustion."""
    last_error = agent._summarize_api_error(api_error)
    message = crash_loop_message(count, last_error)
    with suppress(Exception):
        agent._flush_status_buffer()
    # Durable line on every surface, same channel the ladder's own countdown uses.
    agent._emit_diagnostic_status(
        f"🛑 Provider is crash-looping — the same request failed {count} times in a row; "
        "stopping automatic recovery."
    )
    logger.error(
        "%sAuto-recovery circuit breaker tripped: %d consecutive identical failures "
        "(provider=%s model=%s) — stopping the ladder. Last error: %s",
        agent.log_prefix, count, getattr(agent, "provider", ""), getattr(agent, "model", ""), last_error,
    )
    with suppress(Exception):
        agent._persist_session(messages, conversation_history)
    reason = getattr(getattr(classified, "reason", None), "value", None) or "unknown"
    return {
        "final_response": message,
        "messages": messages,
        "api_calls": api_call_count,
        "completed": False,
        "failed": True,
        "error": message,
        "failure_reason": str(reason),
        "failure_retryable": True,
        "auto_recovery_stopped": True,
    }


def ladder_wait_seconds(cycle: int, api_error: Any) -> float:
    """Wait before recovery ``cycle`` (1-based): jittered 15/30/60/60/60 s, or the provider's
    ``Retry-After`` when present (honoured past the 60 s cap, up to 120 s)."""
    from agent.retry_utils import jittered_backoff, provider_retry_after_seconds
    retry_after = provider_retry_after_seconds(api_error)
    if retry_after is not None:
        return min(retry_after, _RETRY_AFTER_CAP_S)
    return jittered_backoff(cycle, base_delay=_LADDER_BASE_DELAY_S, max_delay=_LADDER_CAP_S, jitter_ratio=0.2)


def ladder_eligible(agent: Any, classified: Any) -> bool:
    """True when the ladder may engage: cycles configured, a transient transport verdict, and no
    answer text streamed to the user yet (delivered text is never replayed by this path)."""
    if auto_recovery_cycles(agent) <= 0 or classified.reason not in _LADDER_REASONS:
        return False
    streamed = getattr(agent, "_current_streamed_assistant_text", "") or ""
    return not agent._has_content_after_think_block(streamed)


def ladder_notice(agent: Any, *, wait_s: float, cycle: int, total: int) -> str:
    hint = _STOP_HINTS.get(str(getattr(agent, "platform", "") or "").lower(), _DEFAULT_STOP_HINT)
    text = f"⏳ Provider temporarily unavailable — retrying automatically in {wait_s:.0f}s (cycle {cycle}/{total})"
    return f"{text}; {hint}" if hint else text


def auto_recover_after_exhaustion(
    agent: Any, api_error: Any, classified: Any, _retry: Any, *, messages: Any,
    conversation_history: Any, api_call_count: int,
) -> Optional[dict[str, Any]]:
    """Run one recovery cycle after retries + fallback exhausted. Returns ``{"action": "continue"}``
    when the wait completed (caller zeroes ``retry_count`` and re-enters the retry loop),
    ``{"action": "break"}`` when a steering correction arrived mid-wait, ``{"action": "return",
    "result": …}`` when the user interrupted, and ``None`` when the ladder does not apply or is spent."""
    from agent.turn_recovery import interruptible_backoff_sleep

    if not ladder_eligible(agent, classified):
        return None
    total = auto_recovery_cycles(agent)
    used = int(getattr(_retry, "auto_recovery_cycles_used", 0) or 0)
    if used >= total:
        agent._emit_diagnostic_status(
            f"⏳ Automatic recovery gave up after {total} cycles — the provider is still unavailable."
        )
        return None
    # Circuit breaker (checked after the gave-up branch so a spent ladder keeps its own message):
    # the identical request keeps failing with nothing gained — a crash-looping provider reproduces
    # the same failure every cycle, so stop instead of parking the turn through more silent waits.
    if identical_failure_limit_reached(agent, _retry):
        return {"action": "return", "result": crash_loop_result(
            agent, classified, api_error, messages=messages,
            conversation_history=conversation_history, api_call_count=api_call_count,
            count=int(getattr(_retry, "auto_recovery_identical_failures", 0) or 0),
        )}
    cycle = used + 1
    _retry.auto_recovery_cycles_used = cycle
    wait_s = ladder_wait_seconds(cycle, api_error)
    notice = ladder_notice(agent, wait_s=wait_s, cycle=cycle, total=total)
    # Durable line on every surface (CLI print, TUI status.update, gateway bubble, api_server SSE)
    # plus the live wait line (spinner / thinking.delta / activity heartbeat).
    agent._emit_diagnostic_status(notice)
    agent._emit_diagnostic_wait(notice)
    logger.warning(
        "%sProvider unavailable (%s) — auto-recovery cycle %d/%d, retrying in %.0fs %s",
        agent.log_prefix, classified.reason.value, cycle, total, wait_s, agent._client_log_context(),
    )
    interrupted = interruptible_backoff_sleep(
        agent, wait_s, _retry, messages=messages, conversation_history=conversation_history,
        api_call_count=api_call_count,
        abort_message="Interrupt detected during automatic recovery wait, aborting.",
        interrupt_text=f"Operation interrupted: waiting for the provider to recover (cycle {cycle}/{total}).",
        activity_label=f"auto-recovery wait ({cycle}/{total})",
    )
    if interrupted is not None:
        return {"action": "return", "result": interrupted}
    if _retry.restart_with_redirected_messages:
        return {"action": "break"}
    return {"action": "continue"}
