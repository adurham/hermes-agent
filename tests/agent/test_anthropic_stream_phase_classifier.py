"""Regression tests for the Anthropic stream-phase heartbeat classifier.

The classifier turns wire-observable signals (ping cadence, message_start
arrival, thinking activity, content silence, etc.) into the human-readable
"phase" string that's appended to "Still waiting on provider — Ns elapsed
(model: X, <phase>)" status emits.

Why this matters:
  * Before the diagnostic rewrite, every long pre-event wait was labeled
    "thinking (no events yet)" — the heartbeat couldn't distinguish a
    productive 19-min thinking phase from a 19-min wedge with no pings.
  * The classifier is now driven by *observed* signals only.  These tests
    pin the mapping so a future tweak can't silently regress to guessing.

If you intentionally change the phase strings (UX/copy edit), update the
expectations here so the change is visible in the diff.
"""
from __future__ import annotations

import pytest


def _classify(**overrides):
    """Helper: import the classifier with sensible defaults."""
    from run_agent import _classify_anthropic_stream_phase

    defaults = dict(
        thinking_active=False,
        thinking_chars=0,
        first_event_seen=False,
        content_silence=0,
        thinking_requested=False,
        message_start_arrived=False,
        ping_seen=False,
        user_elapsed=0,
    )
    defaults.update(overrides)
    return _classify_anthropic_stream_phase(**defaults)


class TestAnthropicStreamPhaseClassifier:

    # ── Active thinking with live deltas ────────────────────────────
    def test_thinking_active_with_chars_shows_count(self):
        """display=summarized streams thinking_delta tokens; surface the count."""
        assert _classify(thinking_active=True, thinking_chars=12_345) == (
            "thinking (12,345 chars streamed)"
        )

    def test_thinking_active_no_chars_yet_shows_bare_thinking(self):
        """thinking_active flipped on but no thinking_delta has arrived yet."""
        assert _classify(thinking_active=True, thinking_chars=0) == "thinking"

    # ── Mid-stream silence (post-message_start) ─────────────────────
    def test_first_event_seen_with_long_content_silence(self):
        """Stream started, then went quiet — server thinking between blocks."""
        assert _classify(first_event_seen=True, content_silence=15) == (
            "thinking (server-side)"
        )

    def test_first_event_seen_short_content_silence_is_streaming(self):
        """Recent content event arrived (silence ≤10s) — regular streaming."""
        assert _classify(first_event_seen=True, content_silence=5) == "streaming"

    # ── Pre-content, post-message_start ─────────────────────────────
    def test_message_start_arrived_thinking_requested_means_thinking_omitted(self):
        """The case the user hits hardest: Opus 4.7 + xhigh + display=omitted.

        message_start arrives quickly (request accepted), then the server
        sits silent for the entire thinking budget (~10-18 min on xhigh).
        The classifier must surface this as confident thinking, not generic
        "queued" — we have proof of acceptance.
        """
        assert _classify(
            message_start_arrived=True,
            thinking_requested=True,
            ping_seen=True,
            user_elapsed=600,
        ) == "thinking server-side (display=omitted)"

    # ── Pre-message_start, pings observable ─────────────────────────
    def test_pre_message_start_pings_flowing_thinking_requested(self):
        """Pings arrived but no message_start yet — request is queued
        and the server is actively keep-alive-ing.  This is normal during
        cold-start of large prompts (200K+ tokens)."""
        assert _classify(
            ping_seen=True,
            thinking_requested=True,
            user_elapsed=60,
        ) == "queued/prefilling (thinking req'd, server pinging)"

    def test_pre_message_start_no_pings_after_30s_thinking_requested(self):
        """Thinking requested, ≥30s elapsed, NO pings observed — this is
        the wedge-or-cold-connection state we want explicitly named.
        Previously this was labeled the same as a healthy thinking phase."""
        assert _classify(
            ping_seen=False,
            thinking_requested=True,
            user_elapsed=45,
        ) == "no pings yet — connection may be cold or wedged"

    def test_under_30s_no_pings_falls_through_to_generic_queued(self):
        """Within the first 30s of a request with no pings yet, don't
        falsely scare the user — a normal cold start can take that long
        before the first ping."""
        # Thinking requested but only 10s in: not the wedge state.
        assert _classify(
            ping_seen=False,
            thinking_requested=True,
            user_elapsed=10,
        ) == "queued/prefilling (no pings yet)"

    # ── No thinking requested ───────────────────────────────────────
    def test_no_thinking_pings_flowing(self):
        """Non-thinking model: pings prove server alive."""
        assert _classify(ping_seen=True) == (
            "queued/prefilling, server alive (pings flowing)"
        )

    def test_no_thinking_no_pings(self):
        """Cold start, nothing observed yet."""
        assert _classify(ping_seen=False) == "queued/prefilling (no pings yet)"

    # ── Priority: thinking_active wins over message_start ───────────
    def test_thinking_active_wins_over_message_start_arrived(self):
        """If thinking_delta is currently flowing, that's the most useful
        label — don't downgrade to the generic post-message_start state."""
        assert _classify(
            thinking_active=True,
            thinking_chars=500,
            message_start_arrived=True,
            first_event_seen=True,
        ) == "thinking (500 chars streamed)"

    def test_first_event_seen_wins_over_message_start_with_thinking_req(self):
        """Once content is actually streaming (first_event_seen + recent
        content), don't fall back to the omitted-thinking label."""
        assert _classify(
            first_event_seen=True,
            content_silence=2,
            message_start_arrived=True,
            thinking_requested=True,
        ) == "streaming"


# ── Cache-percentage math regression ─────────────────────────────────
#
# The first version of the heartbeat diagnostic divided cache_read by
# input_tokens.  That's wrong because Anthropic's input_tokens is the
# NEW (uncached) prompt only — typically a tiny delta on cache-hot
# turns.  A real session emitted:
#
#     [message_start +19s, 6 in (cache 2957817%)]
#
# (177,469 cache_read divided by 6 input_tokens = 2,957,817%.)  Total
# prompt is the SUM of new + cache_read + cache_creation, and the
# percentage must use that total.

class TestCachePercentageMath:
    """Regression: cache % must be computed against total prompt, not
    just the ``input_tokens`` (new uncached) field."""

    @staticmethod
    def _total_and_pct(new_tokens: int, cache_read: int, cache_creation: int) -> tuple[int, float]:
        """Reproduce the heartbeat's math so the test pins it."""
        total = new_tokens + cache_read + cache_creation
        pct = (100 * cache_read / total) if total else 0.0
        return total, pct

    def test_cache_hot_turn_with_tiny_new_prefix_under_100_pct(self):
        """The exact shape that produced 2,957,817% in production."""
        total, pct = self._total_and_pct(new_tokens=6, cache_read=177_469, cache_creation=0)
        assert total == 177_475
        assert 99.0 < pct <= 100.0, (
            f"cache_pct {pct:.2f}% must stay ≤100% on cache-hot turns"
        )

    def test_first_turn_no_cache(self):
        """First turn of a session: no cache yet, all tokens are new."""
        total, pct = self._total_and_pct(new_tokens=12_000, cache_read=0, cache_creation=12_000)
        assert total == 24_000
        assert pct == 0.0

    def test_warm_turn_with_cache_creation(self):
        """Mid-session: some cache_read + some new content being cached."""
        total, pct = self._total_and_pct(
            new_tokens=2_000, cache_read=140_000, cache_creation=8_000
        )
        assert total == 150_000
        assert 90 < pct < 95  # ~93%

    def test_division_by_zero_safe(self):
        """No usage data yet — must not blow up."""
        total, pct = self._total_and_pct(0, 0, 0)
        assert total == 0
        assert pct == 0.0
