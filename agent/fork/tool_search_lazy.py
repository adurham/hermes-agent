"""Client-side lazy MCP tool loading (fork-only).

When ``tool_search.mode == "client_side"``, the anthropic adapter ships
name-only stubs for deferred MCP tools and inflates them to full schemas
only for names present in ``agent._promoted_tools``.  The set survives
for the lifetime of the agent (one session) so the model doesn't have
to re-discover the same tools every turn.

This avoids the prompt-multiplier problem that vanilla MCP tool loading
hits at scale (300+ MCP tools = 100K+ tokens of schemas per turn).
"""

from __future__ import annotations

from typing import Optional


def build_tool_search_config(agent) -> Optional[Dict[str, Any]]:
    """Build the tool_search_config dict for Anthropic adapter, or None.

    Reads ``tool_search`` from config.yaml on every call so /toolsearch
    toggles take effect without process restart. Returns None when the
    feature is disabled, when there are no MCP servers configured (no
    prefixes to defer against), or when config can't be loaded.

    Returned dict is consumed by ``agent.anthropic_adapter._apply_tool_search``;
    see its docstring for the schema.
    """
    try:
        from hermes_cli.config import load_config as _load_cfg
        cfg = _load_cfg() or {}
    except Exception:
        return None

    ts_cfg = cfg.get("tool_search") if isinstance(cfg, dict) else None
    if not isinstance(ts_cfg, dict) or not ts_cfg.get("enabled"):
        return None

    # Mode selects how lazy loading is performed.
    #   "server_side" — Anthropic's tool_search_tool_* server tool (legacy).
    #     Inlines schemas server-side, which charges the full prompt PER
    #     server iteration within one API call. Two stacked tool_search
    #     calls = 3x prompt billing. See agent.log forensics from
    #     2026-05-13 (case 00271597 session).
    #   "client_side" — Hermes-side hermes_load_tools tool. Each schema-
    #     load is one normal round-trip; no multiplier. Default for new
    #     installs.
    # Back-compat: an existing config with `enabled: true` and no `mode`
    # key gets "client_side" automatically — the better behavior for any
    # API-key user. The OAuth wire-bytes argument that motivated the
    # original server_side default (per _apply_tool_search comments) only
    # benefits OAuth/Claude-subscription users; regular API users always
    # paid the multiplier cost without getting that benefit.
    mode = (ts_cfg.get("mode") or "client_side").strip().lower()
    if mode not in {"client_side", "server_side"}:
        mode = "client_side"

    # Build MCP server prefixes from the configured mcp_servers map.
    # Each prefix matches the sanitized server name + "_" — matching the
    # registration form in tools/mcp_tool.py::_convert_mcp_schema
    # (``f"{safe_server_name}_{safe_tool_name}"``). Without this, the
    # defer policy can't tell built-in tools from MCP-sourced ones.
    prefixes: list[str] = []
    mcp_servers = cfg.get("mcp_servers") if isinstance(cfg, dict) else None
    if isinstance(mcp_servers, dict):
        try:
            from tools.mcp_tool import sanitize_mcp_name_component as _san
        except Exception:
            _san = lambda s: re.sub(r"[^A-Za-z0-9_]", "_", str(s or ""))
        for name, server_cfg in mcp_servers.items():
            if isinstance(server_cfg, dict) and server_cfg.get("enabled") is False:
                continue
            prefixes.append(f"{_san(name)}_")

    return {
        "enabled": True,
        "mode": mode,
        "variant": ts_cfg.get("variant", "regex"),
        "defer_mcp_tools": ts_cfg.get("defer_mcp_tools", True),
        "additional_eager": list(ts_cfg.get("additional_eager") or []),
        "additional_deferred": list(ts_cfg.get("additional_deferred") or []),
        "mcp_server_prefixes": prefixes,
        # Snapshot of the agent's promoted-tools set. Consumed by the
        # anthropic adapter in client_side mode to inflate stubs back to
        # full schemas for names the model has already discovered.
        # Empty set when not in client_side mode (harmless).
        "promoted_tools": set(getattr(agent, "_promoted_tools", set()) or ()),
    }


def currently_deferred_names(agent) -> Optional[Set[str]]:
    """Return the set of tool names currently shown to the model as stubs.

    Used by ``hermes_load_tools`` to classify requested names into
    ``loaded`` vs ``already_eager`` vs ``unknown``.  Mirrors the deferral
    policy in ``agent.anthropic_adapter._apply_tool_search`` (kept in
    sync by hand — if you change one, change the other).

    Returns None when tool_search is off (no concept of "deferred"
    applies; hermes_load_tools then just classifies everything as
    ``already_eager`` rather than discriminating).
    """
    ts = agent._build_tool_search_config()
    if not ts:
        return None
    eager_names = set(ts.get("additional_eager") or ())
    deferred_names = set(ts.get("additional_deferred") or ())
    mcp_prefixes = tuple(ts.get("mcp_server_prefixes") or ())
    defer_mcp = bool(ts.get("defer_mcp_tools", True))
    promoted = set(ts.get("promoted_tools") or ())
    out: Set[str] = set()
    for name in agent.valid_tool_names or ():
        if name in promoted:
            continue
        if name in eager_names:
            continue
        if name in deferred_names:
            out.add(name)
            continue
        if defer_mcp and mcp_prefixes and name.startswith(mcp_prefixes):
            out.add(name)
    return out
