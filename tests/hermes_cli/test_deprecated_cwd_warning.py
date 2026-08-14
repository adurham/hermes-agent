"""Tests for warn_deprecated_cwd_env_vars() migration warning.

Regression coverage: the warning must be driven by what is actually written
in the .env FILE, not by os.environ. TERMINAL_CWD legitimately lands in the
process environment on every local-backend run (cli.py force-exports
config.yaml's terminal.cwd every startup) and survives in inherited shell/
launchd/gateway environments — none of that means the user's .env mentions
it. Checking os.environ alone false-positived on every such run.
"""

import os

import pytest


class TestDeprecatedCwdWarning:
    """Warn only when MESSAGING_CWD or TERMINAL_CWD is set in the .env file."""

    def test_messaging_cwd_in_env_file_triggers_warning(
        self, monkeypatch, capsys, tmp_path
    ):
        env_file = tmp_path / ".env"
        env_file.write_text("MESSAGING_CWD=/some/path\n")
        monkeypatch.setattr(
            "hermes_constants.get_hermes_home", lambda: tmp_path
        )
        monkeypatch.delenv("TERMINAL_CWD", raising=False)

        from hermes_cli.config import warn_deprecated_cwd_env_vars

        warn_deprecated_cwd_env_vars(config={})

        captured = capsys.readouterr()
        assert "MESSAGING_CWD" in captured.err
        assert "deprecated" in captured.err.lower()
        assert "config.yaml" in captured.err

    def test_both_deprecated_vars_in_env_file_warn(self, monkeypatch, capsys, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("MESSAGING_CWD=/msg/path\nTERMINAL_CWD=/term/path\n")
        monkeypatch.setattr(
            "hermes_constants.get_hermes_home", lambda: tmp_path
        )

        from hermes_cli.config import warn_deprecated_cwd_env_vars

        warn_deprecated_cwd_env_vars(config={})

        captured = capsys.readouterr()
        assert "MESSAGING_CWD" in captured.err
        assert "TERMINAL_CWD" in captured.err

    def test_terminal_cwd_only_in_process_env_does_not_warn(
        self, monkeypatch, capsys, tmp_path
    ):
        """Regression: TERMINAL_CWD set by cli.py's own config bridge (or
        inherited from a parent shell/launchd/gateway process) must NOT
        trigger the warning when the .env file itself is silent on it.
        """
        env_file = tmp_path / ".env"
        env_file.write_text("SOME_OTHER_KEY=value\n")
        monkeypatch.setattr(
            "hermes_constants.get_hermes_home", lambda: tmp_path
        )
        # Simulate cli.py's force-export / an inherited ancestor env var.
        monkeypatch.setenv("TERMINAL_CWD", "/Users/someone")

        from hermes_cli.config import warn_deprecated_cwd_env_vars

        warn_deprecated_cwd_env_vars(config={})

        captured = capsys.readouterr()
        assert captured.err == ""

    def test_no_env_file_does_not_warn(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setattr(
            "hermes_constants.get_hermes_home", lambda: tmp_path
        )
        monkeypatch.setenv("TERMINAL_CWD", "/Users/someone")

        from hermes_cli.config import warn_deprecated_cwd_env_vars

        warn_deprecated_cwd_env_vars(config={})

        captured = capsys.readouterr()
        assert captured.err == ""
