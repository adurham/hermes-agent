"""The dashboard session token resolves from the environment or a secret file."""

import pytest


def test_env_token_wins_over_file(monkeypatch, tmp_path):
    from hermes_cli import web_server

    token_file = tmp_path / "token"
    token_file.write_text("file-token\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", "env-token")
    monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN_FILE", str(token_file))

    assert web_server._resolve_session_token() == "env-token"


def test_file_token_is_read_and_stripped(monkeypatch, tmp_path):
    from hermes_cli import web_server

    token_file = tmp_path / "token"
    token_file.write_text("  file-token  \r\n", encoding="utf-8")
    monkeypatch.delenv("HERMES_DASHBOARD_SESSION_TOKEN", raising=False)
    monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN_FILE", str(token_file))

    assert web_server._resolve_session_token() == "file-token"


def test_missing_token_file_fails_loudly(monkeypatch, tmp_path):
    from hermes_cli import web_server

    monkeypatch.delenv("HERMES_DASHBOARD_SESSION_TOKEN", raising=False)
    monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN_FILE", str(tmp_path / "absent"))

    with pytest.raises(SystemExit, match="unreadable"):
        web_server._resolve_session_token()


def test_empty_token_file_fails_loudly(monkeypatch, tmp_path):
    from hermes_cli import web_server

    token_file = tmp_path / "token"
    token_file.write_text("\n", encoding="utf-8")
    monkeypatch.delenv("HERMES_DASHBOARD_SESSION_TOKEN", raising=False)
    monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN_FILE", str(token_file))

    with pytest.raises(SystemExit, match="empty"):
        web_server._resolve_session_token()


def test_no_source_mints_a_token(monkeypatch):
    from hermes_cli import web_server

    monkeypatch.delenv("HERMES_DASHBOARD_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("HERMES_DASHBOARD_SESSION_TOKEN_FILE", raising=False)

    assert len(web_server._resolve_session_token()) > 0
