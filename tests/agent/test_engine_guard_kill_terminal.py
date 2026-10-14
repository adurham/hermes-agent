"""Item C regression: an engine guard-kill must END the turn, not be re-sent.

The serving engine can stop a generation itself and say so INSIDE the SSE body (exo, after its
degeneration guard fires — ``exo/src/exo/api/adapters/chat_completions.py`` yields an
``ErrorResponse`` and ``data: [DONE]``):

    data: {"error": {"message": "Generation terminated: repetition-loop degeneration detected …",
                     "type": "InternalServerError", "code": 500}}

Read as an ordinary drop, that frame produced a "length" partial stub and the turn loop
auto-continued up to ~4x, each retry walking straight back into the same degenerate state. The
contract pinned here:

* the stream layer PRESERVES the in-band typed error (nested ``error`` object, plus the flat
  ``error_type``/``error_message`` shape #65631 already handled) instead of silently dropping it;
* the classifier pins it ``generation_terminated``: non-retryable, no fallback, and a future
  engine stamping ``error.code == "repetition_loop"`` is accepted too;
* the truncation / auto-continue path consults that classification and ends the turn — partial
  content KEPT and marked, the engine's own message carried as the turn's error (so a delegated
  child reports the guard-kill instead of a bare "truncated" stub).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_constants import PARTIAL_STREAM_STUB_ID

GUARD_KILL_MESSAGE = (
    "Generation terminated: repetition-loop degeneration detected "
    "(decoded 96 identical tokens in the last 512)"
)
PARTIAL_TEXT = "I'll add a retry helper that I'll add a retry helper that I'll add a retry "


def _make_agent():
    from run_agent import AIAgent

    agent = AIAgent(
        api_key="test-key",
        base_url="https://example.com/v1",
        model="test/model",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    agent.api_mode = "chat_completions"
    agent._interrupt_requested = False
    return agent


def _content_chunk(content):
    delta = SimpleNamespace(content=content, tool_calls=None, reasoning_content=None, reasoning=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(index=0, delta=delta, finish_reason=None)], model=None, usage=None)


def _guard_kill_chunk():
    """exo's guard-kill frame: no ``choices``, a nested OpenAI-shaped ``error`` object."""
    return SimpleNamespace(
        choices=[], model=None, usage=None,
        error={"message": GUARD_KILL_MESSAGE, "type": "InternalServerError", "code": 500},
    )


def _guard_kill_stub():
    """Run the real streaming builder over a guard-killed stream (wire-level fixture)."""
    def _stream():
        yield _content_chunk(PARTIAL_TEXT)
        yield _guard_kill_chunk()

    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = lambda *a, **kw: _stream()
    with patch("run_agent.AIAgent._create_request_openai_client", return_value=mock_client), \
            patch("run_agent.AIAgent._close_request_openai_client"):
        import os

        os.environ["HERMES_STREAM_RETRIES"] = "0"
        agent = _make_agent()
        agent._current_streamed_assistant_text = PARTIAL_TEXT
        return agent, agent._interruptible_streaming_api_call({})


# ── Stream layer: the typed error survives, the partial survives ───────────


class TestGuardKillStreamLayer:
    def test_guard_kill_frame_is_read_not_swallowed(self, monkeypatch):
        monkeypatch.setenv("HERMES_STREAM_RETRIES", "0")
        _agent, response = _guard_kill_stub()

        assert response.id == PARTIAL_STREAM_STUB_ID
        # Preserved in-band typed error, not a bare "empty stream"/"drop".
        err = getattr(response, "_provider_stream_error", None)
        assert err is not None, "the engine's typed in-band error must be preserved"
        assert err.status_code == 500
        assert "repetition-loop degeneration" in str(err.body)
        # Marked terminal, and the partial the model already delivered is KEPT.
        assert getattr(response, "_engine_guard_kill", False) is True
        assert response.choices[0].message.content == PARTIAL_TEXT

    def test_guard_kill_with_no_content_is_not_an_empty_stream_error(self, monkeypatch):
        """A kill on the very first frame must not become EmptyStreamError + retries."""
        monkeypatch.setenv("HERMES_STREAM_RETRIES", "0")

        def _stream():
            yield _guard_kill_chunk()

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = lambda *a, **kw: _stream()
        with patch("run_agent.AIAgent._create_request_openai_client", return_value=mock_client), \
                patch("run_agent.AIAgent._close_request_openai_client"):
            agent = _make_agent()
            response = agent._interruptible_streaming_api_call({})

        assert getattr(response, "_engine_guard_kill", False) is True
        assert response.choices[0].message.content is None


def test_flat_in_stream_error_shape_still_raises():
    """#65631's DeepInfra shape keeps its existing contract (raise, not consume)."""
    from agent.chat_completion_helpers import (
        ProviderStreamError, _in_stream_error_payload, _StreamingCall)

    flat = SimpleNamespace(choices=[], error_type="invalid_request_error",
                           error_message="request validation failed: bad tool schema")
    assert _in_stream_error_payload(flat)["code"] == "invalid_request_error"
    with pytest.raises(ProviderStreamError):
        _StreamingCall._choiceless_chunk(flat, None)


def test_nested_non_guard_kill_error_still_raises():
    from agent.chat_completion_helpers import ProviderStreamError, _StreamingCall

    other = SimpleNamespace(choices=[], error={"message": "upstream relay is out of capacity",
                                              "type": "Overloaded", "code": 503})
    with pytest.raises(ProviderStreamError):
        _StreamingCall._choiceless_chunk(other, None)


def test_usage_only_choiceless_chunk_is_unaffected():
    """The Nous Portal ``lastOne`` clean-terminal path must keep working."""
    from agent.chat_completion_helpers import _in_stream_error_payload, _StreamingCall

    chunk = SimpleNamespace(choices=[], model=None, usage=None, lastOne=True, model_extra=None)
    assert _in_stream_error_payload(chunk) is None
    usage, finish_reason = _StreamingCall._choiceless_chunk(chunk, None)
    assert (usage, finish_reason) == (None, "stop")


# ── Classifier: non-retryable, non-continuable, future-code-ready ──────────


class TestGuardKillClassification:
    def _classify(self, message, code):
        from agent.chat_completion_helpers import ProviderStreamError, _provider_error_body
        from agent.error_classifier import classify_api_error

        body = _provider_error_body({"code": code, "message": message, "type": "InternalServerError"}, 500)
        return classify_api_error(
            ProviderStreamError(status_code=500, body=body, raw_text=message),
            provider="exo", model="dsv41")

    def test_guard_kill_is_terminal(self):
        from agent.error_classifier import FailoverReason

        classified = self._classify(GUARD_KILL_MESSAGE, 500)
        assert classified.reason is FailoverReason.generation_terminated
        assert classified.retryable is False
        assert classified.should_fallback is False
        assert classified.should_compress is False

    def test_future_structured_code_is_accepted(self):
        """A later engine may stamp ``error.code = "repetition_loop"`` instead of prose."""
        from agent.error_classifier import FailoverReason, is_generation_terminated_error

        assert is_generation_terminated_error("", "repetition_loop") is True
        classified = self._classify("Generation terminated", "repetition_loop")
        assert classified.reason is FailoverReason.generation_terminated
        assert classified.retryable is False

    def test_status_number_alone_is_not_a_guard_kill(self):
        """A generic 500 must keep its retryable server_error classification."""
        from agent.error_classifier import FailoverReason

        classified = self._classify("internal server error", 500)
        assert classified.reason is FailoverReason.server_error


# ── Truncation / auto-continue path: consult it, end the turn ──────────────


class TestGuardKillEndsTheTurn:
    def test_stub_ends_turn_with_marked_partial_and_clear_error(self, monkeypatch):
        from agent.turn_truncation import _GUARD_KILL_PARTIAL_FINAL, recover_from_truncation

        monkeypatch.setenv("HERMES_STREAM_RETRIES", "0")
        agent, response = _guard_kill_stub()
        assert getattr(response, "_engine_guard_kill", False) is True

        messages = [{"role": "user", "content": "write the retry helper"}]
        verdict = recover_from_truncation(
            agent, response, "length", MagicMock(),
            messages=messages, conversation_history=None, api_kwargs={},
            api_call_count=1, effective_task_id=None, current_turn_user_idx=0,
            length_continue_retries=0, truncated_response_parts=[],
            truncated_tool_call_retries=0, retry_count=0, compression_attempts=0,
        )

        # Terminal: no continuation was seeded (the bug auto-continued up to 4x).
        assert verdict.action == "return"
        assert verdict.result.get("failed") is True
        assert verdict.result.get("final_response") == _GUARD_KILL_PARTIAL_FINAL
        # …and the classification it consulted is on the verdict.
        assert verdict.result.get("failure_reason") == "generation_terminated"
        assert verdict.result.get("failure_retryable") is False
        # The engine's own message is the turn error — a CHILD reports this, not a bare stub.
        assert "repetition-loop degeneration" in verdict.result.get("error", "")
        # Partial content KEPT and marked, never presented as the answer.
        partial_rows = [m for m in messages if isinstance(m, dict) and m.get("_engine_guard_kill")]
        assert len(partial_rows) == 1, messages
        assert partial_rows[0]["content"] == PARTIAL_TEXT.strip()
        assert partial_rows[0]["_truncated_partial"] is True
        assert partial_rows[0]["role"] == "assistant"
        # No continuation fragment/nudge was appended for this turn.
        assert not [m for m in messages
                    if isinstance(m, dict) and m.get("_length_continuation_fragment")]

    def test_non_retryable_check_gates_the_terminal_path(self, monkeypatch):
        """The path CONSULTS the classification: a retryable verdict must NOT be swallowed by
        the terminal branch. Forced here by stripping the guard-kill wording off the preserved
        error, which leaves a plain retryable 500 -> the normal continuation machinery runs."""
        from agent.chat_completion_helpers import ProviderStreamError, _provider_error_body
        from agent.turn_truncation import _GUARD_KILL_PARTIAL_FINAL, recover_from_truncation

        monkeypatch.setenv("HERMES_STREAM_RETRIES", "0")
        agent, response = _guard_kill_stub()
        # Same terminal marker, but the preserved error no longer names a guard-kill.
        response._provider_stream_error = ProviderStreamError(
            status_code=500, body=_provider_error_body({"message": "internal server error"}, 500),
            raw_text="internal server error")

        verdict = recover_from_truncation(
            agent, response, "length", MagicMock(),
            messages=[{"role": "user", "content": "go"}], conversation_history=None, api_kwargs={},
            api_call_count=1, effective_task_id=None, current_turn_user_idx=0,
            length_continue_retries=0, truncated_response_parts=[],
            truncated_tool_call_retries=0, retry_count=0, compression_attempts=0,
        )
        assert verdict.result is None or verdict.result.get("final_response") != _GUARD_KILL_PARTIAL_FINAL


# ── Stream-retry layer: the exact shape openai's SDK raises in production ──


def _sdk_guard_kill_error():
    """The error openai's SDK raises for exo's in-band guard-kill frame.

    openai >= 2.x ``_streaming.py`` raises on ANY SSE data frame whose JSON has a truthy
    ``error`` key (``data.get("error")``) BEFORE the client's chunk loop can consume it — so in
    production the kill arrives as a RAISE, not as the chunk the wire-level fixtures above feed.
    That raise is what the tests below pin.
    """
    import httpx
    from openai import APIError

    return APIError(
        message=GUARD_KILL_MESSAGE,
        request=httpx.Request("POST", "http://macstudio-tailnet:52415/v1/chat/completions"),
        body={"message": GUARD_KILL_MESSAGE, "type": "InternalServerError", "code": "repetition_loop"},
    )


class TestSdkRaisedGuardKillIsTerminal:
    """Live 2026-10-14 (exo dsv41): the SDK-raised guard-kill fell through _handle_stream_error's
    transient branch — its message's "Generation terminated" matched _SSE_CONN_PHRASES' bare
    "terminated" — and the stream retried the SAME degenerate generation three times
    (byte-identical: 69706 bytes / 1613 chunks / ~51s each) before the turn ended anyway."""

    def test_raised_guard_kill_is_terminal_no_re_stream(self, monkeypatch):
        monkeypatch.setenv("HERMES_STREAM_RETRIES", "2")
        err = _sdk_guard_kill_error()
        attempts = {"n": 0}

        def _stream():
            attempts["n"] += 1
            yield _content_chunk("checking pve02 unsafe shutdowns …")
            raise err

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = lambda *a, **kw: _stream()
        with patch("run_agent.AIAgent._create_request_openai_client", return_value=mock_client), \
                patch("run_agent.AIAgent._close_request_openai_client"):
            agent = _make_agent()
            response = agent._interruptible_streaming_api_call({})

        assert attempts["n"] == 1, "a guard-kill is deterministic — re-streaming re-runs the same decode"
        assert response.id == PARTIAL_STREAM_STUB_ID
        assert getattr(response, "_engine_guard_kill", False) is True
        preserved = getattr(response, "_provider_stream_error", None)
        assert preserved is not None
        assert "repetition-loop degeneration" in str(preserved.body)

    def test_raised_guard_kill_with_nothing_streamed_raises_after_one_attempt(self, monkeypatch):
        monkeypatch.setenv("HERMES_STREAM_RETRIES", "2")
        err = _sdk_guard_kill_error()
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = err
        with patch("run_agent.AIAgent._create_request_openai_client", return_value=mock_client), \
                patch("run_agent.AIAgent._close_request_openai_client"):
            agent = _make_agent()
            with pytest.raises(Exception) as excinfo:
                agent._interruptible_streaming_api_call({})
        assert "repetition-loop degeneration" in str(excinfo.value)
        assert mock_client.chat.completions.create.call_count == 1


def test_sse_phrase_check_no_longer_matches_guard_kill_prose():
    from agent.chat_completion_helpers import _is_sse_connection_error

    assert _is_sse_connection_error(_sdk_guard_kill_error()) is False


def test_sse_phrase_check_still_matches_relay_transport_drops():
    import httpx
    from openai import APIError

    from agent.chat_completion_helpers import _is_sse_connection_error

    relay = APIError(
        message="Network connection lost.",
        request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
        body={"message": "Network connection lost."})
    assert _is_sse_connection_error(relay) is True


# ── Exhausted-transient wording: reachable-but-dying ≠ could-not-connect ────


def test_exhausted_notice_distinguishes_midstream_from_connect_failure():
    from agent.stream_diag import connect_exhausted_notice

    err = _sdk_guard_kill_error()
    mid = connect_exhausted_notice(err, attempts=3, base_url="http://macstudio-tailnet:52415/v1",
                                   diag={"chunks": 1613, "bytes": 69706})
    assert "Could not open a stream" not in mid
    assert "mid-response" in mid and "68 KB" in mid

    never_opened = connect_exhausted_notice(err, attempts=3, base_url="http://macstudio-tailnet:52415/v1",
                                            diag={"chunks": 0, "bytes": 0})
    assert "Could not open a stream" in never_opened

    no_diag = connect_exhausted_notice(err, attempts=3, base_url="http://macstudio-tailnet:52415/v1")
    assert "Could not open a stream" in no_diag
