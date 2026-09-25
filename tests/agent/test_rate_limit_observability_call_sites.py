"""Call-site coverage for fork rate-limit observability (v2026.9.14 regression).

``tests/agent/test_rate_limit_observability.py`` binds
``_log_rate_limit_first_capture`` / ``_log_rate_limit_transitions`` directly off
``AIAgent`` and asserts they behave correctly. That is why it kept passing when
the v2026.9.14 decomposition of ``conversation_loop.py`` into ``turn_*.py``
dropped every CALL to ``_capture_rate_limits_from_headers`` (pre-merge:
conversation_loop.py:6406 and :7564) and the heartbeat's rate-limit fragment
(pre-merge: chat_completion_helpers.py:5937): the helpers were fine, nothing
invoked them, so the 80% hot-zone events and the streaming heartbeat signal were
permanently silent while ``init_state`` still ran and the state vars still existed.

These tests drive the REAL production entry points instead, so a future refactor
that disconnects them again fails here.

Note the deliberate split of responsibilities these tests pin:
``agent/rate_limit_credits.py::_capture_rate_limits`` (upstream) takes a response
OBJECT and only caches state; the fork's ``_capture_rate_limits_from_headers``
takes a header MAPPING and additionally emits the observability events. The error
paths below must use the fork helper -- they already hold the extracted headers,
and using upstream's would cache state while emitting nothing.
"""

from __future__ import annotations

import logging
import time
from types import SimpleNamespace

import pytest

# Anthropic-shaped headers with ITPM at 92% -- above the 80% hot-zone threshold.
HOT_HEADERS = {
    "anthropic-ratelimit-requests-limit": "50",
    "anthropic-ratelimit-requests-remaining": "45",
    "anthropic-ratelimit-requests-reset": "2026-09-22T12:00:45Z",
    "anthropic-ratelimit-input-tokens-limit": "200000",
    "anthropic-ratelimit-input-tokens-remaining": "16000",
    "anthropic-ratelimit-input-tokens-reset": "2026-09-22T12:00:60Z",
}


def _agent():
    """Stub carrying the surface the recovery/heartbeat paths touch, with the
    real rate-limit methods bound off the real class."""
    from run_agent import AIAgent

    a = SimpleNamespace(
        provider="anthropic", model="claude-opus-4-7",
        base_url="https://api.anthropic.com", log_prefix="",
        _rate_limit_state=None, _rate_limit_first_logged=False,
        _rate_limit_hot_buckets=set(), _interrupt_requested=False,
        _consecutive_stale_streams=0,
        _emit_status=lambda *a, **k: None, _buffer_status=lambda *a, **k: None,
        _emit_diagnostic_status=lambda *a, **k: None,
        _buffer_diagnostic_status=lambda *a, **k: None,
        _emit_diagnostic_wait=lambda *a, **k: None,
        _emit_wait_notice=lambda *a, **k: None, _touch_activity=lambda *a, **k: None,
        _client_log_context=lambda: "",
    )
    for name in (
        "_capture_rate_limits_from_headers", "_capture_rate_limits",
        "_log_rate_limit_first_capture", "_log_rate_limit_transitions",
        "get_rate_limit_state",
    ):
        setattr(a, name, getattr(AIAgent, name).__get__(a, AIAgent))
    return a


def _rate_limit_error():
    err = Exception("429 rate_limit_error")
    err.response = SimpleNamespace(headers=HOT_HEADERS, status_code=429)
    err.body = None
    return err


def _hot_warns(caplog):
    return [r.message for r in caplog.records if "crossed 80%" in r.message]


class TestErrorPathsRefreshRateLimitState:
    """Both 429 recovery paths must refresh state from the ERROR headers AND
    emit the hot-zone transition."""

    def test_nous_genuine_check_captures_and_warns(self, caplog):
        from agent.turn_recovery import _is_genuine_nous_rate_limit

        agent = _agent()
        with caplog.at_level(logging.INFO, logger="run_agent"):
            _is_genuine_nous_rate_limit(agent, _rate_limit_error(), {"provider": "nous"})

        assert agent._rate_limit_state is not None, (
            "the Nous genuine-rate-limit check must refresh state from the 429 "
            "headers before classifying -- otherwise it classifies on last-known data"
        )
        assert agent._rate_limit_state.input_tokens_min.usage_pct == pytest.approx(92.0)
        assert len(_hot_warns(caplog)) == 1
        assert "ITPM" in _hot_warns(caplog)[0]

    def test_generic_backoff_captures_non_nous_429(self, caplog):
        """Anthropic-native / OpenRouter 429s never reach the Nous branch, so the
        generic backoff path carries its own capture."""
        from agent.turn_recovery import compute_error_backoff

        agent = _agent()
        with caplog.at_level(logging.INFO, logger="run_agent"):
            compute_error_backoff(
                agent, _rate_limit_error(), retry_count=1, max_retries=5,
                is_rate_limited=True, is_zai_coding_overload=False,
                base_url="https://api.anthropic.com", model="claude-opus-4-7",
            )

        assert agent._rate_limit_state is not None
        assert len(_hot_warns(caplog)) == 1

    def test_non_rate_limited_error_does_not_capture(self, caplog):
        """A plain 500 carries no ratelimit headers worth adopting -- the capture
        is gated on is_rate_limited so unrelated errors stay quiet."""
        from agent.turn_recovery import compute_error_backoff

        agent = _agent()
        err = Exception("500 server_error")
        err.response = SimpleNamespace(headers={}, status_code=500)
        err.body = None
        with caplog.at_level(logging.INFO, logger="run_agent"):
            compute_error_backoff(
                agent, err, retry_count=1, max_retries=5, is_rate_limited=False,
                is_zai_coding_overload=False, base_url="https://api.anthropic.com",
                model="claude-opus-4-7",
            )
        assert agent._rate_limit_state is None
        assert _hot_warns(caplog) == []


class TestStreamingHeartbeatCarriesRateLimit:
    """The 60s+ streaming heartbeat must answer 'is this stall throttle-related?'"""

    @staticmethod
    def _probe(agent):
        from agent import chat_completion_wait_notice as wn
        from agent.chat_completion_stream_monitor import StreamingWaitMonitor

        class _Probe(StreamingWaitMonitor):
            def __init__(self):
                self.agent = agent
                self.api_kwargs = {"model": "claude-opus-4-7"}
                self.last_chunk_time = {"t": time.time() - 90}
                self._stream_stale_timeout = 300.0
                self._mon = SimpleNamespace(
                    last_heartbeat=time.time(), wait_notice_started_ts=None,
                    wait_notice=wn.WaitNoticeState(),
                )

        return _Probe()

    def test_hot_bucket_surfaces_in_heartbeat(self):
        agent = _agent()
        agent._capture_rate_limits_from_headers(HOT_HEADERS)
        notices = []
        agent._emit_wait_notice = lambda s="", *a, **k: notices.append(s)

        self._probe(agent)._heartbeat(90)

        assert notices, "a 60s+ heartbeat must emit a wait notice"
        text = notices[-1]
        assert "ITPM" in text and "92%" in text, text
        # The recovery ETA must survive alongside the new fragment.
        assert "auto-reconnect: stream stale watchdog in 210s" in text, text

    def test_healthy_state_collapses_to_limits_ok(self):
        agent = _agent()
        agent._capture_rate_limits_from_headers(
            dict(HOT_HEADERS, **{"anthropic-ratelimit-input-tokens-remaining": "190000"})
        )
        notices = []
        agent._emit_wait_notice = lambda s="", *a, **k: notices.append(s)

        self._probe(agent)._heartbeat(90)

        assert "limits OK" in notices[-1], notices[-1]

    def test_no_state_leaves_heartbeat_unchanged(self):
        """No headers seen yet -> no fragment, no crash, notice still emitted."""
        agent = _agent()
        notices = []
        agent._emit_wait_notice = lambda s="", *a, **k: notices.append(s)

        self._probe(agent)._heartbeat(90)

        text = notices[-1]
        assert "ITPM" not in text and "limits OK" not in text
        assert "auto-reconnect: stream stale watchdog in 210s" in text

    def test_heartbeat_under_60s_does_not_emit_notice(self):
        """Chunks flowing: touch the activity tracker, leave the display alone."""
        agent = _agent()
        agent._capture_rate_limits_from_headers(HOT_HEADERS)
        notices, touched = [], []
        agent._emit_wait_notice = lambda s="", *a, **k: notices.append(s)
        agent._touch_activity = lambda s="", *a, **k: touched.append(s)

        self._probe(agent)._heartbeat(30)

        assert notices == []
        assert touched
