"""Unit tests for agent.anthropic_adapter._apply_tool_search (client-side mode).

Covers the fork's client-side lazy MCP tool loading:
  * stubs carry no ``defer_loading`` flag and no server tool is prepended,
  * promoted_tools bypass the stub,
  * the deferral policy (additional_eager / additional_deferred / MCP prefixes),
  * safety — "all deferred" or "all eager" returns input unchanged.

The legacy ``server_side`` (Anthropic tool_search server tool) tests were
removed 2026-09-25 with the fork's Anthropic server-tool cluster.
"""

from __future__ import annotations

from typing import Any, Dict, List

from agent.anthropic_adapter import _apply_tool_search


def _tool(name: str, description: str = "x", **extra) -> Dict[str, Any]:
    """Minimal tool dict — same shape produced by the OpenAI->Anthropic converter."""
    out: Dict[str, Any] = {
        "name": name,
        "description": description,
        "input_schema": {"type": "object", "properties": {}, "required": []},
    }
    out.update(extra)
    return out


def _names(tools: List[Dict[str, Any]]) -> List[str]:
    return [t.get("name", "<?>") for t in tools]


# ---------------------------------------------------------------------------
# Disabled / no-config paths
# ---------------------------------------------------------------------------


def test_no_config_returns_input_unchanged():
    tools = [_tool("a"), _tool("slack_x")]
    assert _apply_tool_search(tools, None) is tools


def test_disabled_config_returns_input_unchanged():
    tools = [_tool("a"), _tool("slack_x")]
    cfg = {"enabled": False, "mcp_server_prefixes": ["slack_"]}
    assert _apply_tool_search(tools, cfg) is tools


def test_empty_tools_returns_input():
    tools: List[Dict[str, Any]] = []
    cfg = {"enabled": True, "mcp_server_prefixes": ["slack_"]}
    assert _apply_tool_search(tools, cfg) is tools


# ---------------------------------------------------------------------------
# Stub shape
# ---------------------------------------------------------------------------


def test_stubs_have_no_defer_loading():
    tools = [_tool("core_tool", "do thing"), _tool("slack_send", "send slack msg")]
    cfg = {
        "enabled": True,
        "mcp_server_prefixes": ["slack_"],
        "defer_mcp_tools": True,
    }
    out = _apply_tool_search(tools, cfg)

    # Same names, same order, no server tool prepended.
    assert _names(out) == ["core_tool", "slack_send"]

    stub = next(t for t in out if t["name"] == "slack_send")
    assert "defer_loading" not in stub
    assert "type" not in stub, "client-side stubs are plain tools, not server-tool entries"
    assert stub["input_schema"] == {"type": "object"}
    assert stub["description"] != "send slack msg", "stub should replace description"

    eager = next(t for t in out if t["name"] == "core_tool")
    assert eager["description"] == "do thing"


def test_promoted_tools_skip_stub():
    """Tools in promoted_tools ship their full schema even if MCP-prefixed."""
    tools = [_tool("a"), _tool("slack_promoted", "real desc"), _tool("slack_stubbed")]
    cfg = {
        "enabled": True,
        "mcp_server_prefixes": ["slack_"],
        "defer_mcp_tools": True,
        "promoted_tools": {"slack_promoted"},
    }
    out = _apply_tool_search(tools, cfg)

    promoted = next(t for t in out if t["name"] == "slack_promoted")
    assert promoted["description"] == "real desc", "promoted tool keeps full schema"

    stubbed = next(t for t in out if t["name"] == "slack_stubbed")
    assert stubbed["input_schema"] == {"type": "object"}
    assert "defer_loading" not in stubbed


def test_cache_control_preserved_on_stub():
    tools = [
        _tool("a"),
        _tool("slack_b", cache_control={"type": "ephemeral"}),
    ]
    cfg = {
        "enabled": True,
        "mcp_server_prefixes": ["slack_"],
        "defer_mcp_tools": True,
    }
    out = _apply_tool_search(tools, cfg)
    stub = next(t for t in out if t["name"] == "slack_b")
    assert stub.get("cache_control") == {"type": "ephemeral"}


# ---------------------------------------------------------------------------
# Policy guards
# ---------------------------------------------------------------------------


def test_additional_eager_overrides_mcp_prefix():
    """additional_eager wins over defer_mcp_tools."""
    tools = [_tool("a"), _tool("slack_keep_eager", "real")]
    cfg = {
        "enabled": True,
        "mcp_server_prefixes": ["slack_"],
        "defer_mcp_tools": True,
        "additional_eager": ["slack_keep_eager"],
    }
    out = _apply_tool_search(tools, cfg)
    kept = next(t for t in out if t["name"] == "slack_keep_eager")
    assert kept["description"] == "real", "additional_eager should bypass the stub"


def test_additional_deferred_works_without_mcp_prefix():
    tools = [_tool("a", "real"), _tool("b", "real")]
    cfg = {
        "enabled": True,
        "mcp_server_prefixes": [],
        "defer_mcp_tools": False,
        "additional_deferred": ["b"],
    }
    out = _apply_tool_search(tools, cfg)
    stub = next(t for t in out if t["name"] == "b")
    assert stub["input_schema"] == {"type": "object"}
    eager = next(t for t in out if t["name"] == "a")
    assert eager["description"] == "real"


def test_all_deferred_returns_input_unchanged():
    """A 100% stub array is useless — keep every tool eager instead."""
    tools = [_tool("slack_a"), _tool("slack_b")]
    cfg = {
        "enabled": True,
        "mcp_server_prefixes": ["slack_"],
        "defer_mcp_tools": True,
    }
    out = _apply_tool_search(tools, cfg)
    # All tools matched the slack_ prefix -> no eager anchor -> unchanged.
    assert out is tools


def test_none_deferred_returns_input_unchanged():
    """Nothing matches the deferral policy -> no transformation needed."""
    tools = [_tool("a"), _tool("b")]
    cfg = {
        "enabled": True,
        "mcp_server_prefixes": ["slack_"],
        "defer_mcp_tools": True,
    }
    out = _apply_tool_search(tools, cfg)
    assert out is tools
