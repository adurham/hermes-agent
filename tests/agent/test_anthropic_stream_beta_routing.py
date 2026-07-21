"""Regression: main agent streaming path must route through .beta.messages.

The fork's Claude-Code-mimicry path attaches typed body kwargs
(context_management, output_config, speed, betas) that only exist on
client.beta.messages.* (Anthropic SDK 0.100+). The plain .messages.*
namespace rejects them with TypeError.

This tests the _call_anthropic closure inside
interruptible_streaming_api_call (chat_completion_helpers.py) — the
main agent's streaming path, which is separate from the auxiliary
client path tested in test_anthropic_adapter.py.
"""

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent import chat_completion_helpers as cch


# ── helpers ────────────────────────────────────────────────────────────

BETA_KWARGS = {"context_management", "output_config", "speed", "betas"}


class _StreamContextManager:
    """Mimics Anthropic SDK's stream context manager with event iteration."""
    def __init__(self, events=None):
        self._events = events or []
        self.response = SimpleNamespace(
            headers={},
            status_code=200,
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def __iter__(self):
        return iter(self._events)

    def get_final_message(self):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2),
        )


def _make_event(event_type, **attrs):
    """Build a SimpleNamespace mimicking an Anthropic stream event."""
    obj = SimpleNamespace(type=event_type)
    for k, v in attrs.items():
        setattr(obj, k, v)
    return obj


def _make_agent():
    """A MagicMock agent wired for anthropic_messages streaming."""
    agent = MagicMock()
    agent.api_mode = "anthropic_messages"
    agent._interrupt_requested = False
    agent.verbose_logging = False
    agent._consecutive_stale_streams = 0
    agent._touch_activity = MagicMock()
    agent._safe_print = MagicMock()
    agent._buffer_status = MagicMock()
    agent._fire_stream_delta = MagicMock()
    agent._fire_tool_gen_started = MagicMock()
    agent._fire_reasoning_delta = MagicMock()
    agent._has_stream_consumers = MagicMock(return_value=False)
    agent._stream_diag_init = MagicMock(return_value={})
    agent._stream_diag_capture_response = MagicMock()
    agent._capture_rate_limits = MagicMock()
    agent._claim_stream_writer = MagicMock(return_value=1)
    agent._stream_writer_is_current = MagicMock(return_value=True)
    agent._close_request_anthropic_client = MagicMock()
    agent._abort_request_anthropic_client = MagicMock()
    agent._compute_non_stream_stale_timeout = MagicMock(return_value=5.0)
    agent._codex_silent_hang_hint = MagicMock(return_value=None)
    agent._interrupt_requested = False
    agent.reasoning_callback = None
    agent.stream_delta_callback = None
    agent.provider = "anthropic"
    agent.model = "claude-sonnet-5"
    return agent


def _make_request_client(*, has_beta_messages: bool):
    """Build a mock request client with or without .beta.messages."""
    class _Messages:
        def stream(self, **kwargs):
            return _StreamContextManager(events=[
                _make_event("message_start", message=SimpleNamespace(
                    usage=SimpleNamespace(input_tokens=10, output_tokens=0),
                )),
                _make_event("content_block_start", index=0,
                            content_block=SimpleNamespace(type="text", text="")),
                _make_event("content_block_delta", index=0,
                            delta=SimpleNamespace(type="text_delta", text="hello")),
                _make_event("content_block_stop", index=0),
                _make_event("message_delta",
                            delta=SimpleNamespace(stop_reason="end_turn"),
                            usage=SimpleNamespace(output_tokens=5)),
                _make_event("message_stop"),
            ])

    class _BetaMessages:
        def stream(self, **kwargs):
            return _StreamContextManager(events=[
                _make_event("message_start", message=SimpleNamespace(
                    usage=SimpleNamespace(input_tokens=10, output_tokens=0),
                )),
                _make_event("content_block_start", index=0,
                            content_block=SimpleNamespace(type="text", text="")),
                _make_event("content_block_delta", index=0,
                            delta=SimpleNamespace(type="text_delta", text="hello")),
                _make_event("content_block_stop", index=0),
                _make_event("message_delta",
                            delta=SimpleNamespace(stop_reason="end_turn"),
                            usage=SimpleNamespace(output_tokens=5)),
                _make_event("message_stop"),
            ])

    if has_beta_messages:
        return SimpleNamespace(
            messages=_Messages(),
            beta=SimpleNamespace(messages=_BetaMessages()),
        )
    return SimpleNamespace(messages=_Messages())


# ── Tests ──────────────────────────────────────────────────────────────


class TestAnthropicStreamBetaRouting:
    """The _call_anthropic closure must route through .beta.messages when
    the client supports it, and strip beta-only kwargs when it doesn't."""

    def test_stream_with_beta_messages_routes_through_beta(self):
        """When request client has .beta.messages, stream() is called on
        .beta.messages and beta-only kwargs are preserved."""
        agent = _make_agent()
        request_client = _make_request_client(has_beta_messages=True)

        # Track which namespace stream() was called on
        call_ns = {"name": None}

        original_beta_stream = request_client.beta.messages.stream
        def _track_beta_stream(**kw):
            call_ns["name"] = "beta.messages"
            call_ns["kwargs"] = kw
            return original_beta_stream(**kw)
        request_client.beta.messages.stream = _track_beta_stream

        original_plain_stream = request_client.messages.stream
        def _track_plain_stream(**kw):
            call_ns["name"] = "messages"
            call_ns["kwargs"] = kw
            return original_plain_stream(**kw)
        request_client.messages.stream = _track_plain_stream

        agent._create_request_anthropic_client = MagicMock(return_value=request_client)

        api_kwargs = {
            "model": "claude-sonnet-5",
            "messages": [{"role": "user", "content": "hi"}],
        }
        for k in BETA_KWARGS:
            api_kwargs[k] = "dummy"

        result = cch.interruptible_streaming_api_call(agent, api_kwargs)

        assert call_ns["name"] == "beta.messages", \
            f"Expected stream on .beta.messages, got {call_ns['name']}"
        for k in BETA_KWARGS:
            assert k in call_ns.get("kwargs", {}), \
                f"Beta-only kwarg '{k}' should be preserved on .beta.messages"
        assert result is not None

    def test_stream_without_beta_messages_strips_beta_kwargs(self):
        """When request client lacks .beta.messages, stream() is called on
        .messages and beta-only kwargs are stripped."""
        agent = _make_agent()
        request_client = _make_request_client(has_beta_messages=False)

        call_ns = {"name": None}

        original_plain_stream = request_client.messages.stream
        def _track_plain_stream(**kw):
            call_ns["name"] = "messages"
            call_ns["kwargs"] = kw
            return original_plain_stream(**kw)
        request_client.messages.stream = _track_plain_stream

        agent._create_request_anthropic_client = MagicMock(return_value=request_client)

        api_kwargs = {
            "model": "claude-sonnet-5",
            "messages": [{"role": "user", "content": "hi"}],
        }
        for k in BETA_KWARGS:
            api_kwargs[k] = "dummy"

        result = cch.interruptible_streaming_api_call(agent, api_kwargs)

        assert call_ns["name"] == "messages", \
            f"Expected stream on .messages, got {call_ns['name']}"
        for k in BETA_KWARGS:
            assert k not in call_ns.get("kwargs", {}), \
                f"Beta-only kwarg '{k}' should be stripped on .messages"
        assert result is not None

    def test_stream_without_beta_messages_preserves_normal_kwargs(self):
        """Non-beta kwargs pass through unchanged when .beta.messages is absent."""
        agent = _make_agent()
        request_client = _make_request_client(has_beta_messages=False)

        call_ns = {"name": None}

        original_plain_stream = request_client.messages.stream
        def _track_plain_stream(**kw):
            call_ns["name"] = "messages"
            call_ns["kwargs"] = kw
            return original_plain_stream(**kw)
        request_client.messages.stream = _track_plain_stream

        agent._create_request_anthropic_client = MagicMock(return_value=request_client)

        api_kwargs = {
            "model": "claude-sonnet-5",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1000,
            "temperature": 0.7,
        }

        result = cch.interruptible_streaming_api_call(agent, api_kwargs)

        assert call_ns["name"] == "messages"
        assert call_ns["kwargs"].get("model") == "claude-sonnet-5"
        assert call_ns["kwargs"].get("max_tokens") == 1000
        assert call_ns["kwargs"].get("temperature") == 0.7
        assert result is not None
