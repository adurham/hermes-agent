"""A configured MCP server that is down must never look like "no such capability".

Regression for 2026-10-20: the Slack connector bridge's login expired, the
server registered zero tools, and an agent whose tool_search for "slack" came
back empty told the user it had no Slack tool. These pin the Hermes-side
surfaces: status, banner, tool_search, and the once-per-failure CLI notice.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import mcp_types

from hermes_cli import banner, mcp_startup
from tools import mcp_tool_discovery as disc
from tools import tool_search as ts


def _degraded_server():
    tool = mcp_types.Tool(name="auth_expired_relogin_required", inputSchema={"type": "object"},
                          description="UNAVAILABLE -- Slack connector auth expired",
                          _meta={disc.MCP_DEGRADED_META_KEY: {"kind": "reauth", "message": "Slack connector auth "
                                 "expired. Fix: run `CLAUDE_CONFIG_DIR=/x claude auth login`"}})
    return SimpleNamespace(_tools=[tool])


def test_degraded_info_read_from_stub_meta():
    info = disc.mcp_server_degraded_info(_degraded_server())
    assert info["kind"] == "reauth"
    assert disc.mcp_server_degraded_info(SimpleNamespace(_tools=[
        mcp_types.Tool(name="ok", inputSchema={"type": "object"})])) is None


def test_unhealthy_lists_failed_connecting_and_degraded_but_not_lazy_or_disabled():
    status = [
        {"name": "slack", "connected": True, "status": "connected", "degraded": {"message": "auth expired"}},
        {"name": "notion", "connected": False, "status": "failed", "error": "Connection closed"},
        {"name": "pd", "connected": False, "status": "connecting"},
        {"name": "lazy", "connected": False, "status": "lazy"},
        {"name": "off", "connected": False, "status": "disabled", "disabled": True},
        {"name": "fine", "connected": True, "status": "connected"},
    ]
    got = {u["name"]: u["status"] for u in disc.unhealthy_mcp_servers(status)}
    assert got == {"slack": "degraded", "notion": "failed", "pd": "connecting"}


def test_banner_shows_degraded_server_in_red_with_reason():
    line = banner._mcp_server_line({"name": "slack", "transport": "stdio", "connected": True, "tools": 1,
                                    "degraded": {"message": "Slack connector auth expired"}}, dim="d", text="t")
    assert "UNAVAILABLE" in line and "auth expired" in line and "[red]" in line


def _search(queries, unhealthy, defs=()):
    with patch.object(ts, "_unhealthy_mcp_servers", return_value=unhealthy), \
            patch.object(ts, "_record_search_metric"):
        return json.loads(ts.dispatch_tool_search({"queries": queries}, current_tool_defs=list(defs),
                                                  config=ts.ToolSearchConfig.from_raw({})))


def test_tool_search_for_down_server_names_it_and_its_fix():
    out = _search(["slack send message"], [{"name": "slack", "status": "failed",
                                            "message": "Slack connector auth expired: run `claude auth login`"}])
    group = out["results"][0]
    assert group["unavailable_servers"][0]["name"] == "slack"
    assert "Do not tell the user the capability does not exist" in group["unavailable_hint"]
    assert out["unavailable_mcp_servers"][0]["message"].startswith("Slack connector auth expired")


def test_tool_search_empty_result_lists_all_unhealthy_servers():
    out = _search(["zzz unrelated"], [{"name": "notion", "status": "failed", "message": "x"}])
    assert [u["name"] for u in out["results"][0]["unavailable_servers"]] == ["notion"]


def test_tool_search_unchanged_when_everything_is_healthy():
    out = _search(["slack"], [])
    assert "unavailable_servers" not in out["results"][0] and "unavailable_mcp_servers" not in out


def test_server_query_matching():
    assert ts._server_matches_query("slack", "Slack post message")
    assert ts._server_matches_query("tanium-provisioner", "provisioner create lab")
    assert ts._server_matches_query("microsoft365", "microsoft365 email")
    assert not ts._server_matches_query("slack", "notion page")


def test_cli_announces_each_failure_once(monkeypatch):
    monkeypatch.setattr(mcp_startup, "_reported_unhealthy", set())
    entries = [{"name": "slack", "status": "degraded", "message": "Slack connector auth expired"}]
    lines: list[str] = []
    with patch.object(mcp_startup, "unhealthy_mcp_server_report", return_value=entries):
        assert mcp_startup.announce_unhealthy_mcp_servers(lines.append) == 1
        assert mcp_startup.announce_unhealthy_mcp_servers(lines.append) == 0
    assert lines == ["⚠️  MCP server 'slack' UNAVAILABLE: Slack connector auth expired"]


def test_report_honours_toolset_filter(monkeypatch):
    monkeypatch.setattr(mcp_startup, "_mcp_server_filter", ["slack"])
    rows = [{"name": "slack", "status": "failed", "message": "a"},
            {"name": "notion", "status": "failed", "message": "b"},
            {"name": "slack2", "status": "connecting", "message": "c"}]
    with patch.object(disc, "unhealthy_mcp_servers", return_value=rows):
        assert [r["name"] for r in mcp_startup.unhealthy_mcp_server_report()] == ["slack"]
