"""Tests for decode-only velocity + separate TTFT in the status-bar plumbing.

The status bar's ``↑ N t/s`` previously mixed time-to-first-token into the
throughput denominator (``sum(output_tokens) / sum(full_api_duration)``, where
full_api_duration includes TTFT). The velocity is now DECODE-ONLY: the shared
``_api_latency_history`` stores (full duration − TTFT), ``_api_full_latency_
history`` keeps the unmodified full-wall duration for the (still full-wall)
avg_latency readout, and ``_api_ttft_history`` records TTFT per streaming call.

These are the acceptance oracle assertions from the task spec.
"""
from collections import deque

from agent.conversation_loop import _append_status_history


class _HistAgent:
    """Minimal agent exposing the three rolling deques."""

    def __init__(self, maxlen=10):
        self._api_latency_history = deque(maxlen=maxlen)
        self._api_full_latency_history = deque(maxlen=maxlen)
        self._api_output_history = deque(maxlen=maxlen)
        self._api_ttft_history = deque(maxlen=maxlen)


def test_streaming_call_decode_only_latency_and_ttft():
    """Streaming call, first delta at t=2.0s, returns at t=10.0s."""
    agent = _HistAgent()
    _append_status_history(agent, api_duration=10.0, ttft_value=2.0, output_tokens=30)
    # Decode-only latency = full − TTFT.
    assert agent._api_latency_history[-1] == 8.0
    # Full-wall latency preserved separately for avg_latency.
    assert agent._api_full_latency_history[-1] == 10.0
    assert agent._api_ttft_history[-1] == 2.0


def test_non_streaming_call_full_wall_and_no_ttft():
    """Non-streaming / no on_first_delta fire => full wall, no TTFT appended."""
    agent = _HistAgent()
    _append_status_history(agent, api_duration=10.0, ttft_value=None, output_tokens=30)
    assert agent._api_latency_history[-1] == 10.0
    assert agent._api_full_latency_history[-1] == 10.0
    assert len(agent._api_ttft_history) == 0  # length does not grow


def test_clock_skew_guard_never_negative():
    """ttft > api_duration => latency append exactly 0.0, never negative."""
    agent = _HistAgent()
    _append_status_history(agent, api_duration=1.0, ttft_value=5.0, output_tokens=10)
    assert agent._api_latency_history[-1] == 0.0


def test_staleness_ttft_is_per_call_state():
    """TTFT must be per-call state, not a long-lived attribute.

    Call #1 streams (ttft=2.0); call #2 never fires on_first_delta. The second
    call must record full api_duration and must NOT reuse 2.0. The helper
    receives the ttft value per call — the staleness guarantee lives upstream
    in the per-attempt closure box reset before each API call.
    """
    agent = _HistAgent()
    # Call 1: streams, records ttft.
    _append_status_history(agent, api_duration=10.0, ttft_value=2.0, output_tokens=30)
    assert agent._api_ttft_history[-1] == 2.0
    assert agent._api_latency_history[-1] == 8.0
    # Call 2: non-streaming, ttft=None (fresh per-call closure box).
    _append_status_history(agent, api_duration=6.0, ttft_value=None, output_tokens=40)
    assert agent._api_latency_history[-1] == 6.0  # full wall, NOT 2.0 leaked
    assert agent._api_full_latency_history[-1] == 6.0
    # TTFT history length grew by exactly one (only the streamed call).
    assert len(agent._api_ttft_history) == 1
    assert agent._api_ttft_history[-1] == 2.0


def test_output_accounting_unchanged():
    """_api_output_history is byte-for-byte the appended output tokens."""
    agent = _HistAgent()
    _append_status_history(agent, api_duration=5.0, ttft_value=None, output_tokens=120)
    _append_status_history(agent, api_duration=3.0, ttft_value=1.0, output_tokens=80)
    assert list(agent._api_output_history) == [120, 80]


def test_zero_output_tokens_guarded():
    """Output token 0/None appends 0 without NaN or exceptions."""
    agent = _HistAgent()
    _append_status_history(agent, api_duration=2.0, ttft_value=None, output_tokens=0)
    _append_status_history(agent, api_duration=2.0, ttft_value=None, output_tokens=None)
    assert list(agent._api_output_history) == [0, 0]


def test_missing_history_attr_does_not_raise():
    """Agent missing the deques entirely must be a no-op."""
    import types
    bare = types.SimpleNamespace()
    _append_status_history(bare, api_duration=5.0, ttft_value=None, output_tokens=10)


# ---------------------------------------------------------------------------
# Wiring regression: the PRODUCTION call site must reach this helper.
#
# The v2026.9.14 merge (f6edb27b86) rewrote the fork's monolithic
# ``run_conversation`` onto upstream's ``turn_*`` modules and dropped this
# helper's only call site, leaving all four deques frozen — avg_latency and
# avg_ttft read ``None`` forever. These tests exercise
# ``record_response_usage`` (the anchor that replaced the old call site) so
# the wiring cannot silently regress again.
# ---------------------------------------------------------------------------

import time as _time
from types import SimpleNamespace as _NS


class _Compressor:
    context_length = 200_000
    max_tokens = 8000
    threshold_tokens = 190_000
    _verify_compaction_cleared_threshold = False
    _context_probed = False
    awaiting_real_usage_after_compression = False

    def update_from_response(self, _usage):
        pass


def _usage_agent():
    return _NS(
        _session_db=None, session_id=None, _session_db_created=True,
        model="m", provider="p", base_url="", api_mode="chat_completions", api_key="",
        session_api_calls=0, context_compressor=_Compressor(), quiet_mode=True,
        verbose_logging=False, client=None, _last_turn_usage=None,
        _last_prompt_size_tokens=0, _ensure_db_session=lambda: None,
        _vprint=lambda *a, **k: None, _safe_print=lambda *a, **k: None,
        log_prefix="", _current_streamed_assistant_text="",
        session_prompt_tokens=0, session_completion_tokens=0, session_total_tokens=0,
        session_input_tokens=0, session_output_tokens=0, session_cache_read_tokens=0,
        session_cache_write_tokens=0, session_reasoning_tokens=0,
        session_estimated_cost_usd=0.0, session_cost_status="unknown",
        session_cost_source="none",
        _api_latency_history=deque(maxlen=10),
        _api_full_latency_history=deque(maxlen=10),
        _api_output_history=deque(maxlen=10),
        _api_ttft_history=deque(maxlen=10),
    )


def _usage_response():
    return _NS(
        usage=_NS(prompt_tokens=100, completion_tokens=30, total_tokens=130,
                  prompt_tokens_details=None, completion_tokens_details=None),
        id="probe", provider=None, model="m",
    )


def test_record_response_usage_writes_all_four_deques():
    """Streaming call: first chunk 2.0s before the call ended, 10.0s wall.

    Mirrors the reader math in ``cli.py`` avg_latency / avg_velocity / avg_ttft.
    """
    from agent.turn_usage import record_response_usage

    agent = _usage_agent()
    agent._last_api_first_chunk_at = _time.time() - 2.0
    record_response_usage(
        agent, _usage_response(), messages=[{"role": "user", "content": "hi"}],
        api_call_count=1, api_duration=10.0, compression_attempts=0, max_compression_attempts=3,
    )
    assert abs(agent._api_full_latency_history[-1] - 10.0) < 1e-6
    assert abs(agent._api_latency_history[-1] - 2.0) < 0.5  # decode-only = 10 − ~8
    assert agent._api_output_history[-1] == 30
    assert len(agent._api_ttft_history) == 1
    assert abs(agent._api_ttft_history[-1] - 8.0) < 0.5
    # The readers are no longer starved: avg_latency / avg_ttft resolve.
    assert agent._api_full_latency_history[-1] is not None
    assert sum(agent._api_ttft_history) / len(agent._api_ttft_history) is not None


def test_record_response_usage_non_streaming_records_full_wall_and_no_ttft():
    """No first-chunk stamp (non-streaming / no delta) => full wall, TTFT skipped."""
    from agent.turn_usage import record_response_usage

    agent = _usage_agent()
    agent._last_api_first_chunk_at = None
    record_response_usage(
        agent, _usage_response(), messages=[{"role": "user", "content": "hi"}],
        api_call_count=1, api_duration=6.0, compression_attempts=0, max_compression_attempts=3,
    )
    assert agent._api_latency_history[-1] == 6.0
    assert agent._api_full_latency_history[-1] == 6.0
    assert len(agent._api_ttft_history) == 0
    assert agent._api_output_history[-1] == 30


def test_record_response_usage_without_deques_does_not_raise():
    """A minimal agent surface (no deque attrs) must not break the usage fold."""
    from agent.turn_usage import record_response_usage

    agent = _usage_agent()
    for name in ("_api_latency_history", "_api_full_latency_history",
                 "_api_output_history", "_api_ttft_history"):
        delattr(agent, name)
    record_response_usage(
        agent, _usage_response(), messages=[{"role": "user", "content": "hi"}],
        api_call_count=1, api_duration=1.0, compression_attempts=0, max_compression_attempts=3,
    )

