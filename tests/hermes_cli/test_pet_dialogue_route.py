"""Fork regression: ``POST /api/pet/dialogue`` must stay registered.

The desktop calls this path (``apps/desktop/src/api/system.ts`` ->
``fetchPetDialogue``, driven by ``pet-bubble.tsx``'s ``speakAnnouncedBeat``) on
the two announced pet-voice beats, and degrades to its static line pool on a
404. The fork route was lost in the Sep 2026 web_server router decomposition
(upstream never had it), so a 404 became every request. This pins both the
registration and the opt-in gate: off -> clean 404, on -> a line from the aux
task.

Mirrors the guard style of ``test_desktop_audio_routes_registered`` (a merge
once silently dropped /api/audio/speak + /voices, and that contract test
exists so it cannot happen again).
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    from hermes_cli import web_server

    previous_auth_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.auth_required = False
    test_client = TestClient(web_server.app)
    test_client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    try:
        yield test_client
    finally:
        if previous_auth_required is None:
            try:
                delattr(web_server.app.state, "auth_required")
            except AttributeError:
                pass
        else:
            web_server.app.state.auth_required = previous_auth_required


def _routes():
    from hermes_cli.web_server import app

    return {getattr(r, "path", None) for r in app.routes}


def test_pet_dialogue_route_is_registered():
    assert "/api/pet/dialogue" in _routes()


def test_pet_dialogue_disabled_returns_clean_404(client, monkeypatch):
    """auxiliary.pet_dialogue.enabled false -> 404 the desktop treats as 'no LLM line'."""
    import hermes_cli.config as config_mod

    monkeypatch.setattr(config_mod, "load_config_readonly", lambda: {"auxiliary": {"pet_dialogue": {"enabled": False}}})

    resp = client.post("/api/pet/dialogue", json={"beat": "completed", "context": "tests passed", "pet_slug": "miku"})

    assert resp.status_code == 404
    assert "pet_dialogue" in resp.json()["detail"]


def test_pet_dialogue_enabled_returns_line_from_aux_task(client, monkeypatch):
    """Enabled path: the route calls call_llm(task='pet_dialogue') and returns its line."""
    seen: dict = {}

    def fake_call_llm(**kwargs):
        seen.update(kwargs)
        return type(
            "Resp",
            (),
            {"choices": [type("C", (), {"message": type("M", (), {"content": 'Yay!... done!'})()})()]},
        )()

    monkeypatch.setattr("hermes_cli.config.load_config_readonly",
                        lambda: {"auxiliary": {"pet_dialogue": {"enabled": True, "max_context_chars": 400}}})
    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake_call_llm)

    resp = client.post("/api/pet/dialogue", json={"beat": "completed", "context": "tests passed", "pet_slug": "miku"})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "line": "Yay!... done!"}
    assert seen["task"] == "pet_dialogue"
    # Persona + spoken-delivery rules must ride the system prompt (FORK.md:
    # the delivery_rules block was a separate fork fix).
    system_prompt = seen["messages"][0]["content"]
    assert "producer" in system_prompt
    assert "SPOKEN" in system_prompt
    assert seen["max_tokens"] == 24


def test_pet_dialogue_context_is_capped_server_side(client, monkeypatch):
    seen: dict = {}

    def fake_call_llm(**kwargs):
        seen.update(kwargs)
        return type(
            "Resp",
            (),
            {"choices": [type("C", (), {"message": type("M", (), {"content": "ok"})()})()]},
        )()

    monkeypatch.setattr("hermes_cli.config.load_config_readonly",
                        lambda: {"auxiliary": {"pet_dialogue": {"enabled": True, "max_context_chars": 10}}})
    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake_call_llm)

    resp = client.post("/api/pet/dialogue", json={"beat": "completed", "context": "x" * 500, "pet_slug": ""})

    assert resp.status_code == 200, resp.text
    user_prompt = seen["messages"][1]["content"]
    assert "x" * 10 in user_prompt and "x" * 11 not in user_prompt


def test_pet_dialogue_llm_failure_reports_502(client, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr("hermes_cli.config.load_config_readonly",
                        lambda: {"auxiliary": {"pet_dialogue": {"enabled": True}}})
    monkeypatch.setattr("agent.auxiliary_client.call_llm", boom)

    resp = client.post("/api/pet/dialogue", json={"beat": "waiting", "context": "", "pet_slug": ""})

    assert resp.status_code == 502
