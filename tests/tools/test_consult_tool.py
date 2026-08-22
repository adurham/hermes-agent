"""Tests for the consult tool (second opinion from a reference model).

``consult`` wraps ``agent.auxiliary_client.call_llm(task="consult", ...)``.
Refusals / empty responses / call failures from the reference model must
degrade gracefully to ``{"unavailable": true, ...}`` rather than raising —
that's the whole point of the feature (Fable-class frontier models refuse
often enough that a hard failure here would make the tool useless).
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from tools.consult_tool import consult_tool, MAX_CONTEXT_CHARS, MAX_QUESTION_CHARS


def _fake_response(content="", finish_reason="stop"):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    resp.choices[0].message.reasoning = None
    resp.choices[0].message.reasoning_content = None
    resp.choices[0].message.reasoning_details = None
    resp.choices[0].finish_reason = finish_reason
    return resp


class TestConsultToolSuccess:
    def test_returns_answer_on_success(self):
        resp = _fake_response("Looks sound; watch the race on shutdown.")
        with patch("agent.auxiliary_client.call_llm", return_value=resp):
            result = json.loads(consult_tool("Is this plan sound?"))
        assert result["unavailable"] is False
        assert result["answer"] == "Looks sound; watch the race on shutdown."

    def test_passes_task_consult_to_call_llm(self):
        resp = _fake_response("ok")
        with patch("agent.auxiliary_client.call_llm", return_value=resp) as mock_call:
            consult_tool("Is this plan sound?")
        assert mock_call.call_args.kwargs.get("task") == "consult"

    def test_includes_context_in_user_message(self):
        resp = _fake_response("ok")
        with patch("agent.auxiliary_client.call_llm", return_value=resp) as mock_call:
            consult_tool("Is this plan sound?", context="def foo(): pass")
        messages = mock_call.call_args.kwargs.get("messages")
        user_msg = next(m for m in messages if m["role"] == "user")
        assert "def foo(): pass" in user_msg["content"]
        assert "Is this plan sound?" in user_msg["content"]

    def test_no_context_omits_separator(self):
        resp = _fake_response("ok")
        with patch("agent.auxiliary_client.call_llm", return_value=resp) as mock_call:
            consult_tool("Is this plan sound?")
        messages = mock_call.call_args.kwargs.get("messages")
        user_msg = next(m for m in messages if m["role"] == "user")
        assert user_msg["content"] == "Is this plan sound?"

    def test_strips_whitespace_from_answer(self):
        resp = _fake_response("  padded answer  \n")
        with patch("agent.auxiliary_client.call_llm", return_value=resp):
            result = json.loads(consult_tool("q"))
        assert result["answer"] == "padded answer"


class TestConsultToolGracefulDegradation:
    """The core design requirement: refusal/failure != exception."""

    def test_empty_content_is_unavailable_not_error(self):
        resp = _fake_response("")
        with patch("agent.auxiliary_client.call_llm", return_value=resp):
            result = json.loads(consult_tool("q"))
        assert result["unavailable"] is True
        assert result["answer"] is None
        assert "reason" in result and result["reason"]

    def test_whitespace_only_content_is_unavailable(self):
        resp = _fake_response("   \n\t  ")
        with patch("agent.auxiliary_client.call_llm", return_value=resp):
            result = json.loads(consult_tool("q"))
        assert result["unavailable"] is True

    def test_content_filter_finish_reason_is_unavailable(self):
        # Even if content is non-empty, a content_filter finish reason
        # (Anthropic-native refusal mapping) means "don't trust this".
        resp = _fake_response("partial thing", finish_reason="content_filter")
        with patch("agent.auxiliary_client.call_llm", return_value=resp):
            result = json.loads(consult_tool("q"))
        assert result["unavailable"] is True

    def test_call_llm_exception_is_unavailable_not_raised(self):
        with patch(
            "agent.auxiliary_client.call_llm",
            side_effect=RuntimeError("no provider configured"),
        ):
            result = json.loads(consult_tool("q"))
        assert result["unavailable"] is True
        assert result["answer"] is None
        assert "no provider configured" in result["reason"]

    def test_malformed_response_object_is_unavailable(self):
        # A response missing .choices entirely must not crash the tool.
        with patch("agent.auxiliary_client.call_llm", return_value=object()):
            result = json.loads(consult_tool("q"))
        assert result["unavailable"] is True


class TestConsultToolElisionGuard:
    """The calling model sometimes writes a literal placeholder like
    "[truncated]" or "(see above)" into question/context instead of the
    real content (observed 2026-08-22: a well-under-cap ~2000-char context
    had real early options followed by a literal "...[truncated]" marker
    covering the rest). This must be caught BEFORE the reference-model call
    fires -- it's the calling model's own laziness, not a length overflow,
    and burning a real (often 25-60s) aux call on known-incomplete input is
    pure waste."""

    def test_question_with_elision_marker_is_rejected_before_call(self):
        with patch("agent.auxiliary_client.call_llm") as mock_call:
            result = json.loads(
                consult_tool("Options: (1) foo (2) bar...[truncated]")
            )
        mock_call.assert_not_called()
        assert result["unavailable"] is True
        assert result["answer"] is None
        assert "placeholder" in result["reason"]

    def test_context_with_elision_marker_is_rejected_before_call(self):
        with patch("agent.auxiliary_client.call_llm") as mock_call:
            result = json.loads(
                consult_tool(
                    "Is this plan sound?",
                    context="Baseline numbers: 100K=... [rest omitted]",
                )
            )
        mock_call.assert_not_called()
        assert result["unavailable"] is True

    def test_see_above_placeholder_is_rejected(self):
        with patch("agent.auxiliary_client.call_llm") as mock_call:
            result = json.loads(
                consult_tool("q", context="full details (see above)")
            )
        mock_call.assert_not_called()
        assert result["unavailable"] is True

    def test_normal_ellipsis_is_not_flagged(self):
        # A bare "..." (e.g. mid-sentence trailing off) must NOT trip the
        # guard -- only the specific bracketed/parenthesized placeholders
        # the calling model actually emits.
        resp = _fake_response("ok")
        with patch("agent.auxiliary_client.call_llm", return_value=resp) as mock_call:
            result = json.loads(
                consult_tool("Should we proceed with plan A or B...?")
            )
        mock_call.assert_called_once()
        assert result["unavailable"] is False

    def test_elision_guard_runs_before_length_cap_truncation(self):
        # The length-cap truncation path (MAX_QUESTION_CHARS) appends its
        # own "...(truncated)" marker -- confirm the elision guard doesn't
        # spuriously fire on THAT synthetic marker for an otherwise-clean
        # long question (it must only catch markers the caller itself
        # wrote, checked before the cap is applied).
        resp = _fake_response("ok")
        long_q = "x" * (MAX_QUESTION_CHARS + 500)
        with patch("agent.auxiliary_client.call_llm", return_value=resp) as mock_call:
            result = json.loads(consult_tool(long_q))
        mock_call.assert_called_once()
        assert result["unavailable"] is False


class TestConsultToolValidation:
    def test_empty_question_is_error(self):
        result = json.loads(consult_tool(""))
        assert "error" in result

    def test_whitespace_only_question_is_error(self):
        result = json.loads(consult_tool("   "))
        assert "error" in result

    def test_none_question_is_error(self):
        result = json.loads(consult_tool(None))
        assert "error" in result

    def test_long_question_is_truncated(self):
        resp = _fake_response("ok")
        long_q = "x" * (MAX_QUESTION_CHARS + 500)
        with patch("agent.auxiliary_client.call_llm", return_value=resp) as mock_call:
            consult_tool(long_q)
        messages = mock_call.call_args.kwargs.get("messages")
        user_msg = next(m for m in messages if m["role"] == "user")
        assert len(user_msg["content"]) < len(long_q)
        assert "truncated" in user_msg["content"]

    def test_long_context_is_truncated(self):
        resp = _fake_response("ok")
        long_ctx = "y" * (MAX_CONTEXT_CHARS + 500)
        with patch("agent.auxiliary_client.call_llm", return_value=resp) as mock_call:
            consult_tool("q", context=long_ctx)
        messages = mock_call.call_args.kwargs.get("messages")
        user_msg = next(m for m in messages if m["role"] == "user")
        assert len(user_msg["content"]) < len(long_ctx) + len("q") + 50
        assert "truncated" in user_msg["content"]


class TestConsultToolRegistration:
    def test_registered_in_registry(self):
        import model_tools  # noqa: F401  (triggers discover_builtin_tools)
        from tools.registry import registry

        entry = registry.get_entry("consult")
        assert entry is not None
        assert entry.toolset == "consult"
        assert entry.schema["name"] == "consult"
        assert "question" in entry.schema["parameters"]["properties"]
        assert entry.schema["parameters"]["required"] == ["question"]

    def test_dispatch_through_registry(self):
        import model_tools  # noqa: F401
        from tools.registry import registry

        resp = _fake_response("dispatched ok")
        with patch("agent.auxiliary_client.call_llm", return_value=resp):
            result = json.loads(
                registry.dispatch("consult", {"question": "q"})
            )
        assert result["unavailable"] is False
        assert result["answer"] == "dispatched ok"
