"""mcp_serve's catch-all boundaries keep the traceback in the log instead of dropping it."""

import logging
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _isolate_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    return tmp_path


def _records_with_traceback(caplog, exc_type):
    return [r for r in caplog.records if r.exc_info and r.exc_info[0] is exc_type]


def test_gateway_index_close_failure_is_logged_with_traceback(monkeypatch, caplog):
    import mcp_serve

    db = MagicMock()
    db.list_gateway_sessions.return_value = [{"session_key": "k", "id": "s1"}]
    db.close.side_effect = RuntimeError("close failed")
    monkeypatch.setattr(mcp_serve, "_get_session_db", lambda: db)

    with caplog.at_level(logging.DEBUG, logger="hermes.mcp_serve"):
        index = mcp_serve._load_sessions_index_from_db()

    assert list(index) == ["k"]
    assert _records_with_traceback(caplog, RuntimeError)


def test_poll_loop_error_is_logged_with_traceback(monkeypatch, caplog):
    import mcp_serve

    bridge = mcp_serve.EventBridge()
    monkeypatch.setattr(mcp_serve, "_get_session_db", lambda: MagicMock())

    def failing_poll(db):
        bridge._running = False
        raise RuntimeError("poll failed")

    monkeypatch.setattr(bridge, "_poll_once", failing_poll)
    monkeypatch.setattr(mcp_serve, "POLL_INTERVAL", 0)
    bridge._running = True

    with caplog.at_level(logging.DEBUG, logger="hermes.mcp_serve"):
        bridge._poll_loop()

    assert _records_with_traceback(caplog, RuntimeError)
