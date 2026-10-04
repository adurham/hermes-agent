"""Bounded mid-run primary restore after an EMPTY-caused fallback (agent/empty_fallback_restore.py)
and fallback-event recording in try_activate_fallback."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agent import empty_fallback_restore as efr
from agent.error_classifier import FailoverReason


@pytest.fixture(autouse=True)
def _transient_primary():
    """'pp' (the stub primary) declares the transient policy; every other provider does not."""
    from providers.base import ProviderProfile

    profile = ProviderProfile(name="pp", empty_completion_policy="transient")
    with patch("providers.get_provider_profile", lambda name: profile if name == "pp" else None):
        yield


class _Agent:
    def __init__(self, restore_ok=True):
        self._primary_runtime = {"model": "prim", "provider": "pp"}
        self._fallback_activated = True
        self._empty_content_retries = 0
        self.model, self.provider = "fb", "fbp"
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


def _empty_fallback(agent):
    agent._fallback_activated = True
    agent.model, agent.provider = "fb", "fbp"
    efr.note_empty_fallback_activated(agent)


def _boundaries_until_restore(agent, limit=50):
    for i in range(limit):
        if efr.maybe_restore_primary_mid_run(agent):
            return i
    return None


class TestGating:
    def test_r1_no_restore_for_non_empty_causes(self):
        agent = _Agent()
        agent._last_fallback_cause = FailoverReason.rate_limit.value
        assert _boundaries_until_restore(agent) is None
        assert agent.restores == 0

    def test_r1_no_restore_when_not_on_fallback(self):
        agent = _Agent()
        agent._fallback_activated = False
        agent._last_fallback_cause = "empty_response"
        assert efr.maybe_restore_primary_mid_run(agent) is False
        assert agent.restores == 0

    def test_t5_no_mid_run_restore_for_refusal_like_primary(self):
        agent = _Agent()
        agent._primary_runtime = {"model": "prim", "provider": "metered"}
        _empty_fallback(agent)
        assert _boundaries_until_restore(agent) is None
        assert agent.restores == 0

    def test_r2_first_boundary_after_activation_is_skipped(self):
        agent = _Agent()
        _empty_fallback(agent)
        assert _boundaries_until_restore(agent) == 1  # fallback serves one request first
        assert agent._empty_restore_probe_active is True
        assert agent._empty_content_retries == 0


class TestBackoff:
    def test_r3_declined_restore_doubles_skip(self):
        agent = _Agent(restore_ok=False)
        _empty_fallback(agent)
        gaps = []
        prev = 0
        for i in range(200):
            efr.maybe_restore_primary_mid_run(agent)
            if agent.restores != prev:
                gaps.append(i)
                prev = agent.restores
        # Attempt boundaries: 1, then +3 (skip 2), +5 (skip 4), +9 (skip 8); capped at 4.
        assert gaps == [1, 4, 9, 18]
        assert agent.restores == efr.MAX_RESTORE_ATTEMPTS

    def test_r4_probe_empties_again_doubles_and_caps(self):
        agent = _Agent()
        _empty_fallback(agent)
        waits = []
        for _ in range(6):
            n = _boundaries_until_restore(agent)
            if n is None:
                break
            waits.append(n)
            _empty_fallback(agent)  # restored primary emptied again → ladder re-fell-back
        assert waits == [1, 2, 4, 8]  # skips 1 (post-activation), then 2, 4, 8
        assert agent._empty_restore_attempts == efr.MAX_RESTORE_ATTEMPTS
        assert agent._empty_restore_next_skip == efr.MAX_SKIP

    def test_r4_successful_probe_stays_on_primary(self):
        agent = _Agent()
        _empty_fallback(agent)
        assert _boundaries_until_restore(agent) == 1
        efr.note_primary_response_ok(agent)
        assert agent._empty_restore_probe_active is False
        # A later, unrelated empty fallback is a fresh activation (1-boundary skip), not a failed probe.
        _empty_fallback(agent)
        assert agent._empty_restore_skip_remaining == 1

    def test_restore_exception_counts_as_failed_cycle(self):
        agent = _Agent()
        agent._restore_primary_runtime = MagicMock(side_effect=RuntimeError("boom"))
        _empty_fallback(agent)
        efr.maybe_restore_primary_mid_run(agent)
        assert efr.maybe_restore_primary_mid_run(agent) is False
        assert agent._empty_restore_skip_remaining == 2


class TestPerTurnReset:
    def test_r5_state_is_reset_at_turn_start(self):
        from agent.turn_context import _PER_TURN_RESET_STATE

        reset = dict(_PER_TURN_RESET_STATE)
        for name, default in efr.PER_TURN_DEFAULTS:
            assert reset[name] == default


# ── V1: try_activate_fallback records events ─────────────────────────────────────────────

def _make_agent(fallback_model):
    from run_agent import AIAgent

    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(api_key="k", base_url="https://openrouter.ai/api/v1", quiet_mode=True,
                        skip_context_files=True, skip_memory=True, fallback_model=fallback_model)
    agent.client = MagicMock()
    return agent


def _fb_client():
    client = MagicMock()
    client.base_url, client.api_key = "https://openrouter.ai/api/v1", "fb"
    return client


@pytest.fixture
def two_fallbacks():
    agent = _make_agent([{"provider": "openai", "model": "gpt-4o"}, {"provider": "zai", "model": "glm-4.7"}])
    with patch("agent.auxiliary_client.resolve_provider_client", return_value=(_fb_client(), "x")):
        yield agent


class TestFallbackEvents:
    def test_v1_pending_cause_recorded_and_consumed(self, two_fallbacks):
        agent = two_fallbacks
        start_model, start_provider = agent.model, agent.provider
        agent._fallback_pending_cause = "empty_response"
        assert agent._try_activate_fallback() is True
        assert agent._fallback_pending_cause is None
        assert agent._fallback_events == [{
            "from_model": start_model, "from_provider": start_provider,
            "to_model": "gpt-4o", "to_provider": "openai", "cause": "empty_response",
        }]
        assert agent._last_fallback_cause == "empty_response"

    def test_v1_reason_wins_and_unset_cause_is_none(self, two_fallbacks):
        agent = two_fallbacks
        agent._try_activate_fallback(reason=FailoverReason.rate_limit)
        agent._try_activate_fallback()
        assert [e["cause"] for e in agent._fallback_events] == [FailoverReason.rate_limit.value, None]
        assert agent._fallback_events[1]["from_model"] == "gpt-4o"
        assert agent._last_fallback_cause is None

    def test_r1_rate_limit_fallback_never_restores_mid_run(self, two_fallbacks):
        agent = two_fallbacks
        agent._try_activate_fallback(reason=FailoverReason.server_error)
        assert _boundaries_until_restore(agent, limit=20) is None
        assert agent._fallback_activated is True
