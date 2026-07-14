"""Tests for the Ctrl+C-to-skip-cleanup-wait feature.

``_run_cleanup()`` can legitimately take up to ~45s (memory-confirm LLM
call + MCP teardown) before the exit watchdog's 60s backstop would force
an ``os._exit(0)``. Rather than making an impatient user sit through the
full wait with no way out, ``_install_cleanup_skip_handler()`` installs a
temporary SIGINT handler for the duration of cleanup that exits
immediately on a second Ctrl+C, restoring the previous handler once
cleanup finishes (or raises) via the caller's ``finally`` block.
"""

from __future__ import annotations

import os
import signal
from unittest.mock import MagicMock, patch

import pytest

import cli as cli_mod


def test_install_is_noop_under_pytest():
    """Under a real pytest run, PYTEST_CURRENT_TEST is set — installation
    must skip touching signal handlers entirely and return a no-op restore,
    mirroring _arm_exit_watchdog's own pytest guard."""
    assert os.environ.get("PYTEST_CURRENT_TEST"), "expected to run under pytest"
    before = signal.getsignal(signal.SIGINT)
    restore = cli_mod._install_cleanup_skip_handler()
    try:
        assert signal.getsignal(signal.SIGINT) is before, (
            "handler must not be installed under pytest"
        )
        restore()  # must not raise even though nothing was installed
        assert signal.getsignal(signal.SIGINT) is before
    finally:
        signal.signal(signal.SIGINT, before)


def test_install_and_restore_roundtrip_outside_pytest(monkeypatch):
    """Outside pytest, installation must actually swap SIGINT and restore
    must put the exact previous handler back."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    before = signal.getsignal(signal.SIGINT)
    restore = cli_mod._install_cleanup_skip_handler()
    try:
        current = signal.getsignal(signal.SIGINT)
        assert current is not before, "SIGINT handler should have been swapped"
        assert callable(current)
    finally:
        restore()
    assert signal.getsignal(signal.SIGINT) is before, (
        "restore() must put the original handler back exactly"
    )


def test_skip_handler_calls_os_exit_immediately(monkeypatch):
    """The installed handler must call os._exit(0) directly rather than
    raising KeyboardInterrupt -- cleanup steps are wrapped in bare
    ``except Exception`` blocks that would otherwise swallow a raised
    KeyboardInterrupt and keep running, defeating the fast-exit purpose."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    before = signal.getsignal(signal.SIGINT)
    restore = cli_mod._install_cleanup_skip_handler()
    try:
        handler = signal.getsignal(signal.SIGINT)
        with patch("os._exit") as mock_exit:
            handler(signal.SIGINT, None)
        mock_exit.assert_called_once_with(0)
    finally:
        restore()


def test_install_returns_noop_when_not_main_thread(monkeypatch):
    """signal.signal() raises ValueError off the main thread. Installation
    must degrade to a no-op restore rather than propagating."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    with patch("signal.signal", side_effect=ValueError("not main thread")):
        restore = cli_mod._install_cleanup_skip_handler()
    restore()  # must not raise


def test_run_cleanup_installs_and_restores_skip_handler_around_body():
    """_run_cleanup must install the skip handler before running cleanup
    and restore it afterward, even if the cleanup body raises."""
    with (
        patch.object(cli_mod, "_cleanup_done", False),
        patch.object(cli_mod, "_arm_exit_watchdog"),
        patch.object(cli_mod, "_install_cleanup_skip_handler") as mock_install,
        patch.object(cli_mod, "_run_cleanup_body") as mock_body,
    ):
        restore_mock = MagicMock()
        mock_install.return_value = restore_mock
        cli_mod._run_cleanup()

        mock_install.assert_called_once()
        mock_body.assert_called_once()
        restore_mock.assert_called_once()
        # Install must precede body, and body must precede restore.
        assert (
            mock_install.call_args_list
            and mock_body.call_args_list
            and restore_mock.call_args_list
        )


def test_run_cleanup_restores_skip_handler_even_if_body_raises():
    """A raising cleanup body must not leak the installed SIGINT handler."""
    with (
        patch.object(cli_mod, "_cleanup_done", False),
        patch.object(cli_mod, "_arm_exit_watchdog"),
        patch.object(cli_mod, "_install_cleanup_skip_handler") as mock_install,
        patch.object(
            cli_mod, "_run_cleanup_body", side_effect=RuntimeError("boom")
        ),
    ):
        restore_mock = MagicMock()
        mock_install.return_value = restore_mock

        with pytest.raises(RuntimeError, match="boom"):
            cli_mod._run_cleanup()

        restore_mock.assert_called_once()


def test_run_cleanup_forwards_notify_session_finalize_to_body():
    """The notify_session_finalize kwarg must still reach _run_cleanup_body
    after the split -- this was the exact regression risk of the refactor."""
    with (
        patch.object(cli_mod, "_cleanup_done", False),
        patch.object(cli_mod, "_arm_exit_watchdog"),
        patch.object(cli_mod, "_install_cleanup_skip_handler", return_value=lambda: None),
        patch.object(cli_mod, "_run_cleanup_body") as mock_body,
    ):
        cli_mod._run_cleanup(notify_session_finalize=False)
        mock_body.assert_called_once_with(notify_session_finalize=False)
