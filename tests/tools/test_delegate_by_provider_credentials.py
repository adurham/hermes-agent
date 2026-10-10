"""``delegation.by_provider`` scoping in child credential resolution.

An upstream-sync merge dropped the fork's ``by_provider`` consumption from
``_resolve_delegation_credentials``, so a main session running on
``claude-subscription-directsdk-experimental`` (or exo, or ollama-cloud) got
children resolving to model=None/provider=None — i.e. silently inheriting the
PARENT's own model/provider — instead of the block the user configured for
that provider.

These are behavior contracts on the resolved credential bundle: a matching
block REPLACES the config wholesale, a non-matching one leaves it unchanged,
matching is case-insensitive on BOTH sides, and the block's model/provider —
never the parent's — is what reaches the per-task routes.

The real ``_resolve_delegation_credentials`` / ``_resolve_task_routes`` run
here; only ``resolve_runtime_provider`` (the process/network boundary) is
faked, so the resolution chain itself is exercised, not mocked past.
"""

import threading
from unittest.mock import MagicMock

import pytest

from tools.delegate_tool import _resolve_task_routes
from tools.delegate_tool_config import _resolve_delegation_credentials

PARENT_PROVIDER = "p-parent"
TOP_PROVIDER = "p-top"
BLOCK_PROVIDER = "p-child"

_TOP_CFG = {
    "model": "m-top",
    "provider": TOP_PROVIDER,
    "request_overrides": {"top_marker": True},
}

# What the process/network boundary would return for each provider name.
_RUNTIME_BUNDLES = {
    TOP_PROVIDER: {
        "provider": TOP_PROVIDER, "base_url": "https://top.example/v1", "api_key": "top-key",
        "api_mode": "chat_completions", "request_overrides": {"runtime_top": True},
        "max_output_tokens": 4096, "command": None, "args": [], "model": "runtime-top-default",
    },
    BLOCK_PROVIDER: {
        "provider": BLOCK_PROVIDER, "base_url": "https://block.example/v1", "api_key": "block-key",
        "api_mode": "chat_completions", "request_overrides": {"runtime_block": True},
        "max_output_tokens": 8192, "command": None, "args": [], "model": "runtime-block-default",
    },
}


def _make_parent():
    parent = MagicMock()
    parent.provider = PARENT_PROVIDER
    parent.model = "parent-model"
    parent.base_url = "https://parent.example/v1"
    parent.api_key = "parent-key"
    parent._delegate_depth = 0
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    return parent


def _fake_runtime_provider(requested=None, target_model=None, **_kw):
    key = (requested or "").strip().lower()
    if key not in _RUNTIME_BUNDLES:
        raise RuntimeError(f"Unknown provider {requested!r}")
    return dict(_RUNTIME_BUNDLES[key])


@pytest.fixture
def runtime(monkeypatch):
    import hermes_cli.runtime_provider as runtime_provider

    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", _fake_runtime_provider)


class TestByProviderLookup:
    def test_matching_block_replaces_top_level_config(self, runtime):
        """A block for the parent's provider wins: its model AND its provider."""
        cfg = {**_TOP_CFG, "by_provider": {PARENT_PROVIDER: {"model": "m-child", "provider": BLOCK_PROVIDER}}}
        creds = _resolve_delegation_credentials(cfg, _make_parent())

        assert creds["model"] == "m-child"
        assert creds["provider"] == BLOCK_PROVIDER
        assert creds["base_url"] == _RUNTIME_BUNDLES[BLOCK_PROVIDER]["base_url"]
        assert creds["api_key"] == _RUNTIME_BUNDLES[BLOCK_PROVIDER]["api_key"]

    def test_block_replaces_cfg_wholesale(self, runtime):
        """Keys the block does not carry do NOT survive from the top level."""
        cfg = {**_TOP_CFG, "by_provider": {PARENT_PROVIDER: {"model": "m-child", "provider": BLOCK_PROVIDER}}}
        creds = _resolve_delegation_credentials(cfg, _make_parent())

        # The block is the whole config now: the top-level request_overrides —
        # and, decisively, the top-level provider — must not leak through.
        assert "top_marker" not in (creds.get("request_overrides") or {})
        assert creds["provider"] != TOP_PROVIDER

    def test_no_matching_block_keeps_top_level_values(self, runtime):
        """A config for some other provider leaves the top level untouched."""
        cfg = {**_TOP_CFG, "by_provider": {"some-other-provider": {"model": "m-other", "provider": "p-other"}}}
        creds = _resolve_delegation_credentials(cfg, _make_parent())

        assert creds["model"] == "m-top"
        assert creds["provider"] == TOP_PROVIDER

    def test_match_is_case_insensitive_on_both_sides(self, runtime):
        """The block key and the parent's provider match regardless of case."""
        key_case = {**_TOP_CFG, "by_provider": {"P-PARENT": {"model": "m-child", "provider": BLOCK_PROVIDER}}}
        assert _resolve_delegation_credentials(key_case, _make_parent())["model"] == "m-child"

        parent_upper = _make_parent()
        parent_upper.provider = "P-Parent"
        value_case = {**_TOP_CFG, "by_provider": {"p-parent": {"model": "m-child", "provider": BLOCK_PROVIDER}}}
        assert _resolve_delegation_credentials(value_case, parent_upper)["model"] == "m-child"

    def test_empty_block_means_inherit_the_parent(self, runtime):
        """``{provider: {}}`` is meaningful: it opts that provider into pure
        inheritance, overriding the top-level model/provider for it."""
        cfg = {**_TOP_CFG, "by_provider": {PARENT_PROVIDER: {}}}
        creds = _resolve_delegation_credentials(cfg, _make_parent())

        assert creds["model"] is None
        assert creds["provider"] is None
        assert creds["base_url"] is None


class TestByProviderReachesTaskRoutes:
    """The failure this restores: an unrouted child must land on the block's
    model/provider, never on the parent's own."""

    @pytest.fixture
    def routed(self, runtime, monkeypatch):
        import hermes_cli.personas as ruflo

        # The dispatched role has no model_by_role entry of its own; the
        # sibling entry exists so "no entry" is a real lookup miss rather
        # than an empty map.
        monkeypatch.setattr(ruflo, "get_role_entry_map", lambda: {"researcher": {"model": "m-researcher"}})
        monkeypatch.setattr(ruflo, "get_role_model_map", lambda: {"researcher": "m-researcher"})

        def _run(tasks, cfg):
            parent = _make_parent()
            creds = _resolve_delegation_credentials(cfg, parent)
            routes, err = _resolve_task_routes(
                tasks, creds, cfg=cfg, parent_agent=parent, top_role="leaf", roster_warnings=[],
            )
            assert err is None, err
            return routes

        return _run

    CFG = {
        **_TOP_CFG,
        # Auto-route is off: these routes must come from config, not a classifier.
        "auto_route": {"enabled": False},
        "by_provider": {PARENT_PROVIDER: {"model": "m-child", "provider": BLOCK_PROVIDER}},
    }

    def test_unrouted_task_uses_the_block_not_the_parent(self, routed):
        routes = routed([{"goal": "Summarize the delegation section of AGENTS.md"}], self.CFG)

        assert routes[0]["model"] == "m-child"
        assert routes[0]["model"] != "parent-model"
        assert routes[0]["creds"]["provider"] == BLOCK_PROVIDER
        assert routes[0]["creds"]["provider"] != PARENT_PROVIDER

    def test_unknown_agent_type_is_refused_not_routed(self, routed):
        """A stated agent_type with no model_by_role entry and no persona refuses the
        spawn rather than silently resolving to the default/parent model."""
        parent = _make_parent()
        creds = _resolve_delegation_credentials(self.CFG, parent)
        routes, err = _resolve_task_routes(
            [{"goal": "Do the thing", "agent_type": "no-such-role"}],
            creds, cfg=self.CFG, parent_agent=parent, top_role="leaf", roster_warnings=[],
        )
        assert routes == []
        assert err and "no-such-role" in err
