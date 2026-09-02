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
