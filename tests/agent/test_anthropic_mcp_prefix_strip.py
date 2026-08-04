"""Tests for GH-25255: Anthropic OAuth ``mcp__`` tool-name round-trip.

Anthropic's subscription/OAuth billing classifier treats a **single-underscore**
``mcp_`` tool name as a third-party-app fingerprint and rejects the request with
HTTP 400 "Third-party apps now draw from extra usage, not plan limits".  So on
the OAuth wire NOTHING may carry a single-underscore ``mcp_`` prefix:

  * bare native tools            ``read_file``            -> ``mcp__read_file``
  * native MCP server tools      ``mcp_linear_get_issue`` -> ``mcp__linear_get_issue``

``normalize_response`` reverses the ``mcp__`` wire name back to whatever the tool
registry knows (the single-underscore ``mcp_<server>_<tool>`` form for MCP server
tools, or the bare name for native tools) so the dispatcher is unaffected.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tool_use_block(name: str, block_id: str = "tc_1", input_data: dict | None = None):
    """Create a fake Anthropic tool_use content block."""
    return SimpleNamespace(
        type="tool_use",
        id=block_id,
        name=name,
        input=input_data or {"query": "test"},
    )


def _make_response(*blocks, stop_reason="end_turn"):
    """Create a fake Anthropic Messages response."""
    return SimpleNamespace(
        content=list(blocks),
        stop_reason=stop_reason,
        model="claude-sonnet-4",
        usage=SimpleNamespace(input_tokens=100, output_tokens=50),
    )


def _make_thinking_block(signature: str = "sig123"):
    """Create a fake signed Anthropic thinking content block."""
    return SimpleNamespace(type="thinking", thinking="reasoning...", signature=signature)


class _FakeRegistry:
    """Minimal fake tool registry for testing prefix round-trip logic."""

    def __init__(self, registered_names: set[str]):
        self._names = registered_names

    def get_entry(self, name: str):
        if name in self._names:
            return SimpleNamespace(name=name)  # truthy = tool exists
        return None


# ---------------------------------------------------------------------------
# Response side: mcp__ wire name -> registry name
# ---------------------------------------------------------------------------

class TestAnthropicMcpPrefixStrip:
    """Verify strip_tool_prefix reverses the ``mcp__`` wire prefix correctly."""

    def _get_transport(self):
        from agent.transports.anthropic import AnthropicTransport
        return AnthropicTransport()

    def test_strips_prefix_for_oauth_injected_native_tool(self):
        """``mcp__read_file`` -> ``read_file`` (bare native tool)."""
        transport = self._get_transport()
        block = _make_tool_use_block("mcp__read_file")
        response = _make_response(block)

        registry = _FakeRegistry({"read_file", "terminal", "web_search"})
        with patch("tools.registry.registry", registry):
            result = transport.normalize_response(response, strip_tool_prefix=True)

        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "read_file"


    def test_no_strip_when_flag_false(self):
        """When strip_tool_prefix=False, names are never modified."""
        transport = self._get_transport()
        block = _make_tool_use_block("mcp__read_file")
        response = _make_response(block)

        registry = _FakeRegistry({"read_file"})
        with patch("tools.registry.registry", registry):
            result = transport.normalize_response(response, strip_tool_prefix=False)

        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "mcp__read_file"





    def test_ordered_blocks_name_matches_resolved_tool_call_name(self):
        """Regression: the verbatim replay copy (``anthropic_content_blocks``)
        must carry the SAME resolved name as ``tool_calls``, not the raw
        OAuth wire name.

        Bug reproduced 2026-07-07: a ``clarify`` call round-tripped through
        OAuth as ``mcp__clarify``. ``tool_calls[0].name`` was correctly
        reversed to ``clarify``, but the parallel ``ordered_blocks`` list
        (persisted into provider_data["anthropic_content_blocks"] whenever a
        turn interleaves signed thinking with tool_use) kept the raw
        ``mcp__clarify`` wire name forever. On the NEXT turn,
        ``_strip_unknown_tool_blocks`` compared that stale wire name against
        the live (bare) tool name set, found no match, and silently rewrote
        the historical clarify question/answer into a lossy 400-char-
        truncated "tool no longer available" breadcrumb — corrupting the
        model's view of its own prior turn (it read the truncated stub and
        told the user their message "got cut off").
        """
        transport = self._get_transport()
        thinking = _make_thinking_block()
        tool_use = _make_tool_use_block("mcp__clarify", block_id="tc_1")
        response = _make_response(thinking, tool_use)

        registry = _FakeRegistry({"clarify"})
        with patch("tools.registry.registry", registry):
            result = transport.normalize_response(response, strip_tool_prefix=True)

        assert result.tool_calls[0].name == "clarify"
        ordered_blocks = result.provider_data["anthropic_content_blocks"]
        tool_use_blocks = [b for b in ordered_blocks if b.get("type") == "tool_use"]
        assert len(tool_use_blocks) == 1
        assert tool_use_blocks[0]["name"] == "clarify"

    def test_bridge_tool_names_resolve_even_when_not_in_registry(self):
        """Regression: tool_search/tool_describe/tool_call are NEVER in
        tools/registry.py — they are dynamically synthesized bridge tools
        (tools/tool_search.py) dispatched by a name-check in
        agent/tool_executor.py, not registered ToolRegistry entries.

        Bug reproduced live 2026-07-07 22:53-23:15 (session
        20260707_225321_554b40), AFTER the mcp__/ordered_blocks sync fix
        (e80d8c73f) had already landed: agent.log showed
        ``rewrote N tool_use/result block(s) for tools no longer
        available: ['mcp__tool_call', 'mcp__tool_search']`` climbing 1->20
        over ~20 minutes in a single ongoing conversation. Root cause: the
        registry-lookup reversal in this file always misses for bridge
        tools (neither ``get_entry(name)`` nor the bare/single fallbacks
        ever find them, since they're not registered), so ``name`` fell
        through unresolved and ``clean_block["name"]`` stayed
        ``mcp__tool_call``/``mcp__tool_search`` forever in the replay
        history — even though ``tool_calls[0].name`` was separately
        (and correctly) reversed by the UNRELATED fuzzy-match repair path
        in agent_runtime_helpers.py::repair_tool_call (which matches
        against agent.valid_tool_names, not the registry, and only fixes
        the dispatch copy, never the replay copy this test checks).
        """
        transport = self._get_transport()
        block = _make_tool_use_block("mcp__tool_call", block_id="tc_1")
        response = _make_response(block)

        # Registry deliberately does NOT contain any bridge tool names —
        # this mirrors production exactly (see tools/registry.py, which
        # never registers tool_search/tool_describe/tool_call).
        registry = _FakeRegistry({"read_file", "terminal"})
        with patch("tools.registry.registry", registry):
            result = transport.normalize_response(response, strip_tool_prefix=True)

        assert result.tool_calls[0].name == "tool_call"
        # No thinking block in this response, so ordered_blocks is not
        # promoted to provider_data (only happens when signed thinking +
        # tool_use interleave) — the dispatch-name assertion above is the
        # full check for this shape; the thinking-interleaved shape (the
        # one that actually corrupted history in production) is covered
        # by the next test.

    def test_bridge_tool_name_synced_into_replay_history_with_thinking(self):
        """Same as above but WITH a signed thinking block, so ordered_blocks
        is actually promoted into provider_data["anthropic_content_blocks"]
        (the exact shape that corrupted history in production — every
        clarify/tool_search call in that session interleaved signed
        thinking with tool_use).
        """
        transport = self._get_transport()
        thinking = _make_thinking_block()
        tool_use = _make_tool_use_block("mcp__tool_search", block_id="tc_1")
        response = _make_response(thinking, tool_use)

        registry = _FakeRegistry({"read_file", "terminal"})
        with patch("tools.registry.registry", registry):
            result = transport.normalize_response(response, strip_tool_prefix=True)

        assert result.tool_calls[0].name == "tool_search"
        ordered_blocks = result.provider_data["anthropic_content_blocks"]
        tool_use_blocks = [b for b in ordered_blocks if b.get("type") == "tool_use"]
        assert len(tool_use_blocks) == 1
        assert tool_use_blocks[0]["name"] == "tool_search"


# ---------------------------------------------------------------------------
# Request side: registry name -> mcp__ wire name (no single-underscore leaks)
# ---------------------------------------------------------------------------

class TestAnthropicOAuthOutgoingPrefix:
    """build_anthropic_kwargs must emit ZERO single-underscore ``mcp_`` names on
    the OAuth wire — bare names and MCP server names both land on ``mcp__``."""

    def _build(self, tools, is_oauth=True):
        from agent.anthropic_adapter import build_anthropic_kwargs
        return build_anthropic_kwargs(
            model="claude-sonnet-4-6",
            messages=[{"role": "user", "content": "Hi"}],
            tools=tools,
            max_tokens=4096,
            reasoning_config=None,
            is_oauth=is_oauth,
        )

    def test_oauth_adds_double_prefix_to_bare_tool_name(self):
        """OAuth + bare name -> ``mcp__`` prefix added.

        FORK NOTE: uses ``session_search`` (a genuine non-CC-aliased Hermes
        tool) rather than ``read_file``. In this fork the 5 builtins in
        ``cc_aliases.HERMES_TO_CC`` (read_file→Read, terminal→Bash, …) are
        renamed to their Claude Code canonical names for billing mimicry and
        are deliberately NOT mcp__-prefixed. The mcp__ normalization applies to
        every OTHER tool — that's what this asserts.
        """
        kwargs = self._build([{
            "type": "function",
            "function": {"name": "session_search", "description": "x", "parameters": {}},
        }])
        assert [t["name"] for t in kwargs["tools"]] == ["mcp__session_search"]

    def test_oauth_promotes_single_underscore_mcp_server_tool(self):
        """OAuth + ``mcp_<server>_<tool>`` -> promoted to double underscore.

        This is the gap left by the bare constant swap: MCP server tools used
        to be *skipped* and went on the wire single-underscore, still tripping
        the classifier.  They must become ``mcp__`` and NOT be double-prefixed.
        """
        kwargs = self._build([{
            "type": "function",
            "function": {
                "name": "mcp_linear_get_issue",
                "description": "x",
                "parameters": {},
            },
        }])
        names = [t["name"] for t in kwargs["tools"]]
        assert names == ["mcp__linear_get_issue"]
        # never double-prefixed
        assert not any(n.startswith("mcp__mcp_") for n in names)


    def test_oauth_no_single_underscore_mcp_on_wire(self):
        """Mixed set: every wire name is bare-free of single-underscore mcp_.

        FORK NOTE: CC-aliased builtins (read_file→Read, terminal→Bash) ride the
        CC-canonical billing path and are NOT mcp__-prefixed; genuine MCP /
        other tools get mcp__. The core invariant still holds either way:
        nothing single-underscore ``mcp_`` reaches the wire.
        """
        kwargs = self._build([
            {"type": "function", "function": {"name": "session_search",
                                              "description": "x", "parameters": {}}},
            {"type": "function", "function": {"name": "mcp_linear_get_issue",
                                              "description": "y", "parameters": {}}},
            {"type": "function", "function": {"name": "read_file",
                                              "description": "z", "parameters": {}}},
        ])
        names = sorted(t["name"] for t in kwargs["tools"])
        # session_search + mcp_linear → mcp__; read_file → Read (CC alias).
        assert names == ["Read", "mcp__linear_get_issue", "mcp__session_search"]
        # The core invariant: NOTHING single-underscore reaches the wire.
        for n in names:
            assert not (n.startswith("mcp_") and not n.startswith("mcp__"))

