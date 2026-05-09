"""Tests for rate-limit observability hooks on AIAgent.

Covers:

  * One-shot INFO log on first successful header capture per session.
  * WARN log when a bucket crosses 80% utilisation.
  * INFO log when a previously-hot bucket drops back below 80%.
  * Hysteresis: a bucket already in the hot set doesn't re-warn on every
    capture; a bucket oscillating across the threshold doesn't paint
    paired warn/clear pairs every API call (we report on transition only).

These hooks call back into ``logging.Logger`` directly, so we patch the
module-level logger and inspect its call list rather than spinning up a
full agent.
"""

from __future__ import annotations

import logging
import time
import types
from unittest.mock import patch

import pytest

from agent.rate_limit_tracker import (
    RateLimitBucket,
    RateLimitState,
)


def _make_state(
    *,
    rpm_used: int = 0,
    rpm_limit: int = 50,
    itpm_used: int = 0,
    itpm_limit: int = 200_000,
    schema: str = "anthropic-ratelimit",
    provider: str = "anthropic",
) -> RateLimitState:
    """Build a RateLimitState with chosen utilisation per bucket."""
    now = time.time()
    return RateLimitState(
        requests_min=RateLimitBucket(
            limit=rpm_limit,
            remaining=rpm_limit - rpm_used,
            reset_seconds=45.0,
            captured_at=now,
        ),
        input_tokens_min=RateLimitBucket(
            limit=itpm_limit,
            remaining=itpm_limit - itpm_used,
            reset_seconds=60.0,
            captured_at=now,
        ),
        captured_at=now,
        provider=provider,
        schema=schema,
    )


def _stub_agent():
    """Build a minimal stub with the same hook surface AIAgent exposes.

    Avoids the cost of constructing a full AIAgent (~60 ctor args) just to
    exercise three pure methods.  Bind the methods directly off the class
    so we test the real implementation, not a re-implementation.
    """
    from run_agent import AIAgent

    stub = types.SimpleNamespace(
        _rate_limit_state=None,
        _rate_limit_first_logged=False,
        _rate_limit_hot_buckets=set(),
    )
    # Bind the unbound methods.
    stub._log_rate_limit_first_capture = AIAgent._log_rate_limit_first_capture.__get__(stub, AIAgent)
    stub._log_rate_limit_transitions = AIAgent._log_rate_limit_transitions.__get__(stub, AIAgent)
    return stub


class TestFirstCaptureLog:
    def test_logs_once_per_session(self, caplog):
        stub = _stub_agent()
        state = _make_state(rpm_used=3)

        with caplog.at_level(logging.INFO, logger="run_agent"):
            stub._log_rate_limit_first_capture(state)
            stub._log_rate_limit_first_capture(state)
            stub._log_rate_limit_first_capture(state)

        msgs = [r.message for r in caplog.records if "captured initial state" in r.message]
        assert len(msgs) == 1, f"expected exactly one INFO, got {msgs!r}"

    def test_message_includes_schema_provider_and_summary(self, caplog):
        stub = _stub_agent()
        state = _make_state(rpm_used=3, itpm_used=1500)

        with caplog.at_level(logging.INFO, logger="run_agent"):
            stub._log_rate_limit_first_capture(state)

        msg = next(r.message for r in caplog.records if "captured initial state" in r.message)
        assert "anthropic-ratelimit" in msg
        assert "anthropic" in msg
        # The compact summary should appear inline.
        assert "RPM:" in msg
        assert "ITPM:" in msg


class TestTransitionWarnings:
    def test_warns_once_when_bucket_crosses_threshold(self, caplog):
        stub = _stub_agent()
        # First capture: bucket is healthy (1% used).
        with caplog.at_level(logging.WARNING, logger="run_agent"):
            stub._log_rate_limit_transitions(_make_state(itpm_used=2_000))
        warns = [r for r in caplog.records if r.levelname == "WARNING"]
        assert warns == [], "healthy state should not warn"

        # Second capture: bucket crosses 90% — single WARN.
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="run_agent"):
            stub._log_rate_limit_transitions(_make_state(itpm_used=180_000))
        warns = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warns) == 1
        assert "ITPM crossed 80%" in warns[0].message

        # Third capture: bucket still hot — must NOT re-warn.
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="run_agent"):
            stub._log_rate_limit_transitions(_make_state(itpm_used=185_000))
        warns = [r for r in caplog.records if r.levelname == "WARNING"]
        assert warns == [], "second hot capture should not re-warn"

    def test_recovery_logs_info_clear(self, caplog):
        stub = _stub_agent()
        # Heat the bucket.
        with caplog.at_level(logging.WARNING, logger="run_agent"):
            stub._log_rate_limit_transitions(_make_state(itpm_used=180_000))
        assert "ITPM" in stub._rate_limit_hot_buckets

        # Drop back below 80% — one-shot INFO clear.
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="run_agent"):
            stub._log_rate_limit_transitions(_make_state(itpm_used=20_000))
        infos = [r for r in caplog.records if r.levelname == "INFO" and "recovered" in r.message]
        assert len(infos) == 1
        assert "ITPM" in infos[0].message
        assert "ITPM" not in stub._rate_limit_hot_buckets

    def test_multiple_buckets_tracked_independently(self, caplog):
        stub = _stub_agent()

        # Heat ITPM first, then RPM.
        with caplog.at_level(logging.WARNING, logger="run_agent"):
            stub._log_rate_limit_transitions(_make_state(itpm_used=180_000))
        assert stub._rate_limit_hot_buckets == {"ITPM"}

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="run_agent"):
            stub._log_rate_limit_transitions(_make_state(itpm_used=180_000, rpm_used=45))
        warns = [r for r in caplog.records if r.levelname == "WARNING"]
        # Only the new transition (RPM) warns; ITPM stays silent.
        assert len(warns) == 1
        assert "RPM crossed 80%" in warns[0].message
        assert stub._rate_limit_hot_buckets == {"ITPM", "RPM"}

    def test_buckets_with_zero_limit_skipped(self, caplog):
        """Empty buckets (Anthropic doesn't publish hourly) must not warn."""
        stub = _stub_agent()
        # All buckets are unset → no transitions.
        with caplog.at_level(logging.WARNING, logger="run_agent"):
            stub._log_rate_limit_transitions(RateLimitState(captured_at=time.time()))
        warns = [r for r in caplog.records if r.levelname == "WARNING"]
        assert warns == []
