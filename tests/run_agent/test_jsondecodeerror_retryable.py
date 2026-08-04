"""Regression guard for #14782: json.JSONDecodeError must not be classified
as a local validation error by the main agent loop.

`json.JSONDecodeError` inherits from `ValueError`. The agent loop's
non-retryable classifier at run_agent.py treats `ValueError` / `TypeError`
as local programming bugs and skips retry. Without an explicit carve-out,
a transient provider hiccup (malformed response body, truncated stream,
routing-layer corruption) that surfaces as a JSONDecodeError would bypass
the retry path and fail the turn immediately.

This test mirrors the exact predicate shape used in run_agent.py so that
any future refactor of that predicate must preserve the invariant:

    JSONDecodeError     → NOT local validation error (retryable)
    UnicodeEncodeError  → NOT local validation error (surrogate path)
    bare ValueError     → IS local validation error (programming bug)
    bare TypeError      → IS local validation error (programming bug)
"""
from __future__ import annotations

import json


def _mirror_agent_predicate(err: BaseException) -> bool:
    """Exact shape of run_agent.py's is_local_validation_error check.

    Kept in lock-step with the source. If you change one, change both —
    or, better, refactor the check into a shared helper and have both
    sites import it.
    """
    import ssl

    return (
        isinstance(err, (ValueError, TypeError))
        and not isinstance(err, (UnicodeEncodeError, json.JSONDecodeError))
        and not isinstance(err, ssl.SSLError)
        # NoneType-is-not-iterable shape errors come from upstream SDK /
        # provider response mismatches, not local programming bugs. See
        # the agent/conversation_loop.py inline comment for #33136.
        and not (
            isinstance(err, TypeError)
            and "nonetype" in str(err).lower()
            and "not iterable" in str(err).lower()
        )
        # The Anthropic SDK re-raises a bare ValueError when the model
        # emits malformed tool-call JSON in a streamed input_json_delta.
        # Same class as JSONDecodeError (corrupt wire bytes, retryable) but
        # surfaced without a specific type, so match on the message. See
        # the agent/conversation_loop.py inline comment for #39021.
        and "unable to parse tool parameter json" not in str(err).lower()
    )


class TestJSONDecodeErrorIsRetryable:

    def test_json_decode_error_is_not_local_validation(self):
        """Provider returning malformed JSON surfaces as JSONDecodeError —
        must be treated as transient so the retry path runs."""
        try:
            json.loads("{not valid json")
        except json.JSONDecodeError as exc:
            assert not _mirror_agent_predicate(exc), (
                "json.JSONDecodeError must be excluded from the "
                "ValueError/TypeError local-validation classification."
            )
        else:
            raise AssertionError("json.loads should have raised")


    def test_bare_value_error_is_local_validation(self):
        """Programming bugs that raise bare ValueError must still be
        classified as local validation errors (non-retryable)."""
        assert _mirror_agent_predicate(ValueError("bad arg"))






class TestNoneTypeNotIterableIsRetryable:
    """Regression for #33136 / closes lingering Telegram \"Non-retryable error (HTTP None)\".

    The chatgpt.com Codex backend (and any other upstream SDK / provider shim)
    can surface ``TypeError: 'NoneType' object is not iterable`` as a wire-shape
    mismatch, not a local programming bug. Even after #33042 made our own
    consumer immune, third-party paths and mocked clients can still produce
    this shape. The classifier should treat it as retryable so the normal
    retry/fallback chain runs.
    """

    def test_nonetype_not_iterable_is_retryable(self):
        err = TypeError("'NoneType' object is not iterable")
        assert not _mirror_agent_predicate(err), (
            "TypeError('NoneType ... not iterable') must be excluded from "
            "is_local_validation_error — it is a provider/SDK shape mismatch, "
            "not a local bug. See #33136."
        )


    def test_unrelated_type_error_remains_local_validation(self):
        """TypeError without the NoneType-not-iterable pattern still aborts (programming bug)."""
        assert _mirror_agent_predicate(TypeError("tools must be a list"))
        assert _mirror_agent_predicate(TypeError("expected str, got int"))


class TestAgentLoopSourceHasNoneTypeCarveOut:
    """Belt-and-suspenders: the production source must include the carve-out."""

    def test_conversation_loop_excludes_nonetype_not_iterable_from_local_validation(self):
        import inspect
        from agent import conversation_loop
        src = inspect.getsource(conversation_loop)
        assert "is_local_validation_error" in src
        # The specific check must be present.
        assert "nonetype" in src.lower() and "not iterable" in src.lower(), (
            "agent/conversation_loop.py must carve out 'NoneType is not iterable' "
            "TypeErrors from the is_local_validation_error classification — see #33136."
        )


class TestMalformedToolJSONIsRetryable:
    """Regression for #39021: the Anthropic SDK raises a bare ValueError when
    the model streams malformed tool-call JSON (e.g. a double comma in an
    input_json_delta). It is NOT a json.JSONDecodeError — the SDK catches the
    inner parse error and re-raises a plain ValueError with a fixed prefix.

    This is the same class of failure as JSONDecodeError (corrupt bytes off
    the wire, not a local programming bug); the SDK message literally says
    "Please retry your request". The classifier must treat it as retryable so
    resampling the model can produce well-formed JSON.
    """

    # The exact message the SDK emits — see
    # anthropic/lib/streaming/_beta_messages.py.
    _SDK_MSG = (
        "Unable to parse tool parameter JSON from model. Please retry your "
        "request or adjust your prompt. Error: key must be a string at line 1 "
        'column 94. JSON: {"file_path": "x", "offset": 1642, , "limit": 90'
    )

    def test_malformed_tool_json_is_not_local_validation(self):
        err = ValueError(self._SDK_MSG)
        assert not _mirror_agent_predicate(err), (
            "The SDK's 'Unable to parse tool parameter JSON' ValueError must "
            "be excluded from is_local_validation_error — it is corrupt wire "
            "data, not a local bug, and the SDK asks the caller to retry."
        )

    def test_unrelated_value_error_remains_local_validation(self):
        """A bare ValueError without the SDK tool-JSON prefix still aborts."""
        assert _mirror_agent_predicate(ValueError("bad arg"))
        assert _mirror_agent_predicate(ValueError("invalid literal for int()"))


class TestAgentLoopSourceHasToolJSONCarveOut:
    """Belt-and-suspenders: the production source must include the carve-out."""

    def test_conversation_loop_excludes_tool_json_parse_error_from_local_validation(self):
        import inspect
        from agent import conversation_loop
        src = inspect.getsource(conversation_loop)
        assert "is_local_validation_error" in src
        assert "unable to parse tool parameter json" in src.lower(), (
            "agent/conversation_loop.py must carve out the SDK's 'Unable to "
            "parse tool parameter JSON' ValueError from the "
            "is_local_validation_error classification — see #39021."
        )
