"""The dashboard session token resolves from the environment, or a file it names."""

import pytest


def test_plain_env_token_is_used_verbatim(monkeypatch):
    from hermes_cli import web_server

    monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", "raw-token")

    assert web_server._resolve_session_token() == "raw-token"


def test_file_prefix_reads_and_strips(monkeypatch, tmp_path):
    from hermes_cli import web_server

    token_file = tmp_path / "token"
    token_file.write_text("  file-token  " + chr(13) + chr(10), encoding="utf-8")
    monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", f"file:{token_file}")

    assert web_server._resolve_session_token() == "file-token"


def test_unreadable_token_file_fails_loudly(monkeypatch, tmp_path):
    from hermes_cli import web_server

    monkeypatch.setenv(
        "HERMES_DASHBOARD_SESSION_TOKEN", f"file:{tmp_path / 'absent'}"
    )

    with pytest.raises(SystemExit, match="unreadable"):
        web_server._resolve_session_token()


def test_empty_token_file_fails_loudly(monkeypatch, tmp_path):
    from hermes_cli import web_server

    token_file = tmp_path / "token"
    token_file.write_text(chr(10), encoding="utf-8")
    monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", f"file:{token_file}")

    with pytest.raises(SystemExit, match="empty"):
        web_server._resolve_session_token()


def test_malformed_token_file_fails_loudly(monkeypatch, tmp_path):
    from hermes_cli import web_server

    token_file = tmp_path / "token"
    token_file.write_bytes(b"\xff\xfe\x00not-utf8")
    monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", f"file:{token_file}")

    with pytest.raises(SystemExit, match="unreadable"):
        web_server._resolve_session_token()


def test_no_source_mints_a_token(monkeypatch):
    from hermes_cli import web_server

    monkeypatch.delenv("HERMES_DASHBOARD_SESSION_TOKEN", raising=False)

    assert len(web_server._resolve_session_token()) > 0
