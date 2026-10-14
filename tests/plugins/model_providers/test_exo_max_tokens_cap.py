"""Item D regression: the exo route sends a CLIENT-SIDE max_tokens cap.

Why a cap at all: ``exo`` is a shared, KV-cache-bound local cluster, and an unbounded request
lets a thinking model (dsv41) burn the whole generation on hidden reasoning before any visible
answer — the "reasoning exhausted the output budget" failure the turn loop then has to recover
from. The profile therefore declares a bounded ``default_max_tokens`` (16K).

The mechanism is deliberately the SMALLEST one that works, and every layer of the precedence is
an EXISTING, documented knob (agent/transports/chat_completions.py::_apply_max_tokens):

    ephemeral output cap  >  agent.max_tokens  >  profile.get_max_tokens(model)

``agent.max_tokens`` is filled at init from ``custom_providers``/``providers.<name>``
``models.<model>.max_tokens`` (``hermes_cli.config.get_custom_provider_max_tokens``), so a
per-model raise needs NO code change:

    providers:
      exo:
        models:
          <model-id>:
            max_tokens: 65536

TRADEOFF (asserted in the profile's own docstring): a 16K window can cut a long answer or a
``write_file``/``patch`` whose ARGUMENTS exceed 16K — that ends the stream with
finish_reason="length" and costs one continuation round-trip (up to 4) rather than losing the
work. Tasks that routinely emit very large tool arguments should raise the cap per model.
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def exo_profile():
    import model_tools  # noqa: F401 — triggers bundled provider discovery
    from providers import get_provider_profile

    profile = get_provider_profile("exo")
    assert profile is not None
    return profile


EXO_MODEL = "dealignai/DeepSeek-V4.1-Flash-UNCENSORED-EXL3-2.9bpw"


# ── The cap exists, is bounded, and is per-model overridable ────────────────


def test_profile_declares_a_bounded_client_side_cap(exo_profile):
    assert exo_profile.default_max_tokens == 16384
    # get_max_tokens is the hook the transport consults (base implementation: the
    # profile default, ignoring the model) — no per-model code path was invented.
    assert exo_profile.get_max_tokens(EXO_MODEL) == 16384
    assert exo_profile.get_max_tokens("some-other-model") == 16384


def test_transport_profile_path_sends_the_cap(exo_profile):
    """No config override anywhere: the profile's cap is what reaches the body."""
    from agent.transports.chat_completions import ChatCompletionsTransport

    kwargs = ChatCompletionsTransport().build_kwargs(
        model=EXO_MODEL,
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        provider_profile=exo_profile,
        base_url="http://exo.test/v1",
        max_tokens_param_fn=lambda value: {"max_tokens": value},
    )
    assert kwargs["max_tokens"] == 16384


def test_explicit_max_tokens_beats_the_profile_cap(exo_profile):
    """The documented per-model config key lands in agent.max_tokens, which wins."""
    from agent.transports.chat_completions import ChatCompletionsTransport

    kwargs = ChatCompletionsTransport().build_kwargs(
        model=EXO_MODEL,
        messages=[{"role": "user", "content": "big patch"}],
        tools=None,
        provider_profile=exo_profile,
        base_url="http://exo.test/v1",
        max_tokens=65536,  # what agent_init sets from models.<id>.max_tokens
        max_tokens_param_fn=lambda value: {"max_tokens": value},
    )
    assert kwargs["max_tokens"] == 65536


def test_ephemeral_cap_beats_both(exo_profile):
    from agent.transports.chat_completions import ChatCompletionsTransport

    kwargs = ChatCompletionsTransport().build_kwargs(
        model=EXO_MODEL,
        messages=[{"role": "user", "content": "continuation retry"}],
        tools=None,
        provider_profile=exo_profile,
        base_url="http://exo.test/v1",
        max_tokens=65536,
        ephemeral_max_output_tokens=98304,  # boosted_output_cap on a truncated tool call
        max_tokens_param_fn=lambda value: {"max_tokens": value},
    )
    assert kwargs["max_tokens"] == 98304


# ── The escape hatch is the existing config key, end to end ─────────────────


def test_per_model_config_key_resolves_for_the_exo_route():
    """``providers.exo.models.<id>.max_tokens`` is read for THIS route/model."""
    from hermes_cli.config import get_custom_provider_max_tokens

    config = {
        "providers": {
            "exo": {
                "base_url": "http://exo.test/v1",
                "models": {
                    EXO_MODEL: {"context_length": 1048576, "max_tokens": 32768},
                    "mlx-community/Qwen3.6-35B-A3B-8bit": {"context_length": 262144},
                },
            }
        }
    }
    assert get_custom_provider_max_tokens(EXO_MODEL, "http://exo.test/v1", config=config) == 32768
    # A model with no key keeps the profile default (no accidental global raise).
    assert get_custom_provider_max_tokens(
        "mlx-community/Qwen3.6-35B-A3B-8bit", "http://exo.test/v1", config=config) is None
    # A different route must not borrow it.
    assert get_custom_provider_max_tokens(EXO_MODEL, "http://other.test/v1", config=config) is None


def test_config_key_reaches_the_wire_through_a_real_agent(monkeypatch):
    """End-to-end: the config key -> agent.max_tokens -> the request body on the exo route."""
    from run_agent import AIAgent

    monkeypatch.setattr(
        "hermes_cli.config.get_custom_provider_max_tokens",
        lambda model, base_url, *a, **kw: 32768,
    )
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
    )
    try:
        assert agent.max_tokens == 32768, "the per-model config key must reach agent.max_tokens"
        kwargs = agent._build_api_kwargs([{"role": "user", "content": "dump only"}], [])
    finally:
        client = getattr(agent, "client", None)
        if client is not None:
            client.close()
    assert kwargs["max_tokens"] == 32768


def test_profile_cap_applies_when_no_config_key_exists(monkeypatch):
    """The default case the cap exists for: nothing configured, still bounded."""
    from run_agent import AIAgent

    monkeypatch.setattr(
        "hermes_cli.config.get_custom_provider_max_tokens",
        lambda model, base_url, *a, **kw: None,
    )
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
    )
    try:
        assert agent.max_tokens is None
        kwargs = agent._build_api_kwargs([{"role": "user", "content": "dump only"}], [])
    finally:
        client = getattr(agent, "client", None)
        if client is not None:
            client.close()
    assert kwargs["max_tokens"] == 16384
