"""Tests for gateway /yolo session scoping."""

import os

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from tools.approval import disable_session_yolo, is_session_yolo_enabled


@pytest.fixture(autouse=True)
def _clean_yolo_state(monkeypatch):
    monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)
    disable_session_yolo("agent:main:telegram:dm:chat-a")
    disable_session_yolo("agent:main:telegram:dm:chat-b")
    yield
    monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)
    disable_session_yolo("agent:main:telegram:dm:chat-a")
    disable_session_yolo("agent:main:telegram:dm:chat-b")


def _make_runner():
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.session_store = None
    runner.config = None
    return runner


def _make_event(chat_id: str) -> MessageEvent:
    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id=f"user-{chat_id}",
        chat_id=chat_id,
        user_name="tester",
        chat_type="dm",
    )
    return MessageEvent(text="/yolo", source=source)


@pytest.mark.asyncio
async def test_yolo_command_toggles_only_current_session(monkeypatch):
    runner = _make_runner()

    event_a = _make_event("chat-a")
    session_a = runner._session_key_for_source(event_a.source)
    session_b = runner._session_key_for_source(_make_event("chat-b").source)

    await runner._handle_yolo_command(event_a)

    assert is_session_yolo_enabled(session_a) is True
    assert is_session_yolo_enabled(session_b) is False
    assert os.environ.get("HERMES_YOLO_MODE") is None

    await runner._handle_yolo_command(event_a)

    assert is_session_yolo_enabled(session_a) is False
    assert os.environ.get("HERMES_YOLO_MODE") is None


def test_launch_yolo_revocation_survives_the_next_turn(monkeypatch):
    """`hermes chat --yolo` seeds the bypass once; a later `/yolo` off is not re-enabled per turn."""
    from types import SimpleNamespace
    import gateway.session_policy as session_policy
    from gateway.run_turn_runner import TurnRunner
    from gateway.turn_context import TurnContext

    runner = _make_runner()
    source = SessionSource(platform=Platform.LOCAL, chat_id="launch-yolo", user_id="u", chat_type="dm")
    key = "agent:main:local:dm:launch-yolo"
    monkeypatch.setattr(session_policy, "policy_for_source",
                        lambda _runner, _source: SimpleNamespace(yolo=True, platform="cli", max_turns=5))

    def _stop(**_kw):
        raise RuntimeError("stop after the yolo seam")
    runner._resolve_session_agent_runtime = _stop
    runner._pre_agent_fallback_notice = None
    runner._get_system_prompt_for_channel = lambda *a, **k: ""

    def turn():
        TurnRunner(runner, TurnContext(source=source, session_key=key))._run_sync_scoped()

    try:
        turn()
        assert is_session_yolo_enabled(key) is True
        disable_session_yolo(key)
        turn()
        assert is_session_yolo_enabled(key) is False
    finally:
        from tools.approval import clear_session
        clear_session(key)
