"""The auto-route provider gate keys on the PARENT's provider.

``delegation.auto_route.providers`` names the providers whose sessions want
auto-routing. Before this change ``_auto_route_batch`` handed the router
``creds.get("provider") or parent.provider`` — the provider the CHILDREN
resolved onto. Those are different concepts: a ``delegation.by_provider``
block or a ``delegation.provider`` pin sends children to a provider that has
nothing to do with which session is dispatching, so a directsdk main session
whose children resolve onto ollama-cloud would present 'ollama-cloud' to the
gate and silently lose auto-routing (or, worse, route because the CHILD
provider happened to be listed).

These are end-to-end behavior contracts through the REAL
``tools.delegation_router.route_task_models`` gate: only ``_classify`` (the
LLM boundary) is stubbed. Asserting on the router's argument would test the
plumbing; running the real gate tests the decision.
"""

from unittest.mock import MagicMock

import pytest

import hermes_cli.personas as ruflo
from tools.delegate_tool import _resolve_task_routes

PARENT_PROVIDER = "p-parent"
CREDS_PROVIDER = "p-creds"
ROUTED_MODEL = "m-routed-from-classifier"
CREDS_MODEL = "m-batch-default"

ROLE_MODEL_MAP = {"coder": ROUTED_MODEL, "researcher": ROUTED_MODEL}
ROLE_ENTRY_MAP = {"coder": {"model": ROUTED_MODEL}, "researcher": {"model": ROUTED_MODEL}}


def _cfg(allowed):
    """delegation cfg with auto-route ON and the gate set to ``allowed``."""
    return {
        "model": CREDS_MODEL,
        "provider": CREDS_PROVIDER,
        "auto_route": {"enabled": True, "providers": list(allowed), "classify_persona": False},
    }


def _make_parent(provider=PARENT_PROVIDER):
    parent = MagicMock()
    parent.provider = provider
    parent.model = "parent-model"
    parent.base_url = None
    parent.api_key = "sk-test"
    parent._delegate_depth = 0
    return parent


@pytest.fixture
def router(monkeypatch):
    """The classifier returns one 'standard' verdict per pending task."""
    import tools.delegation_router as dr

    monkeypatch.setattr(
        dr, "_classify", lambda pending, **kw: {i: ("standard", "why", "") for i, _ in pending}
    )
    monkeypatch.setattr(ruflo, "get_role_model_map", lambda: dict(ROLE_MODEL_MAP))
    monkeypatch.setattr(ruflo, "get_role_entry_map", lambda: dict(ROLE_ENTRY_MAP))


def _routes(cfg, parent, task=None):
    creds = {"model": CREDS_MODEL, "provider": CREDS_PROVIDER, "base_url": None, "api_key": "k", "api_mode": None}
    routes, err = _resolve_task_routes(
        [task or {"goal": "Refactor the retry helper to use the new backoff API"}],
        creds, cfg=cfg, parent_agent=parent, top_role="leaf", roster_warnings=[],
    )
    assert err is None, err
    return routes


def test_parent_listed_runs_the_router_even_when_creds_were_redirected(router):
    """Parent's provider is listed; the children's resolved provider is NOT.
    The gate must key on the parent, so routing still happens."""
    parent = _make_parent(PARENT_PROVIDER)
    routes = _routes(_cfg([PARENT_PROVIDER]), parent)

    assert routes[0]["model"] == ROUTED_MODEL
    assert routes[0]["model"] != CREDS_MODEL
    assert routes[0]["route_info"] is not None


def test_parent_unlisted_closes_the_gate_even_when_creds_provider_is_listed(router):
    """The inverse: the children's provider is listed but the dispatching
    session's is not — the router must not run."""
    parent = _make_parent(PARENT_PROVIDER)
    routes = _routes(_cfg([CREDS_PROVIDER]), parent)

    assert routes[0]["model"] == CREDS_MODEL
    assert routes[0]["route_info"] is None
