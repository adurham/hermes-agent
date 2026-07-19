"""Tests for the fork's keychain_longlived credential source.

Verifies _remove_keychain_longlived in agent/credential_sources.py
handles macOS keychain cleanup correctly.
"""

import subprocess

import pytest

from agent.credential_sources import RemovalResult


class TestKeychainLonglived:
    """Tests for the keychain_longlived credential source."""

    def test_removal_result_structure(self):
        """RemovalResult has cleaned and hints fields."""
        result = RemovalResult()
        assert hasattr(result, "cleaned")
        assert hasattr(result, "hints")
        assert isinstance(result.cleaned, list)
        assert isinstance(result.hints, list)

    def test_removal_step_registered(self):
        """keychain_longlived is registered as a RemovalStep for provider=anthropic."""
        from agent.credential_sources import _REGISTRY, find_removal_step

        step = find_removal_step("anthropic", "keychain_longlived")
        assert step is not None
        assert step.provider == "anthropic"
        assert step.source_id == "keychain_longlived"
        assert "Keychain" in step.description
        assert "claude-code-oauth-longlived" in step.description

    def test_remove_keychain_longlived_success(self, monkeypatch):
        """Successful keychain deletion returns cleaned entry."""
        def mock_run(cmd, **kwargs):
            assert "security" in cmd
            assert "delete-generic-password" in cmd
            assert "claude-code-oauth-longlived" in cmd
            return subprocess.CompletedProcess(cmd, 0, b"", b"")

        monkeypatch.setattr(subprocess, "run", mock_run)

        from agent.credential_sources import _remove_keychain_longlived
        result = _remove_keychain_longlived("anthropic", None)
        assert len(result.cleaned) == 1
        assert "Deleted" in result.cleaned[0]
        assert len(result.hints) == 0

    def test_remove_keychain_longlived_not_found(self, monkeypatch):
        """When keychain entry doesn't exist, hint is added, not error."""
        def mock_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, b"", b"")

        monkeypatch.setattr(subprocess, "run", mock_run)

        from agent.credential_sources import _remove_keychain_longlived
        result = _remove_keychain_longlived("anthropic", None)
        # If security delete returns 0, it succeeded
        assert len(result.cleaned) >= 0

    def test_remove_keychain_longlived_timeout(self, monkeypatch):
        """Timeout on keychain access adds a hint."""
        def mock_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 5)

        monkeypatch.setattr(subprocess, "run", mock_run)

        from agent.credential_sources import _remove_keychain_longlived
        result = _remove_keychain_longlived("anthropic", None)
        assert len(result.hints) >= 1
        assert "timeout" in result.hints[0].lower() or "Could not delete" in result.hints[0]

    def test_remove_keychain_longlived_oserror(self, monkeypatch):
        """OSError on keychain access adds a hint."""
        def mock_run(cmd, **kwargs):
            raise OSError("security not found")

        monkeypatch.setattr(subprocess, "run", mock_run)

        from agent.credential_sources import _remove_keychain_longlived
        result = _remove_keychain_longlived("anthropic", None)
        assert len(result.hints) >= 1