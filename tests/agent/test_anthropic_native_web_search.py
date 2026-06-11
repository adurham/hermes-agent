"""Tests for the fork-only provider-aware web search swap.

FORK-ONLY feature — see agent/fork/anthropic_native_web_search.py and FORK.md.

Priority contract:
  * first-party Anthropic (Claude)  → native web_search_20250305 server tool
  * any other endpoint (non-Claude) → client web_search tool unchanged
"""

import pytest

from agent.anthropic_adapter import build_anthropic_kwargs
from agent.fork import anthropic_native_web_search as nws


_NATIVE_TYPE = "web_search_20250305"


def _client_web_search_tool():
    """An OpenAI-format client web_search tool (as registered by web_tools)."""
    return {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for information.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["query"],
            },
        },
    }


def _other_tool():
    return {
        "type": "function",
        "function": {"name": "read_file", "description": "x", "parameters": {"type": "object"}},
    }


@pytest.fixture(autouse=True)
def _default_config(monkeypatch):
    """Default: native search enabled, max_uses 5 — independent of user config."""
    monkeypatch.setattr(
        nws, "_load_web_config",
        lambda: {"anthropic_native_search": True, "anthropic_native_search_max_uses": 5},
    )


# ── unit: apply_native_web_search ────────────────────────────────────────────

class TestApplyNativeWebSearchUnit:
    def _converted_tools(self):
        # Mimics convert_tools_to_anthropic output for [web_search, read_file].
        return [
            {"name": "web_search", "description": "Search.", "input_schema": {"type": "object"}},
            {"name": "read_file", "description": "x", "input_schema": {"type": "object"}},
        ]

    def test_swaps_on_first_party_anthropic(self):
        out = nws.apply_native_web_search(self._converted_tools(), base_url=None)
        ws = [t for t in out if t.get("name") == "web_search"]
        assert len(ws) == 1
        assert ws[0]["type"] == _NATIVE_TYPE
        assert ws[0]["max_uses"] == 5
        # input_schema is dropped for the server tool form.
        assert "input_schema" not in ws[0]

    def test_preserves_order_and_other_tools(self):
        out = nws.apply_native_web_search(self._converted_tools(), base_url=None)
        assert [t["name"] for t in out] == ["web_search", "read_file"]
        assert out[1] == {"name": "read_file", "description": "x", "input_schema": {"type": "object"}}

    def test_no_swap_on_third_party_endpoint(self):
        # MiniMax / Kimi / custom gateway — any non-anthropic.com host.
        tools = self._converted_tools()
        out = nws.apply_native_web_search(tools, base_url="https://api.minimax.io/anthropic")
        assert out is tools  # unchanged object — fast path
        assert all(t.get("type") != _NATIVE_TYPE for t in out)

    def test_first_party_explicit_base_url(self):
        out = nws.apply_native_web_search(
            self._converted_tools(), base_url="https://api.anthropic.com"
        )
        assert any(t.get("type") == _NATIVE_TYPE for t in out)

    def test_no_swap_when_disabled(self, monkeypatch):
        monkeypatch.setattr(nws, "_load_web_config", lambda: {"anthropic_native_search": False})
        tools = self._converted_tools()
        out = nws.apply_native_web_search(tools, base_url=None)
        assert out is tools
        assert all(t.get("type") != _NATIVE_TYPE for t in out)

    def test_disabled_via_string_false(self, monkeypatch):
        monkeypatch.setattr(nws, "_load_web_config", lambda: {"anthropic_native_search": "false"})
        out = nws.apply_native_web_search(self._converted_tools(), base_url=None)
        assert all(t.get("type") != _NATIVE_TYPE for t in out)

    def test_no_web_search_present_is_noop(self):
        tools = [{"name": "read_file", "description": "x", "input_schema": {"type": "object"}}]
        out = nws.apply_native_web_search(tools, base_url=None)
        assert out is tools

    def test_empty_tools_is_noop(self):
        assert nws.apply_native_web_search([], base_url=None) == []

    def test_idempotent_when_native_already_present(self):
        tools = [
            {"type": _NATIVE_TYPE, "name": "web_search", "max_uses": 5},
            {"name": "read_file", "description": "x", "input_schema": {"type": "object"}},
        ]
        out = nws.apply_native_web_search(tools, base_url=None)
        assert out is tools
        assert sum(1 for t in out if t.get("type") == _NATIVE_TYPE) == 1

    def test_preserves_cache_control(self):
        tools = [
            {
                "name": "web_search",
                "description": "Search.",
                "input_schema": {"type": "object"},
                "cache_control": {"type": "ephemeral"},
            },
        ]
        out = nws.apply_native_web_search(tools, base_url=None)
        assert out[0]["cache_control"] == {"type": "ephemeral"}

    def test_max_uses_omitted_when_non_positive(self, monkeypatch):
        monkeypatch.setattr(
            nws, "_load_web_config",
            lambda: {"anthropic_native_search": True, "anthropic_native_search_max_uses": 0},
        )
        out = nws.apply_native_web_search(self._converted_tools(), base_url=None)
        ws = [t for t in out if t.get("name") == "web_search"][0]
        assert "max_uses" not in ws

    def test_garbage_max_uses_falls_back_to_default(self, monkeypatch):
        monkeypatch.setattr(
            nws, "_load_web_config",
            lambda: {"anthropic_native_search": True, "anthropic_native_search_max_uses": "lots"},
        )
        out = nws.apply_native_web_search(self._converted_tools(), base_url=None)
        ws = [t for t in out if t.get("name") == "web_search"][0]
        assert ws["max_uses"] == 5

    def test_never_raises_returns_original_on_error(self, monkeypatch):
        def boom(_):
            raise RuntimeError("classify failed")
        monkeypatch.setattr(nws, "is_first_party_anthropic", boom)
        tools = self._converted_tools()
        out = nws.apply_native_web_search(tools, base_url=None)
        assert out is tools


class TestIsFirstPartyAnthropic:
    def test_none_base_url_is_first_party(self):
        assert nws.is_first_party_anthropic(None) is True

    def test_anthropic_com_is_first_party(self):
        assert nws.is_first_party_anthropic("https://api.anthropic.com") is True

    @pytest.mark.parametrize("url", [
        "https://api.minimax.io/anthropic",
        "https://api.kimi.com/coding",
        "https://bedrock-runtime.us-east-1.amazonaws.com",
        "https://my-proxy.internal/v1",
    ])
    def test_third_party_is_not_first_party(self, url):
        assert nws.is_first_party_anthropic(url) is False


# ── integration: through build_anthropic_kwargs ──────────────────────────────

class TestBuildAnthropicKwargsNativeWebSearch:
    def test_claude_endpoint_gets_native_tool(self):
        kwargs = build_anthropic_kwargs(
            model="claude-opus-4-6",
            messages=[{"role": "user", "content": "search for X"}],
            tools=[_client_web_search_tool(), _other_tool()],
            max_tokens=4096,
            reasoning_config=None,
            base_url=None,  # first-party Anthropic
        )
        ws = [t for t in kwargs["tools"] if t.get("name") == "web_search"]
        assert len(ws) == 1
        assert ws[0]["type"] == _NATIVE_TYPE
        # The other tool is untouched and still a normal client tool.
        rf = [t for t in kwargs["tools"] if t.get("name") == "read_file"]
        assert rf and "type" not in rf[0]

    def test_third_party_endpoint_keeps_client_tool(self):
        kwargs = build_anthropic_kwargs(
            model="MiniMax-M2",
            messages=[{"role": "user", "content": "search for X"}],
            tools=[_client_web_search_tool(), _other_tool()],
            max_tokens=4096,
            reasoning_config=None,
            base_url="https://api.minimax.io/anthropic",
        )
        ws = [t for t in kwargs["tools"] if t.get("name") == "web_search"]
        assert len(ws) == 1
        # Still the client tool — has input_schema, no server-tool type.
        assert ws[0].get("type") != _NATIVE_TYPE
        assert "input_schema" in ws[0]

    def test_oauth_claude_gets_native_tool(self):
        kwargs = build_anthropic_kwargs(
            model="claude-opus-4-6",
            messages=[{"role": "user", "content": "search for X"}],
            tools=[_client_web_search_tool()],
            max_tokens=4096,
            reasoning_config=None,
            is_oauth=True,
            base_url=None,
        )
        ws = [t for t in kwargs["tools"] if t.get("name") == "web_search"]
        assert len(ws) == 1
        assert ws[0]["type"] == _NATIVE_TYPE
