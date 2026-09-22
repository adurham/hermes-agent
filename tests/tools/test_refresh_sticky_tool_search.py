"""FORK: an MCP-refresh rebuild must not flip tool_search's bridge tools off.

``tools/tool_search.py::assemble_tool_defs`` recomputes the activate/deactivate
decision from the live, global ``tools/registry.py`` singleton on every call. The
fork therefore carries a per-conversation one-way latch,
``agent._tool_search_ever_activated``, threaded in as ``sticky_active``: once a
conversation has shown the bridge tools, they stay shown even if the live
deferrable-token total later drops back under threshold. Anthropic rejects any
previous-turn ``tool_use`` block naming a tool that has vanished from the wire
tools array, and ``_strip_unknown_tool_blocks`` then rewrites those into inert
text breadcrumbs -- corrupting tool-call history mid-conversation.

The v2026.9.14 merge extracted the refresh path out of ``tools/mcp_tool.py``
into the new ``tools/mcp_tool_agent.py`` and the latch did not come with it:
``refresh_agent_mcp_tools`` called ``get_tool_definitions`` with no
``sticky_active``, so it defaulted to False. That is the worst possible place to
lose it -- an MCP server reconnecting is exactly what most callers of this
function are reacting to, and it is exactly what shifts the token total.

These assert the real behaviour (are the bridge tools still in the rebuilt
snapshot?), not merely that a kwarg was forwarded.
"""

from __future__ import annotations

import types

import pytest

import model_tools
from tools import mcp_tool_agent as mcp_agent
from tools.tool_search_catalog import BRIDGE_TOOL_NAMES


def _tool(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "description": "", "parameters": {}}}


BRIDGE = sorted(BRIDGE_TOOL_NAMES)
PRIMARY_BRIDGE = BRIDGE[0]


def _agent(tool_names, *, ever_activated: bool):
    return types.SimpleNamespace(
        tools=[_tool(n) for n in tool_names],
        valid_tool_names=set(tool_names),
        enabled_toolsets=None,
        disabled_toolsets=None,
        _tool_search_ever_activated=ever_activated,
    )


@pytest.fixture
def recording_definitions(monkeypatch):
    """Stand in for get_tool_definitions, recording kwargs and honouring
    sticky_active the way the real assemble_tool_defs does: bridge tools are
    emitted when the (simulated) live total clears threshold OR when the caller
    passes the sticky latch."""
    state = {"naturally_active": False, "calls": []}

    def _fake(**kwargs):
        state["calls"].append(kwargs)
        defs = [_tool("read_file"), _tool("terminal")]
        if state["naturally_active"] or kwargs.get("sticky_active"):
            defs += [_tool(name) for name in BRIDGE]
        return defs

    monkeypatch.setattr(model_tools, "get_tool_definitions", _fake)
    return state


def test_refresh_keeps_bridge_tools_when_the_live_total_dropped(recording_definitions) -> None:
    """The regression: a conversation that already activated tool_search must
    still see the bridge tools after a rebuild, even though the live registry no
    longer clears the threshold on its own."""
    agent = _agent(["read_file", "terminal", *BRIDGE], ever_activated=True)
    recording_definitions["naturally_active"] = False  # an MCP server went away

    mcp_agent.refresh_agent_mcp_tools(agent, quiet_mode=True)

    missing = [name for name in BRIDGE if name not in agent.valid_tool_names]
    assert not missing, (
        f"the rebuild dropped bridge tools {missing} from the wire tools array; any "
        f"previous-turn tool_use naming them is now rejected by the provider"
    )


def test_refresh_forwards_the_sticky_latch(recording_definitions) -> None:
    """Mechanism check behind the behaviour above."""
    agent = _agent(["read_file"], ever_activated=True)
    mcp_agent.refresh_agent_mcp_tools(agent, quiet_mode=True)
    assert recording_definitions["calls"], "get_tool_definitions was never called"
    assert recording_definitions["calls"][-1].get("sticky_active") is True


def test_refresh_does_not_conjure_bridge_tools_for_a_fresh_agent(recording_definitions) -> None:
    """The latch is ONE-WAY: off->on still requires clearing the real threshold.
    A conversation that never activated must not gain bridge tools from a refresh."""
    agent = _agent(["read_file"], ever_activated=False)
    recording_definitions["naturally_active"] = False

    mcp_agent.refresh_agent_mcp_tools(agent, quiet_mode=True)

    assert recording_definitions["calls"][-1].get("sticky_active") is False
    assert not (agent.valid_tool_names & BRIDGE_TOOL_NAMES)
    assert agent._tool_search_ever_activated is False


def test_refresh_latches_the_flag_when_the_rebuild_shows_the_bridge(recording_definitions) -> None:
    """A rebuild that legitimately activates (live total now over threshold) must
    set the latch, so the NEXT rebuild keeps the bridge tools."""
    agent = _agent(["read_file"], ever_activated=False)
    recording_definitions["naturally_active"] = True  # a big MCP server just landed

    mcp_agent.refresh_agent_mcp_tools(agent, quiet_mode=True)
    assert agent._tool_search_ever_activated is True, "the sticky latch was not set"

    # Now the total drops again: the bridge must survive purely via the latch.
    recording_definitions["naturally_active"] = False
    agent.tools = [_tool(n) for n in ("read_file", *BRIDGE)]
    agent.valid_tool_names = {"read_file", *BRIDGE}
    mcp_agent.refresh_agent_mcp_tools(agent, quiet_mode=True)
    assert recording_definitions["calls"][-1].get("sticky_active") is True
    assert PRIMARY_BRIDGE in agent.valid_tool_names


def test_latch_never_clears_once_set(recording_definitions) -> None:
    """One-way: a rebuild WITHOUT bridge tools must not reset an already-set latch."""
    agent = _agent(["read_file"], ever_activated=True)
    mcp_agent._latch_tool_search_sticky(agent, [_tool("read_file")])
    assert agent._tool_search_ever_activated is True


def test_latch_survives_a_missing_attribute(recording_definitions) -> None:
    """Non-agent callers / partially-built agents must not raise: the flag is read
    with getattr and the latch is best-effort."""
    agent = types.SimpleNamespace(
        tools=[_tool("read_file")], valid_tool_names={"read_file"},
        enabled_toolsets=None, disabled_toolsets=None,
    )  # no _tool_search_ever_activated at all
    mcp_agent.refresh_agent_mcp_tools(agent, quiet_mode=True)
    assert recording_definitions["calls"][-1].get("sticky_active") is False
