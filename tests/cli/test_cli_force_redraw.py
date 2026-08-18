"""Tests for CLI redraw helpers used to recover from terminal buffer drift.

Covers:
  - _force_full_redraw (#8688 cmux tab switch, /redraw, Ctrl+L)
  - the resize handler we install over prompt_toolkit's _on_resize (#5474)
  - the resize-gated _output_screen_diff patch (text-overwrites-itself
    regression, see TestResizeSafeScreenDiffPatch)

Both behaviors are exercised against fake prompt_toolkit renderer/output
objects — we're asserting the escape sequences the CLI sends, not that
the terminal physically repainted.
"""

import types
from unittest.mock import MagicMock

import pytest

import cli as cli_mod
from cli import HermesCLI


@pytest.fixture
def bare_cli():
    """A HermesCLI with no __init__ — we only exercise the redraw helper."""
    cli = object.__new__(HermesCLI)
    return cli


class TestForceFullRedraw:
    def test_no_app_is_safe(self, bare_cli):
        # _force_full_redraw must be a no-op when the TUI isn't running.
        bare_cli._app = None
        bare_cli._force_full_redraw()  # must not raise




    def test_resize_recovery_clears_viewport_on_width_change(self, bare_cli, monkeypatch):
        """A WIDTH change must wipe the visible viewport (CSI 2J) and replay.

        On column shrink the terminal reflows the old full-width chrome into
        extra rows that prompt_toolkit's stale-cursor erase cannot reach,
        leaving a duplicated status bar (#19280/#5474 class). We route through
        the same recovery as Ctrl+L: erase_screen (2J) + replay transcript.
        It must be banner-safe — CSI 3J (write_raw) must NOT fire.
        """
        app = MagicMock()
        events = []
        app.renderer.output.erase_screen.side_effect = lambda: events.append("erase")
        app.renderer.output.write_raw.side_effect = lambda *_: events.append("scrollback_wipe")
        original_on_resize = lambda: events.append("original_resize")

        bare_cli._status_bar_suppressed_after_resize = False
        bare_cli._last_resize_width = 200
        monkeypatch.setattr(bare_cli, "_get_tui_terminal_width", lambda: 90)
        monkeypatch.setattr(bare_cli, "_schedule_status_bar_unsuppress", lambda *_: None)
        monkeypatch.setattr(cli_mod, "_replay_output_history", lambda: events.append("replay"))

        bare_cli._recover_after_resize(app, original_on_resize)

        # Viewport cleared and transcript replayed BEFORE prompt_toolkit's resize.
        assert "erase" in events
        assert "replay" in events
        assert events.index("erase") < events.index("original_resize")
        # Banner-safe: scrollback (CSI 3J) must never be wiped on a resize.
        assert "scrollback_wipe" not in events
        # New width recorded for the next comparison.
        assert bare_cli._last_resize_width == 90
        assert bare_cli._status_bar_suppressed_after_resize is True


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


class TestResizeSafeScreenDiffPatch:
    """Regression tests for the "text overwriting itself" bug.

    The ghost-status-bar fix monkey-patches prompt_toolkit's
    _output_screen_diff to inflate previous_screen.height whenever the new
    frame is taller. That condition also fires on ordinary typing (the
    completion menu popping open, inline auto-suggest ghost-text appearing)
    with no resize involved — desyncing the renderer's cursor bookkeeping
    and producing fragments of later text overwriting earlier text on the
    input line (observed live: typing "testing that you are working"
    rendered as "tou ing that ye..."). The patch must only inflate height
    while `_status_bar_suppressed_after_resize` is true (the transient
    post-resize recovery window), never during normal keystroke-driven
    frame growth.
    """

    @pytest.fixture
    def patched_diff(self, bare_cli, monkeypatch):
        """Install the patch against the REAL prompt_toolkit.renderer module,
        with its _output_screen_diff swapped for a recording stub, and
        `_hermes_osd_patched` reset so each test re-installs cleanly.

        Returns (bare_cli, patched_osd_callable, orig_osd_calls). Patches
        module attributes directly (monkeypatch.setattr, auto-restored)
        rather than swapping sys.modules — a dotted `import a.b as x`
        performed *inside* the method under test resolves via the
        package's cached submodule attribute, not a sys.modules swap, so
        attribute-level patching is the reliable way to intercept it here.
        """
        import prompt_toolkit.renderer as _real_pt_renderer

        calls = []

        def fake_orig_osd(
            app, output, screen, current_pos, color_depth,
            previous_screen, last_style, is_done, full_screen,
            attrs_for_style_string, style_string_has_style,
            size, previous_width,
        ):
            calls.append(previous_screen.height if previous_screen is not None else None)
            return "sentinel-return"

        monkeypatch.setattr(_real_pt_renderer, "_output_screen_diff", fake_orig_osd)
        monkeypatch.setattr(_real_pt_renderer, "_hermes_osd_patched", False, raising=False)

        bare_cli._install_resize_safe_screen_diff_patch()
        patched_osd = _real_pt_renderer._output_screen_diff
        return bare_cli, patched_osd, calls

    def _make_previous_screen(self, height):
        return types.SimpleNamespace(height=height)

    def test_does_not_inflate_height_outside_resize_window(self, patched_diff):
        """Plain typing (menu/ghost-text growing the frame) must NOT
        trigger the height inflate — this is the actual regression."""
        bare_cli, patched_osd, calls = patched_diff
        bare_cli._status_bar_suppressed_after_resize = False

        previous_screen = self._make_previous_screen(height=1)
        screen = types.SimpleNamespace(height=3)  # e.g. completion menu opened

        result = patched_osd(
            None, None, screen, None, None, previous_screen, None, False, False,
            None, None, None, None,
        )

        assert result == "sentinel-return"
        # Height must be passed through UNCHANGED to the real implementation.
        assert previous_screen.height == 1
        assert calls == [1]

    def test_inflates_height_during_resize_recovery_window(self, patched_diff):
        """The original ghost-status-bar fix must still work during an
        actual resize (when _status_bar_suppressed_after_resize is True)."""
        bare_cli, patched_osd, calls = patched_diff
        bare_cli._status_bar_suppressed_after_resize = True

        previous_screen = self._make_previous_screen(height=1)
        screen = types.SimpleNamespace(height=3)

        patched_osd(
            None, None, screen, None, None, previous_screen, None, False, False,
            None, None, None, None,
        )

        # Height WAS inflated to match before delegating to the real impl.
        assert previous_screen.height == 3
        assert calls == [3]

    def test_never_shrinks_previous_screen_height(self, patched_diff):
        """Only inflate (never shrink) — a taller previous frame than the
        new one is left untouched even inside the resize window."""
        bare_cli, patched_osd, calls = patched_diff
        bare_cli._status_bar_suppressed_after_resize = True

        previous_screen = self._make_previous_screen(height=5)
        screen = types.SimpleNamespace(height=3)

        patched_osd(
            None, None, screen, None, None, previous_screen, None, False, False,
            None, None, None, None,
        )

        assert previous_screen.height == 5
        assert calls == [5]

    def test_none_previous_screen_is_left_untouched(self, patched_diff):
        """previous_screen=None (first paint) must reach the real impl
        unmodified — never replaced with a fresh Screen()."""
        bare_cli, patched_osd, calls = patched_diff
        bare_cli._status_bar_suppressed_after_resize = True

        screen = types.SimpleNamespace(height=3)

        result = patched_osd(
            None, None, screen, None, None, None, None, False, False,
            None, None, None, None,
        )
        assert result == "sentinel-return"
        assert calls == [None]
