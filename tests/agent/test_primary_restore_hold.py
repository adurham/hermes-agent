"""Anti-flap hold for primary-restore probes (agent/fallback_cooldown.py).

Fallback is turn-scoped: ``restore_primary_runtime`` re-installs the primary at every turn
start. When the primary is still broken, each such restore is a failed PROBE — the turn
re-marshals the full context to the primary, then falls back and re-uploads it cold to the
fallback. Observed as the fallback↔primary flap (5 cycles in 30 min in the 2026-10-08
session log; every flip = a cold re-prefill on both sides).

Contract under test:
* arming: only a fallback that fires while the primary is UNPROVEN since its last restore
  (i.e. a failed restore probe) arms the hold; fresh failures keep the legacy re-probe;
  rate-limit reasons keep their own cooldown; a chain failover away from an active fallback
  LIFTS the hold and does not re-count;
* ladder: 600 s base, doubling per consecutive flap, cap 7200 s; the counter restarts only
  after >= 3 completed primary responses;
* gate: restore_primary_runtime declines while the hold runs, proceeds after it expires,
  and a successful restore re-arms the "unproven" flag (the probe must prove itself);
* mid-run probe: waits the hold out without spending a probe attempt or advancing its
  failed-cycle back-off; the first boundary after expiry probes normally;
* process map: a hold armed by one agent gates another agent on the same primary route.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent import empty_fallback_restore as efr
from agent import fallback_cooldown as fc
from agent.error_classifier import FailoverReason
from run_agent import AIAgent

FB = [{"provider": "openai", "model": "gpt-4o"}]
FB_TWO = [
    {"provider": "openai", "model": "gpt-4o"},
    {"provider": "zai", "model": "glm-4.7"},
]
FROZEN = 1_000_000.0


@pytest.fixture(autouse=True)
def _clean_process_holds():
    fc._PROCESS_RESTORE_HOLD_UNTIL.clear()
    yield
    fc._PROCESS_RESTORE_HOLD_UNTIL.clear()


@pytest.fixture(autouse=True)
def _transient_primary():
    """'pp' (the stub primary) declares the transient empty policy, like test_mid_run_restore."""
    from providers.base import ProviderProfile

    profile = ProviderProfile(name="pp", empty_completion_policy="transient")
    with patch("providers.get_provider_profile", lambda name: profile if name == "pp" else None):
        yield


def _make_agent(fallback_model=None):
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-12345678",
            base_url="https://my-llm.example.com/v1",
            provider="custom",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fallback_model,
        )
        agent.client = MagicMock()
        return agent


def _mock_client():
    mock = MagicMock()
    mock.api_key = "fb-key-1234"
    mock.base_url = "https://openrouter.ai/api/v1"
    return mock


def _activate(agent, reason=None):
    with patch(
        "agent.auxiliary_client.resolve_provider_client",
        return_value=(_mock_client(), None),
    ):
        assert agent._try_activate_fallback(reason=reason) is True
    assert agent._fallback_activated is True


def _simulate_turn_start_restore(agent, orig):
    """What restore_primary_runtime does to the live identity (minus the flag it sets)."""
    agent.model, agent.provider, agent.base_url = orig
    agent._fallback_activated = False
    agent._fallback_index = 0
    agent._primary_unproven_since_restore = True


class _FakePool:
    """Minimal stand-in for CredentialPool (matches the primary provider)."""

    def __init__(self, provider, next_at=None):
        self.provider = provider
        self._next_at = next_at

    def next_available_at(self, **_kwargs):
        return self._next_at

    def has_credentials(self):
        return True

    def has_available(self, **_kwargs):
        return False

    def select(self, **_kwargs):
        return None


class TestArming:
    def test_fresh_failure_does_not_arm(self):
        agent = _make_agent(FB)
        _activate(agent, reason=FailoverReason.server_error)
        assert float(getattr(agent, "_primary_restore_hold_until", 0.0) or 0.0) == 0.0

    def test_post_restore_failure_arms_base_hold(self):
        agent = _make_agent(FB)
        agent._primary_unproven_since_restore = True
        with patch("agent.fallback_cooldown.time.monotonic", new=lambda: FROZEN):
            _activate(agent, reason=FailoverReason.server_error)
            assert agent._primary_restore_hold_until == FROZEN + fc._PRIMARY_RESTORE_HOLD_BASE_S
            assert agent._primary_restore_hold_count == 1
            assert agent._primary_completions_since_flap == 0

    def test_consecutive_flaps_escalate(self):
        agent = _make_agent(FB)
        orig = (agent.model, agent.provider, agent.base_url)
        clock = [FROZEN]
        with patch("agent.fallback_cooldown.time.monotonic", new=lambda: clock[0]):
            agent._primary_unproven_since_restore = True
            _activate(agent, reason=FailoverReason.server_error)
            # Next turn restores the primary; its first request fails again.
            _simulate_turn_start_restore(agent, orig)
            clock[0] += 60
            _activate(agent, reason=FailoverReason.timeout)
            assert agent._primary_restore_hold_count == 2
            assert agent._primary_restore_hold_until == (FROZEN + 60) + 2 * fc._PRIMARY_RESTORE_HOLD_BASE_S

    def test_rate_limit_reason_keeps_own_cooldown_not_hold(self):
        agent = _make_agent(FB)
        agent._primary_unproven_since_restore = True
        with patch("agent.fallback_cooldown.time.monotonic", new=lambda: FROZEN):
            _activate(agent, reason=FailoverReason.rate_limit)
            assert float(getattr(agent, "_primary_restore_hold_until", 0.0) or 0.0) == 0.0
            assert agent._rate_limited_until > FROZEN  # the existing cooldown did arm

    def test_chain_failover_lifts_hold_without_recount(self):
        agent = _make_agent(FB_TWO)
        with patch("agent.fallback_cooldown.time.monotonic", new=lambda: FROZEN):
            agent._primary_unproven_since_restore = True
            _activate(agent, reason=FailoverReason.server_error)  # -> entry 0; hold armed
            assert agent._primary_restore_hold_until == FROZEN + fc._PRIMARY_RESTORE_HOLD_BASE_S
            # The fallback itself dies: chain failover to entry 1 while still on the fallback.
            _activate(agent, reason=FailoverReason.server_error)
            assert agent._primary_restore_hold_until == 0.0
            assert agent._primary_restore_hold_count == 1  # not re-counted

    def test_completed_primary_responses_restart_ladder(self):
        agent = _make_agent(FB)
        agent._primary_unproven_since_restore = True
        agent._primary_completions_since_flap = fc._PRIMARY_RESTORE_HOLD_RESET_COMPLETIONS
        with patch("agent.fallback_cooldown.time.monotonic", new=lambda: FROZEN):
            _activate(agent, reason=FailoverReason.server_error)
            assert agent._primary_restore_hold_count == 1
            assert agent._primary_restore_hold_until == FROZEN + fc._PRIMARY_RESTORE_HOLD_BASE_S

    def test_below_threshold_completions_still_escalate(self):
        agent = _make_agent(FB)
        agent._primary_unproven_since_restore = True
        agent._primary_restore_hold_count = 2
        agent._primary_completions_since_flap = fc._PRIMARY_RESTORE_HOLD_RESET_COMPLETIONS - 1
        with patch("agent.fallback_cooldown.time.monotonic", new=lambda: FROZEN):
            _activate(agent, reason=FailoverReason.server_error)
            assert agent._primary_restore_hold_count == 3
            assert agent._primary_restore_hold_until == FROZEN + 4 * fc._PRIMARY_RESTORE_HOLD_BASE_S


class TestRestoreGate:
    def test_restore_declined_while_held(self):
        agent = _make_agent(FB)
        _activate(agent)
        agent._rate_limited_until = 0
        agent._primary_restore_hold_until = time.monotonic() + 600
        assert agent._restore_primary_runtime() is False
        assert agent._fallback_activated is True
        assert agent.provider == "openai"

    def test_restore_proceeds_once_hold_expires_and_reproves(self):
        agent = _make_agent(FB)
        _activate(agent)
        agent._rate_limited_until = 0
        agent._primary_restore_hold_until = time.monotonic() - 1  # expired
        agent._credential_pool = _FakePool("custom", next_at=None)
        with patch("agent.process_bootstrap.OpenAI", return_value=MagicMock()):
            assert agent._restore_primary_runtime() is True
        assert agent._fallback_activated is False
        # The new probe must prove itself: a fallback before the first completed primary
        # response arms the hold.
        assert agent._primary_unproven_since_restore is True


class TestProcessMap:
    def test_process_hold_gates_another_agent_on_same_route(self):
        a = _make_agent(FB)
        b = _make_agent(FB)
        a._primary_unproven_since_restore = True
        with patch("agent.fallback_cooldown.time.monotonic", new=lambda: FROZEN):
            _activate(a, reason=FailoverReason.server_error)
            assert fc.primary_restore_hold_remaining(b) > 0
            fc.clear_primary_restore_hold(a)
            assert fc.primary_restore_hold_remaining(b) == 0.0


class _StubAgent:
    """Bare agent for the mid-run module (mirrors test_mid_run_restore's stub)."""

    def __init__(self, hold_until=0.0, restore_ok=True):
        self._primary_runtime = {"model": "prim", "provider": "pp"}
        self._fallback_activated = True
        self._empty_content_retries = 0
        self.model, self.provider = "fb", "fbp"
        self._primary_restore_hold_until = hold_until
        self.restore_ok = restore_ok
        self.restores = 0
        for name, value in efr.PER_TURN_DEFAULTS:
            setattr(self, name, value)

    def _restore_primary_runtime(self):
        self.restores += 1
        if self.restore_ok:
            self._fallback_activated = False
            self.model, self.provider = "prim", "pp"
        return self.restore_ok


def _empty_fallback(stub):
    stub._fallback_activated = True
    stub.model, stub.provider = "fb", "fbp"
    efr.note_empty_fallback_activated(stub)


def _boundaries_until_restore(stub, limit=50):
    for i in range(limit):
        if efr.maybe_restore_primary_mid_run(stub):
            return i
    return None


class TestMidRunProbe:
    def test_mid_run_probe_waits_out_hold_without_budget_burn(self):
        stub = _StubAgent(hold_until=time.monotonic() + 600)
        _empty_fallback(stub)
        assert efr.maybe_restore_primary_mid_run(stub) is False
        assert stub.restores == 0
        assert stub._empty_restore_attempts == 0
        assert stub._empty_restore_skip_remaining == efr._POST_ACTIVATION_SKIP

    def test_mid_run_probe_resumes_after_hold_expires(self):
        stub = _StubAgent(hold_until=time.monotonic() - 1)
        _empty_fallback(stub)
        assert _boundaries_until_restore(stub) == 1  # post-activation skip, then probe
        assert stub.restores == 1


class TestProvenTracking:
    def test_response_ok_marks_primary_proven(self):
        agent = SimpleNamespace(
            _fallback_activated=False,
            _primary_runtime={"model": "prim", "provider": "pp"},
            model="prim",
            provider="pp",
            _primary_unproven_since_restore=True,
            _primary_completions_since_flap=0,
        )
        efr.note_primary_response_ok(agent)
        assert agent._primary_unproven_since_restore is False
        assert agent._primary_completions_since_flap == 1
        assert agent._primary_run_started_at > 0

    def test_fallback_response_does_not_prove_primary(self):
        agent = SimpleNamespace(
            _fallback_activated=True,
            _primary_runtime={"model": "prim", "provider": "pp"},
            model="prim",
            provider="pp",
            _primary_unproven_since_restore=True,
            _primary_completions_since_flap=0,
        )
        fc.note_primary_completion(agent)
        assert agent._primary_unproven_since_restore is True
        assert agent._primary_completions_since_flap == 0
