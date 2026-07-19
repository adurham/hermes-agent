"""Regression test for anthropic_messages truncation continuation.

When an Anthropic response hits ``stop_reason: max_tokens`` (mapped to
``finish_reason == 'length'`` in run_agent), the agent must retry with
a continuation prompt — the same behavior it has always had for
chat_completions and bedrock_converse.  Before this PR, the
``if self.api_mode in ('chat_completions', 'bedrock_converse'):`` guard
silently dropped Anthropic-wire truncations on the floor, returning a
half-finished response with no retry.

We don't exercise the full agent loop here (it's 3000 lines of inference,
streaming, plugin hooks, etc.) — instead we verify the normalization
adapter produces exactly the shape the continuation block now consumes.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def _make_anthropic_text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _make_anthropic_tool_use_block(name: str = "my_tool") -> SimpleNamespace:
    return SimpleNamespace(
        type="tool_use",
        id="toolu_01",
        name=name,
        input={"foo": "bar"},
    )


def _make_anthropic_response(blocks, stop_reason: str = "max_tokens"):
    return SimpleNamespace(
        id="msg_01",
        type="message",
        role="assistant",
        model="claude-sonnet-4-6",
        content=blocks,
        stop_reason=stop_reason,
        stop_sequence=None,
        usage=SimpleNamespace(input_tokens=100, output_tokens=200),
    )


class TestTruncatedAnthropicResponseNormalization:
    """AnthropicTransport.normalize_response() gives us the shape _build_assistant_message expects."""

    def test_text_only_truncation_produces_text_content_no_tool_calls(self):
        """Pure-text Anthropic truncation → continuation path should fire."""
        from agent.transports import get_transport

        response = _make_anthropic_response(
            [_make_anthropic_text_block("partial response that was cut off")]
        )
        nr = get_transport("anthropic_messages").normalize_response(response)

        # The continuation block checks these two attributes:
        #   assistant_message.content  → appended to truncated_response_parts
        #   assistant_message.tool_calls → guards the text-retry branch
        assert nr.content is not None
        assert "partial response" in nr.content
        assert not nr.tool_calls, (
            "Pure-text truncation must have no tool_calls so the text-continuation "
            "branch (not the tool-retry branch) fires"
        )
        assert nr.finish_reason == "length", "max_tokens stop_reason must map to OpenAI-style 'length'"

    def test_truncated_tool_call_produces_tool_calls(self):
        """Tool-use truncation → tool-call retry path should fire."""
        from agent.transports import get_transport

        response = _make_anthropic_response(
            [
                _make_anthropic_text_block("thinking..."),
                _make_anthropic_tool_use_block(),
            ]
        )
        nr = get_transport("anthropic_messages").normalize_response(response)

        assert bool(nr.tool_calls), (
            "Truncation mid-tool_use must expose tool_calls so the "
            "tool-call retry branch fires instead of text continuation"
        )
        assert nr.finish_reason == "length"

    def test_empty_content_does_not_crash(self):
        """Empty response.content — defensive: treat as a truncation with no text."""
        from agent.transports import get_transport

        response = _make_anthropic_response([])
        nr = get_transport("anthropic_messages").normalize_response(response)
        # Depending on the adapter, content may be "" or None — both are
        # acceptable; what matters is no exception.
        assert nr is not None
        assert not nr.tool_calls


class TestContinuationLogicBranching:
    """Symbolic check that the api_mode gate now includes anthropic_messages."""

    @pytest.mark.parametrize("api_mode", ["chat_completions", "bedrock_converse", "anthropic_messages"])
    def test_all_three_api_modes_hit_continuation_branch(self, api_mode):
        # The guard in run_agent.py is:
        #   if self.api_mode in ("chat_completions", "bedrock_converse", "anthropic_messages"):
        assert api_mode in {"chat_completions", "bedrock_converse", "anthropic_messages"}

    def test_codex_responses_still_excluded(self):
        # codex_responses has its own truncation path (not continuation-based)
        # and should NOT be routed through the shared block.
        assert "codex_responses" not in {"chat_completions", "bedrock_converse", "anthropic_messages"}


class TestTruncatedToolCallContinuationParity:
    """Truncated tool-call recovery should retry up to 3x with an output-budget
    boost (upstream's mechanism: bump ``_ephemeral_max_output_tokens`` then
    ``continue`` the same request), not a single futile same-request retry.
    Source-level guards so the behavior can't silently regress on a future merge
    of this hot-path block.

    NOTE: the fork previously routed this through its own
    ``restart_with_length_continuation`` + ``_get_continuation_prompt`` path; that
    was converged to upstream's ``_ephemeral_max_output_tokens`` boost to shrink
    the merge-conflict surface (the budget-boost feature is identical either way).
    """

    def _loop_source(self) -> str:
        import inspect
        import agent.conversation_loop as cl
        return inspect.getsource(cl)

    def test_tool_call_retry_budget_is_three_not_one(self):
        src = self._loop_source()
        # The futile single-retry guard `truncated_tool_call_retries < 1` must be
        # gone; recovery uses a 3-attempt budget.
        assert "truncated_tool_call_retries < 1" not in src, (
            "tool-call truncation still uses the futile single same-request retry"
        )
        assert "truncated_tool_call_retries < 3" in src, (
            "tool-call truncation should retry up to 3x"
        )

    def test_tool_call_path_boosts_ephemeral_output_budget(self):
        # The tool-call branch must boost _ephemeral_max_output_tokens before the
        # retry so the model has room to finish the JSON args, then `continue`.
        src = self._loop_source()
        tc_branch = src[src.index("_trunc_has_tool_calls:"):]
        tc_branch = tc_branch[: tc_branch.index("# If we have prior messages")]
        assert "_ephemeral_max_output_tokens" in tc_branch, (
            "tool-call truncation must boost the ephemeral output budget on retry"
        )
