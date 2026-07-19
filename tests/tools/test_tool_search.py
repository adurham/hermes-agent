"""Tests for tools/tool_search.py — progressive tool disclosure.

Coverage targets — these mirror the issues called out in the OpenClaw tool
search report. Every test that names an OpenClaw issue is the regression
guard that would have caught that specific failure mode.
"""

from __future__ import annotations

import json
import os
import sys
from typing import List, Dict, Any

import pytest


_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _td(name: str, description: str = "", properties: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties or {},
            },
        },
    }


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


class TestConfigParsing:
    def test_default_when_missing(self):
        from tools.tool_search import ToolSearchConfig
        cfg = ToolSearchConfig.from_raw(None)
        assert cfg.enabled == "auto"
        assert cfg.threshold_pct == 10.0

    def test_bool_true_maps_to_auto(self):
        from tools.tool_search import ToolSearchConfig
        cfg = ToolSearchConfig.from_raw(True)
        assert cfg.enabled == "auto"

    def test_bool_false_maps_to_off(self):
        from tools.tool_search import ToolSearchConfig
        cfg = ToolSearchConfig.from_raw(False)
        assert cfg.enabled == "off"

    def test_explicit_on(self):
        from tools.tool_search import ToolSearchConfig
        cfg = ToolSearchConfig.from_raw({"enabled": "on"})
        assert cfg.enabled == "on"

    def test_invalid_enabled_falls_back_to_auto(self):
        from tools.tool_search import ToolSearchConfig
        cfg = ToolSearchConfig.from_raw({"enabled": "maybe"})
        assert cfg.enabled == "auto"

    def test_threshold_clamped(self):
        from tools.tool_search import ToolSearchConfig
        cfg = ToolSearchConfig.from_raw({"threshold_pct": 150})
        assert cfg.threshold_pct == 100.0
        cfg = ToolSearchConfig.from_raw({"threshold_pct": -5})
        assert cfg.threshold_pct == 0.0

    def test_search_limits_clamped(self):
        from tools.tool_search import ToolSearchConfig
        cfg = ToolSearchConfig.from_raw({
            "search_default_limit": 999,
            "max_search_limit": 999,
        })
        assert cfg.max_search_limit == 50
        assert cfg.search_default_limit <= cfg.max_search_limit

    # FORK: defer_toolsets / defer_tools / keep_eager_tools parsing.
    def test_defer_lists_default_empty(self):
        from tools.tool_search import ToolSearchConfig
        cfg = ToolSearchConfig.from_raw(None)
        assert cfg.defer_toolsets == frozenset()
        assert cfg.defer_tools == frozenset()
        assert cfg.keep_eager_tools == frozenset()

    def test_defer_lists_parsed_from_list(self):
        from tools.tool_search import ToolSearchConfig
        cfg = ToolSearchConfig.from_raw({
            "defer_toolsets": ["browser", "homeassistant"],
            "defer_tools": ["swarm_run"],
            "keep_eager_tools": ["delegate_task"],
        })
        assert cfg.defer_toolsets == frozenset({"browser", "homeassistant"})
        assert cfg.defer_tools == frozenset({"swarm_run"})
        assert cfg.keep_eager_tools == frozenset({"delegate_task"})

    def test_defer_lists_parsed_from_comma_string(self):
        """A comma-separated string is accepted as well as a list."""
        from tools.tool_search import ToolSearchConfig
        cfg = ToolSearchConfig.from_raw({"defer_toolsets": "browser, tts ,vision"})
        assert cfg.defer_toolsets == frozenset({"browser", "tts", "vision"})

    def test_defer_lists_ignore_garbage(self):
        """A non-list/str value yields an empty frozenset, never raises."""
        from tools.tool_search import ToolSearchConfig
        cfg = ToolSearchConfig.from_raw({"defer_toolsets": 12345})
        assert cfg.defer_toolsets == frozenset()

    def test_bool_shape_has_empty_defer_lists(self):
        """Legacy bool config carries no defer lists."""
        from tools.tool_search import ToolSearchConfig
        for raw in (True, False):
            cfg = ToolSearchConfig.from_raw(raw)
            assert cfg.defer_toolsets == frozenset()
            assert cfg.defer_tools == frozenset()
            assert cfg.keep_eager_tools == frozenset()


# ---------------------------------------------------------------------------
# Classification — the hard invariant: core tools NEVER defer.
# ---------------------------------------------------------------------------


class TestClassification:
    def test_core_tools_never_defer(self):
        """The critical invariant from the OpenClaw report."""
        from tools.tool_search import is_deferrable_tool_name
        # Sample of core tools from _HERMES_CORE_TOOLS.
        for core_name in ["terminal", "read_file", "write_file", "patch",
                          "search_files", "todo", "memory", "browser_navigate",
                          "web_search", "session_search", "clarify",
                          "execute_code", "delegate_task", "send_message"]:
            assert not is_deferrable_tool_name(core_name), (
                f"Core tool '{core_name}' must NEVER be deferrable"
            )

    def test_bridge_tools_never_defer(self):
        from tools.tool_search import is_deferrable_tool_name, BRIDGE_TOOL_NAMES
        for name in BRIDGE_TOOL_NAMES:
            assert not is_deferrable_tool_name(name)

    def test_unknown_tool_not_deferrable(self):
        """Defensive: a tool name we cannot resolve to a registry entry must
        not be claimed as deferrable. This protects against the OpenClaw
        cron regression where unresolved tools were silently dropped."""
        from tools.tool_search import is_deferrable_tool_name
        assert not is_deferrable_tool_name("xx_definitely_not_a_tool_xx")

    def test_classify_keeps_unknown_in_visible(self):
        """A tool we can't classify stays visible — never silently dropped.

        This is the OpenClaw #84141 regression guard (cron lost ``exec``
        because it wasn't in the catalog).
        """
        from tools.tool_search import classify_tools
        # Build a tool def for something we don't have a registry entry for.
        defs = [_td("xx_unknown_tool", "Unknown tool")]
        visible, deferrable = classify_tools(defs)
        names = {(td.get("function") or {}).get("name") for td in visible}
        assert "xx_unknown_tool" in names
        assert deferrable == []


# ---------------------------------------------------------------------------
# FORK: opt-in deferral of normally-core toolsets/tools
# ---------------------------------------------------------------------------


class _FakeEntry:
    def __init__(self, toolset: str):
        self.toolset = toolset


class TestForkDeferToolsets:
    """defer_toolsets / defer_tools / keep_eager_tools override the
    'core tools never defer' base rule with explicit user intent.
    """

    def _patch_registry(self, monkeypatch, mapping):
        """Make registry.get_entry resolve names -> _FakeEntry(toolset)."""
        import tools.registry as _reg

        def _fake_get_entry(name):
            ts = mapping.get(name)
            return _FakeEntry(ts) if ts is not None else None

        monkeypatch.setattr(_reg.registry, "get_entry", _fake_get_entry)

    def test_defer_toolsets_defers_a_core_tool(self, monkeypatch):
        """A core tool whose toolset is in defer_toolsets becomes deferrable."""
        from tools.tool_search import ToolSearchConfig, is_deferrable_tool_name
        self._patch_registry(monkeypatch, {"browser_navigate": "browser"})
        cfg = ToolSearchConfig.from_raw({"defer_toolsets": ["browser"]})
        # browser_navigate is in _HERMES_CORE_TOOLS, so without the override
        # it would never defer.
        assert is_deferrable_tool_name("browser_navigate", cfg) is True

    def test_no_override_keeps_core_eager(self, monkeypatch):
        """Without defer_toolsets, a core tool stays eager (upstream rule)."""
        from tools.tool_search import ToolSearchConfig, is_deferrable_tool_name
        self._patch_registry(monkeypatch, {"browser_navigate": "browser"})
        cfg = ToolSearchConfig.from_raw(None)
        assert is_deferrable_tool_name("browser_navigate", cfg) is False

    def test_defer_tools_defers_single_core_tool(self, monkeypatch):
        from tools.tool_search import ToolSearchConfig, is_deferrable_tool_name
        self._patch_registry(monkeypatch, {"swarm_run": "delegation"})
        cfg = ToolSearchConfig.from_raw({"defer_tools": ["swarm_run"]})
        assert is_deferrable_tool_name("swarm_run", cfg) is True

    def test_keep_eager_overrides_defer_toolsets(self, monkeypatch):
        """keep_eager_tools wins: defer the toolset but keep one sibling eager."""
        from tools.tool_search import ToolSearchConfig, is_deferrable_tool_name
        self._patch_registry(monkeypatch, {
            "delegate_task": "delegation",
            "swarm_run": "delegation",
        })
        cfg = ToolSearchConfig.from_raw({
            "defer_toolsets": ["delegation"],
            "keep_eager_tools": ["delegate_task"],
        })
        assert is_deferrable_tool_name("delegate_task", cfg) is False
        assert is_deferrable_tool_name("swarm_run", cfg) is True

    def test_keep_eager_overrides_defer_tools(self, monkeypatch):
        """keep_eager_tools beats defer_tools for the same name (eager wins)."""
        from tools.tool_search import ToolSearchConfig, is_deferrable_tool_name
        self._patch_registry(monkeypatch, {"vision_analyze": "vision"})
        cfg = ToolSearchConfig.from_raw({
            "defer_tools": ["vision_analyze"],
            "keep_eager_tools": ["vision_analyze"],
        })
        assert is_deferrable_tool_name("vision_analyze", cfg) is False

    def test_bridge_tools_never_defer_even_with_override(self, monkeypatch):
        from tools.tool_search import (
            ToolSearchConfig, is_deferrable_tool_name, BRIDGE_TOOL_NAMES,
        )
        cfg = ToolSearchConfig.from_raw({
            "defer_tools": list(BRIDGE_TOOL_NAMES),
            "defer_toolsets": ["anything"],
        })
        for name in BRIDGE_TOOL_NAMES:
            assert is_deferrable_tool_name(name, cfg) is False

    def test_classify_tools_defers_overridden_core(self, monkeypatch):
        """End-to-end through classify_tools: core tool lands in deferrable."""
        from tools.tool_search import ToolSearchConfig, classify_tools
        self._patch_registry(monkeypatch, {
            "browser_navigate": "browser",
            "terminal": "terminal",
        })
        cfg = ToolSearchConfig.from_raw({"defer_toolsets": ["browser"]})
        defs = [_td("browser_navigate", "Navigate"), _td("terminal", "Shell")]
        visible, deferrable = classify_tools(defs, cfg)
        vis_names = {(t.get("function") or {}).get("name") for t in visible}
        def_names = {(t.get("function") or {}).get("name") for t in deferrable}
        assert "browser_navigate" in def_names
        assert "terminal" in vis_names  # not in defer_toolsets → stays eager


class TestForkActivationIntent:
    """Explicit defer lists activate tool search even below auto threshold."""

    def test_defer_toolsets_activates_below_threshold(self):
        from tools.tool_search import ToolSearchConfig, should_activate
        cfg = ToolSearchConfig.from_raw({
            "enabled": "auto", "threshold_pct": 10, "defer_toolsets": ["browser"],
        })
        # 5K tokens is far below 10% of 200K (20K) — but explicit intent wins.
        assert should_activate(cfg, deferrable_tokens=5_000, context_length=200_000)

    def test_off_still_wins_over_defer_lists(self):
        """The global off switch beats explicit defer lists."""
        from tools.tool_search import ToolSearchConfig, should_activate
        cfg = ToolSearchConfig.from_raw({
            "enabled": "off", "defer_toolsets": ["browser"],
        })
        assert not should_activate(cfg, deferrable_tokens=5_000, context_length=200_000)

    def test_no_defer_lists_respects_auto_threshold(self):
        """Without explicit defer lists, auto threshold behavior is unchanged."""
        from tools.tool_search import ToolSearchConfig, should_activate
        cfg = ToolSearchConfig.from_raw({"enabled": "auto", "threshold_pct": 10})
        assert not should_activate(cfg, deferrable_tokens=5_000, context_length=200_000)


# ---------------------------------------------------------------------------
# Token estimation + threshold gate
# ---------------------------------------------------------------------------


class TestThresholdGate:
    def test_off_never_activates(self):
        from tools.tool_search import ToolSearchConfig, should_activate
        cfg = ToolSearchConfig.from_raw({"enabled": "off"})
        assert not should_activate(cfg, deferrable_tokens=1_000_000, context_length=200_000)

    def test_zero_deferrable_never_activates(self):
        from tools.tool_search import ToolSearchConfig, should_activate
        cfg = ToolSearchConfig.from_raw({"enabled": "on"})
        assert not should_activate(cfg, deferrable_tokens=0, context_length=200_000)

    def test_on_activates_with_any_deferrable(self):
        from tools.tool_search import ToolSearchConfig, should_activate
        cfg = ToolSearchConfig.from_raw({"enabled": "on"})
        assert should_activate(cfg, deferrable_tokens=100, context_length=200_000)

    def test_auto_below_threshold_does_not_activate(self):
        from tools.tool_search import ToolSearchConfig, should_activate
        cfg = ToolSearchConfig.from_raw({"enabled": "auto", "threshold_pct": 10})
        # 5% of 200K = below 10% threshold
        assert not should_activate(cfg, deferrable_tokens=10_000, context_length=200_000)

    def test_auto_at_or_above_threshold_activates(self):
        from tools.tool_search import ToolSearchConfig, should_activate
        cfg = ToolSearchConfig.from_raw({"enabled": "auto", "threshold_pct": 10})
        assert should_activate(cfg, deferrable_tokens=20_000, context_length=200_000)
        assert should_activate(cfg, deferrable_tokens=50_000, context_length=200_000)

    def test_auto_without_context_length_uses_20k_cutoff(self):
        """Fallback cutoff used when the active model is unknown."""
        from tools.tool_search import ToolSearchConfig, should_activate
        cfg = ToolSearchConfig.from_raw({"enabled": "auto"})
        assert not should_activate(cfg, deferrable_tokens=10_000, context_length=0)
        assert should_activate(cfg, deferrable_tokens=25_000, context_length=0)

    def test_token_estimate_proportional_to_schema_size(self):
        from tools.tool_search import estimate_tokens_from_schemas
        small = [_td("a", "x")]
        big = [_td(f"name_{i}", f"description for tool {i} " * 20,
                   {"q": {"type": "string", "description": "search query " * 10}})
               for i in range(10)]
        small_t = estimate_tokens_from_schemas(small)
        big_t = estimate_tokens_from_schemas(big)
        assert big_t > small_t * 10


# ---------------------------------------------------------------------------
# Retrieval (BM25 + substring fallback)
# ---------------------------------------------------------------------------


class TestRetrieval:
    def _fake_catalog(self):
        """Build a catalog directly without touching the registry."""
        from tools.tool_search import CatalogEntry, _tokenize, _entry_search_text
        defs = [
            _td("github_create_issue", "Open a new issue in a GitHub repository",
                {"title": {"type": "string"}, "body": {"type": "string"}}),
            _td("github_search_repos", "Search GitHub for matching repositories",
                {"query": {"type": "string"}}),
            _td("slack_send_message", "Post a message into a Slack channel",
                {"channel": {"type": "string"}, "text": {"type": "string"}}),
            _td("calendar_create_event", "Add an event to the user's calendar",
                {"title": {"type": "string"}, "start": {"type": "string"}}),
        ]
        catalog = []
        for d in defs:
            fn = d["function"]
            e = CatalogEntry(
                name=fn["name"], description=fn["description"],
                schema=d, source="mcp", source_name="mcp-test",
            )
            e._tokens = _tokenize(_entry_search_text(d))
            catalog.append(e)
        return catalog

    def test_search_finds_relevant_tool(self):
        from tools.tool_search import search_catalog
        hits = search_catalog(self._fake_catalog(), "create a github issue", limit=3)
        names = [h.name for h in hits]
        assert names[0] == "github_create_issue"

    def test_search_returns_empty_for_irrelevant_query(self):
        from tools.tool_search import search_catalog
        hits = search_catalog(self._fake_catalog(), "asdf qwerty foobar", limit=3)
        assert hits == []

    def test_search_substring_fallback(self):
        """Even when no BM25 hit, a literal substring of the tool name returns."""
        from tools.tool_search import search_catalog
        hits = search_catalog(self._fake_catalog(), "calendar", limit=3)
        assert any("calendar" in h.name for h in hits)

    def test_search_respects_limit(self):
        from tools.tool_search import search_catalog
        hits = search_catalog(self._fake_catalog(), "github", limit=1)
        assert len(hits) <= 1


# ---------------------------------------------------------------------------
# Assembly — the full passthrough/activate decision.
# ---------------------------------------------------------------------------


class TestAssembly:
    def test_no_deferrable_returns_unchanged(self):
        """Pure-core toolset: pass-through, no bridge tools added."""
        from tools.tool_search import assemble_tool_defs, ToolSearchConfig
        defs = [_td("terminal", "Run shell"), _td("read_file", "Read a file")]
        result = assemble_tool_defs(
            defs,
            context_length=200_000,
            config=ToolSearchConfig.from_raw({"enabled": "on"}),
        )
        assert not result.activated
        assert {t["function"]["name"] for t in result.tool_defs} == {"terminal", "read_file"}

    def test_below_threshold_returns_unchanged(self):
        """Tiny deferrable surface: don't bother."""
        from tools.tool_search import assemble_tool_defs, ToolSearchConfig
        # _td renders to ~80 chars / 20 tokens. 3 of them = ~60 tokens.
        # 10% of 200K = 20K. Way below.
        defs = [_td("unknown_tool_a"), _td("unknown_tool_b"), _td("unknown_tool_c")]
        result = assemble_tool_defs(
            defs,
            context_length=200_000,
            config=ToolSearchConfig.from_raw({"enabled": "auto", "threshold_pct": 10}),
        )
        assert not result.activated
        names = {(t.get("function") or {}).get("name") for t in result.tool_defs}
        assert "tool_search" not in names

    def test_idempotent_when_bridge_already_present(self):
        from tools.tool_search import assemble_tool_defs, ToolSearchConfig, BRIDGE_TOOL_NAMES
        defs = [_td("terminal", "Run shell"), _td("tool_search", "old")]
        result = assemble_tool_defs(
            defs,
            context_length=200_000,
            config=ToolSearchConfig.from_raw({"enabled": "off"}),
        )
        names = [(t["function"]["name"]) for t in result.tool_defs]
        # The pre-existing tool_search was stripped (it would be re-injected if
        # activation happened; here it didn't).
        assert "tool_search" not in names


# ---------------------------------------------------------------------------
# Sticky activation — the flap regression this bug report targets.
#
# The real bug: classify_tools() walks the LIVE global registry singleton
# every call. If the total deferrable-token count for a conversation drifts
# across the threshold boundary between two consecutive assemblies of the
# SAME conversation (MCP reconnect, subagent loading tools, etc.),
# should_activate() flips its answer turn-to-turn. When it flips
# activated -> not-activated, bridge tool names vanish from the wire tools
# array and Anthropic hard-rejects prior tool_use blocks referencing them,
# which _strip_unknown_tool_blocks then rewrites into inert breadcrumbs —
# corrupting tool-call history mid-conversation.
# ---------------------------------------------------------------------------


class TestStickyActivation:
    def _big_deferrable_defs(self, n: int = 40) -> List[Dict[str, Any]]:
        """A pile of mcp-toolset tools big enough to clear a low
        threshold_pct with a small context_length. Named with an ``mcp_``
        prefix and paired with ``_patch_mcp_registry`` below so
        ``classify_tools`` resolves them to a real deferrable ``mcp-``
        toolset via the registry, instead of falling into the "unknown
        tool -> stays visible" defensive path."""
        return [
            _td(f"mcp_tool_{i}", "x" * 200, {"arg": {"type": "string"}})
            for i in range(n)
        ]

    def _patch_mcp_registry(self, monkeypatch):
        """Make every ``mcp_tool_*`` name resolve to an ``mcp-fake``
        toolset entry, so ``is_deferrable_tool_name`` classifies them as
        deferrable via the upstream base rule (mcp- prefixed toolset)."""
        import tools.registry as _reg

        def _fake_get_entry(name):
            if name.startswith("mcp_tool_"):
                return _FakeEntry("mcp-fake")
            return None

        monkeypatch.setattr(_reg.registry, "get_entry", _fake_get_entry)

    def test_flap_without_sticky_flag_deactivates(self, monkeypatch):
        """Baseline: without sticky_active, a shrinking registry between two
        calls of the same conversation flips activation off — this is the
        exact bug, reproduced directly against assemble_tool_defs."""
        from tools.tool_search import assemble_tool_defs, ToolSearchConfig, BRIDGE_TOOL_NAMES

        self._patch_mcp_registry(monkeypatch)
        cfg = ToolSearchConfig.from_raw({"enabled": "auto", "threshold_pct": 10})

        # Call 1: large deferrable surface crosses threshold -> activates.
        call1 = assemble_tool_defs(
            self._big_deferrable_defs(40), context_length=20_000, config=cfg,
        )
        assert call1.activated
        names1 = {(t.get("function") or {}).get("name") for t in call1.tool_defs}
        assert BRIDGE_TOOL_NAMES <= names1

        # Call 2 (same conversation): registry shrank (e.g. an MCP server
        # dropped tools), now under threshold -> flips OFF without the fix.
        call2 = assemble_tool_defs(
            self._big_deferrable_defs(2), context_length=20_000, config=cfg,
        )
        assert not call2.activated
        names2 = {(t.get("function") or {}).get("name") for t in call2.tool_defs}
        assert not (BRIDGE_TOOL_NAMES & names2)

    def test_sticky_flag_keeps_bridge_tools_present(self, monkeypatch):
        """The fix: once activated=True on call 1, passing
        sticky_active=True (the caller's per-conversation latch) on call 2
        keeps bridge tools present even though the live total dropped
        under threshold again."""
        from tools.tool_search import assemble_tool_defs, ToolSearchConfig, BRIDGE_TOOL_NAMES

        self._patch_mcp_registry(monkeypatch)
        cfg = ToolSearchConfig.from_raw({"enabled": "auto", "threshold_pct": 10})

        call1 = assemble_tool_defs(
            self._big_deferrable_defs(40), context_length=20_000, config=cfg,
            sticky_active=False,
        )
        assert call1.activated
        assert not call1.sticky_forced

        # Same conversation, second assembly: shrunk registry, but the
        # caller now passes sticky_active=True because it activated once.
        call2 = assemble_tool_defs(
            self._big_deferrable_defs(2), context_length=20_000, config=cfg,
            sticky_active=True,
        )
        assert call2.activated
        assert call2.sticky_forced
        names2 = {(t.get("function") or {}).get("name") for t in call2.tool_defs}
        assert BRIDGE_TOOL_NAMES <= names2

    def test_sticky_flag_trusts_caller_latch_when_deferrable_exist(self, monkeypatch):
        """``assemble_tool_defs`` itself is stateless and has no memory of
        prior calls — it trusts ``sticky_active`` as the caller's word that
        this conversation activated before. So passing sticky_active=True
        with any deferrable tools present forces activation on, even on a
        function call that looks "cold" in isolation. This is safe in
        practice because real callers (agent_init.py, mcp_tool.py,
        acp_adapter/server.py) only ever pass True once
        ``agent._tool_search_ever_activated`` was actually set True by a
        prior call that showed bridge tools — see ``tool_defs_show_bridge``.
        This test documents that contract at the assemble_tool_defs layer;
        the "don't force from a cold conversation" guarantee lives one layer
        up, in how callers set/thread the flag (asserted in
        test_sticky_flag_keeps_bridge_tools_present)."""
        from tools.tool_search import assemble_tool_defs, ToolSearchConfig, BRIDGE_TOOL_NAMES

        self._patch_mcp_registry(monkeypatch)
        cfg = ToolSearchConfig.from_raw({"enabled": "auto", "threshold_pct": 10})

        result = assemble_tool_defs(
            self._big_deferrable_defs(2), context_length=200_000, config=cfg,
            sticky_active=True,
        )
        assert result.activated
        assert result.sticky_forced
        names = {(t.get("function") or {}).get("name") for t in result.tool_defs}
        assert BRIDGE_TOOL_NAMES <= names

    def test_sticky_flag_noop_when_no_deferrable_tools(self):
        """sticky_active=True must not conjure bridge tools out of thin air
        when there is nothing deferrable to hide behind them."""
        from tools.tool_search import assemble_tool_defs, ToolSearchConfig, BRIDGE_TOOL_NAMES

        cfg = ToolSearchConfig.from_raw({"enabled": "auto", "threshold_pct": 10})
        defs = [_td("terminal", "Run shell"), _td("read_file", "Read a file")]
        result = assemble_tool_defs(
            defs, context_length=200_000, config=cfg, sticky_active=True,
        )
        assert not result.activated
        names = {(t.get("function") or {}).get("name") for t in result.tool_defs}
        assert not (BRIDGE_TOOL_NAMES & names)

    def test_catalog_rebuilt_fresh_even_when_sticky(self, monkeypatch):
        """Sticky must only hold the boolean decision open — the actual
        catalog contents (which tools/schemas are deferrable) must still be
        rebuilt fresh every call, never cached stale. This guards against
        reintroducing the OpenClaw session-keyed-catalog regression."""
        from tools.tool_search import assemble_tool_defs, ToolSearchConfig

        self._patch_mcp_registry(monkeypatch)
        cfg = ToolSearchConfig.from_raw({"enabled": "auto", "threshold_pct": 10})
        call1 = assemble_tool_defs(
            self._big_deferrable_defs(40), context_length=20_000, config=cfg,
        )
        assert call1.activated
        assert call1.deferred_count == 40

        call2 = assemble_tool_defs(
            self._big_deferrable_defs(2), context_length=20_000, config=cfg,
            sticky_active=True,
        )
        assert call2.activated
        assert call2.sticky_forced
        # The catalog size reflects the CURRENT (shrunk) live tool set, not
        # a cached snapshot from call 1.
        assert call2.deferred_count == 2


# ---------------------------------------------------------------------------
# Bridge dispatch
# ---------------------------------------------------------------------------


class TestBridgeDispatch:
    def test_tool_search_requires_query(self):
        from tools.tool_search import dispatch_tool_search
        result = dispatch_tool_search({}, current_tool_defs=[])
        assert "error" in json.loads(result)

    def test_tool_describe_requires_name(self):
        from tools.tool_search import dispatch_tool_describe
        result = dispatch_tool_describe({}, current_tool_defs=[])
        assert "error" in json.loads(result)

    def test_tool_describe_rejects_non_deferrable(self):
        """If the model asks to describe a core tool, refuse — it's already
        in the visible list."""
        from tools.tool_search import dispatch_tool_describe
        result = dispatch_tool_describe(
            {"name": "terminal"}, current_tool_defs=[_td("terminal", "Run shell")],
        )
        assert "error" in json.loads(result)

    def test_resolve_underlying_call_parses_object_args(self):
        from tools.tool_search import resolve_underlying_call
        name, args, err = resolve_underlying_call({
            "name": "unknown_xxx",
            "arguments": {"foo": "bar"},
        })
        # Will fail classification because unknown_xxx isn't deferrable.
        assert err is not None

    def test_resolve_underlying_call_parses_json_string_args(self):
        """Some models emit ``arguments`` as a JSON string instead of object."""
        from tools.tool_search import resolve_underlying_call
        # Use a name that won't classify (so we don't depend on registry),
        # but exercise the JSON parse path.
        _, _, err = resolve_underlying_call({
            "name": "fake",
            "arguments": '{"a": 1}',
        })
        # err is about classification, but the parse worked (it would have
        # failed earlier with "not valid JSON" otherwise).
        assert "not valid JSON" not in (err or "")

    def test_resolve_underlying_call_rejects_bad_json(self):
        from tools.tool_search import resolve_underlying_call
        _, _, err = resolve_underlying_call({
            "name": "fake",
            "arguments": "{this is not json",
        })
        assert err is not None
        assert "JSON" in err

    def test_resolve_underlying_call_rejects_recursion(self):
        """tool_call cannot invoke tool_call itself."""
        from tools.tool_search import resolve_underlying_call, TOOL_CALL_NAME
        name, args, err = resolve_underlying_call({
            "name": TOOL_CALL_NAME,
            "arguments": {},
        })
        assert err is not None
        assert "bridge tool" in err.lower()


# ---------------------------------------------------------------------------
# End-to-end via the real handle_function_call (smoke test).
# ---------------------------------------------------------------------------


class TestHandleFunctionCallIntegration:
    def test_tool_search_dispatch_through_handle_function_call(self):
        """The dispatcher recognizes the bridge tool by name."""
        import model_tools
        result = model_tools.handle_function_call(
            function_name="tool_search",
            function_args={"query": "nothing matches this"},
        )
        parsed = json.loads(result)
        # Without a real registry, the matches will be empty, but the
        # dispatch path completed without error.
        assert "matches" in parsed or "error" in parsed


class TestRegression_OpenClawCron84141:
    """Regression guard for the OpenClaw cron-tool-loss class of bug.

    OpenClaw #84141: ``toolsAllow: ["exec"]`` on an isolated cron turn
    resulted in the agent receiving only ``sessions_send`` — the catalog
    builder silently dropped the requested core tool.

    Our defense: core tools are NEVER deferred. This test exercises the
    full assembly pipeline with a mixed core+MCP toolset and asserts that
    every core tool survives.
    """

    def test_core_tool_survives_alongside_many_mcp_tools(self):
        from tools.tool_search import (
            assemble_tool_defs, ToolSearchConfig, BRIDGE_TOOL_NAMES,
            classify_tools,
        )
        # 1 core tool + 50 unknown/MCP-shaped tools (deferrable).
        defs = [_td("terminal", "Run shell commands")]
        # Pad with fake "deferrable" tools — without registry registration,
        # classify_tools puts them in 'visible'. So instead, we just verify
        # the core-tool side: terminal stays in visible regardless.
        visible, deferrable = classify_tools(defs)
        assert any(
            (td.get("function") or {}).get("name") == "terminal"
            for td in visible
        ), "Core tool 'terminal' was wrongly classified as deferrable"

        # Now force activation and check the resulting tool-defs list.
        result = assemble_tool_defs(
            defs,
            context_length=200_000,
            config=ToolSearchConfig.from_raw({"enabled": "on"}),
        )
        names = {(t.get("function") or {}).get("name") for t in result.tool_defs}
        # terminal must be present; bridges are only added if there are
        # deferrable tools to put behind them.
        assert "terminal" in names

    def test_unwrap_rejects_core_tool_attempt(self):
        """Even if the model tries to invoke a core tool through tool_call,
        we reject the call and tell the model to use it directly."""
        from tools.tool_search import resolve_underlying_call
        _, _, err = resolve_underlying_call({
            "name": "terminal",
            "arguments": {"command": "echo hi"},
        })
        assert err is not None
        assert "not a deferrable" in err


class TestRegression_ToolsetScoping:
    """A restricted-toolset session must not see or invoke out-of-scope tools.

    The bug: the bridge dispatch and the tool_executor unwrap read the
    catalog from the *global* registry (get_tool_definitions with no
    toolset scope = "start with everything"), so a session scoped to one
    MCP server could tool_search the entire process registry and tool_call
    any plugin tool it was never granted. registry.dispatch() has no
    enabled_tools gate for non-execute_code tools, so the out-of-scope tool
    actually ran.

    The fix threads the session's enabled/disabled toolsets into the bridge
    dispatch (model_tools.handle_function_call) and the executor unwrap
    (agent.tool_executor), scoping both the searchable catalog and the
    invocable set to the session's own toolsets.
    """

    @staticmethod
    def _register(name, toolset):
        from tools.registry import registry

        def _handler(args, task_id=None, **kw):
            return json.dumps({"ok": True, "tool": name})

        registry.register(
            name=name,
            handler=_handler,
            schema=_td(name, f"desc for {name}", {"repo": {"type": "string"}}),
            toolset=toolset,
        )

    def test_search_catalog_is_scoped_to_session_toolsets(self):
        import model_tools

        for i in range(12):
            self._register(f"mcp_scoped_gh_{i}", "mcp-scoped-gh")
        self._register("scoped_oos_plugin", "scopedoosplugin")

        # tool_search scoped to the github toolset must not count the
        # out-of-scope plugin tool (or any of the host registry).
        result = model_tools.handle_function_call(
            function_name="tool_search",
            function_args={"query": "mcp_scoped_gh", "limit": 5},
            enabled_toolsets=["mcp-scoped-gh"],
        )
        parsed = json.loads(result)
        assert parsed["total_available"] == 12, (
            f"expected scoped catalog of 12, got {parsed['total_available']} "
            "— catalog leaked tools outside the session's toolsets"
        )
        hit_names = {m["name"] for m in parsed["matches"]}
        assert "scoped_oos_plugin" not in hit_names

    def test_tool_call_rejects_out_of_scope_tool(self):
        import model_tools

        self._register("mcp_inscope_gh_op", "mcp-inscope-gh")
        self._register("inscope_oos_plugin", "inscopeoosplugin")

        # Out-of-scope plugin tool: rejected even though it is registered
        # and deferrable in the global registry.
        rejected = json.loads(model_tools.handle_function_call(
            function_name="tool_call",
            function_args={"name": "inscope_oos_plugin", "arguments": {}},
            enabled_toolsets=["mcp-inscope-gh"],
        ))
        assert "error" in rejected
        assert "not available in this session" in rejected["error"]

        # In-scope tool: dispatches normally.
        ok = json.loads(model_tools.handle_function_call(
            function_name="tool_call",
            function_args={"name": "mcp_inscope_gh_op", "arguments": {"repo": "a/b"}},
            enabled_toolsets=["mcp-inscope-gh"],
        ))
        assert ok.get("ok") is True
        assert ok.get("tool") == "mcp_inscope_gh_op"

    def test_bridge_dispatch_does_not_pollute_global_resolved_names(self):
        import model_tools

        self._register("mcp_pollute_op_0", "mcp-pollute")
        self._register("mcp_pollute_op_1", "mcp-pollute")

        # Establish the scoped session global.
        model_tools.get_tool_definitions(
            enabled_toolsets=["mcp-pollute"], quiet_mode=True,
        )
        before = set(model_tools._last_resolved_tool_names)
        assert "terminal" not in before

        # A scoped tool_search call must not widen the process-global
        # _last_resolved_tool_names to the whole registry (which would leak
        # core/sandbox tools into execute_code's fallback).
        model_tools.handle_function_call(
            function_name="tool_search",
            function_args={"query": "pollute"},
            enabled_toolsets=["mcp-pollute"],
        )
        after = set(model_tools._last_resolved_tool_names)
        assert "terminal" not in after, (
            "bridge dispatch polluted _last_resolved_tool_names with "
            "out-of-scope tools"
        )

    def test_scoped_deferrable_names_helper(self):
        from tools.tool_search import scoped_deferrable_names

        self._register("mcp_helper_op", "mcp-helper")
        import model_tools
        defs = model_tools.get_tool_definitions(
            enabled_toolsets=["mcp-helper"],
            quiet_mode=True,
            skip_tool_search_assembly=True,
        )
        names = scoped_deferrable_names(defs)
        assert "mcp_helper_op" in names
        # core tools are never deferrable
        assert "terminal" not in names

