"""slash.exec /usage: the Nous block gets the last reply's window gauges only on a session already known
to be on a usage-window plan; every other session makes exactly main's call."""

from __future__ import annotations

import contextlib
import threading
from types import SimpleNamespace

import pytest

from tui_gateway import methods_slash


@pytest.fixture
def calls(monkeypatch):
    seen: list[dict] = []

    def _lines(**kw):
        seen.append(kw)
        return ["Nous credits block"]

    monkeypatch.setattr("agent.account_usage.nous_credits_lines", _lines)
    monkeypatch.setattr("agent.account_usage.fetch_account_usage", lambda *a, **kw: None)
    monkeypatch.setattr(methods_slash, "_session_profile_runtime_scope", lambda session, **kw: contextlib.nullcontext())
    monkeypatch.setattr(methods_slash, "_config_model_target", lambda: ("", ""))
    monkeypatch.setattr(methods_slash, "_session_usage_snapshot", lambda session: {"model": "m"}, raising=False)
    monkeypatch.setattr(methods_slash, "_metadata_mirror", lambda session: {}, raising=False)
    return seen


def _session(state):
    agent = SimpleNamespace(model="m", _credits_state=state)
    return {"agent": agent, "history": [], "history_lock": threading.Lock()}


def test_no_credits_state_makes_mains_call(calls):
    out = methods_slash._format_live_usage_output("sid", _session(None), "")
    assert calls == [{}]
    assert out.count("Nous credits block") == 1


def test_non_window_credits_state_makes_mains_call(calls):
    methods_slash._format_live_usage_output("sid", _session(SimpleNamespace(is_usage_window=False)), "")
    assert calls == [{}]


def test_a_mock_agent_attribute_is_not_a_window_plan(calls):
    methods_slash._format_live_usage_output("sid", _session(SimpleNamespace(is_usage_window=object())), "")
    assert calls == [{}]


def test_window_session_passes_the_reply_gauges_once(calls):
    state = SimpleNamespace(is_usage_window=True)
    out = methods_slash._format_live_usage_output("sid", _session(state), "")
    assert calls == [{"credits_state": state}]
    assert out.count("Nous credits block") == 1  # one Nous block, never a second plan block
