"""Rate-limit observability (fork-only).

Tracks rate-limit headers per API call and emits one-shot observability
events when buckets enter/leave the 90% hot zone (with 80% hysteresis
to prevent paired warn/clear oscillations on 89↔91 noise).

Hot tier state lives on the :class:`AIAgent` instance:
  - ``agent._rate_limit_state``        — last-seen header snapshot
  - ``agent._rate_limit_first_logged`` — one-shot INFO gate
  - ``agent._rate_limit_hot_buckets``  — set of currently-hot bucket names
"""

from __future__ import annotations

import logging

logger = logging.getLogger("run_agent")


def capture_rate_limits_from_headers(agent, headers: Any) -> None:
    """Parse rate-limit headers (any Mapping-like) and cache the state.

    Split out from ``_capture_rate_limits`` so error-handling paths
    that already extracted ``response.headers`` for other purposes
    (Retry-After parsing, Nous rate-limit verification) can hand the
    same Mapping directly without re-walking the response object.

    Emits two one-shot observability events:

      * INFO on first successful capture per session — proves the
        tracker is wired and shows what limits the provider published.
      * WARN when a bucket crosses 80% utilisation; INFO when it drops
        back below 80%.  Hysteresis (warn at ≥80%, clear at <80%) is
        handled in ``_log_rate_limit_transitions`` so a bucket
        oscillating around the warn threshold doesn't paint the log
        with paired warn/clear pairs.
    """
    if not headers:
        return
    try:
        from agent.rate_limit_tracker import parse_rate_limit_headers
        state = parse_rate_limit_headers(headers, provider=agent.provider)
        if state is None:
            return
        agent._rate_limit_state = state
        agent._log_rate_limit_first_capture(state)
        agent._log_rate_limit_transitions(state)
    except Exception:
        pass  # Never let header parsing break the agent loop


def log_rate_limit_first_capture(agent, state: "RateLimitState") -> None:
    """Emit a one-shot INFO when the tracker first sees rate-limit data.

    Useful for confirming the tracker is wired against the active
    provider, and for diff-ing the published caps against what the
    agent expected.  Subsequent captures are silent — ``/usage`` is
    the live view; transitions are warned separately.
    """
    if agent._rate_limit_first_logged:
        return
    try:
        from agent.rate_limit_tracker import format_rate_limit_compact
        logger.info(
            "rate-limit tracker captured initial state (%s schema, "
            "provider=%s): %s",
            state.schema or "unknown",
            state.provider or "unknown",
            format_rate_limit_compact(state),
        )
    except Exception:
        pass
    agent._rate_limit_first_logged = True


def log_rate_limit_transitions(agent, state: "RateLimitState") -> None:
    """Warn when buckets cross the 80% line; info when they drop back.

    Tracks per-bucket "currently hot" state in
    ``agent._rate_limit_hot_buckets``; transitions are reported once
    per change.  Hysteresis uses the same 80% threshold as the
    warning string in ``format_rate_limit_display`` so reporting
    stays consistent with the user-facing display.
    """
    try:
        from agent.rate_limit_tracker import _fmt_count, _fmt_seconds  # noqa
    except Exception:
        return
    from agent.rate_limit_tracker import _fmt_count, _fmt_seconds
    candidates = [
        ("RPM", state.requests_min),
        ("RPH", state.requests_hour),
        ("TPM", state.tokens_min),
        ("TPH", state.tokens_hour),
        ("ITPM", state.input_tokens_min),
        ("OTPM", state.output_tokens_min),
    ]
    currently_hot: set[str] = set()
    for label, bucket in candidates:
        if bucket.limit <= 0:
            continue
        if bucket.usage_pct >= 80.0:
            currently_hot.add(label)
            if label not in agent._rate_limit_hot_buckets:
                try:
                    logger.warning(
                        "rate-limit bucket %s crossed 80%% utilisation: "
                        "%.0f%% (%s/%s remaining, resets in %s)",
                        label,
                        bucket.usage_pct,
                        _fmt_count(bucket.remaining),
                        _fmt_count(bucket.limit),
                        _fmt_seconds(bucket.remaining_seconds_now),
                    )
                except Exception:
                    pass
    # Buckets that left the hot set: log a clear note so the timeline
    # shows pressure released, not just absence of alarms.
    cleared = agent._rate_limit_hot_buckets - currently_hot
    for label in cleared:
        try:
            logger.info(
                "rate-limit bucket %s recovered (back below 80%%)",
                label,
            )
        except Exception:
            pass
    agent._rate_limit_hot_buckets = currently_hot


def init_state(agent) -> None:
    """Initialize fork instance state for rate-limit observability.

    Called once from ``agent.agent_init.init_agent``.  Sets:

    * ``agent._rate_limit_first_logged`` — gates the one-shot INFO message
      emitted on the first rate-limit header capture per session.
    * ``agent._rate_limit_hot_buckets``  — set of bucket names currently
      above the 90% threshold.  Used for the 80% hysteresis on transition
      warnings.

    Note: ``agent._rate_limit_state`` (the last-seen header snapshot) is
    initialized to ``None`` by ``init_agent`` directly because the
    type-annotated form ``Optional["RateLimitState"]`` lives at the
    module's class-attribute scope and needs to import the
    forward-referenced type.
    """
    agent._rate_limit_first_logged = False
    agent._rate_limit_hot_buckets = set()
