"""Transient empty-completion policy (ProviderProfile.empty_completion_policy).

Behaviour contracts:
- T1 transient route: deterministic_empty() never short-circuits.
- T2 transient route: same-provider retry budget >= transient_max_retries, backoff unchanged.
- T3 transient route: fallback only once budget spent AND (floor elapsed OR hard cap).
- T4 a non-empty response starts a new streak (clock + counters).
- T5 refusal-like routes (default / no profile field) behave exactly as before.
- Carrier-only reasoning_details does not count as thinking-only.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent import empty_response_guard as guard
from agent import turn_empty_response as ter
from providers.base import ProviderProfile


def _agent(provider="flaky-sub", **overrides):
    base = dict(
        model="m", provider=provider, api_mode="chat_completions", base_url=None, api_key=None,
        _empty_content_retries=0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _zero_usage():
    return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=20_000, completion_tokens=0, total_tokens=20_000))


@pytest.fixture
def transient_profile():
    """Route lookups of 'flaky-sub' to a transient profile; everything else → None."""
    profile = ProviderProfile(name="flaky-sub", empty_completion_policy="transient")
    with patch("providers.get_provider_profile", lambda name: profile if name == "flaky-sub" else None):
        yield profile


def _record(agent, n, response=None):
    for _ in range(n):
        guard.record_empty_attempt(agent, finish_reason="stop", response=response or _zero_usage(),
                                   observed_generation=False)
        agent._empty_content_retries += 1


class TestPolicyField:
    def test_default_profile_is_refusal_like(self):
        assert ProviderProfile(name="x").empty_completion_policy == guard.POLICY_REFUSAL_LIKE

    def test_policy_lookup(self, transient_profile):
        assert guard.empty_completion_policy(_agent()) == guard.POLICY_TRANSIENT
        assert guard.empty_completion_policy(_agent(provider="other")) == guard.POLICY_REFUSAL_LIKE
        assert guard.empty_completion_policy(_agent(provider=None)) == guard.POLICY_REFUSAL_LIKE

    def test_unknown_policy_value_is_refusal_like(self):
        profile = ProviderProfile(name="weird", empty_completion_policy="bogus")
        with patch("providers.get_provider_profile", lambda name: profile):
            assert guard.empty_completion_policy(_agent(provider="weird")) == guard.POLICY_REFUSAL_LIKE

    def test_lookup_failure_is_refusal_like(self):
        def _boom(name):
            raise RuntimeError("registry broken")
        with patch("providers.get_provider_profile", _boom):
            assert guard.empty_completion_policy(_agent()) == guard.POLICY_REFUSAL_LIKE


class TestDeterministicGate:
    def test_t1_transient_route_never_deterministic(self, transient_profile):
        agent = _agent()
        _record(agent, 5)
        assert guard.deterministic_empty(agent) is False

    def test_t5_refusal_like_route_still_deterministic(self, transient_profile):
        agent = _agent(provider="other")
        _record(agent, 2)
        assert guard.deterministic_empty(agent) is True


class TestBudget:
    def test_t2_transient_budget_raised_to_config(self, transient_profile):
        agent = _agent()
        assert guard.empty_retry_budget(agent, _zero_usage()) == guard.DEFAULT_TRANSIENT_MAX_RETRIES
        agent._empty_guard_transient_max_retries = 10
        assert guard.empty_retry_budget(agent, _zero_usage()) == 10

    def test_transient_budget_never_below_base(self, transient_profile):
        agent = _agent(_empty_guard_transient_max_retries=1)
        assert guard.empty_retry_budget(agent, _zero_usage()) == guard.DEFAULT_EMPTY_RETRY_BUDGET

    def test_t2_transient_budget_overrides_cost_reduction(self, transient_profile, monkeypatch):
        monkeypatch.setattr(guard, "_estimate_attempt_cost", lambda a, r: Decimal("5"))
        assert guard.empty_retry_budget(_agent(), _zero_usage()) == guard.DEFAULT_TRANSIENT_MAX_RETRIES

    def test_t5_refusal_like_budget_unchanged(self, transient_profile, monkeypatch):
        assert guard.empty_retry_budget(_agent(provider="other"), _zero_usage()) == 3
        monkeypatch.setattr(guard, "_estimate_attempt_cost", lambda a, r: Decimal("5"))
        assert guard.empty_retry_budget(_agent(provider="other"), _zero_usage()) == 1


class TestFallbackFloor:
    def _streak(self, retries, elapsed):
        agent = _agent(_empty_content_retries=retries, _empty_streak_started_at=100.0)
        with patch.object(guard, "_monotonic", lambda: 100.0 + elapsed):
            return guard.transient_fallback_allowed(agent, 6)

    def test_t3_budget_not_spent_blocks(self, transient_profile):
        assert self._streak(retries=5, elapsed=500) is False

    def test_t3_budget_spent_floor_not_elapsed_blocks(self, transient_profile):
        assert self._streak(retries=6, elapsed=30) is False

    def test_t3_budget_spent_and_floor_elapsed_allows(self, transient_profile):
        assert self._streak(retries=6, elapsed=90) is True

    def test_t3_hard_cap_allows_without_floor(self, transient_profile):
        assert self._streak(retries=8, elapsed=0) is True

    def test_t5_refusal_like_route_ungated(self, transient_profile):
        agent = _agent(provider="other", _empty_content_retries=0)
        assert guard.transient_fallback_allowed(agent, 3) is True

    def test_floor_config(self, transient_profile):
        agent = _agent(_empty_content_retries=6, _empty_streak_started_at=0.0,
                       _empty_guard_transient_floor_seconds=10.0)
        with patch.object(guard, "_monotonic", lambda: 11.0):
            assert guard.transient_fallback_allowed(agent, 6) is True


class TestStreakClock:
    def test_t4_new_streak_restamps_clock(self):
        agent = _agent()
        with patch.object(guard, "_monotonic", lambda: 10.0):
            _record(agent, 2)
        assert agent._empty_streak_started_at == 10.0
        agent._empty_content_retries = 0  # any non-empty reset site
        with patch.object(guard, "_monotonic", lambda: 500.0):
            _record(agent, 1)
            assert guard.streak_elapsed_seconds(agent) == 0.0


class TestRetryLadder:
    """``_retry_empty`` — the seam the conversation loop calls per retry-eligible empty."""

    def _agent(self, provider):
        agent = _agent(provider=provider)
        agent._buffer_diagnostic_status = lambda *a, **k: None
        return agent

    def _drive(self, agent, clock):
        actions = []
        with (
            patch.object(ter, "interruptible_backoff_sleep", lambda *a, **k: None),
            patch.object(guard, "_monotonic", lambda: clock[0]),
        ):
            for _ in range(20):
                action, _, _ = ter._retry_empty(agent, _zero_usage(), "stop", True, messages=[],
                                                conversation_history=None, api_call_count=1)
                actions.append(action)
                if action is None:
                    break
                clock[0] += 1.0
        return actions

    def test_t2_t3_transient_holds_same_provider_until_hard_cap(self, transient_profile):
        actions = self._drive(self._agent("flaky-sub"), [0.0])
        assert actions == ["continue"] * 8 + [None]

    def test_t3_transient_floor_releases_after_budget(self, transient_profile):
        agent = self._agent("flaky-sub")
        agent._empty_guard_transient_floor_seconds = 3.0
        assert self._drive(agent, [0.0]) == ["continue"] * 6 + [None]

    def test_t5_refusal_like_stops_after_two_deterministic(self, transient_profile):
        assert self._drive(self._agent("other"), [0.0]) == ["continue", None]

    def test_transient_backoff_uses_existing_schedule(self, transient_profile):
        seen = []

        def _backoff(n, *, base_delay, max_delay, **_):
            seen.append((n, base_delay, max_delay))
            return 0.0
        with patch("agent.retry_utils.jittered_backoff", _backoff):
            self._drive(self._agent("flaky-sub"), [0.0])
        assert [s[0] for s in seen] == list(range(1, 9))
        assert {s[1:] for s in seen} == {(5.0, 60.0)}


class TestCarrierFilter:
    CARRIER = {"type": "claude-subscription-directsdk-experimental.native_assistant", "data": "x"}

    def test_carrier_only_is_not_reasoning(self):
        assert ter._model_reasoning_details([self.CARRIER]) is False

    def test_real_reasoning_detail_counts(self):
        assert ter._model_reasoning_details([self.CARRIER, {"type": "reasoning.text", "text": "hm"}]) is True

    def test_empty_and_none(self):
        assert ter._model_reasoning_details(None) is False
        assert ter._model_reasoning_details([]) is False

    def test_object_shaped_details(self):
        assert ter._model_reasoning_details([SimpleNamespace(type=self.CARRIER["type"])]) is False
        assert ter._model_reasoning_details([SimpleNamespace(type="reasoning.summary")]) is True


class TestResolveTransientSettings:
    def test_defaults(self):
        assert guard.resolve_transient_settings(None) == (6, 90.0)
        assert guard.resolve_transient_settings({}) == (6, 90.0)

    def test_custom(self):
        assert guard.resolve_transient_settings(
            {"transient_max_retries": "9", "transient_fallback_floor_seconds": 30}) == (9, 30.0)

    @pytest.mark.parametrize("bad", [True, "banana", 0, -3, None, [1]])
    def test_malformed_retries_fall_back(self, bad):
        assert guard.resolve_transient_settings({"transient_max_retries": bad})[0] == 6

    @pytest.mark.parametrize("bad", [True, "x", -1, "nan"])
    def test_malformed_floor_falls_back(self, bad):
        assert guard.resolve_transient_settings({"transient_fallback_floor_seconds": bad})[1] == 90.0

    def test_default_config_schema_matches(self):
        from hermes_cli.config_defaults import DEFAULT_CONFIG

        section = DEFAULT_CONFIG["agent"]["empty_response_guard"]
        assert guard.resolve_transient_settings(section) == (
            guard.DEFAULT_TRANSIENT_MAX_RETRIES, guard.DEFAULT_TRANSIENT_FLOOR_SECONDS)
