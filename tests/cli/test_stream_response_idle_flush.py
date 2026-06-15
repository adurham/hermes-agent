"""Response-body parity with reasoning: prose wrap width + idle flush.

The response body streams line-at-a-time and flushes a partial only on a
newline, at the prose wrap width, or on box close. exo streams prose as tiny
word-fragments with almost no newlines, so on a wide terminal a line used to
wait ~2s (full terminal width) before appearing — "chunky" output. Two fixes:
  1. Prose flushes/wraps at _STREAM_PROSE_WRAP (80), capped by the terminal,
     not the full terminal width — smooth ~0.8s cadence like the reasoning box.
  2. _maybe_idle_flush_response() pushes the trailing partial when the stream
     stalls (parity with _maybe_idle_flush_reasoning).
"""
import os
import sys
import threading
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


def _make_cli_stub():
    from cli import HermesCLI

    cli = HermesCLI.__new__(HermesCLI)
    cli.show_reasoning = False
    cli.show_timestamps = False
    cli.final_response_markdown = "render"
    cli.intraline_streaming = False
    cli._stream_text_ansi = ""
    cli._stream_box_opened = True   # skip header; exercise the emit path
    cli._stream_buf = ""
    cli._stream_table_buf = []
    cli._in_stream_table = False
    cli._stream_lock = threading.RLock()
    cli._stream_last_delta_ts = 0.0
    cli._stream_idle_flush_secs = 0.25
    return cli


def _body(printed):
    return [p for p in printed if p.strip() and "╭" not in p and "╰" not in p]


# ── prose wrap width ─────────────────────────────────────────────────────

def test_prose_width_caps_below_wide_terminal():
    import cli
    cap = cli._configured_stream_wrap_cap()
    with patch("cli._terminal_width_for_streaming", return_value=200):
        # Wide terminal → capped at the configured readable width.
        assert cli._stream_wrap_width() == (200 if cap == 0 else cap)
        assert cli._prose_wrap_width_for_streaming() == cli._stream_wrap_width()


def test_prose_width_follows_narrow_terminal():
    import cli
    with patch("cli._terminal_width_for_streaming", return_value=50):
        # Narrow terminal → follows the terminal (auto-detected), below cap.
        assert cli._stream_wrap_width() == 50


def test_stream_wrap_width_zero_cap_uses_full_terminal():
    import cli
    with patch("cli._configured_stream_wrap_cap", return_value=0), \
         patch("cli._terminal_width_for_streaming", return_value=200):
        assert cli._stream_wrap_width() == 200


def test_long_line_flushes_at_prose_width_not_terminal_width():
    """On a wide terminal, a long line wraps at the prose cap (≈80), so it
    flushes sooner than it would at full terminal width."""
    from cli import _STREAM_PAD
    import cli as climod

    cli = _make_cli_stub()
    printed = []
    long_line = "word " * 60  # ~300 chars
    with patch("cli._terminal_width_for_streaming", return_value=200), \
         patch("cli._cprint", side_effect=lambda s="": printed.append(s)):
        cli._emit_stream_text(long_line + "\n")

    body = _body(printed)
    # At width 200 the whole 300-char line would be 2 segments; at prose-cap 80
    # it's ~4 — assert we wrapped tighter than the terminal would.
    assert len(body) >= 4, f"expected prose-width wrapping (~80), got {len(body)} segs"
    import re
    with patch("cli._terminal_width_for_streaming", return_value=200):
        cap = climod._stream_wrap_width()
    for seg in body:
        vis = re.sub(r"\x1b\[[0-9;]*m", "", seg)[len(_STREAM_PAD):]
        assert len(vis) <= cap, f"segment exceeds stream wrap width: {vis!r}"


# ── response idle flush ──────────────────────────────────────────────────

def test_idle_flush_pushes_response_partial():
    cli = _make_cli_stub()
    printed = []
    # delta at t=10, poll at t=10.5 (idle 0.5 > 0.25)
    times = iter([10.0, 10.5, 10.5, 10.5])
    with patch("cli._terminal_width_for_streaming", return_value=200), \
         patch("cli._cprint", side_effect=lambda s="": printed.append(s)), \
         patch("cli.time.monotonic", side_effect=lambda: next(times)):
        # Short partial, no newline, under prose width → normally buffered.
        cli._emit_stream_text("a trailing response sentence with no newline yet")
        assert _body(printed) == [], "short partial should buffer initially"
        cli._maybe_idle_flush_response()

    assert _body(printed), "idle flush should emit the response partial"
    assert cli._stream_buf == ""


def test_idle_flush_response_holds_when_active():
    cli = _make_cli_stub()
    printed = []
    times = iter([10.0, 10.1])  # idle 0.1 < 0.25
    with patch("cli._terminal_width_for_streaming", return_value=200), \
         patch("cli._cprint", side_effect=lambda s="": printed.append(s)), \
         patch("cli.time.monotonic", side_effect=lambda: next(times)):
        cli._emit_stream_text("still streaming")
        cli._maybe_idle_flush_response()
    assert _body(printed) == [], "must not flush while stream is active"
    assert "still streaming" in cli._stream_buf


def test_idle_flush_response_skips_table_partial():
    cli = _make_cli_stub()
    printed = []
    times = iter([10.0, 20.0, 20.0])
    with patch("cli._terminal_width_for_streaming", return_value=200), \
         patch("cli._cprint", side_effect=lambda s="": printed.append(s)), \
         patch("cli.time.monotonic", side_effect=lambda: next(times)):
        cli._stream_buf = "| col a | col b | col c |"  # table-shaped partial
        cli._maybe_idle_flush_response()
    assert _body(printed) == [], "table-shaped partial must not idle-flush"
    assert cli._stream_buf.startswith("|")


def test_idle_flush_response_noop_when_box_closed():
    cli = _make_cli_stub()
    cli._stream_box_opened = False
    cli._stream_buf = "orphan"
    printed = []
    with patch("cli._cprint", side_effect=lambda s="": printed.append(s)):
        cli._maybe_idle_flush_response()
    assert printed == []


def test_idle_flush_response_lock_contention_skips():
    cli = _make_cli_stub()
    cli._stream_buf = "contended"
    printed = []
    cli._stream_lock.acquire()
    # Spawn the flush on another thread so the non-blocking acquire truly fails
    # (RLock would re-enter on the same thread).
    def worker():
        with patch("cli._cprint", side_effect=lambda s="": printed.append(s)):
            cli._maybe_idle_flush_response()
    t = threading.Thread(target=worker)
    try:
        t.start()
        t.join()
    finally:
        cli._stream_lock.release()
    assert printed == [], "must not emit while another thread holds the lock"
    assert cli._stream_buf == "contended"
