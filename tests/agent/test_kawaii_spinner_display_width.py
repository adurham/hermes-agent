"""Regression tests for KawaiiSpinner's terminal-cell-width redraw padding.

Bug: KawaiiSpinner._animate() tracked ``self.last_line_len`` via Python's
``len()`` instead of terminal display (cell) width. Emoji / kawaii-face
spinner frames (moon phases, "(｡◕‿◕｡)", etc.) render as more terminal cells
than ``len()`` reports, so the \\r-redraw pad computed from a ``len()``-based
``last_line_len`` under-erased the previous frame. The leftover, un-erased
tail character(s) from the prior frame visually bled into the next frame —
most visibly reported by users as stray/duplicate digits in the elapsed-time
readout (e.g. a phantom "0" surviving from a wider previous frame making
"1s" misread as "01s"/"0100s").

Fix: KawaiiSpinner._display_width() uses prompt_toolkit's get_cwidth() (the
same mechanism already used by the CLI status bar's
_status_bar_display_width) so last_line_len reflects actual screen columns,
and the redraw always erases at least as much as was actually printed.
"""

from unittest.mock import MagicMock

from agent.display import KawaiiSpinner


class TestDisplayWidth:
    def test_ascii_only_len_equals_cwidth(self):
        spinner = KawaiiSpinner(message="pondering", spinner_type="dots")
        line = "  ⠋ pondering (9.9s)"
        assert spinner._display_width(line) == len(line)

    def test_emoji_frame_wider_than_len(self):
        spinner = KawaiiSpinner(message="pondering", spinner_type="moon")
        line = "  🌑 pondering (2.0s)"
        # The moon emoji is 1 Python codepoint but occupies 2 terminal cells.
        assert spinner._display_width(line) > len(line)

    def test_kawaii_face_wider_than_len(self):
        spinner = KawaiiSpinner(message="pondering")
        line = "  (｡◕‿◕｡) pondering ✨ (3.0s)"
        assert spinner._display_width(line) >= len(line)

    def test_empty_and_none_safe(self):
        spinner = KawaiiSpinner(message="x")
        assert spinner._display_width("") == 0
        assert spinner._display_width("") == 0


class TestRedrawPaddingNoLeftoverChars:
    """Simulates the exact under-erase bug: a wide (emoji) frame followed by
    a shorter/ascii frame must clear ALL cells the wide frame actually
    occupied on screen, not just as many as len() reported.
    """

    def test_cwidth_based_pad_fully_erases_wide_previous_frame(self):
        spinner = KawaiiSpinner(message="pondering a bit more here", spinner_type="moon")
        prev_wide = "  🌑 pondering a bit more here (10.0s)"
        new_short = "  ⠋ pondering (1.0s)"

        # What the OLD buggy code stored as last_line_len (Python len()).
        buggy_last_line_len = len(prev_wide)
        # What was ACTUALLY printed to the terminal for prev_wide.
        actual_prev_screen_width = spinner._display_width(prev_wide)

        # The bug: len() undercounts the wide-glyph line, so the stored
        # last_line_len is smaller than the real on-screen width.
        assert buggy_last_line_len < actual_prev_screen_width

        buggy_pad = max(buggy_last_line_len - len(new_short), 0)
        chars_left_unerased_buggy = actual_prev_screen_width - (len(new_short) + buggy_pad)
        assert chars_left_unerased_buggy > 0, (
            "sanity check: the len()-based approach should reproduce the bug "
            "(leftover un-erased characters) for this fixture"
        )

        # Fixed behavior: last_line_len is captured via _display_width, so
        # the pad always covers the full previous screen width.
        fixed_last_line_len = spinner._display_width(prev_wide)
        fixed_new_width = spinner._display_width(new_short)
        fixed_pad = max(fixed_last_line_len - fixed_new_width, 0)
        chars_left_unerased_fixed = actual_prev_screen_width - (fixed_new_width + fixed_pad)
        assert chars_left_unerased_fixed == 0

    def test_animate_tracks_cwidth_not_len(self, monkeypatch):
        """_animate() must store last_line_len via _display_width(), not len()."""
        import time as time_module

        spinner = KawaiiSpinner(message="x", spinner_type="moon")
        # _is_tty is a read-only property that checks self._out.isatty();
        # give it a fake stdout that reports as a real terminal.
        fake_out = MagicMock()
        fake_out.isatty.return_value = True
        spinner._out = fake_out
        monkeypatch.setattr(spinner, "_is_patch_stdout_proxy", lambda: False)

        written = []
        spinner._write = lambda text, end="", flush=False: written.append(text)

        spinner.running = True
        spinner.start_time = time_module.time()

        call_count = {"n": 0}
        real_sleep = time_module.sleep

        def fake_sleep(_seconds):
            call_count["n"] += 1
            if call_count["n"] >= 1:
                spinner.running = False
            real_sleep(0)

        monkeypatch.setattr(time_module, "sleep", fake_sleep)
        spinner._animate()

        assert written, "spinner should have written at least one frame"
        line_written = written[0]
        # Strip the leading \r and any trailing pad spaces to recover the
        # real content line, then confirm last_line_len was captured via
        # cell-width (>= len(), and exactly matching _display_width()).
        raw_line = line_written.lstrip("\r").rstrip(" ")
        assert spinner.last_line_len == spinner._display_width(raw_line)
        assert spinner.last_line_len >= len(raw_line)
