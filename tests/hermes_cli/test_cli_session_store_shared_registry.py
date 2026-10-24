"""The CLI's session store is the gateway's, never a state.db writer in the CLI process.

Main acquired ``hermes_state_registry`` (a writer handle) at startup. Rewired, the gateway is the
single writer: ``_init_session_store`` builds a ``GatewaySessionStore`` and opens no SessionDB.
"""

from types import SimpleNamespace

from cli import HermesCLI


def test_cli_session_store_is_the_gateway_store_and_opens_no_writer(monkeypatch):
    import hermes_state_registry
    from hermes_cli.gateway_session_store import GatewaySessionStore

    constructed = []
    monkeypatch.setattr(hermes_state_registry, "_open_session_db", lambda path: constructed.append(path))
    cli = SimpleNamespace()
    HermesCLI._init_session_store(cli)
    assert isinstance(cli._session_db, GatewaySessionStore) and not cli._session_db_unavailable
    assert constructed == []


def test_store_failure_warning_carries_the_raw_details(monkeypatch, capsys):
    from hermes_cli import gateway_session_store

    def failing_session_store(*_a, **_kw):
        raise OSError("database disk image is malformed: zz-sentinel")

    monkeypatch.setattr(gateway_session_store, "session_store", failing_session_store)

    cli = SimpleNamespace()
    HermesCLI._init_session_store(cli)

    assert cli._session_db is None and cli._session_db_unavailable
    err = capsys.readouterr().err
    # The rich variant (not the plain fallback) rendered, with the exception text as details.
    assert "zz-sentinel" in err
    assert "WARNING: Session store unavailable" not in err
