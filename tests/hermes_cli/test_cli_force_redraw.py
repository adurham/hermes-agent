"""Tests for CLI redraw helpers used to recover from terminal buffer drift.

Covers:
  - _force_full_redraw (#8688 cmux tab switch, /redraw, Ctrl+L)
  - the resize handler we install over prompt_toolkit's _on_resize (#5474)

Both behaviors are exercised against fake prompt_toolkit renderer/output
objects — we're asserting the escape sequences the CLI sends, not that
the terminal physically repainted.
"""

from unittest.mock import MagicMock

import pytest

import cli as cli_mod
from cli import HermesCLI


@pytest.fixture
def bare_cli():
    """A HermesCLI with no __init__ — we only exercise the redraw helper."""
    cli = object.__new__(HermesCLI)
    return cli


def _fake_app(*, rows, columns, chrome, painted=None, cursor_y=0):
    """MagicMock app whose output reports a real size and whose layout is ``chrome`` rows tall;
    ``painted`` lists the row widths of the renderer's last paint (``None``: nothing yet), its
    cursor on row ``cursor_y``."""
    from collections import defaultdict

    from prompt_toolkit.data_structures import Point, Size
    from prompt_toolkit.layout.screen import Char, Screen

    app = MagicMock()
    app.renderer.output.get_size.return_value = Size(rows=rows, columns=columns)
    screen = None
    if painted is not None:
        screen = Screen()
        for y, width in enumerate(painted):
            for x in range(width):
                screen.data_buffer[y][x] = Char("─")
        screen.height = len(painted)
    app.renderer._last_screen = screen
    app.renderer._cursor_pos = Point(x=0, y=cursor_y)
    app.renderer._style_string_has_style = defaultdict(bool)
    app.renderer._min_available_height = 0
    app.layout.container.preferred_height.return_value.preferred = chrome
    return app


class TestForceFullRedraw:
    def test_no_app_is_safe(self, bare_cli):
        # _force_full_redraw must be a no-op when the TUI isn't running.
        bare_cli._app = None
        bare_cli._force_full_redraw()  # must not raise




    @pytest.mark.parametrize("reflows,new_width,cursor_y,replayed", [
        (True, 90, 1 + 3 + 1 + 3, None),  # re-wrapped rows: erase from their new top, replay nothing
        (None, 90, 8, (8 - 4, 90, True)),   # unknown: same aim, and replay what it takes if rows stayed
        (False, 90, 4, None),               # rows kept in place: prompt_toolkit's own erase is right
        (True, 250, 4, None),               # a widen re-wraps nothing into more rows
    ])
    def test_resize_erases_the_chrome_where_the_terminal_left_it(
            self, bare_cli, monkeypatch, reflows, new_width, cursor_y, replayed):
        """#95375: the transcript above the chrome is never wiped and replayed — a replay cannot
        know which rows a reflowing terminal kept on screen and which it pushed into scrollback.
        On a shrink prompt_toolkit's erase is aimed at the chrome's top as re-wrapped; when
        nothing says the terminal reflows, the rows that aim takes on one that does not are
        replayed, so the erase never takes more than the replay puts back."""
        from prompt_toolkit.layout.screen import Char
        app = _fake_app(rows=30, columns=new_width, chrome=5, painted=[0, 200, 50, 200, 30], cursor_y=4)
        for x in range(200):  # blanks without a colour are never written: one row, not three
            app.renderer._last_screen.data_buffer[0][x] = Char(" ")
        events, aimed = [], []
        app.renderer.output.erase_end_of_line.side_effect = lambda: events.append("erase")
        app.renderer.output.erase_screen.side_effect = lambda: events.append("erase")
        app.renderer.output.write_raw.side_effect = lambda *_: events.append("scrollback_wipe")

        bare_cli._status_bar_suppressed_after_resize = False
        bare_cli._last_resize_width = 200
        monkeypatch.setattr(bare_cli, "_get_tui_terminal_width", lambda: new_width)
        monkeypatch.setattr(bare_cli, "_schedule_status_bar_unsuppress", lambda *_: None)
        monkeypatch.setattr(cli_mod, "_replay_output_history", lambda fit=None: events.append(("replay", fit)))
        monkeypatch.setattr(cli_mod, "_terminal_reflows", lambda: reflows)
        monkeypatch.setattr(cli_mod, "CLI_CONFIG", {"display": {"cli_rebuild_scrollback_on_redraw": False}})

        bare_cli._recover_after_resize(
            app, lambda: (events.append("original_resize"), aimed.append(app.renderer._cursor_pos.y)))

        assert events == ["original_resize"] + ([("replay", replayed)] if replayed else [])
        assert aimed == [cursor_y]
        assert bare_cli._last_resize_width == new_width
        assert bare_cli._status_bar_suppressed_after_resize is True

    @pytest.mark.parametrize("env,reflows", [
        ({"TMUX": "/tmp/tmux-1/default,1,0", "TERM": "screen-256color"}, True),
        ({"XTERM_VERSION": "XTerm(390)", "VTE_VERSION": "7600"}, False),  # xterm started from a VTE shell
        ({"LC_TERMINAL": "iTerm2", "TERM": "xterm-256color"}, True),     # iTerm2 over ssh
        ({"TERM": "xterm-256color"}, None),                              # xterm or iTerm2 over ssh
        ({"TERM": "st-256color"}, False),
        ({"TERM": "screen.xterm-256color"}, False),                      # GNU screen over ssh
        ({"TERM": "linux"}, False),
    ])
    def test_terminal_reflows_from_what_the_environment_says(self, monkeypatch, env, reflows):
        from hermes_cli import cli_render
        for name in (*cli_render._NO_REFLOW_ENV, *cli_render._REFLOW_ENV, "TERM_PROGRAM", "LC_TERMINAL", "TERM"):
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        assert cli_render._terminal_reflows() is reflows

    def test_force_redraw_refills_only_the_viewport_without_a_screen_clear(self, bare_cli, monkeypatch):
        """#95375: scrollback already holds the older transcript, so a redraw must
        neither emit CSI 2J (scroll-on-clear terminals — tmux, VTE — copy the whole
        screen into scrollback first) nor replay more than fits above the chrome."""
        app = _fake_app(rows=30, columns=100, chrome=6)
        bare_cli._app = app
        fits = []
        monkeypatch.setattr(cli_mod, "_replay_output_history", lambda fit=None, output=None: fits.append((fit, output)))
        monkeypatch.setattr(
            cli_mod,
            "CLI_CONFIG",
            {"display": {"cli_rebuild_scrollback_on_redraw": False}},
        )

        monkeypatch.setattr(cli_mod, "_terminal_reflows", lambda: False)

        bare_cli._force_full_redraw()

        out = app.renderer.output
        out.erase_screen.assert_not_called()
        out.write_raw.assert_not_called()
        assert [c.args for c in out.cursor_goto.call_args_list][:30] == [(row, 0) for row in range(1, 31)]
        assert out.erase_end_of_line.call_count == 30
        # Rows kept where they were painted count at the width they were painted at, and the
        # refill is painted straight away: prompt_toolkit's own erase would start at the top
        # row, which tmux takes as a clear and copies the blank screen into scrollback.
        assert fits == [((24, 100, True), out)]

    def test_force_redraw_can_clear_scrollback_when_configured(self, bare_cli, monkeypatch):
        app = MagicMock()
        bare_cli._app = app
        fits = []
        monkeypatch.setattr(cli_mod, "_replay_output_history", lambda fit=None, output=None: fits.append((fit, output)))
        monkeypatch.setattr(
            cli_mod,
            "CLI_CONFIG",
            {"display": {"cli_rebuild_scrollback_on_redraw": True}},
        )

        bare_cli._force_full_redraw()

        app.renderer.output.erase_screen.assert_called_once()
        app.renderer.output.write_raw.assert_called_once_with("\x1b[3J")
        # Scrollback was wiped, so the whole history is replayed to rebuild it.
        assert fits == [(None, None)]

    def test_resize_recovery_can_clear_scrollback_when_configured(self, bare_cli, monkeypatch):
        app = MagicMock()
        events = []
        app.renderer.output.erase_screen.side_effect = lambda: events.append("erase")
        app.renderer.output.write_raw.side_effect = lambda *_: events.append("scrollback_wipe")
        original_on_resize = lambda: events.append("original_resize")

        bare_cli._status_bar_suppressed_after_resize = False
        bare_cli._last_resize_width = 200
        monkeypatch.setattr(bare_cli, "_get_tui_terminal_width", lambda: 90)
        monkeypatch.setattr(bare_cli, "_schedule_status_bar_unsuppress", lambda *_: None)
        monkeypatch.setattr(cli_mod, "_replay_output_history", lambda *_: events.append("replay"))
        monkeypatch.setattr(
            cli_mod,
            "CLI_CONFIG",
            {"display": {"cli_rebuild_scrollback_on_redraw": "true"}},
        )

        bare_cli._recover_after_resize(app, original_on_resize)

        assert events[:3] == ["erase", "scrollback_wipe", "replay"]
        assert events.index("scrollback_wipe") < events.index("original_resize")

    def test_same_width_sigwinch_is_left_untouched(self, bare_cli, monkeypatch):
        """Same-width SIGWINCH (tmux attach, benign focus/tab signals) must not
        clear the viewport or replay: a 2J without replay erases the visible
        transcript, and a replay duplicates it (#65293). The tmux-attach
        stale-paint crash is handled by _hermes_call_output_screen_diff's
        retry instead (#83874)."""
        app = MagicMock()
        events = []
        app.renderer.output.erase_screen.side_effect = lambda: events.append("erase")
        app.renderer.output.write_raw.side_effect = lambda *_: events.append("scrollback_wipe")
        original_on_resize = lambda: events.append("original_resize")

        bare_cli._status_bar_suppressed_after_resize = False
        bare_cli._last_resize_width = 120
        monkeypatch.setattr(bare_cli, "_get_tui_terminal_width", lambda: 120)
        monkeypatch.setattr(bare_cli, "_schedule_status_bar_unsuppress", lambda *_: None)
        monkeypatch.setattr(cli_mod, "_replay_output_history", lambda *_: events.append("replay"))

        bare_cli._recover_after_resize(app, original_on_resize)

        assert "erase" not in events
        assert "replay" not in events
        assert "scrollback_wipe" not in events
        assert events == ["original_resize"]
        assert bare_cli._last_resize_width == 120
        assert bare_cli._status_bar_suppressed_after_resize is True

    def test_output_screen_diff_retries_on_corrupt_previous_screen(self, bare_cli):
        """Corrupt previous_screen must not wedge the paint loop.

        After tmux attach, _output_screen_diff can raise AttributeError
        ('cell' object has no attribute 'char'). Retry with previous_screen=None.
        """
        calls = []

        def fake_osd(
            app, output, screen, current_pos, color_depth,
            previous_screen, last_style, is_done, full_screen,
            attrs_for_style_string, style_string_has_style,
            size, previous_width,
        ):
            calls.append((previous_screen, previous_width, last_style))
            if previous_screen is not None:
                # Exact failure mode from the classic CLI event loop.
                raise AttributeError("'cell' object has no attribute 'char'")
            return ("ok", current_pos, last_style)

        screen = MagicMock()
        screen.height = 10
        previous = MagicMock()
        previous.height = 8

        result = cli_mod._hermes_call_output_screen_diff(
            fake_osd,
            app=None,
            output=None,
            screen=screen,
            current_pos=None,
            color_depth=None,
            previous_screen=previous,
            last_style="style",
            is_done=False,
            full_screen=False,
            attrs_for_style_string=None,
            style_string_has_style=None,
            size=None,
            previous_width=80,
        )

        assert result[0] == "ok"
        assert len(calls) == 2
        assert calls[0][0] is previous
        assert previous.height == 10  # height inflate still applied first
        assert calls[1] == (None, 0, None)

    def test_resize_recovery_is_debounced(self, bare_cli, monkeypatch):
        timers = []
        calls = []

        class FakeTimer:
            def __init__(self, delay, callback):
                self.delay = delay
                self.callback = callback
                self.cancelled = False
                self.daemon = False
                timers.append(self)

            def start(self):
                calls.append(("start", self.delay))

            def cancel(self):
                self.cancelled = True
                calls.append(("cancel", self.delay))

            def fire(self):
                self.callback()

        app = MagicMock()
        app.loop.call_soon_threadsafe.side_effect = lambda cb: cb()
        monkeypatch.setattr(cli_mod.threading, "Timer", FakeTimer)
        monkeypatch.setattr(
            bare_cli,
            "_recover_after_resize",
            lambda _app, _orig: calls.append(("recover", _orig())),
        )

        original_one = lambda: "first"
        original_two = lambda: "second"

        bare_cli._schedule_resize_recovery(app, original_one, delay=0.25)
        assert bare_cli._resize_recovery_pending is True
        bare_cli._schedule_resize_recovery(app, original_two, delay=0.25)

        assert len(timers) == 2
        assert timers[0].cancelled is True
        timers[0].fire()
        assert ("recover", "first") not in calls

        timers[1].fire()
        assert ("recover", "second") in calls
        assert bare_cli._resize_recovery_pending is False

    def test_invalidate_is_suppressed_while_resize_recovery_is_pending(self, bare_cli):
        app = MagicMock()
        bare_cli._app = app
        bare_cli._last_invalidate = 0.0
        bare_cli._resize_recovery_pending = True

        bare_cli._invalidate(min_interval=0)

        app.invalidate.assert_not_called()

    def test_swallows_renderer_exceptions(self, bare_cli):
        # If the renderer blows up for any reason, the helper must not
        # propagate — otherwise a stray Ctrl+L would crash the CLI.
        app = MagicMock()
        app.renderer.output.erase_screen.side_effect = RuntimeError("boom")
        bare_cli._app = app

        bare_cli._force_full_redraw()  # must not raise

        # invalidate() is still attempted after a renderer failure.
        app.invalidate.assert_called_once()

    def test_swallows_invalidate_exceptions(self, bare_cli):
        app = MagicMock()
        app.invalidate.side_effect = RuntimeError("boom")
        bare_cli._app = app

        bare_cli._force_full_redraw()  # must not raise


class TestFirstSigwinchBaseline:
    """Bug #65293: the session's FIRST SIGWINCH used to be force-treated as a
    width change (no prior width to compare against), so a benign resize
    signal — GNOME Terminal tab bar appearing, monitor-scale change, focus
    events — cleared the viewport and replayed ``_OUTPUT_HISTORY``.  After a
    resume that deque holds the whole "Previous Conversation" recap plus the
    first live exchange, so everything reprinted as a duplicate.  A replay
    must require an OBSERVED width change against a recorded baseline.
    """

    def test_first_sigwinch_with_unchanged_width_does_not_replay(
        self, bare_cli, monkeypatch
    ):
        app = MagicMock()
        events = []
        app.renderer.output.erase_screen.side_effect = lambda: events.append("erase")
        original_on_resize = lambda: events.append("original_resize")

        bare_cli._status_bar_suppressed_after_resize = False
        # No baseline recorded yet — the pre-fix code forced width_changed=True.
        assert getattr(bare_cli, "_last_resize_width", None) is None
        monkeypatch.setattr(bare_cli, "_get_tui_terminal_width", lambda: 120)
        monkeypatch.setattr(bare_cli, "_schedule_status_bar_unsuppress", lambda *_: None)
        monkeypatch.setattr(
            cli_mod, "_replay_output_history", lambda *_: events.append("replay")
        )

        bare_cli._recover_after_resize(app, original_on_resize)

        # Width did not change — no clear, no replay, straight to prompt_toolkit.
        assert events == ["original_resize"]
        app.renderer.output.erase_screen.assert_not_called()
        # The signal still records the baseline for the next comparison.
        assert bare_cli._last_resize_width == 120

    def test_real_width_change_after_baseline_still_replays(
        self, bare_cli, monkeypatch
    ):
        """The #49120 recovery must still fire on a real change: here a shrink on a terminal
        that may not reflow, so the rows the aimed erase takes are replayed."""
        app = _fake_app(rows=30, columns=90, chrome=3, painted=[120, 120, 30], cursor_y=2)
        events = []
        original_on_resize = lambda: events.append("original_resize")

        bare_cli._status_bar_suppressed_after_resize = False
        bare_cli._last_resize_width = 120
        monkeypatch.setattr(bare_cli, "_get_tui_terminal_width", lambda: 90)
        monkeypatch.setattr(bare_cli, "_schedule_status_bar_unsuppress", lambda *_: None)
        monkeypatch.setattr(cli_mod, "_terminal_reflows", lambda: None)
        monkeypatch.setattr(
            cli_mod, "_replay_output_history", lambda *_: events.append("replay")
        )

        bare_cli._recover_after_resize(app, original_on_resize)

        assert events == ["original_resize", "replay"]
        assert bare_cli._last_resize_width == 90

    def test_install_resize_recovery_seeds_width_baseline(self, bare_cli):
        """Hook installation records the CURRENT width as the baseline, so an
        initial maximize/restore (a real change vs that baseline) is still
        recovered while a same-size first signal is not.

        The baseline must come from ``app.output`` — the same object the
        running app measures on SIGWINCH — not from ``get_app()``, which
        before ``app.run()`` is a DummyApplication reporting a fake 80 cols.
        """
        app = MagicMock()
        app.output.get_size.return_value.columns = 132
        scheduled = []
        bare_cli._schedule_resize_recovery = lambda *a, **k: scheduled.append(a)

        original = app._on_resize
        bare_cli._install_resize_recovery(app)

        assert bare_cli._last_resize_width == 132
        assert app._on_resize is not original  # hook installed
        app._on_resize()  # simulated SIGWINCH → routes to the debouncer
        assert len(scheduled) == 1
        assert scheduled[0][0] is app
        assert scheduled[0][1] is original

    def test_install_resize_recovery_falls_back_to_shutil(
        self, bare_cli, monkeypatch
    ):
        """A dead app.output probe falls back to shutil, never to the
        DummyApplication's fake width."""
        import os as os_mod

        app = MagicMock()
        app.output.get_size.side_effect = RuntimeError("not attached")
        monkeypatch.setattr(
            cli_mod.shutil,
            "get_terminal_size",
            lambda _default: os_mod.terminal_size((97, 40)),
        )

        bare_cli._install_resize_recovery(app)

        assert bare_cli._last_resize_width == 97

    def test_install_resize_recovery_survives_width_probe_failure(
        self, bare_cli, monkeypatch
    ):
        app = MagicMock()
        app.output.get_size.side_effect = RuntimeError("not attached")

        def _boom(_default):
            raise RuntimeError("no tty")

        monkeypatch.setattr(cli_mod.shutil, "get_terminal_size", _boom)

        bare_cli._install_resize_recovery(app)  # must not raise

        assert getattr(bare_cli, "_last_resize_width", None) is None


class TestReplayFitsViewport:
    """#95375: a redraw that keeps scrollback replays only what the viewport holds."""

    def test_replay_keeps_newest_lines_that_fit_wrapped(self, monkeypatch):
        """A viewport refill paints at once to the given output (no ``run_in_terminal``)."""
        cli_mod._configure_output_history(True, 200)
        for i in range(200):
            cli_mod._record_output_history(f"history line {i}")
        cli_mod._record_output_history("x" * 150)  # soft-wraps to 2 rows at 100 cols
        monkeypatch.setattr(cli_mod, "_pt_print", lambda *_: pytest.fail("deferred print"))
        out = MagicMock()

        cli_mod._replay_output_history((10, 100), output=out)

        written = "".join(c.args[0] for c in out.write.call_args_list)
        assert written.split("\r\n") == [f"history line {i}" for i in range(192, 200)] + ["x" * 150, ""]
        assert len(cli_mod._OUTPUT_HISTORY) == 200  # the buffer itself is untouched

    def test_tail_counts_rows_at_the_painted_width_and_keeps_a_tall_lines_bottom(self):
        """A line keeps the rows it wrapped into when painted (a terminal that does not reflow
        never re-wraps it), and a line taller than the room keeps its bottom rows instead of
        vanishing: the rows above them are what already scrolled into scrollback."""
        tall = "".join(chr(ord("a") + i) * 10 for i in range(6))  # 60 cells = 6 rows at 10 cols
        assert cli_mod._output_tail_fitting(["older", tall], 2, 10) == ["eeeeeeeeeeffffffffff"]
        styled = f"\x1b[1m{tall}\x1b[0m"
        assert cli_mod._output_tail_fitting([styled], 2, 10) == ["\x1b[1meeeeeeeeeeffffffffff\x1b[0m"]

        from hermes_cli.cli_render import _PaintedLine
        narrow = _PaintedLine("n" * 150)
        narrow.width = 50  # painted before the terminal widened to 100: 3 rows, not 2
        assert cli_mod._output_tail_fitting(["older", narrow, "last"], 4, 100) == [narrow, "last"]


class TestFocusRegainRedraw:
    """Focus-in (CSI I) routes through the same recovery as Ctrl+L, rate-limited.

    While the tab/window is hidden the emulator may coalesce output or repaint
    the surface; on regain prompt_toolkit's incremental diff stacks a fresh
    copy of the prompt chrome on top of the stale one (#60920 focus-regain
    variant, #25337).
    """

    def test_focus_regain_triggers_full_redraw(self, bare_cli):
        calls = []
        bare_cli._force_full_redraw = lambda: calls.append("redraw")

        bare_cli._schedule_focus_regain_redraw()

        assert calls == ["redraw"]

    def test_focus_regain_redraw_is_rate_limited(self, bare_cli):
        calls = []
        bare_cli._force_full_redraw = lambda: calls.append("redraw")

        bare_cli._schedule_focus_regain_redraw(min_interval=60.0)
        bare_cli._schedule_focus_regain_redraw(min_interval=60.0)
        bare_cli._schedule_focus_regain_redraw(min_interval=60.0)

        assert calls == ["redraw"]

    def test_focus_regain_redraw_fires_again_after_interval(self, bare_cli):
        calls = []
        bare_cli._force_full_redraw = lambda: calls.append("redraw")

        bare_cli._schedule_focus_regain_redraw(min_interval=0.0)
        bare_cli._schedule_focus_regain_redraw(min_interval=0.0)

        assert calls == ["redraw", "redraw"]

    def test_first_redraw_fires_even_with_small_monotonic_clock(
        self, bare_cli, monkeypatch
    ):
        """The first-ever redraw must fire regardless of monotonic's epoch.

        time.monotonic() starts from an arbitrary point (boot on Linux); on a
        fresh CI VM it can be smaller than min_interval. The old 0.0 sentinel
        turned that into ``now - 0.0 < min_interval`` and silently swallowed
        the FIRST redraw (CI run 32494557030 — both retry attempts failed).
        """
        calls = []
        bare_cli._force_full_redraw = lambda: calls.append("redraw")
        monkeypatch.setattr(cli_mod.time, "monotonic", lambda: 3.0)

        bare_cli._schedule_focus_regain_redraw(min_interval=60.0)

        assert calls == ["redraw"]

    def test_first_invalidate_fires_even_with_small_monotonic_clock(
        self, bare_cli, monkeypatch
    ):
        """Same class as above for the streaming/spinner repaint throttle."""
        app = MagicMock()
        bare_cli._app = app
        bare_cli._last_invalidate = None
        monkeypatch.setattr(cli_mod.time, "monotonic", lambda: 0.1)

        bare_cli._invalidate(min_interval=0.25)

        app.invalidate.assert_called_once()
