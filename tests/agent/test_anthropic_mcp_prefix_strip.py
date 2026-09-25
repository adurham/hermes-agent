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


class TestAnthropicOAuthAliasRoundTrip:
    """#65365: session_search / memory schemas alone deterministically trip
    Anthropic's OAuth billing classifier (verified live via the
    anthropic-ratelimit-unified-* response headers, see issue comments).
    Both are aliased to neutral wire names; normalize_response must reverse
    the mapping so the dispatcher still sees the real tool."""

    def _get_transport(self):
        from agent.transports.anthropic import AnthropicTransport
        return AnthropicTransport()

    def test_oauth_session_search_alias_round_trips_to_registry_name(self):
        transport = self._get_transport()
        block = _make_tool_use_block("mcp__chat_history_lookup")
        response = _make_response(block)

        registry = _FakeRegistry({"session_search"})
        with patch("tools.registry.registry", registry):
            result = transport.normalize_response(response, strip_tool_prefix=True)

        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "session_search"

    def test_oauth_memory_alias_round_trips_to_registry_name(self):
        transport = self._get_transport()
        block = _make_tool_use_block("mcp__context_notes")
        response = _make_response(block)

        registry = _FakeRegistry({"memory"})
        with patch("tools.registry.registry", registry):
            result = transport.normalize_response(response, strip_tool_prefix=True)

        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "memory"

    def test_registered_tool_wins_over_oauth_alias(self):
        """A real tool actually registered under the wire name keeps
        GH-25255 precedence — the alias must not hijack it."""
        transport = self._get_transport()
        block = _make_tool_use_block("mcp__chat_history_lookup")
        response = _make_response(block)

        registry = _FakeRegistry({"mcp_chat_history_lookup", "session_search"})
        with patch("tools.registry.registry", registry):
            result = transport.normalize_response(response, strip_tool_prefix=True)

        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "mcp_chat_history_lookup"





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

    def _build(self, tools, is_oauth=True, messages=None, tool_choice=None):
        from agent.anthropic_adapter import build_anthropic_kwargs
        return build_anthropic_kwargs(
            model="claude-sonnet-4-6",
            messages=messages or [{"role": "user", "content": "Hi"}],
            tools=tools,
            max_tokens=4096,
            reasoning_config=None,
            tool_choice=tool_choice,
            is_oauth=is_oauth,
        )

    def test_oauth_adds_double_prefix_to_bare_tool_name(self):
        """OAuth + bare name -> ``mcp__`` prefix added.

        FORK NOTE: uses ``some_custom_tool`` rather than
        ``session_search`` or ``web_search`` — those are special-cased
        elsewhere (upstream's #65365 classifier alias and the fork's native
        server-side web-search swap respectively). This test picks a bare
        name with NONE of those special cases so it isolates the plain mcp__
        normalization that applies to every other tool.
        """
        kwargs = self._build([{
            "type": "function",
            "function": {"name": "some_custom_tool", "description": "x", "parameters": {}},
        }])
        assert [t["name"] for t in kwargs["tools"]] == ["mcp__some_custom_tool"]



    def test_oauth_no_single_underscore_mcp_on_wire(self):
        """Mixed set: every wire name is bare-free of single-underscore mcp_.

        FORK NOTE: uses ``some_custom_tool`` (not ``session_search``/
        ``web_search``) to avoid the upstream #65365 classifier alias and the
        native server-tool swap, both covered elsewhere in this file.
        """
        kwargs = self._build([
            {"type": "function", "function": {"name": "some_custom_tool",
                                              "description": "x", "parameters": {}}},
            {"type": "function", "function": {"name": "mcp_linear_get_issue",
                                              "description": "y", "parameters": {}}},
            {"type": "function", "function": {"name": "read_file",
                                              "description": "z", "parameters": {}}},
        ])
        names = sorted(t["name"] for t in kwargs["tools"])
        # All three land on the double-underscore form.
        assert names == ["mcp__linear_get_issue", "mcp__read_file", "mcp__some_custom_tool"]
        # The core invariant: NOTHING single-underscore reaches the wire.
        for n in names:
            assert not (n.startswith("mcp_") and not n.startswith("mcp__"))


# ---------------------------------------------------------------------------
# #65365: session_search / memory OAuth billing-classifier trigger
# ---------------------------------------------------------------------------

class TestAnthropicOAuthClassifierAlias:
    """OAuth must alias the two schemas issue #65365 isolated as independent,
    deterministic triggers (session_search alone, memory alone) — in tool
    name, tool description, system-prompt prose, and named tool_choice."""

    def _build(self, tools, is_oauth=True, messages=None, tool_choice=None):
        from agent.anthropic_adapter import build_anthropic_kwargs
        return build_anthropic_kwargs(
            model="claude-sonnet-4-6",
            messages=messages or [{"role": "user", "content": "Hi"}],
            tools=tools,
            max_tokens=4096,
            reasoning_config=None,
            tool_choice=tool_choice,
            is_oauth=is_oauth,
        )

    def _tool(self, name, description="x"):
        return {"type": "function", "function": {"name": name, "description": description, "parameters": {}}}

    def test_oauth_aliases_session_search_and_memory_names(self):
        kwargs = self._build([
            self._tool("session_search", "Use session_search to recall prior chats."),
            self._tool("memory", "Persist notes with memory."),
        ])
        names = sorted(t["name"] for t in kwargs["tools"])
        assert names == ["mcp__chat_history_lookup", "mcp__context_notes"]

    def test_oauth_aliases_session_search_in_tool_description_not_memory(self):
        """session_search is prose-safe (unambiguous token); memory is
        ordinary English and must NOT be rewritten in free text — only its
        tool name is aliased."""
        kwargs = self._build([
            self._tool("session_search", "Call session_search to recall prior chats."),
            self._tool("memory", "Persist notes; uses working memory internally."),
        ])
        by_name = {t["name"]: t["description"] for t in kwargs["tools"]}
        assert "chat_history_lookup" in by_name["mcp__chat_history_lookup"]
        assert "session_search" not in by_name["mcp__chat_history_lookup"]
        assert "memory" in by_name["mcp__context_notes"]  # untouched prose

    def test_oauth_aliases_session_search_in_system_prompt_prose(self):
        kwargs = self._build(
            [self._tool("session_search")],
            messages=[
                {"role": "system", "content": "When relevant, use session_search to recall it."},
                {"role": "user", "content": "Hi"},
            ],
        )
        system_text = "\n".join(
            b["text"] for b in kwargs["system"] if isinstance(b, dict) and b.get("type") == "text"
        )
        assert "chat_history_lookup" in system_text
        assert "session_search" not in system_text

    def test_oauth_does_not_alias_longer_identifier_containing_token(self):
        """Word-boundary matching: a path like tools/session_search_tool.py
        must survive untouched, not become chat_history_lookup_tool.py."""
        kwargs = self._build(
            [self._tool("session_search")],
            messages=[
                {"role": "system", "content": "See tools/session_search_tool.py for details."},
                {"role": "user", "content": "Hi"},
            ],
        )
        system_text = "\n".join(
            b["text"] for b in kwargs["system"] if isinstance(b, dict) and b.get("type") == "text"
        )
        assert "tools/session_search_tool.py" in system_text

    def test_oauth_tool_choice_named_alias_matches_wire_name(self):
        """The gap left open by prior alias work: a forced tool_choice must
        be normalized through the same alias + mcp__ prefix as tools[], or
        (a) the literal trigger string still reaches the wire and (b) the
        name no longer matches any entry in tools[]."""
        kwargs = self._build(
            [self._tool("session_search")],
            tool_choice="session_search",
        )
        assert kwargs["tool_choice"] == {"type": "tool", "name": "mcp__chat_history_lookup"}
        assert kwargs["tool_choice"]["name"] in {t["name"] for t in kwargs["tools"]}

    def test_oauth_tool_choice_bare_name_still_gets_mcp_prefix(self):
        """Non-aliased tool_choice names still need the mcp__ prefix under
        OAuth (pre-existing GH-25255 invariant, now routed consistently).

        Uses ``some_custom_tool``, not ``session_search`` — that one is
        aliased by the upstream #65365 classifier fix and is covered in
        TestAnthropicOAuthClassifierAlias.
        """
        kwargs = self._build(
            [self._tool("some_custom_tool")],
            tool_choice="some_custom_tool",
        )
        assert kwargs["tool_choice"] == {"type": "tool", "name": "mcp__some_custom_tool"}

    def test_oauth_skips_alias_on_wire_name_collision(self):
        """If a real (e.g. MCP server) tool already owns the alias's wire
        name, session_search must keep its own name rather than collide —
        two identical tool names is a hard 400, strictly worse than #65365."""
        kwargs = self._build([
            self._tool("session_search"),
            self._tool("mcp_chat_history_lookup"),
        ])
        names = sorted(t["name"] for t in kwargs["tools"])
        assert names == ["mcp__chat_history_lookup", "mcp__session_search"]

    def test_api_key_path_never_aliases(self):
        """The alias is OAuth-only — API-key requests must be byte-identical
        to before this fix."""
        kwargs = self._build(
            [self._tool("session_search", "Use session_search to recall prior chats.")],
            is_oauth=False,
        )
        assert kwargs["tools"][0]["name"] == "session_search"
        assert "session_search" in kwargs["tools"][0]["description"]



class TestBridgeToolNameSetStaysInSync:
    """The transport mirrors the bridge-tool name set as a local literal.

    It cannot import ``tools.tool_search.BRIDGE_TOOL_NAMES`` at call time: that module imports
    ``tools.connectors``, which calls ``registry.register(...)`` on import, and under a test that
    patches ``tools.registry.registry`` with a stand-in the registration raises — which would
    silently disable bridge-name resolution and leave ``mcp__tool_call`` stuck in replay history
    (the exact production corruption this file exists to prevent). The literal is therefore the
    contract, and this test catches drift in either direction.
    """

    def test_transport_literal_matches_the_bridge_module(self):
        from agent.transports.anthropic import _BRIDGE_TOOL_NAMES
        from tools.tool_search import BRIDGE_TOOL_NAMES

        assert _BRIDGE_TOOL_NAMES == set(BRIDGE_TOOL_NAMES), (
            "agent/transports/anthropic.py::_BRIDGE_TOOL_NAMES drifted from "
            "tools/tool_search.py::BRIDGE_TOOL_NAMES — update the literal."
        )

    def test_bridge_names_reverse_without_any_registry(self):
        """Resolution must not depend on the live registry being intact."""
        from unittest.mock import patch

        from agent.transports.anthropic import _BRIDGE_TOOL_NAMES, _unprefix_oauth_tool_name

        class _ExplodingRegistry:
            def get_entry(self, name):  # pragma: no cover - must never be consulted for these
                raise AssertionError("registry consulted for a bridge tool name")

        with patch("tools.registry.registry", _ExplodingRegistry()):
            for bare in sorted(_BRIDGE_TOOL_NAMES):
                assert _unprefix_oauth_tool_name(f"mcp__{bare}") == bare
