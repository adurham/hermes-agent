"""End-to-end: transient-empty provider policy, fallback floor, and mid-run primary restore.

Real imports, real ``AIAgent`` and the real turn loop against a temp ``HERMES_HOME`` whose
``plugins/model-providers/`` registers a provider profile declaring
``empty_completion_policy="transient"`` (the seam the DirectSDK subscription route uses).
Only the network edge is scripted: one function stands in for both the primary and the
fallback client's ``chat.completions.create`` and records which (model, provider) served
each call. Backoff sleeps and the streak clock are patched so the floor is exercised
deterministically.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent

PRIMARY_PROVIDER = "flaky-sub-e2e"
PRIMARY_MODEL = "primary-model"
FALLBACK = {"provider": "openrouter", "model": "fallback-model"}
CARRIER = f"{PRIMARY_PROVIDER}.native_assistant"


def _clear_provider_caches():
    import providers

    providers._REGISTRY.clear()
    providers._ALIASES.clear()
    providers._SOURCES.clear()
    providers._discovered = False
    providers._PROVIDER_LIST_CACHE = None
    for attr in ("_HOME_LAYERS", "_home_layers"):
        layers = getattr(providers, attr, None)
        if isinstance(layers, dict):
            layers.clear()


@pytest.fixture
def transient_home(tmp_path, monkeypatch):
    """Temp HERMES_HOME with a user model-provider plugin declaring the transient policy."""
    home = tmp_path / ".hermes"
    plugin = home / "plugins" / "model-providers" / PRIMARY_PROVIDER
    plugin.mkdir(parents=True)
    (plugin / "__init__.py").write_text(
        "from providers import register_provider\n"
        "from providers.base import ProviderProfile\n"
        "register_provider(ProviderProfile(\n"
        f"    name={PRIMARY_PROVIDER!r},\n"
        "    base_url='https://flaky-sub.example.invalid/v1',\n"
        "    auth_type='api_key',\n"
        f"    native_reasoning_details_type={CARRIER!r},\n"
        "    empty_completion_policy='transient',\n"
        "))\n"
    )
    (plugin / "plugin.yaml").write_text(
        f"name: {PRIMARY_PROVIDER}\nkind: model-provider\nversion: 0.0.1\ndescription: e2e\n"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    _clear_provider_caches()
    yield home
    _clear_provider_caches()


def _response(content="", *, carrier=True, finish_reason="stop"):
    """A chat-completions response; empties carry only the provider-private carrier."""
    details = [{"type": CARRIER, "data": "opaque"}] if carrier else None
    message = SimpleNamespace(content=content, tool_calls=None, reasoning_details=details)
    usage = SimpleNamespace(prompt_tokens=1200, completion_tokens=0 if not content else 7,
                            total_tokens=1200 + (0 if not content else 7))
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        model="scripted", usage=usage,
    )


def _tool_response(name="web_search", call_id="c1"):
    call = SimpleNamespace(id=call_id, type="function", function=SimpleNamespace(name=name, arguments="{}"))
    message = SimpleNamespace(content="", tool_calls=[call], reasoning_details=None)
    usage = SimpleNamespace(prompt_tokens=1200, completion_tokens=9, total_tokens=1209)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="tool_calls")],
                           model="scripted", usage=usage)


class _Script:
    """Shared ``create`` for primary + fallback clients. ``plan`` maps provider → list of
    responses (popped in order); every call records the live (model, provider)."""

    def __init__(self, agent_ref, plan, clock):
        self.agent_ref, self.plan, self.clock, self.calls, self.systems = agent_ref, plan, clock, [], []

    def __call__(self, *args, **kwargs):
        agent = self.agent_ref()
        self.clock.now += self.clock.step  # each request takes ``step`` seconds
        self.calls.append((agent.model, agent.provider))
        msgs = kwargs.get("messages") or []
        self.systems.append(msgs[0].get("content") if msgs and msgs[0].get("role") == "system" else None)
        return self.plan[agent.provider].pop(0)


def _tool_defs(*names):
    return [{"type": "function", "function": {"name": n, "description": "t",
             "parameters": {"type": "object", "properties": {}}}} for n in names]


def _build_agent(transient_home):
    with (
        patch("model_tools.get_tool_definitions", return_value=_tool_defs("web_search")),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key", base_url="https://flaky-sub.example.invalid/v1",
            provider=PRIMARY_PROVIDER, model=PRIMARY_MODEL,
            quiet_mode=True, skip_context_files=True, skip_memory=True,
            fallback_model=[FALLBACK],
        )
    agent._cached_system_prompt = f"You are helpful.\nModel: {PRIMARY_MODEL}\nProvider: {PRIMARY_PROVIDER}"
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent.valid_tool_names = {"web_search"}
    return agent


def _run(agent, plan, clock, *, max_iterations=40):
    import weakref

    script = _Script(weakref.ref(agent), plan, clock)
    primary = MagicMock()
    primary.chat.completions.create.side_effect = script
    agent.client = primary
    fb_client = MagicMock()
    fb_client.base_url = "https://openrouter.ai/api/v1"
    fb_client.api_key = "fb-key"
    fb_client.chat.completions.create.side_effect = script
    agent.max_iterations = max_iterations

    def _primary_client_rebuild(agent_, rt, reason=None):
        agent_.client = primary

    with (
        patch("agent.auxiliary_client.resolve_provider_client", return_value=(fb_client, FALLBACK["model"])),
        patch("agent.agent_runtime_helpers._rebuild_primary_client", _primary_client_rebuild),
        patch("agent.turn_empty_response.interruptible_backoff_sleep", lambda *a, **k: None),
        patch("agent.empty_response_guard._monotonic", clock),
        patch("model_tools.handle_function_call", return_value="ok"),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("do the task")
    agent._last_script = script
    return result, script.calls


class _Clock:
    """Monotonic stand-in for the streak clock; advanced ``step`` seconds per API request."""

    def __init__(self, step):
        self.now, self.step = 1000.0, step

    def __call__(self):
        return self.now


def test_transient_route_no_fallback_before_budget_and_floor(transient_home):
    """Five carrier-only empties then an answer: the old ladder would have spent two
    prefill calls and fallen back after two same-signature empties; the transient route
    retries the SAME provider and never touches the fallback."""
    agent = _build_agent(transient_home)
    plan = {PRIMARY_PROVIDER: [_response() for _ in range(5)] + [_response("done on primary")],
            "openrouter": []}
    result, calls = _run(agent, plan, _Clock(step=1.0))

    assert result["final_response"] == "done on primary"
    assert calls == [(PRIMARY_MODEL, PRIMARY_PROVIDER)] * 6  # no prefill burn, no fallback
    assert agent._fallback_events == []
    assert agent._fallback_activated is False


def test_transient_route_falls_back_only_after_budget_and_floor(transient_home):
    """Budget (6) is spent while the floor (90s) has not elapsed → the primary is held
    until the floor passes; then the fallback activates exactly once, cause recorded."""
    agent = _build_agent(transient_home)
    plan = {PRIMARY_PROVIDER: [_response() for _ in range(9)],
            "openrouter": [_response("fallback answered", carrier=False)]}
    # 14s per request; streak starts at empty #1. After empty #7 (budget of 6 retries spent)
    # the streak is 84s old (< 90 floor) → hold the primary; after empty #8 it is 98s → fall back.
    result, calls = _run(agent, plan, _Clock(step=14.0))

    primary_calls = [c for c in calls if c[1] == PRIMARY_PROVIDER]
    assert len(primary_calls) == 8, "fallback must wait for the floor, not the hard cap"
    assert calls[-1] == (FALLBACK["model"], "openrouter")
    assert result["final_response"] == "fallback answered"
    assert [e["cause"] for e in agent._fallback_events] == ["empty_response"]
    assert agent._fallback_events[0]["from_provider"] == PRIMARY_PROVIDER


def test_hard_cap_bounds_attempts_when_clock_never_reaches_floor(transient_home):
    agent = _build_agent(transient_home)
    plan = {PRIMARY_PROVIDER: [_response() for _ in range(20)],
            "openrouter": [_response("fallback answered", carrier=False)]}
    result, calls = _run(agent, plan, _Clock(step=0.0))  # frozen clock

    primary_calls = [c for c in calls if c[1] == PRIMARY_PROVIDER]
    assert len(primary_calls) == 1 + 6 + 2  # first empty + retries up to the hard cap
    assert result["final_response"] == "fallback answered"


def test_mid_run_restore_returns_child_to_primary_after_empty_fallback(transient_home):
    """After an empty-caused fallback the primary is re-tried at an iteration boundary:
    the fallback serves one tool round, then the next request goes to the primary, which
    answers — the run ends ON THE PRIMARY (a delegated child no longer finishes its whole
    run on the weaker model)."""
    agent = _build_agent(transient_home)
    plan = {
        PRIMARY_PROVIDER: [_response() for _ in range(9)] + [_response("primary is back")],
        "openrouter": [_tool_response()],
    }
    result, calls = _run(agent, plan, _Clock(step=0.0))

    fb_index = calls.index((FALLBACK["model"], "openrouter"))
    assert calls[fb_index + 1] == (PRIMARY_MODEL, PRIMARY_PROVIDER), "no mid-run restore at the boundary"
    assert result["final_response"] == "primary is back"
    assert agent._fallback_activated is False
    assert agent.model == PRIMARY_MODEL and agent.provider == PRIMARY_PROVIDER
    assert agent._empty_restore_probe_active is False
    # The system prompt actually SENT tracks the live identity: the fallback request names
    # the fallback model, and the restored primary's request is re-synced back (not stale).
    systems = agent._last_script.systems
    assert f"Model: {FALLBACK['model']}" in systems[fb_index]
    assert f"Model: {PRIMARY_MODEL}" in systems[fb_index + 1]
    assert f"Provider: {PRIMARY_PROVIDER}" in systems[fb_index + 1]


def _runs(calls):
    """Run-length encode the provider sequence: [(provider, n), ...]."""
    out = []
    for _, provider in calls:
        if out and out[-1][0] == provider:
            out[-1] = (provider, out[-1][1] + 1)
        else:
            out.append((provider, 1))
    return out


def test_restored_primary_emptying_again_refalls_back_with_doubled_backoff(transient_home):
    """Probe fails (the restored primary empties out again) → the ladder re-falls-back with
    the same cause, and the next restore waits the doubled skip (2 boundaries, not 1) before
    probing again; the second probe answers and the run ends on the primary."""
    agent = _build_agent(transient_home)
    plan = {
        # streak 1: 9 empties (first + 6 budget + 2 cap, frozen clock); probe 1: post-tool
        # nudge + 9 empties; probe 2 answers.
        PRIMARY_PROVIDER: [_response() for _ in range(9 + 10)] + [_response("primary is back")],
        "openrouter": [_tool_response("web_search", f"c{i}") for i in range(3)],
    }
    result, calls = _run(agent, plan, _Clock(step=0.0))

    assert _runs(calls) == [
        (PRIMARY_PROVIDER, 9), ("openrouter", 1),   # first fallback: 1 boundary skipped
        (PRIMARY_PROVIDER, 10), ("openrouter", 2),  # failed probe → 2 boundaries skipped
        (PRIMARY_PROVIDER, 1),                      # second probe succeeds
    ]
    assert result["final_response"] == "primary is back"
    assert [e["cause"] for e in agent._fallback_events] == ["empty_response", "empty_response"]
    assert agent._empty_restore_attempts == 2
    assert agent._fallback_activated is False


def test_t4_landed_tool_round_ends_the_empty_streak(transient_home):
    """Isolated hiccups spread across a run must not pool into one fallback-worthy streak:
    5 empties, a real tool round, then the post-tool nudge + 5 more empties, then an answer.
    Pooled, the 10 retries would pass the hard cap (8) and fall back; reset, they never do."""
    agent = _build_agent(transient_home)
    plan = {
        PRIMARY_PROVIDER: [_response() for _ in range(5)] + [_tool_response()]
        + [_response() for _ in range(6)] + [_response("done on primary")],
        "openrouter": [],
    }
    result, calls = _run(agent, plan, _Clock(step=0.0))

    assert result["final_response"] == "done on primary"
    assert {p for _, p in calls} == {PRIMARY_PROVIDER}
    assert agent._fallback_events == []
