"""Restored delegations require a real notification consumer (#138348)."""

import asyncio
import io
import queue
import threading
import time
from unittest.mock import Mock

import pytest

from tools import async_delegation as ad
from tools.process_registry import process_registry
from tools.process_registry_notifications import format_process_notification
from tui_gateway import server
from tui_gateway.transport import StdioTransport, TeeTransport
from tui_gateway.ws import WSTransport


@pytest.fixture
def notification_env(monkeypatch, tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    ad._reset_for_tests()
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(process_registry, "completion_queue", queue.Queue())
    monkeypatch.setattr(server, "_emit", Mock())
    # The execution boundary reports admission; no model or worker is started.
    submit = Mock(return_value=True)
    monkeypatch.setattr(server, "_run_prompt_submit", submit)
    loop = asyncio.new_event_loop()
    try:
        yield home, loop, submit
    finally:
        loop.close()
        ad._reset_for_tests()


def _session(notification_env, monkeypatch, kind):
    home, loop, _submit = notification_env
    stdio = StdioTransport(io.StringIO, threading.Lock())
    if "sidecar" in kind:
        stdio = TeeTransport(stdio, StdioTransport(io.StringIO, threading.Lock()))
    monkeypatch.setattr(server, "_stdio_transport", stdio)
    monkeypatch.setattr(server, "_stdio_is_rpc_channel", kind in {"stdio", "sidecar"})
    ws = WSTransport(None, loop)
    if kind == "closed":
        ws._closed = True
    transport = {
        "stdio": stdio, "sidecar": stdio, "websocket": ws,
        "headless": stdio, "headless_sidecar": stdio,
        "detached": server._detached_ws_transport, "closed": ws, "absent": None,
        "foreign_stdio": StdioTransport(io.StringIO, threading.Lock()),
        "finalized": stdio,
    }[kind]
    if kind in {"foreign_stdio", "finalized"}:
        monkeypatch.setattr(server, "_stdio_is_rpc_channel", True)
    session = {
        "session_key": "chat", "transport": transport, "profile_home": str(home),
        "history_lock": threading.RLock(), "running": False,
        "_finalized": kind == "finalized",
    }
    server._sessions["ui1"] = session
    return session


def _completion(*, restored=True):
    event = {
        "type": "async_delegation", "delegation_id": "completed-child",
        "session_key": "chat", "origin_ui_session_id": "ui1",
        "dispatched_at": time.time(), "completed_at": time.time(),
        "status": "completed", "summary": "child result",
        "results": [{"status": "completed", "summary": "child result"}],
    }
    ad._persist_dispatch(event)
    ad._persist_completion(event, {"summary": "child result"})
    if restored:
        offered = queue.Queue()
        assert ad.restore_undelivered_completions(offered) == 1
        return offered.get_nowait()
    return event


@pytest.mark.parametrize("kind,owns", [
    ("stdio", True), ("sidecar", True), ("websocket", True),
    ("headless", False), ("headless_sidecar", False), ("detached", False),
    ("closed", False), ("absent", False), ("foreign_stdio", False), ("finalized", False),
])
@pytest.mark.parametrize("origin", ["ui1", "retired-tab"])
def test_restored_notification_transport(notification_env, monkeypatch, kind, owns, origin):
    session = _session(notification_env, monkeypatch, kind)
    event = _completion()
    event["origin_ui_session_id"] = origin
    assert event["restored"] is True
    assert server._session_owns_notification_event("ui1", session, event) is owns
    foreign = dict(event, origin_ui_session_id="other", session_key="other")
    assert server._session_owns_notification_event("ui1", session, foreign) is False
    # Stdio's notification exception must not change shared peer membership.
    assert server._session_has_live_transport(session) is (kind in {"websocket"})

    server._notif_handle_ready(
        "ui1", session, [event], set(), process_registry, format_process_notification, None)

    submit = notification_env[2]
    assert submit.call_count == int(owns)
    assert server._emit.call_count == int(owns)
    row = ad.get_durable_delegation(event["delegation_id"])
    assert row["delivery_state"] == ("delivered" if owns else "pending")
    assert row["delivery_attempts"] == int(owns)
    if owns:
        assert submit.call_args.args[1:3] == ("ui1", session)
        assert "child result" in submit.call_args.args[3]
        assert submit.call_args.kwargs["display_kind"] == "async_delegation_complete"
    else:
        assert session["running"] is False
        # Rejection returns the durable offer for a future attached owner.
        offered = queue.Queue()
        assert ad.restore_undelivered_completions(offered) == 1
        assert offered.get_nowait()["delegation_id"] == event["delegation_id"]


@pytest.mark.parametrize("kind", ["stdio", "sidecar", "websocket", "headless", "detached", "closed", "absent"])
def test_non_restored_routing_is_preserved(notification_env, monkeypatch, kind):
    session = _session(notification_env, monkeypatch, kind)
    event = _completion(restored=False)
    assert server._session_owns_notification_event("ui1", session, event) is True
    foreign = dict(event, origin_ui_session_id="other", session_key="other")
    assert server._session_owns_notification_event("ui1", session, foreign) is False
    server._notif_handle_ready(
        "ui1", session, [event], set(), process_registry, format_process_notification, None)
    notification_env[2].assert_called_once()
    assert ad.get_durable_delegation(event["delegation_id"])["delivery_state"] == "delivered"


@pytest.mark.parametrize("blocked", ["execution", "cancel", "busy"])
def test_live_stdio_waits_for_admission(notification_env, monkeypatch, blocked):
    session = _session(notification_env, monkeypatch, "stdio")
    event = _completion()
    submit = notification_env[2]
    if blocked == "execution":
        submit.return_value = False
    elif blocked == "cancel":
        session["_turn_cancel_requested"] = True
    else:
        session["running"] = True
    deferred = []
    server._notif_handle_ready(
        "ui1", session, [event], set(), process_registry, format_process_notification, deferred)
    row = ad.get_durable_delegation(event["delegation_id"])
    assert row["delivery_state"] == "pending"
    assert row["delivery_attempts"] == 0
    assert submit.call_count == int(blocked == "execution")
    assert session["running"] is (blocked == "busy")
    assert process_registry.completion_queue.get_nowait() == event
