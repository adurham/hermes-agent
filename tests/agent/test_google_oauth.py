"""Tests for agent/google_oauth.py — Google OAuth PKCE flow for Gemini.

Verifies credential path, PKCE generation, and refresh token parsing.
"""

import json
import os
import tempfile
from pathlib import Path

import pytest


class TestGoogleOAuth:
    """Tests for the Google OAuth credential handling."""

    def test_credentials_path(self, monkeypatch):
        """_credentials_path returns path under HERMES_HOME."""
        from agent.google_oauth import _credentials_path
        with tempfile.TemporaryDirectory() as td:
            monkeypatch.setenv("HERMES_HOME", td)
            path = _credentials_path()
            assert str(td) in str(path)
            assert path.name == "google_oauth.json"

    def test_lock_path(self, monkeypatch):
        """_lock_path returns path under HERMES_HOME."""
        from agent.google_oauth import _lock_path
        with tempfile.TemporaryDirectory() as td:
            monkeypatch.setenv("HERMES_HOME", td)
            path = _lock_path()
            assert str(td) in str(path)
            assert ".lock" in path.name

    def test_generate_pkce_pair_returns_code_verifier_and_challenge(self):
        """_generate_pkce_pair returns (code_verifier, code_challenge)."""
        from agent.google_oauth import _generate_pkce_pair
        verifier, challenge = _generate_pkce_pair()
        assert isinstance(verifier, str)
        assert isinstance(challenge, str)
        assert len(verifier) > 0
        assert len(challenge) > 0

    def test_pkce_verifier_is_base64_url(self):
        """PKCE code verifier is URL-safe base64 (no +/ or = padding)."""
        from agent.google_oauth import _generate_pkce_pair
        verifier, _ = _generate_pkce_pair()
        assert "+" not in verifier
        assert "/" not in verifier
        assert "=" not in verifier

    def test_pkce_challenge_is_base64_url(self):
        """PKCE code challenge is URL-safe base64."""
        from agent.google_oauth import _generate_pkce_pair
        _, challenge = _generate_pkce_pair()
        assert "+" not in challenge
        assert "/" not in challenge
        assert "=" not in challenge

    def test_refresh_parts_parse(self):
        """RefreshParts.parse handles the packed format."""
        from agent.google_oauth import RefreshParts
        packed = "refreshToken|projectId|managedProjectId"
        parts = RefreshParts.parse(packed)
        assert parts.refresh_token == "refreshToken"
        assert parts.project_id == "projectId"
        assert parts.managed_project_id == "managedProjectId"

    def test_refresh_parts_format(self):
        """RefreshParts.format produces the packed format."""
        from agent.google_oauth import RefreshParts
        parts = RefreshParts("tok", "proj", "mproj")
        assert parts.format() == "tok|proj|mproj"

    def test_refresh_parts_roundtrip(self):
        """RefreshParts.parse(parts.format()) is identity."""
        from agent.google_oauth import RefreshParts
        original = RefreshParts("abc123", "my-project", "managed-456")
        packed = original.format()
        parsed = RefreshParts.parse(packed)
        assert parsed.refresh_token == original.refresh_token

    def test_google_oauth_error(self):
        """GoogleOAuthError is a RuntimeError with a code."""
        from agent.google_oauth import GoogleOAuthError
        err = GoogleOAuthError("test error", code="test_code")
        assert isinstance(err, RuntimeError)
        assert str(err) == "test error"
        assert err.code == "test_code"

    def test_google_oauth_error_default_code(self):
        """GoogleOAuthError has a default code."""
        from agent.google_oauth import GoogleOAuthError
        err = GoogleOAuthError("test")
        assert err.code == "google_oauth_error"

    def test_require_client_id_raises_when_missing(self, monkeypatch):
        """_require_client_id raises when no client ID is available."""
        from agent.google_oauth import _require_client_id, GoogleOAuthError
        monkeypatch.setattr("agent.google_oauth._get_client_id", lambda: "")
        with pytest.raises(GoogleOAuthError):
            _require_client_id()

    def test_credentials_lock_context_manager(self, monkeypatch):
        """_credentials_lock returns a context manager."""
        from agent.google_oauth import _credentials_lock
        with tempfile.TemporaryDirectory() as td:
            monkeypatch.setenv("HERMES_HOME", td)
            with _credentials_lock(timeout_seconds=1):
                pass  # Should not raise

    def test_credentials_lock_acquires_exclusive(self, monkeypatch):
        """_credentials_lock acquires an exclusive file lock."""
        from agent.google_oauth import _credentials_lock
        import threading
        with tempfile.TemporaryDirectory() as td:
            monkeypatch.setenv("HERMES_HOME", td)
            acquired = []
            def try_lock():
                try:
                    with _credentials_lock(timeout_seconds=0.1):
                        acquired.append(True)
                except Exception:
                    acquired.append(False)
            t = threading.Thread(target=try_lock)
            t.start()
            t.join(timeout=2)
            assert len(acquired) == 1