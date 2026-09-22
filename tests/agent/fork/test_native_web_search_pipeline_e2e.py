"""End-to-end native-web-search pipeline through the CONSOLIDATED converter.

FORK-ONLY feature — see agent/fork/anthropic_native_web_search.py and FORK.md.

The other native-web-search tests exercise the tool-swap and the converter
separately. This one drives the FULL request-build path in one call —
``build_anthropic_kwargs`` -> message conversion (upstream's
``anthropic_message_convert``) + fork server-tool passes -> native tool
injection -> final wire kwargs — and asserts the server-tool blocks and the
native tool definition both land correctly on the same request.

This is the integration proof that retiring the fork's vendored converter
(``agent/fork/anthropic_messages.py``) kept native web search working.
"""
from unittest.mock import patch

import pytest

from agent.anthropic_adapter import build_anthropic_kwargs

ANTHROPIC = "https://api.anthropic.com"
CLAUDE = "claude-opus-4-20250514"

BASH_TOOL = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": "Run a command",
        "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}},
    },
}

CLIENT_WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the web",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
    },
}


def _server_tool_use(tu_id, name="web_search"):
    return {"type": "server_tool_use", "id": tu_id, "name": name, "input": {"query": "q"}}


def _web_search_result(tu_id):
    return {"type": "web_search_tool_result", "tool_use_id": tu_id, "content": [
        {"type": "web_search_result", "url": "https://example.com/", "title": "t",
         "page_age": None, "encrypted_content": "enc"}]}


@pytest.fixture
def native_search_on():
    """Force the native-search config ON regardless of the developer's config.yaml."""
    with patch("agent.fork.anthropic_native_web_search._load_web_config",
               return_value={"anthropic_native_search": True,
                             "anthropic_native_search_max_uses": 5}):
        yield


def _build(messages, tools=(CLIENT_WEB_SEARCH_TOOL,)):
    return build_anthropic_kwargs(
        model=CLAUDE, messages=list(messages), tools=list(tools), max_tokens=1024,
        reasoning_config=None, base_url=ANTHROPIC,
    )


def _assistant(kwargs):
    return next(m for m in kwargs["messages"] if m["role"] == "assistant")


def _types(blocks):
    return [b.get("type") for b in blocks]


def test_full_turn_native_tool_injected_and_blocks_replayed(native_search_on):
    """One request carries BOTH the native server-tool definition and the prior
    turn's replayed server_tool_use/web_search_tool_result pair."""
    kwargs = _build([
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "search for X"},
        {"role": "assistant", "content": "", "anthropic_content_blocks": [
            {"type": "text", "text": "Searching."},
            _server_tool_use("srvtoolu_1"), _web_search_result("srvtoolu_1"),
            {"type": "text", "text": "Found it."}]},
        {"role": "user", "content": "thanks"},
    ])

    # Tool side: the client web_search tool became Anthropic's native server tool.
    native = [t for t in kwargs["tools"] if t.get("type") == "web_search_20250305"]
    assert len(native) == 1, "native web_search server tool must be on the wire"
    assert native[0]["name"] == "web_search"
    assert native[0]["max_uses"] == 5
    assert not any(t.get("name") == "web_search" and "input_schema" in t
                   for t in kwargs["tools"]), "client web_search must be gone"

    # Message side: the pair survived conversion, adjacent and in order.
    blocks = _assistant(kwargs)["content"]
    types = _types(blocks)
    use_idx = types.index("server_tool_use")
    assert blocks[use_idx + 1]["type"] == "web_search_tool_result"
    assert blocks[use_idx + 1]["tool_use_id"] == "srvtoolu_1"


def test_full_turn_strips_split_pair_but_keeps_native_tool(native_search_on):
    """A pair split by compaction is stripped by the fork's orphan pass, while the
    native tool definition still ships — search stays available for this turn."""
    kwargs = _build([
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "anthropic_content_blocks": [
            {"type": "text", "text": "partial"}, _web_search_result("srvtoolu_lost")]},
        {"role": "user", "content": "u2"},
    ])
    assert any(t.get("type") == "web_search_20250305" for t in kwargs["tools"])
    assert "web_search_tool_result" not in _types(_assistant(kwargs)["content"])


def test_full_turn_multi_search_pairs_all_survive(native_search_on):
    """Several native searches in one turn: every pair must stay adjacent."""
    kwargs = _build([
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "anthropic_content_blocks": [
            _server_tool_use("s_a"), _web_search_result("s_a"),
            {"type": "text", "text": "mid"},
            _server_tool_use("s_b"), _web_search_result("s_b")]},
        {"role": "user", "content": "u2"},
    ])
    blocks = _assistant(kwargs)["content"]
    pairs = [(i, b) for i, b in enumerate(blocks) if b.get("type") == "server_tool_use"]
    assert len(pairs) == 2
    for i, use in pairs:
        nxt = blocks[i + 1]
        assert nxt["type"] == "web_search_tool_result"
        assert nxt["tool_use_id"] == use["id"]


def test_full_turn_client_tool_use_ordered_after_server_blocks(native_search_on):
    """A client tool_use in the same turn as native search must be moved to the
    tail so the following tool_result is "immediately after" it."""
    kwargs = _build([
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "toolu_x", "type": "function",
                         "function": {"name": "bash", "arguments": '{"cmd": "ls"}'}}],
         "anthropic_content_blocks": [
             {"type": "tool_use", "id": "toolu_x", "name": "bash", "input": {"cmd": "ls"}},
             _server_tool_use("s_c"), _web_search_result("s_c")]},
        {"role": "tool", "tool_call_id": "toolu_x", "content": "ok"},
    ], tools=(CLIENT_WEB_SEARCH_TOOL, BASH_TOOL))
    types = _types(_assistant(kwargs)["content"])
    # bash must be in `tools` or the adapter's unrelated _strip_unknown_tool_blocks
    # pass removes the client tool_use before ordering can be observed.
    assert "tool_use" in types, "client tool_use must survive to be reordered"
    assert types.index("tool_use") > types.index("web_search_tool_result")


def test_native_search_disabled_keeps_client_tool_and_still_replays_blocks():
    """With native search disabled the client tool stays, but the converter must
    still replay stored server-tool blocks from earlier native-search turns."""
    with patch("agent.fork.anthropic_native_web_search._load_web_config",
               return_value={"anthropic_native_search": False}):
        kwargs = _build([
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "", "anthropic_content_blocks": [
                _server_tool_use("s_d"), _web_search_result("s_d")]},
            {"role": "user", "content": "u2"},
        ])
    assert not any(t.get("type") == "web_search_20250305" for t in kwargs["tools"])
    assert any(t.get("name") == "web_search" for t in kwargs["tools"])
    assert "web_search_tool_result" in _types(_assistant(kwargs)["content"])


def test_recomposition_path_server_tool_blocks_reach_the_wire(native_search_on):
    """The recomposition path (no anthropic_content_blocks — blocks stashed under
    ``server_tool_blocks`` by the transport) must also reach the wire."""
    kwargs = _build([
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "answer text",
         "server_tool_blocks": [_server_tool_use("s_e"), _web_search_result("s_e")]},
        {"role": "user", "content": "u2"},
    ])
    types = _types(_assistant(kwargs)["content"])
    assert "server_tool_use" in types and "web_search_tool_result" in types
    assert types.index("server_tool_use") < types.index("text")
