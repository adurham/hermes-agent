"""A tier-only auto-route must carry its role's provider pin.

The auto-route classifier can route a task by TIER alone (no persona pick):
``{model, tier, role, reason}`` with no ``agent_type`` key. delegate_task
consumes that entry as ``auto_route_model`` — the bare model slug from
``delegation.model_by_role[<tier role>]``.

A ``model_by_role`` entry may pin its own ``provider`` (see
test_delegate_role_provider.py): such a role must run on THAT provider, never
on the batch-level one. A STATED ``agent_type`` already gets this through
``role_cfg_key`` -> ``_resolve_role_credentials``. A tier-only route used to
resolve through the model map only, so the role's provider pin was never
applied and the tier role's model slug was sent to the batch creds' provider
(the parent's, when no ``delegation.by_provider`` block matched) — the exact
wrong-endpoint 404 the per-role pin exists to prevent.

The real ``_resolve_task_routes`` / ``_resolve_role_credentials`` run here;
only ``resolve_runtime_provider`` (the process/network boundary) and the
classifier's OUTPUT (patched onto ``route_task_models``) are stubbed.
"""

from unittest.mock import MagicMock

import pytest

import hermes_cli.personas as ruflo
from tools.delegate_tool import _resolve_task_routes

PARENT_PROVIDER = "p-parent"
BATCH_PROVIDER = "p-batch"
ROLE_PROVIDER = "p-role"
ROLE_MODEL = "m-role"

_RUNTIME_BUNDLES = {
    BATCH_PROVIDER: {
        "provider": BATCH_PROVIDER, "base_url": "https://batch.example/v1", "api_key": "batch-key",
        "api_mode": "chat_completions", "request_overrides": None,
        "max_output_tokens": 4096, "command": None, "args": [], "model": "batch-default-model",
    },
    ROLE_PROVIDER: {
        "provider": ROLE_PROVIDER, "base_url": "https://role.example/v1", "api_key": "role-key",
        "api_mode": "chat_completions", "request_overrides": None,
        "max_output_tokens": 8192, "command": None, "args": [], "model": "role-default-model",
    },
}

# The tier's role entry: a model AND a provider pin (with a fallback, so the
# resolution path is the full one).
ROLE_ENTRY_MAP = {
    "coder": {
        "model": ROLE_MODEL,
        "provider": ROLE_PROVIDER,
        "fallback": {"model": "m-role-fallback", "provider": BATCH_PROVIDER},
    },
    "researcher": {"model": "m-haiku"},
}
ROLE_MODEL_MAP = {
    "coder": ROLE_MODEL,
    "researcher": "m-haiku",
}

# A TIER-ONLY classifier verdict: tier + role + model, NO persona agent_type.
TIER_ONLY_ROUTE = {
    0: {"model": ROLE_MODEL, "tier": "standard", "role": "coder", "reason": "bounded work"},
}


@pytest.fixture
def harness(monkeypatch):
    import hermes_cli.runtime_provider as runtime_provider
    import tools.delegation_router as dr

    def _resolve(requested=None, target_model=None, **_kw):
        key = (requested or "").strip().lower()
        if key not in _RUNTIME_BUNDLES:
            raise RuntimeError(f"Unknown provider {requested!r}")
        return dict(_RUNTIME_BUNDLES[key])

    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", _resolve)
    monkeypatch.setattr(ruflo, "get_role_model_map", lambda: dict(ROLE_MODEL_MAP))
    monkeypatch.setattr(ruflo, "get_role_entry_map", lambda: dict(ROLE_ENTRY_MAP))
    # The classifier's OUTPUT is stubbed; everything downstream of it is real.
    monkeypatch.setattr(dr, "route_task_models", lambda *a, **k: dict(TIER_ONLY_ROUTE))

    def _run(tasks=None):
        parent = MagicMock()
        parent.provider = PARENT_PROVIDER
        parent.model = "parent-model"
        parent.base_url = None
        parent.api_key = "sk-test"
        parent._delegate_depth = 0
        cfg = {
            "model": "m-config-default",
            "provider": BATCH_PROVIDER,
            "auto_route": {"enabled": True, "providers": [PARENT_PROVIDER]},
        }
        creds = {"model": "m-config-default", "provider": BATCH_PROVIDER, "base_url": None, "api_key": "k", "api_mode": None}
        routes, err = _resolve_task_routes(
            tasks or [{"goal": "Refactor the retry helper to use the new backoff API"}],
            creds, cfg=cfg, parent_agent=parent, top_role="leaf", roster_warnings=[],
        )
        assert err is None, err
        return routes

    return _run


class TestTierOnlyRouteCarriesProviderPin:
    def test_tier_only_route_uses_the_roles_pinned_provider(self, harness):
        """The role's provider pin must be applied to a tier-only route."""
        routes = harness()
        route = routes[0]

        assert route["model"] == ROLE_MODEL
        # The decisive assertion: the child the route produces must run on the
        # role's pinned provider, not on the batch creds provider.
        assert route["creds"]["provider"] == ROLE_PROVIDER, (
            f"tier-only route sent {route['model']!r} to provider "
            f"{route['creds']['provider']!r}; the role pins {ROLE_PROVIDER!r}"
        )
        assert route["creds"]["base_url"] == f"https://role.example/v1"

    def test_tier_only_route_whole_bundle_travels(self, harness):
        """No mixing: the tier-role's endpoint/key travel with its provider."""
        route = harness()[0]
        creds = route["creds"]

        assert creds["provider"] == ROLE_PROVIDER
        assert creds["base_url"] == "https://role.example/v1"
        assert creds["api_key"] == "role-key"
        assert creds["provider"] != BATCH_PROVIDER
        assert creds["base_url"] != _RUNTIME_BUNDLES[BATCH_PROVIDER]["base_url"]

    def test_stated_agent_type_still_resolves_its_own_pin(self, harness):
        """Control: the pre-existing stated-agent_type path is unchanged."""
        routes = harness([{"goal": "Refactor the retry helper", "agent_type": "coder"}])
        route = routes[0]

        assert route["agent_type"] == "coder"
        assert route["model"] == ROLE_MODEL
        assert route["creds"]["provider"] == ROLE_PROVIDER
