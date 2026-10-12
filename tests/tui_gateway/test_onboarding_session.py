"""The onboarding chat in the gateway: ``onboarding.start`` / ``onboarding.state`` / ``onboarding.skip``,
the toolset fold-in that gives setup_beat to that chat only, the scripted opening, the turn note,
and the run record across a compression rotation."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import onboarding_record
from agent.onboarding_flow import Facts, Shown
from agent.onboarding_text import GREETING
from tui_gateway import server, server_requests

FACTS = Facts(os="macos", arch="arm64", machine_kind="Mac", plugins=("blender",))


@pytest.fixture
def root(tmp_path, monkeypatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(server, "_hermes_home", str(home))
    monkeypatch.setattr(server, "_db", None)
    monkeypatch.setattr("agent.onboarding_facts.read_facts", lambda **kw: FACTS)
    return home


@pytest.fixture
def client(root, monkeypatch):
    """session.resume and prompt.submit as a recorder: the RPCs onboarding.start drives."""
    calls = []

    def resume(rid, params):
        calls.append(("resume", params))
        return {"result": {"session_id": f"rt-{params['session_id']}"}}

    def submit(rid, params):
        calls.append(("submit", params))
        return {"result": {"status": "streaming"}}

    monkeypatch.setitem(server._methods, "session.resume", resume)
    monkeypatch.setitem(server._methods, "prompt.submit", submit)
    return calls


def _call(method: str, params: dict | None = None) -> dict:
    return server.handle_request({"jsonrpc": "2.0", "id": 3, "method": method, "params": params or {}})


def test_start_creates_one_onboarding_chat_and_plays_the_opening_once(client):
    started = _call("onboarding.start", {"locale": "en-IN"})["result"]
    stored = started["stored_session_id"]

    db = server._get_db()
    assert started == {"session_id": f"rt-{stored}", "stored_session_id": stored, "resumed": False}
    assert onboarding_record.load(db, stored)["phase"] == "onboarding"
    assert client[0] == ("resume", {"profile": "default", "session_id": stored, "source": "desktop"})
    kind, submitted = client[1]
    assert kind == "submit" and submitted["display_kind"] == "hidden"
    assert "first-run onboarding" in submitted["text"] and "setup_beat" in submitted["text"]
    assert _call("onboarding.state")["result"]["session_id"] == stored

    # A relaunch reopens the same chat without a second opening; rerun starts a new one.
    again = _call("onboarding.start")["result"]
    assert again["stored_session_id"] == stored and again["resumed"] is True and len(client) == 3
    assert _call("onboarding.start", {"rerun": True})["result"]["stored_session_id"] != stored


def test_only_a_chat_with_a_run_in_progress_gets_the_onboarding_toolset(root):
    db = server._get_db()
    db.create_session("onb", "desktop")
    db.create_session("plain", "desktop")
    onboarding_record.start(db, "onb", FACTS)

    assert server._onboarding_toolsets(["web", "file"], db, "onb") == ["web", "file", "onboarding"]
    assert server._onboarding_toolsets(["web", "file"], db, "plain") == ["web", "file"]
    assert server._onboarding_toolsets([], db, "onb") == []
    # A refresh keeps what the agent was built with, and never adds it to another chat.
    built = SimpleNamespace(enabled_toolsets=["web", "onboarding"])
    assert server._carry_onboarding_toolset(["web"], built) == ["web", "onboarding"]
    assert server._carry_onboarding_toolset(["web"], SimpleNamespace(enabled_toolsets=["web"])) == ["web"]


def _agent(db, sid="onb"):
    return SimpleNamespace(valid_tool_names={"setup_beat", "terminal"}, _session_db=db, session_id=sid,
                           _gateway_turn_context_notes="")


def test_the_opening_plays_the_greeting_and_name_card_until_the_name_card_was_shown(root):
    db = server._get_db()
    db.create_session("onb", "desktop")
    onboarding_record.start(db, "onb", FACTS)
    agent = _agent(db)

    assert list(server._onboarding_prelude(agent, [])) == [(GREETING, "setup_beat", {"beat": "name"})]
    relaunch = [{"role": "assistant", "content": GREETING}]
    assert list(server._onboarding_prelude(agent, relaunch)) == [("", "setup_beat", {"beat": "name"})]
    onboarding_record.advance(db, "onb", Shown("name"))
    assert server._onboarding_prelude(agent, []) is None
    assert server._onboarding_prelude(SimpleNamespace(valid_tool_names={"terminal"}), []) is None


def test_the_turn_note_rides_a_user_turn_only_while_the_run_is_open(root):
    db = server._get_db()
    db.create_session("onb", "desktop")
    onboarding_record.start(db, "onb", FACTS)
    onboarding_record.advance(db, "onb", Shown("name"))
    agent = _agent(db)

    server._stage_onboarding_turn_note("ui", agent, history_empty=True)
    assert agent._gateway_turn_context_notes == ""
    server._stage_onboarding_turn_note("ui", agent, history_empty=False)
    assert 'setup_beat(beat="name")' in agent._gateway_turn_context_notes


def test_skip_setup_ends_the_run_and_closes_the_waiting_card(root, monkeypatch):
    db = server._get_db()
    db.create_session("onb", "desktop")
    onboarding_record.start(db, "onb", FACTS)
    sid, frames, events = "ui-onb", [], []
    monkeypatch.setitem(server._sessions, sid, {"session_key": "onb", "agent": _agent(db), "source": "desktop"})
    monkeypatch.setattr(server_requests, "_answerable", lambda _sid: True)
    monkeypatch.setattr(server_requests, "_write", frames.append)
    monkeypatch.setattr(server_requests, "_emit", lambda kind, _sid, payload: events.append((kind, payload)))
    reply = []
    waiter = threading.Thread(target=lambda: reply.append(server._setup_beat_request(
        sid, {"beat": "theme", "card": {"picker": "accent"}})))
    waiter.start()
    from tests.waiting import wait_until
    wait_until(lambda: server_requests.pending_kind(sid) == "setup_beat")

    assert _call("onboarding.skip", {"session_id": sid})["result"] == {"skipped": True}
    waiter.join(timeout=5)
    assert reply == [None]
    assert events == [("request.cancel", {"id": frames[0]["id"], "method": "setup_beat", "reason": "resolved"})]
    assert onboarding_record.load(db, "onb")["phase"] == "skipped"
    from hermes_cli.config import read_user_config_raw
    onboarding = read_user_config_raw(root / "config.yaml")["onboarding"]
    assert onboarding["run"] is False and onboarding["seen"]["profile_build_offered"] is True


def test_a_compression_rotation_carries_the_run_record(root):
    db = server._get_db()
    db.create_session("parent", "desktop")
    onboarding_record.start(db, "parent", FACTS)
    db.patch_session_model_config("parent", {"codex_thread_id": "segment-only"})

    db.publish_compression_child(parent_session_id="parent", child_session_id="child", source="desktop",
                                 messages=[{"role": "user", "content": "summary"}],
                                 require_compression_lease=False)

    assert onboarding_record.load(db, "child") == onboarding_record.load(db, "parent")
    assert db.get_session_model_config_value("child", "codex_thread_id") is None
