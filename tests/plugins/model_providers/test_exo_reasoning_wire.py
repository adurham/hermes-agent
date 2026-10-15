"""Item B regression: the exo route's reasoning effort must reach the wire.

``exo`` is a first-class named provider (``plugins/model-providers/exo``), so the
transport takes its PROFILE path and the base ``build_api_kwargs_extras`` returned
nothing — a resolved reasoning level (parent ``agent.reasoning_effort`` /
``reasoning_effort_by_model``, or a delegated child's
``delegation.reasoning_effort_by_role``) was dropped before the wire. Verified
against the live engine by reading ``exo/src/exo/api/types/api.py``
(``ChatCompletionRequest.reasoning_effort`` + ``enable_thinking``) and
``exo/src/exo/shared/types/text_generation.py`` (``ReasoningEffort`` =
none..xhigh, ``resolve_reasoning_params``): the knob is a TOP-LEVEL
``reasoning_effort`` string and ``enable_thinking`` is an exo-only body
extension, which is why the flag must travel in ``extra_body`` (not an OpenAI SDK
keyword) and why the emission lives in this ONE profile (the per-provider gate
that keeps a strict OpenAI-compatible backend from ever seeing the field).

Everything here is offline: real profile objects, the real transport builder,
the real ``AIAgent._build_api_kwargs`` request dump and the real delegate_task
dispatch loop. Only the process/network boundary (runtime provider resolution,
the auxiliary classifier) is faked.
"""

from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock

import pytest


@pytest.fixture(scope="module")
def exo_profile():
    import model_tools  # triggers bundled provider discovery
    from providers import get_provider_profile

    profile = get_provider_profile("exo")
    assert profile is not None, "exo provider profile must be registered"
    return profile


# ── Profile mapping: the vocabulary translation ────────────────────────────


@pytest.mark.parametrize(
    "reasoning_config, expected_top_level, expected_extra_body",
    [
        ({"enabled": True, "effort": "high"}, {"reasoning_effort": "high"}, {}),
        ({"enabled": True, "effort": "xhigh"}, {"reasoning_effort": "xhigh"}, {}),  # exo's own ceiling, sent verbatim
        ({"enabled": True, "effort": "max"}, {"reasoning_effort": "xhigh"}, {}),  # wire ceiling is xhigh
        ({"enabled": True, "effort": "ultra"}, {"reasoning_effort": "xhigh"}, {}),  # Hermes-internal tier clamps
        (
            {"enabled": False},
            {"reasoning_effort": "none"},
            {"enable_thinking": False},
        ),
        (
            {"enabled": True, "effort": "none"},
            {"reasoning_effort": "none"},
            {"enable_thinking": False},
        ),
        (None, {}, {}),  # nothing requested → keep exo's own per-model default
        ({"enabled": True}, {}, {}),  # enabled, no level → let the server decide
        ({"enabled": True, "effort": "future-tier"}, {}, {}),  # unknown level omitted, never a 422
    ],
)
def test_profile_translates_reasoning_config(
    exo_profile, reasoning_config, expected_top_level, expected_extra_body
):
    extra_body, top_level = exo_profile.build_api_kwargs_extras(
        reasoning_config=reasoning_config,
        supports_reasoning=False,  # the core allowlist does not know this host
        model="dsv41",
        base_url="http://exo.test/v1",
    )
    assert top_level == expected_top_level
    assert extra_body == expected_extra_body


def test_exo_only_flag_never_lands_top_level(exo_profile):
    """``enable_thinking`` is not an OpenAI SDK keyword — top-level would TypeError."""
    extra_body, top_level = exo_profile.build_api_kwargs_extras(
        reasoning_config={"enabled": False}, supports_reasoning=False
    )
    assert "enable_thinking" not in top_level
    assert extra_body["enable_thinking"] is False


# ── Per-provider gate ───────────────────────────────────────────────────────


def test_generic_profile_emits_no_reasoning_field_for_the_same_config():
    """The gate: emission is opt-in per profile. A strict/plain OpenAI-compatible
    backend's profile returns nothing, so it can never be handed a field (or a
    value) it would reject."""
    from providers.base import ProviderProfile

    extra_body, top_level = ProviderProfile(name="strict-openai-compat").build_api_kwargs_extras(
        reasoning_config={"enabled": True, "effort": "high"}, supports_reasoning=True
    )
    assert (extra_body, top_level) == ({}, {})


def test_only_the_exo_profile_declares_the_exo_ladder():
    from agent.reasoning_effort import EXO_WIRE_EFFORTS

    # The wire vocabulary is exo's own: no ``max``/``ultra`` (exo's Literal stops
    # at ``xhigh``; an older build 422s rather than clamping).
    assert EXO_WIRE_EFFORTS == ("none", "minimal", "low", "medium", "high", "xhigh")
    assert "max" not in EXO_WIRE_EFFORTS and "ultra" not in EXO_WIRE_EFFORTS


# ── Transport wiring: top-level vs extra_body placement ────────────────────


def test_transport_profile_path_puts_effort_top_level(exo_profile):
    from agent.transports.chat_completions import ChatCompletionsTransport

    kwargs = ChatCompletionsTransport().build_kwargs(
        model="dsv41",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        provider_profile=exo_profile,
        base_url="http://exo.test/v1",
        reasoning_config={"enabled": True, "effort": "max"},
    )
    assert kwargs["reasoning_effort"] == "xhigh"
    assert "extra_body" not in kwargs


def test_transport_profile_path_puts_the_off_switch_in_extra_body(exo_profile):
    from agent.transports.chat_completions import ChatCompletionsTransport

    kwargs = ChatCompletionsTransport().build_kwargs(
        model="dsv41",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        provider_profile=exo_profile,
        base_url="http://exo.test/v1",
        reasoning_config={"enabled": False},
    )
    assert kwargs["reasoning_effort"] == "none"
    # extra_body is merged verbatim into the JSON body by the SDK, so the flag
    # reaches the engine as a top-level request field.
    assert kwargs["extra_body"]["enable_thinking"] is False


# ── Parent-agent path: the real request dump ────────────────────────────────


def test_parent_agent_request_dump_carries_the_effort(monkeypatch):
    """The parent (main-loop) path builds its body through the same profile —
    this is the request dump that used to be missing the field entirely."""
    from run_agent import AIAgent

    agent = AIAgent(
        api_key="offline-placeholder",
        base_url="http://127.0.0.1:1/v1",
        model="dsv41",
        provider="exo",
        api_mode="chat_completions",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        skip_background_review=True,
        enabled_toolsets=[],
        reasoning_config={"enabled": True, "effort": "high"},
    )
    try:
        kwargs = agent._build_api_kwargs([{"role": "user", "content": "dump only"}], [])
    finally:
        client = getattr(agent, "client", None)
        if client is not None:
            client.close()
    assert kwargs["reasoning_effort"] == "high"


# ── Child path: delegation resolves it, and it survives to the wire ─────────


def _make_parent():
    parent = MagicMock()
    parent.base_url = "http://exo.test/v1"
    parent.api_key = "parent-key"
    parent.provider = "exo"
    parent.api_mode = "chat_completions"
    parent.model = "dsv41"
    parent.platform = "cli"
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent._session_db = None
    parent._delegate_depth = 0
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    parent.reasoning_config = {"enabled": True, "effort": "low"}
    return parent


EXO_BUNDLE = {
    "provider": "exo",
    "base_url": "http://exo.test/v1",
    "api_key": "exo-key",
    "api_mode": "chat_completions",
    "request_overrides": {},
    "max_output_tokens": 16384,
    "command": None,
    "args": [],
    "model": "dsv41",
}


def test_child_role_effort_survives_from_config_to_the_wire(monkeypatch):
    """The full chain, no wire: ``delegation.reasoning_effort_by_role`` →
    the child's resolved ``reasoning_config`` → the transport body on the child's
    own provider. This is the delegated path the fix exists for."""
    import hermes_cli.personas as ruflo
    from hermes_cli import runtime_provider
    import tools.delegate_tool as dt
    import tools.delegation_router as dr
    from tools.delegate_tool import delegate_task

    monkeypatch.setattr(ruflo, "get_role_model_map", lambda: {"mid-coder": "dsv41"}, raising=False)
    monkeypatch.setattr(
        ruflo, "get_role_entry_map", lambda: {"mid-coder": {"model": "dsv41", "provider": "exo"}},
        raising=False,
    )
    monkeypatch.setattr(ruflo, "lookup_reasoning_for_role", lambda role: "high" if role == "mid-coder" else None)
    monkeypatch.setattr(
        runtime_provider, "resolve_runtime_provider",
        lambda requested=None, target_model=None, **_kw: dict(EXO_BUNDLE),
    )
    monkeypatch.setattr(
        dt, "_load_config",
        lambda: {"model": {"provider": "exo", "default": "dsv41"},
                 "delegation": {"model_by_role": {"mid-coder": "dsv41"}}},
    )
    monkeypatch.setattr(dr, "route_task_models", lambda *a, **k: {})

    resolved: list[dict] = []
    real_resolve = dt._resolve_child_runtime

    def _rec(*a, **kw):
        rt = real_resolve(*a, **kw)
        resolved.append(dict(rt))
        return rt

    monkeypatch.setattr(dt, "_resolve_child_runtime", _rec)
    # Let the REAL _build_child_agent run (so _resolve_child_runtime is exercised
    # for real), but stub the child object itself — no AIAgent, no wire.
    import run_agent

    monkeypatch.setattr(run_agent, "AIAgent", MagicMock(name="ChildAgent"))
    monkeypatch.setattr(
        dt, "_run_single_child",
        lambda *a, **k: {"task_index": 0, "status": "completed", "summary": "ok",
                         "api_calls": 1, "duration_seconds": 0.1},
    )

    result = json.loads(delegate_task(
        tasks=[{"goal": "wire the exo reasoning knob", "agent_type": "mid-coder"}],
        parent_agent=_make_parent(),
    ))
    assert "results" in result, result

    assert len(resolved) == 1, resolved
    rt = resolved[0]
    # The per-role effort won over the parent's own "low"…
    assert rt["reasoning_config"] == {"enabled": True, "effort": "high"}
    # …on the child's own (exo) provider.
    assert rt["provider"] == "exo"

    # …and that exact config, on that exact route, reaches the request body.
    from agent.transports.chat_completions import ChatCompletionsTransport
    from providers import get_provider_profile

    kwargs = ChatCompletionsTransport().build_kwargs(
        model=rt["model"],
        messages=[{"role": "user", "content": "child turn"}],
        tools=None,
        provider_profile=get_provider_profile(rt["provider"]),
        base_url=rt["base_url"],
        reasoning_config=rt["reasoning_config"],
    )
    assert kwargs["reasoning_effort"] == "high"
