"""A provider fallback must not become the session's own route.

A fallback is turn-scoped (`restore_primary_runtime` undoes it next turn). Persisting the backup
route as the session's model/provider stranded a resumed chat on the backup provider: it came back
on `ollama-cloud`, so the primary was never retried and the preflight never consulted the plan.
"""

from __future__ import annotations

from types import SimpleNamespace

from agent.voice_turn_route import session_runtime_view
from gateway.run_turn_prepare import GatewayTurnPrepareMixin

PRIMARY = {"model": "deepseek/deepseek-v4.1-flash", "provider": "nous",
           "base_url": "https://inference-api.nousresearch.com/v1", "api_mode": "chat_completions",
           "reasoning_config": {"effort": "high"}}


def _agent(**overrides):
    base = dict(
        model="deepseek-v4.1-flash", provider="ollama-cloud", base_url="https://ollama.com/v1",
        api_mode="chat_completions", reasoning_config={"effort": "low"},
        _fallback_activated=True, _primary_runtime=dict(PRIMARY), _voice_route_state=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestView:
    def test_a_fallback_turn_reports_the_session_own_route(self):
        view = session_runtime_view(_agent())
        assert (view.model, view.provider) == (PRIMARY["model"], "nous")
        assert view.base_url == PRIMARY["base_url"]
        # The session's effort, not the effort the fallback turn re-resolved.
        assert view.reasoning_config == PRIMARY["reasoning_config"]

    def test_no_fallback_returns_the_agent_itself(self):
        agent = _agent(_fallback_activated=False)
        assert session_runtime_view(agent) is agent

    def test_a_fallback_without_a_primary_snapshot_returns_the_agent(self):
        agent = _agent(_primary_runtime=None)
        assert session_runtime_view(agent) is agent

    def test_a_voice_route_still_wins(self):
        agent = _agent(_voice_route_state={"snapshot": {"model": "voice-model", "provider": "exo"},
                                           "reasoning_config": None})
        view = session_runtime_view(agent)
        assert (view.model, view.provider) == ("voice-model", "exo")


class _Db:
    def __init__(self, row):
        self.row = row
        self.written = None

    def get_session(self, _sid):
        return self.row

    def update_session_meta(self, sid, meta, model=None):
        self.written = {"sid": sid, "meta": meta, "model": model}


class TestGatewayPersist:
    def _runner(self, row):
        db = _Db(row)
        return SimpleNamespace(_session_db=SimpleNamespace(_db=db)), db

    def test_a_fallback_turn_persists_the_primary_and_flags_the_state(self):
        runner, db = self._runner({"model": "deepseek/deepseek-v4.1-flash", "model_config": None})
        GatewayTurnPrepareMixin._sync_session_model_from_agent(runner, "sid-1", _agent())
        assert db.written is not None
        assert db.written["model"] == PRIMARY["model"]
        meta = db.written["meta"]
        assert '"provider": "nous"' in meta
        assert '"fallback_active": true' in meta

    def test_a_plain_turn_persists_the_live_route(self):
        runner, db = self._runner({"model": "deepseek/deepseek-v4.1-flash", "model_config": None})
        agent = _agent(_fallback_activated=False, provider="nous", base_url=PRIMARY["base_url"])
        GatewayTurnPrepareMixin._sync_session_model_from_agent(runner, "sid-1", agent)
        meta = db.written["meta"]
        assert '"provider": "nous"' in meta and '"fallback_active": false' in meta
