"""Regression test: build_api_kwargs must thread tool_search_config and
cache_tools into the Anthropic transport.

History: the anthropic_messages branch of build_api_kwargs() omitted
tool_search_config= and cache_tools= entirely, so MCP-tool deferral
(client-side lazy loading) and the native tools[] cache breakpoint were
dead code on the live path. With an MCP-heavy install this meant every
request shipped all MCP tool schemas in full (observed 253 tools /
~399KB / ~100K tokens cold-cached) instead of ~120-byte stubs.

The existing tests/agent/test_apply_tool_search_modes.py exercised the
_apply_tool_search transform in isolation and passed -- but nothing
asserted the live caller actually PASSES the config. This test closes
that gap by spying on the transport.build_kwargs call.
"""
import sys
import types
from types import SimpleNamespace

# Stub optional heavy deps the import chain might touch.
sys.modules.setdefault("fire", types.SimpleNamespace(Fire=lambda *a, **k: None))
sys.modules.setdefault("firecrawl", types.SimpleNamespace(Firecrawl=object))
sys.modules.setdefault("fal_client", types.SimpleNamespace())

from agent.chat_completion_helpers import build_api_kwargs


def _make_fake_agent(captured, tsc_value):
    """Minimal duck-typed agent for the anthropic_messages branch."""
    transport = SimpleNamespace()

    def _build_kwargs(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    transport.build_kwargs = _build_kwargs

    agent = SimpleNamespace(
        api_mode="anthropic_messages",
        tools=[{"name": "web_search"}],
        model="claude-opus-4-8",
        max_tokens=16384,
        reasoning_config=None,
        request_overrides={},
        session_id="sess-123",
        _is_anthropic_oauth=False,
        _anthropic_base_url=None,
        _oauth_1m_beta_disabled=False,
        _use_native_cache_layout=True,
        _cache_ttl="1h",
        _ephemeral_max_output_tokens=None,
        context_compressor=SimpleNamespace(context_length=200000),
        _get_transport=lambda: transport,
        _prepare_anthropic_messages_for_api=lambda m: m,
        _anthropic_preserve_dots=lambda: False,
        _build_tool_search_config=lambda: tsc_value,
    )
    return agent


def test_build_api_kwargs_threads_tool_search_config():
    captured = {}
    tsc = {
        "enabled": True,
        "mode": "client_side",
        "defer_mcp_tools": True,
        "mcp_server_prefixes": ["notion_", "slack_"],
    }
    agent = _make_fake_agent(captured, tsc)
    build_api_kwargs(agent, [{"role": "user", "content": "hi"}])

    assert "tool_search_config" in captured, (
        "build_api_kwargs must pass tool_search_config to the transport; "
        "omitting it silently disables MCP-tool deferral on the live path."
    )
    assert captured["tool_search_config"] is tsc
    assert captured["tool_search_config"]["enabled"] is True


def test_build_api_kwargs_threads_cache_tools_and_ttl():
    captured = {}
    agent = _make_fake_agent(captured, None)
    build_api_kwargs(agent, [{"role": "user", "content": "hi"}])

    assert captured.get("cache_tools") is True, (
        "cache_tools must mirror _use_native_cache_layout so the reserved "
        "tools[] cache breakpoint is actually used."
    )
    assert captured.get("cache_ttl") == "1h"
    assert captured.get("session_id") == "sess-123"


def test_build_api_kwargs_cache_tools_off_when_no_native_layout():
    captured = {}
    agent = _make_fake_agent(captured, None)
    agent._use_native_cache_layout = False
    build_api_kwargs(agent, [{"role": "user", "content": "hi"}])
    assert captured.get("cache_tools") is False
