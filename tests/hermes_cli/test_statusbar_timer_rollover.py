"""Byte-level regression tests for the CLI status-bar timer defects.

Two user-visible classes, both reported repeatedly across sessions:

1. A field displayed AT or ABOVE its own boundary — a seconds field >= 60
   (``60s``, ``90s``, ``4m361s``) or a minutes field >= 60 (``60m``,
   ``284m41s``).  Root cause: formatters kept the raw ``divmod(_, 60)`` minute
   count and never rolled minutes into hours, and ``format_duration_compact``
   rounded the *raw* value before comparing it to the unit boundary.
2. Stale trailing characters surviving a ``\\r`` repaint that does not clear to
   end-of-line (the sanctioned pattern is space-padding, never ``\\033[K``).

These assert on the EXACT rendered strings and the EXACT on-screen cells after a
simulated no-clear repaint, not on substrings, so a re-introduced doubling or an
un-padded rewrite fails here.
"""

import time

from agent.display import display_cwidth
from agent.turn_summary import format_elapsed as turn_elapsed
from hermes_cli.cli_process_dock import process_activity
from hermes_cli.cli_subagent_monitor import format_elapsed as dock_elapsed

# ── 1. no field ever reaches its own boundary ───────────────────────────────


def _seconds_field(s: str):
    """The trailing ``NNs`` token of an ``MmSSs`` / ``Nm`` string, or None."""
    # last "digits"s group, e.g. "59m59s" -> "59"
    import re

    m = re.search(r"(\d+)s$", s)
    return int(m.group(1)) if m else None


def _minutes_field(s: str):
    import re

    m = re.search(r"(\d+)m", s)
    return int(m.group(1)) if m else None


def test_rollover_never_shows_seconds_or_minutes_at_60():
    """Walk the exact boundaries the user reported across every CLI formatter."""
    # 59 -> 60 -> 61 (minute rollover) and 599 -> 600 (the "600s" class).
    for seconds in (59, 60, 61, 599, 600, 601, 3599, 3600, 3601, 17081):
        for fmt in (dock_elapsed, turn_elapsed):
            out = fmt(seconds)
            sec = _seconds_field(out)
            mins = _minutes_field(out)
            assert sec is None or sec < 60, f"{fmt.__name__}({seconds}) -> {out!r} (seconds >= 60)"
            assert mins is None or mins < 60, f"{fmt.__name__}({seconds}) -> {out!r} (minutes >= 60)"
            assert "ss" not in out, f"{fmt.__name__}({seconds}) -> {out!r} (doubled suffix)"

    # The exact strings the user reported must now be impossible.
    assert dock_elapsed(17081) == "4h44m"  # was "284m41s"
    assert dock_elapsed(3600) == "1h00m"  # was "60m00s"
    assert turn_elapsed(3601) == "1h00m"  # was "60m01s"

    # The process dock used to render a bare "696s"/"300s".
    assert process_activity({"status": "running", "elapsed": 600, "detail": ""}) == "10m00s · starting"
    assert process_activity({"status": "running", "elapsed": 60, "detail": ""}) == "1m00s · starting"


# ── 2. no-clear repaint must not leave stale trailing cells ─────────────────


def _render_row(writes, columns=60):
    """Model a terminal row: apply ``\\r{line}{pad}`` writes with NO erase-EOL.

    Returns the visible row after the writes (trailing spaces stripped, as a
    terminal's *rendered* row would read).
    """
    row = [" "] * columns
    for w in writes:
        assert w.startswith("\r"), "raw repaint must lead with a carriage return"
        for i, ch in enumerate(w[1:]):
            if i < columns:
                row[i] = ch
    return "".join(row).rstrip()


def test_no_clear_repaint_shrink_leaves_no_stale_chars():
    """A shrinking timer rewrite must space-pad to the previous width.

    The real shrink: a fresh tool call resets the spinner timer from a long
    "11m36s" to a short "0.3s".  With the sanctioned space-pad pattern the new
    row is byte-clean; without the pad the old tail survives (proving teeth).
    """
    long_line = f"⚙ read_file  ({turn_elapsed(696)})"  # (11m36s)
    short_line = f"⚙ read_file  ({turn_elapsed(0.3)})"  # (0.3s), narrower
    assert display_cwidth(short_line) < display_cwidth(long_line)

    padded = []
    prev = 0
    for line in (long_line, short_line):
        width = display_cwidth(line)
        padded.append("\r" + line + " " * max(prev - width, 0))  # sanctioned pattern
        prev = width
    assert _render_row(padded) == short_line, "space-padded repaint left stale cells"

    # Teeth: the same shrink with NO pad (a bare \r rewrite) leaves stale chars.
    unpadded = ["\r" + long_line, "\r" + short_line]
    assert _render_row(unpadded) != short_line, "un-padded rewrite must leave a stale tail"
    # And the stale tail is the part the pad specifically erases.
    assert _render_row(unpadded).startswith(short_line[: len(short_line) - 1])


def test_kawaii_spinner_repaint_covers_previous_line_width(monkeypatch):
    """The real ``KawaiiSpinner._animate`` ``\\r`` pad covers the previous frame.

    Drives the shipped redraw loop with a wide->short message change and asserts
    the written bytes erase at least the previous frame's display width (so no
    stale digit/glyph survives), using terminal cells not ``len()``.
    """
    import agent.display as display

    class _FakeOut:
        def __init__(self):
            self.frames = []

        def isatty(self):
            return True

        def write(self, s):
            self.frames.append(s)

        def flush(self):
            pass

    spinner = display.KawaiiSpinner(message="wide " + "\u2699\ufe0f" * 8, spinner_type="dots")
    spinner._out = _FakeOut()
    spinner.running = True
    spinner.start_time = time.time()

    seen = []
    spinner._write = lambda text, end="", flush=False: seen.append(text)

    calls = {"n": 0}

    def _fake_sleep(_):
        calls["n"] += 1
        if calls["n"] == 1:
            spinner.message = "short"
        if calls["n"] >= 2:
            spinner.running = False

    monkeypatch.setattr(display.time, "sleep", _fake_sleep)
    spinner._animate()

    assert len(seen) >= 2, "expected at least two repaints"
    first_width = display_cwidth(seen[0][1:])  # first frame (pad 0) => line width
    second_on_screen = display_cwidth(seen[1][1:])  # line + pad, in display cells
    assert second_on_screen >= first_width, (
        "second repaint under-erased the first frame: "
        f"{second_on_screen} < {first_width} cells"
    )
