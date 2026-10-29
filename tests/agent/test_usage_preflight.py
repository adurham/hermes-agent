"""Per-turn usage preflight (FORK ``fallback.preflight_usage``).

The point of the knob is that a spent plan is discovered *before* the request, so the turn never
spends a refused 402 + cold cache re-read on the fallback, and never restores a primary that will
refuse again. These tests pin the decision table (the same rule the refusal path uses), the three
routing outcomes and the fail-open behaviour.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from agent import usage_preflight as up
from agent.usage_gauges import SCOPE_ALL, SCOPE_TOP_TIER, WindowGauge

MODEL = "deepseek/deepseek-v4.1-flash"
BASE_URL = "https://inference-api.nousresearch.com/v1"
CHAIN = [{"provider": "ollama-cloud", "model": "deepseek-v4.1-flash"}]
PRIMARY_RT = {"provider": "nous", "model": MODEL, "base_url": BASE_URL}


def _agent(**overrides):
    base = dict(
        provider="nous", model=MODEL, base_url=BASE_URL,
        _fallback_chain=list(CHAIN), _fallback_activated=False, _primary_runtime=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _on_fallback(**overrides):
    """A turn that starts on the chain, with the primary remembered for the restore."""
    return _agent(provider="ollama-cloud", model=MODEL, base_url="https://ollama.com/v1",
                  _fallback_activated=True, _primary_runtime=dict(PRIMARY_RT), **overrides)


def _window(*gauges, paused=None):
    return SimpleNamespace(gauges=tuple(gauges), auto_top_up_enabled=False,
                           paused_reason=paused, keep_going_on_credits=False)


def _gauge(gid="day", scope=SCOPE_ALL, used_bp=10_000, resets_in_s=3600):
    return WindowGauge(gid, scope, gid, used_bp, int((time.time() + resets_in_s) * 1000))


def _portal(monkeypatch, window, *, calls=None):
    info = SimpleNamespace(usage_window=window)
    calls = [] if calls is None else calls

    def fake_get(force_fresh=False, **_kw):
        calls.append(force_fresh)
        return info

    monkeypatch.setattr("hermes_cli.nous_account.get_nous_portal_account_info", fake_get)
    monkeypatch.setattr("hermes_cli.nous_account.cached_nous_portal_account_info", lambda **_kw: None)
    return calls


def _config(monkeypatch, **fallback):
    monkeypatch.setattr("hermes_cli.config.load_config_readonly",
                        lambda: {"fallback": {"preflight_usage": True, **fallback}})


def _catalog(monkeypatch, rows):
    monkeypatch.setattr("agent.plan_limits.cached_pricing", lambda *a, **k: dict(rows))
    monkeypatch.setattr("hermes_cli.models_pricing.get_pricing_for_provider", lambda *a, **k: dict(rows))


class TestEnabled:
    def test_default_off(self, monkeypatch):
        monkeypatch.setattr("hermes_cli.config.load_config_readonly", dict)
        assert up.usage_preflight_enabled() is False

    def test_opt_in(self, monkeypatch):
        _config(monkeypatch)
        assert up.usage_preflight_enabled() is True

    def test_unreadable_config_keeps_the_request_first_path(self, monkeypatch):
        def boom():
            raise RuntimeError("unreadable")
        monkeypatch.setattr("hermes_cli.config.load_config_readonly", boom)
        assert up.usage_preflight_enabled() is False


class TestBlockedGauge:
    def test_a_full_daily_allowance_blocks_a_paid_model(self, monkeypatch):
        _portal(monkeypatch, _window(_gauge("day")))
        _catalog(monkeypatch, {MODEL: {"usage_level": "medium"}})
        g = up.blocked_gauge(_agent(), provider="nous", model=MODEL, base_url=BASE_URL)
        assert g is not None and g.id == "day"

    def test_room_in_every_allowance_means_no_switch(self, monkeypatch):
        _portal(monkeypatch, _window(_gauge("day", used_bp=9_860)))
        _catalog(monkeypatch, {MODEL: {"usage_level": "medium"}})
        assert up.blocked_gauge(_agent(), provider="nous", model=MODEL, base_url=BASE_URL) is None

    def test_a_passed_reset_is_room_again(self, monkeypatch):
        """A portal snapshot can lag the clock; the first request after a reset is how we learn."""
        _portal(monkeypatch, _window(_gauge("day", resets_in_s=-60)))
        _catalog(monkeypatch, {MODEL: {"usage_level": "medium"}})
        assert up.blocked_gauge(_agent(), provider="nous", model=MODEL, base_url=BASE_URL) is None

    def test_top_tier_gauge_blocks_a_model_the_catalog_marks_top_tier(self, monkeypatch):
        _portal(monkeypatch, _window(_gauge("day", used_bp=100),
                                     _gauge("top_tier", scope=SCOPE_TOP_TIER)))
        _catalog(monkeypatch, {MODEL: {"usage_level": "top_tier"}})
        g = up.blocked_gauge(_agent(), provider="nous", model=MODEL, base_url=BASE_URL)
        assert g is not None and g.id == "top_tier"

    def test_top_tier_gauge_never_preempts_a_model_with_no_tier_mark(self, monkeypatch):
        """A wrong switch spends another provider's quota; the reactive 402 still catches this one."""
        _portal(monkeypatch, _window(_gauge("day", used_bp=100),
                                     _gauge("top_tier", scope=SCOPE_TOP_TIER)))
        _catalog(monkeypatch, {})
        assert up.blocked_gauge(_agent(), provider="nous", model=MODEL, base_url=BASE_URL) is None

    def test_a_non_nous_route_is_never_checked(self, monkeypatch):
        calls = _portal(monkeypatch, _window(_gauge("day")))
        assert up.blocked_gauge(_agent(), provider="ollama-cloud", model=MODEL,
                                base_url="https://ollama.com/v1") is None
        assert calls == []

    def test_a_non_window_account_has_no_opinion(self, monkeypatch):
        _portal(monkeypatch, None)
        _catalog(monkeypatch, {MODEL: {"usage_level": "medium"}})
        assert up.blocked_gauge(_agent(), provider="nous", model=MODEL, base_url=BASE_URL) is None

    def test_the_lookup_is_fresh_for_the_turn(self, monkeypatch):
        calls = _portal(monkeypatch, _window(_gauge("day", used_bp=100)))
        _catalog(monkeypatch, {MODEL: {"usage_level": "medium"}})
        up.blocked_gauge(_agent(), provider="nous", model=MODEL, base_url=BASE_URL)
        assert calls == [True]


class TestRoute:
    def _capture(self, monkeypatch, *, result=True):
        calls = []

        def fake_try(agent, **kwargs):
            calls.append(kwargs)
            return result

        monkeypatch.setattr("agent.chat_completion_helpers.try_activate_fallback", fake_try)
        return calls

    def test_a_spent_allowance_binds_the_chain_before_any_request(self, monkeypatch):
        _config(monkeypatch)
        _portal(monkeypatch, _window(_gauge("day", resets_in_s=3600)))
        _catalog(monkeypatch, {MODEL: {"usage_level": "medium"}})
        calls = self._capture(monkeypatch)
        assert up.preflight_usage_route(_agent()) is True
        assert len(calls) == 1
        kw = calls[0]
        assert kw["reason"].value == "billing"
        assert kw["reset_at"] > time.time()          # seconds: the shape the cooldown expects
        notice = kw["notice_override"]
        assert "daily allowance is used up" in notice
        assert "ollama-cloud" in notice and "deepseek-v4.1-flash" in notice

    def test_on_the_chain_with_a_still_spent_plan_keeps_the_fallback_silently(self, monkeypatch):
        """No restore attempt, no second notice: the reason the primary would refuse is unchanged."""
        _config(monkeypatch)
        _portal(monkeypatch, _window(_gauge("day")))
        _catalog(monkeypatch, {MODEL: {"usage_level": "medium"}})
        switched = self._capture(monkeypatch)
        assert up.preflight_usage_route(_on_fallback()) is True
        assert switched == []

    def test_on_the_chain_after_the_reset_hands_the_turn_back_to_the_primary(self, monkeypatch):
        _config(monkeypatch)
        _portal(monkeypatch, _window(_gauge("day", used_bp=4_000)))
        _catalog(monkeypatch, {MODEL: {"usage_level": "medium"}})
        assert up.preflight_usage_route(_on_fallback()) is False

    def test_knob_off_costs_no_lookup_and_no_switch(self, monkeypatch):
        monkeypatch.setattr("hermes_cli.config.load_config_readonly",
                            lambda: {"fallback": {"preflight_usage": False}})
        calls = _portal(monkeypatch, _window(_gauge("day")))
        switched = self._capture(monkeypatch)
        assert up.preflight_usage_route(_agent()) is False
        assert calls == [] and switched == []

    def test_no_chain_means_no_switch(self, monkeypatch):
        _config(monkeypatch)
        _portal(monkeypatch, _window(_gauge("day")))
        _catalog(monkeypatch, {MODEL: {"usage_level": "medium"}})
        switched = self._capture(monkeypatch)
        assert up.preflight_usage_route(_agent(_fallback_chain=[])) is False
        assert switched == []

    def test_a_failed_lookup_never_blocks_the_turn(self, monkeypatch):
        _config(monkeypatch)

        def boom(**_kw):
            raise RuntimeError("portal down")
        monkeypatch.setattr("hermes_cli.nous_account.get_nous_portal_account_info", boom)
        monkeypatch.setattr("hermes_cli.nous_account.cached_nous_portal_account_info", lambda **_kw: None)
        switched = self._capture(monkeypatch)
        assert up.preflight_usage_route(_agent()) is False
        assert switched == []
