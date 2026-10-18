"""The exo dsv41 loop cause: a truncation that must END the turn, not continue it."""

from __future__ import annotations

from agent.chat_completion_helpers_relay import RelayChatAccumulator
from agent.transports.types import NormalizedResponse

CAUSE = {"finish_cause": "repetition_loop", "cycle_period": 2, "dropped_tokens": 7, "retryable": False}


def test_normalized_response_carries_the_loop_cause():
    r = NormalizedResponse(content="0 0 0", tool_calls=None, finish_reason="length", loop_cause=CAUSE)
    assert r.loop_cause == CAUSE


def test_stream_accumulator_keeps_the_extension_on_the_final_chunk():
    acc = RelayChatAccumulator()
    acc.observe({"model": "m", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "0 0 0"}}]})
    acc.observe({"model": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "length", "dsv41": CAUSE}]})
    out = acc.finalize()
    assert out["choices"][0]["finish_reason"] == "length"
    assert out["choices"][0]["dsv41"] == CAUSE


def test_stream_accumulator_adds_no_extension_without_one():
    acc = RelayChatAccumulator()
    acc.observe({"model": "m", "choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": "stop"}]})
    assert "dsv41" not in acc.finalize()["choices"][0]


def test_engine_loop_cause_recognizes_the_truncation():
    from agent.turn_truncation import _engine_loop_cause

    class _Transport:
        def normalize_response(self, response):
            return NormalizedResponse(
                content="x", tool_calls=None, finish_reason="length", loop_cause=CAUSE
            )

    class _Agent:
        def _get_transport(self):
            return _Transport()

    assert _engine_loop_cause(_Agent(), object()) == CAUSE


def test_engine_loop_cause_ignores_an_ordinary_length_cut():
    from agent.turn_truncation import _engine_loop_cause

    class _Transport:
        def normalize_response(self, response):
            return NormalizedResponse(content="x", tool_calls=None, finish_reason="length")

    class _Agent:
        def _get_transport(self):
            return _Transport()

    assert _engine_loop_cause(_Agent(), object()) is None


def test_engine_loop_cause_is_defensive():
    from agent.turn_truncation import _engine_loop_cause

    class _Agent:
        def _get_transport(self):
            raise RuntimeError("no transport")

    assert _engine_loop_cause(_Agent(), object()) is None
