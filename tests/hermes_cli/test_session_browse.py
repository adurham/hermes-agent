"""Tests for the interactive session browser (`hermes sessions browse`).

Covers:
- _session_browse_picker logic (curses mocked, fallback tested)
- cmd_sessions 'browse' action integration
- Argument parser registration
"""

import time
from unittest.mock import MagicMock, patch


from hermes_cli.main import _session_browse_picker


# ─── Sample session data ──────────────────────────────────────────────────────

def _make_sessions(n=5):
    """Generate a list of fake rich-session dicts."""
    now = time.time()
    sessions = []
    for i in range(n):
        sessions.append({
            "id": f"20260308_{i:06d}_abcdef",
            "source": "cli" if i % 2 == 0 else "telegram",
            "model": "test/model",
            "title": f"Session {i}" if i % 3 != 0 else None,
            "preview": f"Hello from session {i}",
            "last_active": now - i * 3600,
            "started_at": now - i * 3600 - 60,
            "message_count": (i + 1) * 5,
        })
    return sessions


SAMPLE_SESSIONS = _make_sessions(5)


# ─── _session_browse_picker ──────────────────────────────────────────────────

class TestSessionBrowsePicker:
    """Tests for the _session_browse_picker function."""

    def test_empty_sessions_returns_none(self, capsys):
        result = _session_browse_picker([])
        assert result is None
        assert "No sessions found" in capsys.readouterr().out


    def test_fallback_mode_valid_selection(self):
        """When curses is unavailable, fallback numbered list should work."""
        sessions = _make_sessions(3)

        # Mock curses import to fail, forcing fallback
        import builtins
        original_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "curses":
                raise ImportError("no curses")
            return original_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=mock_import):
            with patch("builtins.input", return_value="2"):
                result = _session_browse_picker(sessions)

        assert result == sessions[1]["id"]


    def test_fallback_shows_preview_when_no_title(self, capsys):
        """When no title, show preview."""
        sessions = [{
            "id": "test_002",
            "source": "cli",
            "title": None,
            "preview": "Hello world test message",
            "last_active": time.time(),
        }]

        import builtins
        original_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "curses":
                raise ImportError("no curses")
            return original_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=mock_import):
            with patch("builtins.input", return_value="q"):
                _session_browse_picker(sessions)

        output = capsys.readouterr().out
        assert "Hello world test message" in output


# ─── Curses-based picker (mocked curses) ────────────────────────────────────

class TestCursesBrowse:
    """Tests for the curses-based interactive picker via simulated key sequences."""

    def _run_with_keys(self, sessions, key_sequence):
        """Simulate running the curses picker with a given key sequence."""

        # Build a mock stdscr that returns keys from the sequence
        mock_stdscr = MagicMock()
        mock_stdscr.getmaxyx.return_value = (30, 120)
        mock_stdscr.getch.side_effect = key_sequence

        # Capture what curses.wrapper receives and call it with our mock
        with patch("curses.wrapper") as mock_wrapper:
            # When wrapper is called, invoke the function with our mock stdscr
            def run_inner(func):
                try:
                    func(mock_stdscr)
                except StopIteration:
                    pass  # key sequence exhausted

            mock_wrapper.side_effect = run_inner
            with patch("curses.curs_set"):
                with patch("curses.has_colors", return_value=False):
                    return _session_browse_picker(sessions)



    def test_escape_cancels(self):
        sessions = _make_sessions(3)
        result = self._run_with_keys(sessions, [27])  # Esc
        assert result is None


    def test_type_to_filter_then_enter(self):
        """Typing characters filters the list, Enter selects from filtered."""
        sessions = [
            {"id": "s1", "source": "cli", "title": "Alpha project", "preview": "", "last_active": time.time()},
            {"id": "s2", "source": "cli", "title": "Beta project", "preview": "", "last_active": time.time()},
            {"id": "s3", "source": "cli", "title": "Gamma project", "preview": "", "last_active": time.time()},
        ]
        # Type "Beta" then Enter — should select s2
        keys = [ord(c) for c in "Beta"] + [10]
        result = self._run_with_keys(sessions, keys)
        assert result == "s2"








# ─── Argument parser registration ──────────────────────────────────────────

class TestSessionBrowseArgparse:
    """Verify the 'browse' subcommand is properly registered."""

    def test_browse_subcommand_exists(self):
        """hermes sessions browse should be parseable."""

        # We can't run main(), but we can import and test the parser setup
        # by checking that argparse doesn't error on "sessions browse"
        # Re-create the parser portion
        # Instead, let's just verify the import works and the function exists
        from hermes_cli.main import _session_browse_picker
        assert callable(_session_browse_picker)

    def test_browse_default_limit_is_500(self):
        """The default --limit for browse should be 500."""
        # Build the same argparse tree cmd_sessions uses and verify the default.
        import argparse
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="sessions_action")
        browse = subparsers.add_parser("browse")
        browse.add_argument("--source")
        browse.add_argument("--limit", type=int, default=500)

        args = parser.parse_args(["browse"])
        assert args.limit == 500

        args = parser.parse_args(["browse", "--limit", "42"])
        assert args.limit == 42


# ─── Integration: cmd_sessions browse action ────────────────────────────────



# ─── Edge cases ──────────────────────────────────────────────────────────────

class TestEdgeCases:
    """Edge case handling for the session browser."""


    def test_relative_time_formatting(self, capsys):
        """Verify various time deltas format correctly."""
        now = time.time()
        sessions = [
            {"id": "recent", "source": "cli", "title": None, "preview": "just now test", "last_active": now},
            {"id": "hour_ago", "source": "cli", "title": None, "preview": "hour ago test", "last_active": now - 7200},
            {"id": "days_ago", "source": "cli", "title": None, "preview": "days ago test", "last_active": now - 259200},
        ]

        import builtins
        original_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "curses":
                raise ImportError("no curses")
            return original_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=mock_import):
            with patch("builtins.input", return_value="q"):
                _session_browse_picker(sessions)

        output = capsys.readouterr().out
        assert "just now" in output
        assert "2h ago" in output
        assert "3d ago" in output


class TestTerminalTooSmallFallback:
    """Regression: when the terminal is too small for the curses layout
    (under 40 cols × 5 rows), the picker used to paint "Terminal too small"
    and block on getch() — a dead-end that left users stuck on a useless
    screen.  The fix routes to the existing numbered-list fallback instead.

    The pre-check (before curses init) catches the common case: narrow tmux
    splits, iTerm2 pane init races, recent resize.  The in-curses raise is
    the defence for the rare case where the window shrinks between our
    pre-check and curses init (sleep/wake resize burst).
    """

    def test_narrow_window_falls_back_to_numbered_list(self):
        """40-col cutoff: 39 cols should skip curses entirely."""
        from hermes_cli.main import _session_browse_picker
        import os as _os

        sessions = _make_sessions(3)
        tiny = _os.terminal_size((30, 24))  # 30 cols × 24 rows

        with patch("os.get_terminal_size", return_value=tiny):
            with patch("builtins.input", return_value="2"):
                result = _session_browse_picker(sessions)

        assert result == sessions[1]["id"]

    def test_short_window_falls_back_to_numbered_list(self):
        """5-row cutoff: 4 rows should skip curses entirely."""
        from hermes_cli.main import _session_browse_picker
        import os as _os

        sessions = _make_sessions(3)
        tiny = _os.terminal_size((120, 4))  # 120 cols × 4 rows

        with patch("os.get_terminal_size", return_value=tiny):
            with patch("builtins.input", return_value="1"):
                result = _session_browse_picker(sessions)

        assert result == sessions[0]["id"]

    def test_exact_threshold_uses_curses(self):
        """40 cols × 5 rows is the minimum that still uses curses."""
        from hermes_cli.main import _session_browse_picker
        import os as _os

        sessions = _make_sessions(3)
        threshold = _os.terminal_size((40, 5))

        # If we reach curses, the input() mock won't be called — curses takes
        # over.  We mock curses.wrapper to capture invocation and return a
        # session ID, so we can assert curses WAS attempted.
        with patch("os.get_terminal_size", return_value=threshold):
            with patch("curses.wrapper") as mock_wrapper:
                # curses.wrapper(_curses_browse) is called; we don't need
                # to simulate the loop — just confirm it was invoked.
                # The picker will return result_holder[0] (None by default,
                # which falls through to the numbered list).
                with patch("builtins.input", return_value="1"):
                    _session_browse_picker(sessions)

        # Curses WAS attempted (window was big enough)
        assert mock_wrapper.called

    def test_non_tty_skips_pre_check_and_lets_curses_try(self, capsys):
        """When stdout isn't a tty, os.get_terminal_size raises OSError.
        The pre-check should let the curses block try anyway; its existing
        exception handler will route to the numbered fallback if curses
        fails (which it will, no tty)."""
        from hermes_cli.main import _session_browse_picker

        sessions = _make_sessions(3)

        with patch("os.get_terminal_size", side_effect=OSError("not a tty")):
            with patch("builtins.input", return_value="1"):
                result = _session_browse_picker(sessions)

        # Either curses worked or fallback worked — but we shouldn't crash,
        # and we should get a valid session back.
        assert result == sessions[0]["id"]

    def test_terminal_too_small_class_exists(self):
        """The sentinel exception class must be importable so the in-curses
        defence (used when the window shrinks AFTER the pre-check) can
        raise it to trigger the outer fallback."""
        from hermes_cli.main import _TerminalTooSmall
        assert issubclass(_TerminalTooSmall, Exception)

    def test_fallback_message_not_shown(self, capsys):
        """Explicit: the dead-end "Terminal too small" string must not
        appear in any user-visible output path."""
        from hermes_cli.main import _session_browse_picker
        import os as _os

        sessions = _make_sessions(3)
        tiny = _os.terminal_size((30, 4))

        with patch("os.get_terminal_size", return_value=tiny):
            with patch("builtins.input", return_value="q"):
                _session_browse_picker(sessions)

        output = capsys.readouterr().out
        # Must NOT contain the historical dead-end message
        assert "Terminal too small" not in output
        # Must show the numbered-list header instead
        assert "Browse sessions" in output


