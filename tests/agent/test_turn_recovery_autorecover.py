"""Bounded post-exhaustion auto-recovery ladder (#85426, #107307): fallback first, transient
outages only, visible on every surface, interrupt cancels."""
from types import SimpleNamespace
from typing import Optional
from unittest.mock import patch

import pytest

from agent.error_classifier import classify_api_error
from agent.turn_api_error import settle_unrecovered_error
from agent.turn_recovery_autorecover import (
    auto_recovery_identical_failure_limit, crash_loop_message, failure_fingerprint,
    identical_failure_limit_reached, ladder_wait_seconds, note_recovery_failure,
)
from agent.turn_retry_state import TurnRetryState


class _Err(Exception):
    def __init__(self, status_code, message, headers=None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.response = SimpleNamespace(headers=headers or {})
        self.body = None


class _Agent:
    """Real seams: fallback state, the status/wait callbacks the surfaces read, the streamed-text
    gate and the interrupt flag; everything else the terminal path touches is a no-op."""
    log_prefix = ""
    verbose = False
    provider = "custom"
    platform = "cli"
    base_url = "http://x"
    model = "m"
    _credential_pool = None
    _current_streamed_assistant_text = ""
    _interrupt_requested = False
    # Per-attempt "any token/chunk received" marker (agent/turn_api_request.py resets it per call).
    _last_api_first_chunk_at: Optional[float] = None
    sleep_outcome = None  # what the (patched) interruptible wait returns

    def __init__(self, *, fallback_left=False, cycles=5, platform="cli", identical_limit=2):
        self.statuses, self.waits, self.activated = [], [], []
        self._fallback_left = fallback_left
        self._auto_recovery_cycles = cycles
        self._auto_recovery_identical_failure_limit = identical_limit
        self.platform = platform

    def _has_pending_fallback(self):
        return self._fallback_left

    def _try_activate_fallback(self, **kwargs):
        self.activated.append(True)
        return self._fallback_left

    def _try_recover_primary_transport(self, *a, **k):
        return False

    def _has_content_after_think_block(self, text):
        return bool(text.strip())

    def _emit_diagnostic_status(self, text):
        self.statuses.append(str(text))

    def _emit_diagnostic_wait(self, text):
        self.waits.append(str(text))

    def _summarize_api_error(self, error):
        return str(error)

    def _client_log_context(self):
        return ""

    def clear_interrupt(self, **kwargs):
        return False

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _settle(agent, err, retry=None):
    retry = retry or TurnRetryState(primary_recovery_attempted=True)
    classified = classify_api_error(err, provider=agent.provider)
    with patch("agent.conversation_loop._is_copilot_provider", lambda a: False), \
            patch("agent.conversation_loop._arm_fallback_restart", lambda a, m, s, r: s), \
            patch("agent.turn_recovery.interruptible_backoff_sleep", lambda agent, *a, **k: agent.sleep_outcome), \
            patch("agent.turn_api_error.max_retries_exhausted_result", lambda *a, **k: {"failed": True}), \
            patch("agent.turn_api_error.nonretryable_client_error_result", lambda *a, **k: {"failed": True, "nonretryable": True}):
        return settle_unrecovered_error(
            agent, api_error=err, classified=classified, _retry=retry, status_code=err.status_code,
            error_msg=str(err).lower(), is_context_length_error=False, is_rate_limited=False,
            _is_zai_coding_overload=False, _provider=agent.provider, _base="http://x", _model="m",
            messages=[], api_messages=[], api_kwargs=None, active_system_prompt=None,
            conversation_history=None, approx_tokens=0, retry_count=3, max_retries=3,
            compression_attempts=0, api_call_count=1,
        ), retry


@pytest.mark.parametrize("status,message", [(503, "Service temporarily unavailable"), (529, "Overloaded")])
def test_transient_exhaustion_without_fallback_enters_ladder_and_retries(status, message):
    """No fallback left + 5xx/overloaded + nothing delivered -> one visible cycle, then retry from 0."""
    agent = _Agent()
    verdict, retry = _settle(agent, _Err(status, message))
    assert (verdict.action, verdict.retry_count, retry.auto_recovery_cycles_used) == ("continue", 0, 1)
    assert agent.statuses == agent.waits and len(agent.statuses) == 1
    line = agent.statuses[0]
    assert "(cycle 1/5)" in line


def test_ladder_is_bounded_and_yields_to_fallback_and_shuns_nonretryable():
    agent = _Agent()
    spent = TurnRetryState(primary_recovery_attempted=True, auto_recovery_cycles_used=5)
    verdict, _ = _settle(agent, _Err(503, "down"), retry=spent)
    assert verdict.action == "return" and verdict.result == {"failed": True}
    assert any("gave up after 5 cycles" in s for s in agent.statuses)

    # Fallback first: a remaining fallback engages and the ladder never runs.
    fb = _Agent(fallback_left=True)
    verdict, retry = _settle(fb, _Err(503, "down"))
    assert (verdict.action, fb.activated, retry.auto_recovery_cycles_used) == ("break", [True], 0)

    # Non-retryable classes never enter (401 here) and delivered text is never replayed.
    auth = _Agent()
    verdict, retry = _settle(auth, _Err(401, "Incorrect API key provided"))
    assert verdict.result.get("nonretryable") and retry.auto_recovery_cycles_used == 0
    streamed = _Agent()
    streamed._current_streamed_assistant_text = "Here is the first half of the answer"
    verdict, retry = _settle(streamed, _Err(503, "down"))
    assert (verdict.action, retry.auto_recovery_cycles_used) == ("return", 0)

    # Disabled by config.
    off = _Agent(cycles=0)
    verdict, retry = _settle(off, _Err(503, "down"))
    assert (verdict.action, retry.auto_recovery_cycles_used, off.statuses) == ("return", 0, [])


def test_ladder_schedule_honours_retry_after_and_platform_stop_hint(monkeypatch):
    from agent import retry_utils
    calls = []

    def _recording_backoff(attempt, *, base_delay, max_delay, jitter_ratio):
        calls.append((attempt, base_delay, max_delay))
        return min(base_delay * 2 ** (attempt - 1), max_delay)
    monkeypatch.setattr(retry_utils, "jittered_backoff", _recording_backoff)

    plain = _Err(503, "down")
    assert [ladder_wait_seconds(c, plain) for c in (1, 2, 3, 4, 5)] == [15, 30, 60, 60, 60]
    assert calls[0] == (1, 15.0, 60.0)
    assert ladder_wait_seconds(1, _Err(529, "Overloaded", {"Retry-After": "4"})) == 4
    # Past the 60 s cap the provider's own cooldown is honoured, up to 120 s.
    assert ladder_wait_seconds(1, _Err(529, "Overloaded", {"Retry-After": "90"})) == 90
    assert ladder_wait_seconds(1, _Err(529, "Overloaded", {"Retry-After": "900"})) == 120

    tg = _Agent(platform="telegram")
    _settle(tg, plain)
    assert "send /stop to cancel" in tg.statuses[0]
    cron = _Agent(platform="cron")
    _settle(cron, plain)
    assert cron.statuses[0].endswith("(cycle 1/5)")


def test_interrupt_during_ladder_wait_returns_interrupted_result():
    agent = _Agent()
    agent.sleep_outcome = {"interrupted": True, "completed": False}
    verdict, retry = _settle(agent, _Err(503, "down"))
    assert verdict.action == "return" and verdict.result["interrupted"] is True
    assert retry.auto_recovery_cycles_used == 1


# ── Identical-failure circuit breaker (agent.auto_recovery_identical_failure_limit) ──────────────

def _classified(reason):
    return SimpleNamespace(reason=reason)


def test_failure_fingerprint_names_provider_endpoint_model_class_and_progress():
    """The fingerprint is (provider, base_url, model, normalized error class, received_token)."""
    from agent.error_classifier import FailoverReason
    agent = _Agent()
    quiet = failure_fingerprint(agent, _classified(FailoverReason.server_error), received_token=False)
    assert quiet == ("custom", "http://x", "m", "server_error", False)
    progressed = failure_fingerprint(agent, _classified(FailoverReason.server_error), received_token=True)
    assert progressed[-1] is True and progressed[:4] == quiet[:4]


def test_note_recovery_failure_counts_consecutive_identical_and_resets_on_class_or_window():
    from agent.error_classifier import FailoverReason
    agent = _Agent()
    retry = TurnRetryState()
    server = _classified(FailoverReason.server_error)
    assert note_recovery_failure(agent, server, retry, now=1000.0) == 1
    assert note_recovery_failure(agent, server, retry, now=1001.0) == 2
    assert note_recovery_failure(agent, server, retry, now=1002.0) == 3
    # A different error class is a different fingerprint -> the streak restarts at 1.
    assert note_recovery_failure(agent, _classified(FailoverReason.overloaded), retry, now=1003.0) == 1
    # An identical failure more than 30 minutes after the last one restarts the streak too.
    retry2 = TurnRetryState()
    assert note_recovery_failure(agent, server, retry2, now=5000.0) == 1
    assert note_recovery_failure(agent, server, retry2, now=5000.0 + 1801.0) == 1
    assert note_recovery_failure(agent, server, retry2, now=5001.0 + 1801.0) == 2


def test_note_recovery_failure_ignores_attempts_that_received_tokens():
    from agent.error_classifier import FailoverReason
    agent = _Agent()
    server = _classified(FailoverReason.server_error)
    retry = TurnRetryState()
    agent._last_api_first_chunk_at = 42.0  # any chunk arrived this attempt
    assert note_recovery_failure(agent, server, retry, now=10.0) == 0
    assert note_recovery_failure(agent, server, retry, now=11.0) == 0
    assert retry.auto_recovery_failure_fingerprint is None
    # Visible streamed text is progress too.
    agent._last_api_first_chunk_at = None
    agent._current_streamed_assistant_text = "half an answer"
    assert note_recovery_failure(agent, server, retry, now=12.0) == 0


def test_identical_failure_limit_reached_respects_config_and_zero_disables():
    agent = _Agent(identical_limit=2)
    retry = TurnRetryState(auto_recovery_identical_failures=1)
    assert not identical_failure_limit_reached(agent, retry)
    retry.auto_recovery_identical_failures = 2
    assert identical_failure_limit_reached(agent, retry)
    off = _Agent(identical_limit=0)
    assert auto_recovery_identical_failure_limit(off) == 0
    assert not identical_failure_limit_reached(off, retry)


def test_crash_loop_message_states_cause_count_last_error_and_next_step():
    msg = crash_loop_message(2, "HTTP 502 Bad Gateway")
    assert "crash-looping" in msg
    assert "2 times in a row" in msg
    assert "HTTP 502 Bad Gateway" in msg
    assert "Auto-recovery stopped" in msg
    assert "retry manually" in msg


def test_breaker_stops_ladder_and_surfaces_crash_loop_after_identical_failures():
    """Two consecutive identical exhausted attempts trip the breaker; the ladder does not park."""
    agent = _Agent()
    retry = TurnRetryState(primary_recovery_attempted=True)
    first, retry = _settle(agent, _Err(502, "Connection reset by peer"), retry=retry)
    assert first.action == "continue" and retry.auto_recovery_identical_failures == 1
    second, retry = _settle(agent, _Err(502, "Connection reset by peer"), retry=retry)
    assert second.action == "return"
    assert second.result["auto_recovery_stopped"] is True
    body = second.result["final_response"]
    assert "crash-looping" in body
    assert "2 times in a row" in body
    assert "Connection reset by peer" in body
    assert "retry manually" in body
    # Exactly one cycle was spent before the breaker stopped the loop.
    assert retry.auto_recovery_cycles_used == 1
    assert any("crash-looping" in s for s in agent.statuses)


def test_breaker_never_trips_when_an_attempt_received_tokens():
    agent = _Agent()
    agent._last_api_first_chunk_at = 1.0  # every attempt streamed a chunk then died
    retry = TurnRetryState(primary_recovery_attempted=True)
    for _ in range(6):
        _settle(agent, _Err(502, "down"), retry=retry)
    assert all("crash-looping" not in s for s in agent.statuses)
    assert retry.auto_recovery_identical_failures == 0


def test_breaker_never_trips_when_error_classes_differ():
    agent = _Agent()
    retry = TurnRetryState(primary_recovery_attempted=True)
    for err in (_Err(503, "down"), _Err(502, "bad gateway"), _Err(503, "down"), _Err(502, "bad gateway")):
        _settle(agent, err, retry=retry)
    assert all("crash-looping" not in s for s in agent.statuses)
    assert retry.auto_recovery_identical_failures == 1


def test_breaker_disabled_by_zero_limit_keeps_the_ladder_running():
    agent = _Agent(identical_limit=0)
    retry = TurnRetryState(primary_recovery_attempted=True)
    for _ in range(3):
        verdict, retry = _settle(agent, _Err(502, "down"), retry=retry)
    assert verdict.action == "continue"
    assert retry.auto_recovery_identical_failures == 3  # tracked, but never acted on
    assert all("crash-looping" not in s for s in agent.statuses)
