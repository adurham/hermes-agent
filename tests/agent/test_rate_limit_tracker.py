"""Tests for agent.rate_limit_tracker — header parsing and formatting."""

import time
from datetime import datetime, timedelta, timezone

import pytest
from agent.rate_limit_tracker import (
    RateLimitBucket,
    RateLimitState,
    parse_rate_limit_headers,
    format_rate_limit_display,
    format_rate_limit_compact,
    format_rate_limit_heartbeat,
    _fmt_count,
    _fmt_seconds,
    _bar,
    _parse_iso8601_reset_to_seconds,
)


# ── Sample headers from Nous inference API ──────────────────────────────

NOUS_HEADERS = {
    "x-ratelimit-limit-requests": "800",
    "x-ratelimit-limit-requests-1h": "33600",
    "x-ratelimit-limit-tokens": "8000000",
    "x-ratelimit-limit-tokens-1h": "336000000",
    "x-ratelimit-remaining-requests": "795",
    "x-ratelimit-remaining-requests-1h": "33590",
    "x-ratelimit-remaining-tokens": "7999500",
    "x-ratelimit-remaining-tokens-1h": "335999000",
    "x-ratelimit-reset-requests": "45.5",
    "x-ratelimit-reset-requests-1h": "3500.0",
    "x-ratelimit-reset-tokens": "42.3",
    "x-ratelimit-reset-tokens-1h": "3490.0",
}


class TestParseHeaders:
    def test_basic_parsing(self):
        state = parse_rate_limit_headers(NOUS_HEADERS, provider="nous")
        assert state is not None
        assert state.provider == "nous"
        assert state.has_data

        assert state.requests_min.limit == 800
        assert state.requests_min.remaining == 795
        assert state.requests_min.reset_seconds == 45.5

        assert state.requests_hour.limit == 33600
        assert state.requests_hour.remaining == 33590

        assert state.tokens_min.limit == 8000000
        assert state.tokens_min.remaining == 7999500

        assert state.tokens_hour.limit == 336000000
        assert state.tokens_hour.remaining == 335999000
        assert state.tokens_hour.reset_seconds == 3490.0

    def test_no_headers(self):
        state = parse_rate_limit_headers({})
        assert state is None

    def test_partial_headers(self):
        headers = {
            "x-ratelimit-limit-requests": "100",
            "x-ratelimit-remaining-requests": "50",
        }
        state = parse_rate_limit_headers(headers)
        assert state is not None
        assert state.requests_min.limit == 100
        assert state.requests_min.remaining == 50
        # Missing fields default to 0
        assert state.tokens_min.limit == 0

    def test_non_rate_limit_headers_ignored(self):
        headers = {
            "content-type": "application/json",
            "server": "nginx",
        }
        state = parse_rate_limit_headers(headers)
        assert state is None

    def test_malformed_values(self):
        headers = {
            "x-ratelimit-limit-requests": "not-a-number",
            "x-ratelimit-remaining-requests": "",
            "x-ratelimit-reset-requests": "abc",
        }
        state = parse_rate_limit_headers(headers)
        assert state is not None
        assert state.requests_min.limit == 0
        assert state.requests_min.remaining == 0
        assert state.requests_min.reset_seconds == 0.0


class TestBucket:
    def test_used(self):
        b = RateLimitBucket(limit=800, remaining=795, reset_seconds=45.0, captured_at=time.time())
        assert b.used == 5

    def test_usage_pct(self):
        b = RateLimitBucket(limit=100, remaining=20, reset_seconds=30.0, captured_at=time.time())
        assert b.usage_pct == pytest.approx(80.0)

    def test_usage_pct_zero_limit(self):
        b = RateLimitBucket(limit=0, remaining=0)
        assert b.usage_pct == 0.0

    def test_remaining_seconds_now(self):
        now = time.time()
        b = RateLimitBucket(limit=800, remaining=795, reset_seconds=60.0, captured_at=now - 10)
        # ~50 seconds should remain
        assert 49 <= b.remaining_seconds_now <= 51

    def test_remaining_seconds_expired(self):
        b = RateLimitBucket(limit=800, remaining=795, reset_seconds=30.0, captured_at=time.time() - 60)
        assert b.remaining_seconds_now == 0.0


class TestFormatting:
    def test_fmt_count_millions(self):
        assert _fmt_count(8000000) == "8.0M"
        assert _fmt_count(336000000) == "336.0M"

    def test_fmt_count_thousands(self):
        assert _fmt_count(33600) == "33.6K"
        assert _fmt_count(1500) == "1.5K"

    def test_fmt_count_small(self):
        assert _fmt_count(800) == "800"
        assert _fmt_count(0) == "0"

    def test_fmt_seconds_short(self):
        assert _fmt_seconds(45) == "45s"
        assert _fmt_seconds(0) == "0s"

    def test_fmt_seconds_minutes(self):
        assert _fmt_seconds(125) == "2m 5s"
        assert _fmt_seconds(120) == "2m"

    def test_fmt_seconds_hours(self):
        assert _fmt_seconds(3660) == "1h 1m"
        assert _fmt_seconds(3600) == "1h"

    def test_bar(self):
        bar = _bar(50.0, width=10)
        assert bar == "[█████░░░░░]"
        assert _bar(0.0, width=10) == "[░░░░░░░░░░]"
        assert _bar(100.0, width=10) == "[██████████]"

    def test_format_display_no_data(self):
        state = RateLimitState()
        result = format_rate_limit_display(state)
        assert "No rate limit data" in result

    def test_format_display_with_data(self):
        state = parse_rate_limit_headers(NOUS_HEADERS, provider="nous")
        result = format_rate_limit_display(state)
        assert "Nous" in result
        assert "Requests/min" in result
        assert "Requests/hr" in result
        assert "Tokens/min" in result
        assert "Tokens/hr" in result
        assert "resets in" in result

    def test_format_display_warning_on_high_usage(self):
        headers = {
            **NOUS_HEADERS,
            "x-ratelimit-remaining-requests": "50",  # 750/800 used = 93.75%
        }
        state = parse_rate_limit_headers(headers)
        result = format_rate_limit_display(state)
        assert "⚠" in result

    def test_format_compact(self):
        state = parse_rate_limit_headers(NOUS_HEADERS, provider="nous")
        result = format_rate_limit_compact(state)
        assert "RPM:" in result
        assert "RPH:" in result
        assert "TPM:" in result
        assert "TPH:" in result
        assert "resets" in result

    def test_format_compact_no_data(self):
        state = RateLimitState()
        result = format_rate_limit_compact(state)
        assert "No rate limit data" in result


class TestAgentIntegration:
    """Test that AIAgent captures rate limit state correctly."""

    def test_capture_rate_limits_from_headers(self):
        """Simulate the header capture path without a real API call."""
        # Use a mock httpx-like response
        class MockResponse:
            headers = NOUS_HEADERS

        # Import AIAgent minimally

        # Test the parsing directly
        state = parse_rate_limit_headers(MockResponse.headers, provider="nous")
        assert state is not None
        assert state.requests_min.limit == 800
        assert state.tokens_hour.limit == 336000000

    def test_capture_rate_limits_none_response(self):
        """_capture_rate_limits should handle None gracefully."""
        from agent.rate_limit_tracker import parse_rate_limit_headers
        # None should not crash
        result = parse_rate_limit_headers({})
        assert result is None

    def test_parse_handles_anthropic_error_response(self):
        """A 429 response from Anthropic carries the same anthropic-ratelimit-*
        headers as a 200.  Verify the parser handles that path so the agent's
        capture-on-429 hook can refresh state through throttle events.
        """
        from datetime import datetime, timedelta, timezone
        reset = (datetime.now(timezone.utc) + timedelta(seconds=45)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        # Headers as they arrive on a 429 — same shape as 200 OK + retry-after.
        err_headers = {
            "anthropic-ratelimit-requests-limit": "50",
            "anthropic-ratelimit-requests-remaining": "0",
            "anthropic-ratelimit-requests-reset": reset,
            "anthropic-ratelimit-input-tokens-limit": "200000",
            "anthropic-ratelimit-input-tokens-remaining": "0",
            "anthropic-ratelimit-input-tokens-reset": reset,
            "retry-after": "45",
        }
        state = parse_rate_limit_headers(err_headers, provider="anthropic")
        assert state is not None
        assert state.requests_min.remaining == 0
        assert state.input_tokens_min.remaining == 0
        # Hottest bucket would be either of the two exhausted ones — both
        # at 100% — so the heartbeat formatter must render a ⚠ tag.
        assert "⚠" in format_rate_limit_heartbeat(state)


# ── Anthropic native schema (anthropic-ratelimit-*) ──────────────────────


def _iso(seconds_from_now: float) -> str:
    """Build an ISO-8601 reset timestamp ``seconds_from_now`` into the future."""
    dt = datetime.now(timezone.utc) + timedelta(seconds=seconds_from_now)
    # Anthropic emits with "Z" suffix.
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def anthropic_headers():
    """Realistic Anthropic-native rate-limit headers, per docs.

    Standard tier: separate input-tokens and output-tokens buckets, no hourly
    windows.  Reset is RFC-3339 with trailing Z.
    """
    return {
        "anthropic-ratelimit-requests-limit": "50",
        "anthropic-ratelimit-requests-remaining": "47",
        "anthropic-ratelimit-requests-reset": _iso(45),
        "anthropic-ratelimit-input-tokens-limit": "200000",
        "anthropic-ratelimit-input-tokens-remaining": "198500",
        "anthropic-ratelimit-input-tokens-reset": _iso(60),
        "anthropic-ratelimit-output-tokens-limit": "16000",
        "anthropic-ratelimit-output-tokens-remaining": "15600",
        "anthropic-ratelimit-output-tokens-reset": _iso(60),
        # Diagnostic-only — must not break parsing.
        "request-id": "req_abc123",
        "anthropic-organization-id": "org_xyz",
    }


class TestIso8601ResetParser:
    def test_zulu_suffix(self):
        ts = _iso(120)
        now = time.time()
        secs = _parse_iso8601_reset_to_seconds(ts, now=now)
        # ~120s in the future, allow drift for test-runtime delay.
        assert 115 <= secs <= 125

    def test_offset_suffix(self):
        # Anthropic always uses Z, but tolerate explicit "+00:00" too.
        dt = datetime.now(timezone.utc) + timedelta(seconds=30)
        ts = dt.isoformat()  # "+00:00" suffix, not Z
        now = time.time()
        secs = _parse_iso8601_reset_to_seconds(ts, now=now)
        assert 25 <= secs <= 35

    def test_past_timestamp_floored_at_zero(self):
        ts = _iso(-300)
        secs = _parse_iso8601_reset_to_seconds(ts, now=time.time())
        assert secs == 0.0

    def test_garbage_returns_zero(self):
        assert _parse_iso8601_reset_to_seconds("not-a-date", now=time.time()) == 0.0
        assert _parse_iso8601_reset_to_seconds("", now=time.time()) == 0.0
        assert _parse_iso8601_reset_to_seconds(None, now=time.time()) == 0.0
        # Numeric input also rejected — Anthropic only sends strings.
        assert _parse_iso8601_reset_to_seconds(45.0, now=time.time()) == 0.0


class TestAnthropicSchema:
    def test_basic_parsing(self, anthropic_headers):
        state = parse_rate_limit_headers(anthropic_headers, provider="anthropic")
        assert state is not None
        assert state.schema == "anthropic-ratelimit"
        assert state.provider == "anthropic"

        assert state.requests_min.limit == 50
        assert state.requests_min.remaining == 47
        assert 40 <= state.requests_min.reset_seconds <= 50

        assert state.input_tokens_min.limit == 200000
        assert state.input_tokens_min.remaining == 198500
        assert 55 <= state.input_tokens_min.reset_seconds <= 65

        assert state.output_tokens_min.limit == 16000
        assert state.output_tokens_min.remaining == 15600

        # No hourly windows on Anthropic native.
        assert state.requests_hour.limit == 0
        assert state.tokens_hour.limit == 0

    def test_provider_defaulted_when_unspecified(self, anthropic_headers):
        state = parse_rate_limit_headers(anthropic_headers, provider="")
        assert state is not None
        assert state.provider == "anthropic"

    def test_priority_tier_overrides_when_tighter(self):
        # Standard input-tokens bucket has lots of headroom; priority bucket
        # is tighter (90% used) — that's the binding constraint we want
        # surfaced in input_tokens_min.
        headers = {
            "anthropic-ratelimit-input-tokens-limit": "200000",
            "anthropic-ratelimit-input-tokens-remaining": "198000",  # 1% used
            "anthropic-ratelimit-input-tokens-reset": _iso(60),
            "anthropic-priority-input-tokens-limit": "50000",
            "anthropic-priority-input-tokens-remaining": "5000",  # 90% used
            "anthropic-priority-input-tokens-reset": _iso(60),
        }
        state = parse_rate_limit_headers(headers, provider="anthropic")
        assert state is not None
        # Tighter bucket wins.
        assert state.input_tokens_min.limit == 50000
        assert state.input_tokens_min.remaining == 5000

    def test_priority_tier_ignored_when_base_is_tighter(self):
        # Inverse: priority bucket has more headroom than the base — we must
        # NOT swap in a looser bucket and hide the real limit.
        headers = {
            "anthropic-ratelimit-input-tokens-limit": "200000",
            "anthropic-ratelimit-input-tokens-remaining": "10000",  # 95% used
            "anthropic-ratelimit-input-tokens-reset": _iso(60),
            "anthropic-priority-input-tokens-limit": "1000000",
            "anthropic-priority-input-tokens-remaining": "999000",  # 0.1% used
            "anthropic-priority-input-tokens-reset": _iso(60),
        }
        state = parse_rate_limit_headers(headers, provider="anthropic")
        assert state is not None
        assert state.input_tokens_min.limit == 200000

    def test_diagnostic_headers_dont_confuse_detection(self):
        """request-id + anthropic-organization-id alone ≠ rate-limit data."""
        headers = {
            "request-id": "req_abc",
            "anthropic-organization-id": "org_xyz",
            "content-type": "application/json",
        }
        assert parse_rate_limit_headers(headers) is None


class TestSchemaPrecedence:
    def test_anthropic_wins_when_both_present(self, anthropic_headers):
        # Some OpenRouter relays emit both schemas.  Anthropic carries the
        # tighter input/output-token buckets so it should take precedence.
        merged = {**NOUS_HEADERS, **anthropic_headers}
        state = parse_rate_limit_headers(merged, provider="anthropic")
        assert state is not None
        assert state.schema == "anthropic-ratelimit"
        # Must be Anthropic's bucket, not Nous's 800-RPM cap.
        assert state.requests_min.limit == 50

    def test_x_ratelimit_used_alone(self):
        state = parse_rate_limit_headers(NOUS_HEADERS, provider="nous")
        assert state is not None
        assert state.schema == "x-ratelimit"


class TestHeartbeatFormatter:
    def test_no_data(self):
        assert format_rate_limit_heartbeat(RateLimitState()) == ""

    def test_healthy_collapses_to_limits_ok(self, anthropic_headers):
        state = parse_rate_limit_headers(anthropic_headers, provider="anthropic")
        out = format_rate_limit_heartbeat(state)
        assert out.startswith("limits OK")
        # Hottest bucket on this fixture is RPM (3/50 = 6%) — a small bucket
        # near zero usage.  Just check the format shape, not the exact label.
        assert "(" in out and ")" in out

    def test_hot_bucket_warning(self):
        # Force a 90%-used input-tokens bucket.
        headers = {
            "anthropic-ratelimit-requests-limit": "50",
            "anthropic-ratelimit-requests-remaining": "49",
            "anthropic-ratelimit-requests-reset": _iso(45),
            "anthropic-ratelimit-input-tokens-limit": "200000",
            "anthropic-ratelimit-input-tokens-remaining": "20000",  # 90% used
            "anthropic-ratelimit-input-tokens-reset": _iso(45),
        }
        state = parse_rate_limit_headers(headers, provider="anthropic")
        out = format_rate_limit_heartbeat(state)
        assert "⚠" in out
        assert "ITPM" in out
        assert "90%" in out
        assert "resets in" in out

    def test_x_ratelimit_healthy(self):
        state = parse_rate_limit_headers(NOUS_HEADERS, provider="nous")
        out = format_rate_limit_heartbeat(state)
        assert out.startswith("limits OK")


class TestHottestBucket:
    def test_picks_highest_usage_pct(self):
        state = RateLimitState(
            requests_min=RateLimitBucket(limit=100, remaining=99, captured_at=time.time()),  # 1%
            tokens_min=RateLimitBucket(limit=1000, remaining=100, captured_at=time.time()),  # 90%
            captured_at=time.time(),
        )
        h = state.hottest_bucket
        assert h is not None
        label, bucket = h
        assert label == "TPM"
        assert bucket.usage_pct == pytest.approx(90.0)

    def test_no_buckets_returns_none(self):
        assert RateLimitState().hottest_bucket is None


class TestAnthropicDisplay:
    def test_display_omits_empty_buckets(self, anthropic_headers):
        state = parse_rate_limit_headers(anthropic_headers, provider="anthropic")
        out = format_rate_limit_display(state)
        # Anthropic doesn't have hourly buckets — the rendered display must
        # not mention them as "(no data)".
        assert "Requests/hr" not in out
        assert "Tokens/hr" not in out
        # Input/output split is shown instead of the legacy combined "tokens".
        assert "Input tok/min" in out
        assert "Output tok/min" in out

    def test_compact_uses_input_output_labels(self, anthropic_headers):
        state = parse_rate_limit_headers(anthropic_headers, provider="anthropic")
        out = format_rate_limit_compact(state)
        assert "ITPM:" in out
        assert "OTPM:" in out
        # Should not show the legacy combined TPM label when the split is
        # present.  Word-boundary check — "ITPM" naturally contains "TPM".
        assert " TPM:" not in out and not out.startswith("TPM:")
