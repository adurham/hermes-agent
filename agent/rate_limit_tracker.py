"""Rate limit tracking for inference API responses.

Captures rate-limit headers from provider responses and provides formatted
display for the /usage slash command and the streaming heartbeat.

Two header schemas are supported:

1. ``x-ratelimit-*`` (Nous Portal / OpenRouter / OpenAI-compatible).  Reset
   values are seconds until the window resets:

       x-ratelimit-limit-requests          RPM cap
       x-ratelimit-limit-requests-1h       RPH cap
       x-ratelimit-limit-tokens            TPM cap
       x-ratelimit-limit-tokens-1h         TPH cap
       x-ratelimit-remaining-requests      requests left in minute window
       x-ratelimit-remaining-requests-1h   requests left in hour window
       x-ratelimit-remaining-tokens        tokens left in minute window
       x-ratelimit-remaining-tokens-1h     tokens left in hour window
       x-ratelimit-reset-requests          seconds until minute request window resets
       x-ratelimit-reset-requests-1h       seconds until hour request window resets
       x-ratelimit-reset-tokens            seconds until minute token window resets
       x-ratelimit-reset-tokens-1h         seconds until hour token window resets

2. ``anthropic-ratelimit-*`` (Anthropic native API).  Reset values are RFC
   3339 / ISO 8601 timestamps (e.g. ``2025-11-08T18:42:30Z``).  Anthropic
   advertises minute-window buckets for requests, input tokens, and output
   tokens; on priority-tier orgs there are additional ``priority-input-tokens``
   buckets (those are folded into ``input_tokens_min`` so the heartbeat shows
   the most restrictive bucket).  No hour-window buckets exist on Anthropic
   native; ``requests_hour`` / ``tokens_hour`` stay zero.

       anthropic-ratelimit-requests-limit / -remaining / -reset
       anthropic-ratelimit-tokens-limit / -remaining / -reset                 (legacy combined)
       anthropic-ratelimit-input-tokens-limit / -remaining / -reset
       anthropic-ratelimit-output-tokens-limit / -remaining / -reset
       anthropic-priority-input-tokens-limit / -remaining / -reset            (priority tier)
       anthropic-priority-output-tokens-limit / -remaining / -reset           (priority tier)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional


@dataclass
class RateLimitBucket:
    """One rate-limit window (e.g. requests per minute)."""

    limit: int = 0
    remaining: int = 0
    reset_seconds: float = 0.0
    captured_at: float = 0.0  # time.time() when this was captured

    @property
    def used(self) -> int:
        return max(0, self.limit - self.remaining)

    @property
    def usage_pct(self) -> float:
        if self.limit <= 0:
            return 0.0
        return (self.used / self.limit) * 100.0

    @property
    def remaining_seconds_now(self) -> float:
        """Estimated seconds remaining until reset, adjusted for elapsed time."""
        elapsed = time.time() - self.captured_at
        return max(0.0, self.reset_seconds - elapsed)


@dataclass
class RateLimitState:
    """Full rate-limit state parsed from response headers.

    ``requests_min`` / ``tokens_min`` / ``requests_hour`` / ``tokens_hour``
    are filled by both schemas.  ``input_tokens_min`` / ``output_tokens_min``
    are Anthropic-only (separate input vs output token buckets) and stay
    zero on the OpenAI-compatible path.

    ``schema`` records which header family produced this state — useful for
    UX hints ("Anthropic doesn't publish hourly windows") and for tests.
    """

    requests_min: RateLimitBucket = field(default_factory=RateLimitBucket)
    requests_hour: RateLimitBucket = field(default_factory=RateLimitBucket)
    tokens_min: RateLimitBucket = field(default_factory=RateLimitBucket)
    tokens_hour: RateLimitBucket = field(default_factory=RateLimitBucket)
    input_tokens_min: RateLimitBucket = field(default_factory=RateLimitBucket)
    output_tokens_min: RateLimitBucket = field(default_factory=RateLimitBucket)
    captured_at: float = 0.0  # when the headers were captured
    provider: str = ""
    schema: str = ""  # "x-ratelimit" or "anthropic-ratelimit"

    @property
    def has_data(self) -> bool:
        return self.captured_at > 0

    @property
    def age_seconds(self) -> float:
        if not self.has_data:
            return float("inf")
        return time.time() - self.captured_at

    @property
    def hottest_bucket(self) -> Optional[tuple[str, "RateLimitBucket"]]:
        """The bucket closest to its limit, by usage_pct.  None if no data.

        Used by the streaming heartbeat to surface a single most-relevant
        signal: "the input-token bucket is at 92%" tells you a stall might
        actually be near-throttle, while "all buckets <10%" tells you it's
        almost certainly upstream queueing instead.
        """
        candidates = [
            ("RPM", self.requests_min),
            ("RPH", self.requests_hour),
            ("TPM", self.tokens_min),
            ("TPH", self.tokens_hour),
            ("ITPM", self.input_tokens_min),
            ("OTPM", self.output_tokens_min),
        ]
        live = [(label, b) for label, b in candidates if b.limit > 0]
        if not live:
            return None
        return max(live, key=lambda lb: lb[1].usage_pct)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_iso8601_reset_to_seconds(value: Any, *, now: float) -> float:
    """Parse an ISO-8601 / RFC-3339 reset timestamp to seconds-until-reset.

    Anthropic emits resets as e.g. ``2025-11-08T18:42:30Z``.  Returns the
    delta from ``now`` to the parsed instant, floored at 0.0.  Returns 0.0
    for unparseable input — callers treat that as "no data" downstream.
    """
    if not value:
        return 0.0
    if not isinstance(value, str):
        return 0.0
    raw = value.strip()
    if not raw:
        return 0.0
    # Python's fromisoformat handles "+00:00" but not the trailing "Z".
    # Normalise both styles.  Cheaper than dragging dateutil in for this.
    if raw.endswith("Z") or raw.endswith("z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = dt.timestamp() - now
    return max(0.0, delta)


def _parse_x_ratelimit(
    lowered: Mapping[str, str],
    *,
    provider: str,
    now: float,
) -> RateLimitState:
    """Parse the ``x-ratelimit-*`` schema (Nous / OpenRouter / OpenAI-style)."""

    def _bucket(resource: str, suffix: str = "") -> RateLimitBucket:
        # e.g. resource="requests", suffix="" -> per-minute
        #      resource="tokens", suffix="-1h" -> per-hour
        tag = f"{resource}{suffix}"
        return RateLimitBucket(
            limit=_safe_int(lowered.get(f"x-ratelimit-limit-{tag}")),
            remaining=_safe_int(lowered.get(f"x-ratelimit-remaining-{tag}")),
            reset_seconds=_safe_float(lowered.get(f"x-ratelimit-reset-{tag}")),
            captured_at=now,
        )

    return RateLimitState(
        requests_min=_bucket("requests"),
        requests_hour=_bucket("requests", "-1h"),
        tokens_min=_bucket("tokens"),
        tokens_hour=_bucket("tokens", "-1h"),
        captured_at=now,
        provider=provider,
        schema="x-ratelimit",
    )


def _parse_anthropic_ratelimit(
    lowered: Mapping[str, str],
    *,
    provider: str,
    now: float,
) -> RateLimitState:
    """Parse the ``anthropic-ratelimit-*`` schema.

    Anthropic doesn't publish hourly windows on the native API, so
    ``requests_hour`` / ``tokens_hour`` stay zero.  Priority-tier orgs see
    additional ``anthropic-priority-*-tokens-*`` headers; when those are
    present and tighter than the base buckets, we fold them in so the
    heartbeat reflects the binding limit.
    """

    def _bucket(prefix: str, kind: str) -> RateLimitBucket:
        # e.g. prefix="anthropic-ratelimit-input-tokens", kind=""
        # -> looks up …-limit / …-remaining / …-reset
        return RateLimitBucket(
            limit=_safe_int(lowered.get(f"{prefix}-limit")),
            remaining=_safe_int(lowered.get(f"{prefix}-remaining")),
            reset_seconds=_parse_iso8601_reset_to_seconds(
                lowered.get(f"{prefix}-reset"), now=now,
            ),
            captured_at=now,
        )

    requests_min = _bucket("anthropic-ratelimit-requests", "")
    tokens_min = _bucket("anthropic-ratelimit-tokens", "")
    input_tokens_min = _bucket("anthropic-ratelimit-input-tokens", "")
    output_tokens_min = _bucket("anthropic-ratelimit-output-tokens", "")

    # Priority-tier overrides: pick the tighter of (base, priority).  A
    # priority bucket with a lower remaining count is the binding constraint
    # we want to surface.
    def _tighter(base: RateLimitBucket, priority_prefix: str) -> RateLimitBucket:
        if f"{priority_prefix}-limit" not in lowered:
            return base
        priority = _bucket(priority_prefix, "")
        if priority.limit <= 0:
            return base
        if base.limit <= 0:
            return priority
        # Pick whichever bucket has higher usage_pct (tighter).
        return priority if priority.usage_pct > base.usage_pct else base

    input_tokens_min = _tighter(input_tokens_min, "anthropic-priority-input-tokens")
    output_tokens_min = _tighter(output_tokens_min, "anthropic-priority-output-tokens")

    return RateLimitState(
        requests_min=requests_min,
        requests_hour=RateLimitBucket(),  # not advertised
        tokens_min=tokens_min,
        tokens_hour=RateLimitBucket(),  # not advertised
        input_tokens_min=input_tokens_min,
        output_tokens_min=output_tokens_min,
        captured_at=now,
        provider=provider or "anthropic",
        schema="anthropic-ratelimit",
    )


def parse_rate_limit_headers(
    headers: Mapping[str, str],
    provider: str = "",
) -> Optional[RateLimitState]:
    """Parse rate-limit headers into a RateLimitState.

    Auto-detects the schema from header prefixes:
      * ``x-ratelimit-*``         → Nous / OpenRouter / OpenAI-compatible
      * ``anthropic-ratelimit-*`` → Anthropic native API

    Returns None if no rate-limit headers are present.  When BOTH prefixes
    appear (Anthropic-via-OpenRouter relays both schemas in some configs),
    the Anthropic schema wins because it carries the tighter input/output
    token buckets that the OpenAI-style schema can't express.
    """
    # Normalize to lowercase so lookups work regardless of how the server
    # capitalises headers (HTTP header names are case-insensitive per RFC 7230).
    lowered = {k.lower(): v for k, v in headers.items()}

    has_anthropic = any(k.startswith("anthropic-ratelimit-") for k in lowered)
    has_x = any(k.startswith("x-ratelimit-") for k in lowered)

    if not (has_anthropic or has_x):
        return None

    now = time.time()
    if has_anthropic:
        return _parse_anthropic_ratelimit(lowered, provider=provider, now=now)
    return _parse_x_ratelimit(lowered, provider=provider, now=now)


# ── Formatting ──────────────────────────────────────────────────────────


def _fmt_count(n: int) -> str:
    """Human-friendly number: 7999856 -> '8.0M', 33599 -> '33.6K', 799 -> '799'."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1_000:.1f}K"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _fmt_seconds(seconds: float) -> str:
    """Seconds -> human-friendly duration: '58s', '2m 14s', '58m 57s', '1h 2m'."""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        m, sec = divmod(s, 60)
        return f"{m}m {sec}s" if sec else f"{m}m"
    h, remainder = divmod(s, 3600)
    m = remainder // 60
    return f"{h}h {m}m" if m else f"{h}h"


def _bar(pct: float, width: int = 20) -> str:
    """ASCII progress bar: [████████░░░░░░░░░░░░] 40%."""
    filled = int(pct / 100.0 * width)
    filled = max(0, min(width, filled))
    empty = width - filled
    return f"[{'█' * filled}{'░' * empty}]"


def _bucket_line(label: str, bucket: RateLimitBucket, label_width: int = 14) -> str:
    """Format one bucket as a single line."""
    if bucket.limit <= 0:
        return f"  {label:<{label_width}}  (no data)"

    pct = bucket.usage_pct
    used = _fmt_count(bucket.used)
    limit = _fmt_count(bucket.limit)
    remaining = _fmt_count(bucket.remaining)
    reset = _fmt_seconds(bucket.remaining_seconds_now)

    bar = _bar(pct)
    return f"  {label:<{label_width}} {bar} {pct:5.1f}%  {used}/{limit} used  ({remaining} left, resets in {reset})"


def format_rate_limit_display(state: RateLimitState) -> str:
    """Format rate limit state for terminal/chat display."""
    if not state.has_data:
        return "No rate limit data yet — make an API request first."

    age = state.age_seconds
    if age < 5:
        freshness = "just now"
    elif age < 60:
        freshness = f"{int(age)}s ago"
    else:
        freshness = f"{_fmt_seconds(age)} ago"

    provider_label = state.provider.title() if state.provider else "Provider"

    lines = [
        f"{provider_label} Rate Limits (captured {freshness}):",
        "",
        _bucket_line("Requests/min", state.requests_min),
    ]
    # Hour buckets only exist on the x-ratelimit schema; suppress on Anthropic
    # so the display doesn't show "(no data)" for buckets the API never
    # advertises.
    if state.requests_hour.limit > 0:
        lines.append(_bucket_line("Requests/hr", state.requests_hour))
    lines.append("")
    # Anthropic exposes input vs output separately; if those are populated,
    # prefer them over the legacy combined "tokens" bucket (which Anthropic
    # may or may not still emit).
    if state.input_tokens_min.limit > 0 or state.output_tokens_min.limit > 0:
        if state.input_tokens_min.limit > 0:
            lines.append(_bucket_line("Input tok/min", state.input_tokens_min))
        if state.output_tokens_min.limit > 0:
            lines.append(_bucket_line("Output tok/min", state.output_tokens_min))
    elif state.tokens_min.limit > 0:
        lines.append(_bucket_line("Tokens/min", state.tokens_min))
    if state.tokens_hour.limit > 0:
        lines.append(_bucket_line("Tokens/hr", state.tokens_hour))

    # Add warnings if any bucket is getting hot
    warnings = []
    for label, bucket in [
        ("requests/min", state.requests_min),
        ("requests/hr", state.requests_hour),
        ("tokens/min", state.tokens_min),
        ("tokens/hr", state.tokens_hour),
        ("input-tokens/min", state.input_tokens_min),
        ("output-tokens/min", state.output_tokens_min),
    ]:
        if bucket.limit > 0 and bucket.usage_pct >= 80:
            reset = _fmt_seconds(bucket.remaining_seconds_now)
            warnings.append(f"  ⚠ {label} at {bucket.usage_pct:.0f}% — resets in {reset}")

    if warnings:
        lines.append("")
        lines.extend(warnings)

    return "\n".join(lines)


def format_rate_limit_compact(state: RateLimitState) -> str:
    """One-line compact summary for status bars / gateway messages."""
    if not state.has_data:
        return "No rate limit data."

    rm = state.requests_min
    tm = state.tokens_min
    rh = state.requests_hour
    th = state.tokens_hour
    itm = state.input_tokens_min
    otm = state.output_tokens_min

    parts = []
    if rm.limit > 0:
        parts.append(f"RPM: {rm.remaining}/{rm.limit}")
    if rh.limit > 0:
        parts.append(f"RPH: {_fmt_count(rh.remaining)}/{_fmt_count(rh.limit)} (resets {_fmt_seconds(rh.remaining_seconds_now)})")
    # Prefer input/output split when present (Anthropic), else show legacy combined.
    if itm.limit > 0:
        parts.append(f"ITPM: {_fmt_count(itm.remaining)}/{_fmt_count(itm.limit)}")
    if otm.limit > 0:
        parts.append(f"OTPM: {_fmt_count(otm.remaining)}/{_fmt_count(otm.limit)}")
    if itm.limit == 0 and otm.limit == 0 and tm.limit > 0:
        parts.append(f"TPM: {_fmt_count(tm.remaining)}/{_fmt_count(tm.limit)}")
    if th.limit > 0:
        parts.append(f"TPH: {_fmt_count(th.remaining)}/{_fmt_count(th.limit)} (resets {_fmt_seconds(th.remaining_seconds_now)})")

    return " | ".join(parts)


def format_rate_limit_heartbeat(state: RateLimitState) -> str:
    """Tiny one-fragment summary for the streaming heartbeat ``[…]`` slot.

    The heartbeat already crowds the line with model name, elapsed time,
    phase, ping count, and prompt size.  We add a single bit that answers
    "is this stall plausibly throttle-related?":

      * If the hottest bucket is ≥80%: surface its label + percentage +
        reset window — this is the most-likely throttle cause.
      * If the hottest bucket is <80%: collapse to "limits OK (RPM 50/50,
        ITPM 198K/200K)" — proves headroom exists, so the stall is upstream.

    Returns "" when there's no data to surface (no headers seen yet, or the
    response had none).  Caller appends to ``_diag_bits`` only when non-empty.
    """
    if not state.has_data:
        return ""

    hottest = state.hottest_bucket
    if hottest is None:
        return ""
    label, bucket = hottest

    if bucket.usage_pct >= 80:
        reset = _fmt_seconds(bucket.remaining_seconds_now)
        return (
            f"⚠ {label} {bucket.usage_pct:.0f}% "
            f"({_fmt_count(bucket.remaining)}/{_fmt_count(bucket.limit)}, "
            f"resets in {reset})"
        )

    # Healthy: compress to "limits OK (hottest=ITPM 1%)".  The hottest label +
    # % is what tells the user "no, you aren't being rate-limited" without
    # them having to run /usage.
    return (
        f"limits OK ({label} "
        f"{_fmt_count(bucket.remaining)}/{_fmt_count(bucket.limit)})"
    )
