"""Role parity: every configured role resolves to exactly the model/provider it names.

This is the safety net the routing extraction (tools/delegate_tool_routing.py) had to have before
anything else could move, and the drift guard it keeps: a refactor that reorders precedence, adds a
default, or fuzzes a role name changes one of these lines and fails here instead of surfacing weeks
later as "the reviewer was actually answering with the cheap model".

Eight roles is a table, not a program — the assertions are deliberately literal strings, not
computed from the same map the code reads, so a bug in the lookup cannot make the expectation agree.
"""

from unittest.mock import MagicMock

import pytest

import hermes_cli.personas as ruflo
import tools.delegation_router as dr
from tools.delegate_tool import _resolve_task_routes

PARENT_PROVIDER = "p-parent"
PARENT_MODEL = "parent-model"
BATCH_MODEL = "m-batch-default"
BATCH_PROVIDER = "p-batch"

CLAUDE_ROUTE = "claude-subscription-directsdk-experimental"
OLLAMA = "ollama-cloud"

# The operator's real roster, spelled out. If a role is added or repinned, this table changes in the
# same commit — that is the point.
ROLES = {
    "pm": ("deepseek-v4.1-flash", OLLAMA),
    "coder": ("deepseek-v4.1-flash", OLLAMA),
    "mid-coder": ("deepseek-v4.1-flash", OLLAMA),
    "jr-coder": ("glm-5.3-flash", OLLAMA),
    "reviewer": ("glm-5.3-flash", OLLAMA),
    "sr-coder": ("claude-opus-5-5", CLAUDE_ROUTE),
    "consultant": ("claude-fable-5-1", CLAUDE_ROUTE),
    "researcher": ("deepseek-v4.1-flash", OLLAMA),
}

ROLE_ENTRY_MAP = {
    name: {"model": model, "provider": provider} for name, (model, provider) in ROLES.items()
}
ROLE_MODEL_MAP = {name: model for name, (model, _provider) in ROLES.items()}


@pytest.fixture
def harness(monkeypatch):
    def _resolve(requested=None, target_model=None, **_kw):
        # Every provider the roster names must be resolvable, or routing refuses before the
        # assertion we actually care about.
        for name, (_model, provider) in ROLES.items():
            if (requested or "") == provider:
                return {
                    "provider": provider, "base_url": f"https://{provider}.example/v1",
                    "api_key": "k", "api_mode": "chat_completions", "request_overrides": None,
                    "max_output_tokens": 8192, "command": None, "args": [],
                    "model": target_model or ROLES[name][0],
                }
        raise RuntimeError(f"Unknown provider {requested!r}")

    from hermes_cli import runtime_provider
    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", _resolve)
    monkeypatch.setattr(ruflo, "get_role_model_map", lambda: dict(ROLE_MODEL_MAP))
    monkeypatch.setattr(ruflo, "get_role_entry_map", lambda: dict(ROLE_ENTRY_MAP))
    # Auto-route OFF: this file is about explicit role pins, not the classifier.
    monkeypatch.setattr(dr, "route_task_models", lambda *a, **k: {})

    def _run(roles):
        parent = MagicMock()
        parent.provider = PARENT_PROVIDER
        parent.model = PARENT_MODEL
        parent.base_url = None
        parent.api_key = "sk-test"
        parent._delegate_depth = 0
        tasks = [{"goal": f"task for {r}", "agent_type": r} for r in roles]
        cfg = {
            "model": BATCH_MODEL, "provider": BATCH_PROVIDER,
            "auto_route": {"enabled": False},
        }
        creds = {"model": BATCH_MODEL, "provider": BATCH_PROVIDER,
                 "base_url": None, "api_key": "k", "api_mode": None}
        routes, err = _resolve_task_routes(
            tasks, creds, cfg=cfg, parent_agent=parent, top_role="leaf", roster_warnings=[],
        )
        assert err is None, err
        return routes

    return _run


@pytest.mark.parametrize("role", sorted(ROLES))
def test_role_resolves_to_its_own_model_and_provider(harness, role):
    route = harness([role])[0]
    expected_model, expected_provider = ROLES[role]
    assert route["agent_type"] == role
    assert route["model"] == expected_model
    assert route["creds"]["provider"] == expected_provider


def test_all_eight_roles_in_one_batch_resolve_independently(harness):
    """The batch path must not let one role's pin leak onto its neighbours."""
    roles = sorted(ROLES)
    routes = harness(roles)
    assert len(routes) == len(roles)
    for route, role in zip(routes, roles):
        expected_model, expected_provider = ROLES[role]
        assert (route["agent_type"], route["model"], route["creds"]["provider"]) == (
            role, expected_model, expected_provider,
        )


def test_a_role_never_inherits_another_roles_model(harness):
    """The failure this guards: sr-coder silently answering on the batch default model."""
    routes = harness(["sr-coder", "coder"])
    sr, coder = routes
    assert sr["model"] == "claude-opus-5-5"
    assert coder["model"] == "deepseek-v4.1-flash"
    assert sr["model"] != coder["model"]
