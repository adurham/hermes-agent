"""Streamed response body: width-aware wrapping + over-long partial flush.

Covers two related fixes:
  1. Long prose lines are wrapped to the box width with the _STREAM_PAD indent
     preserved on every segment, instead of relying on the terminal's
     soft-wrap (which restarts continuation at column 0, losing the indent).
  2. An over-long *partial* line (no trailing newline) is force-flushed during
     streaming instead of sitting invisible until end-of-stream — the
     "2nd-to-last line hangs, then pops in one chunk" symptom. Tables stay
     buffered for whole-block realignment.
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


def _make_cli_stub():
    from cli import HermesCLI

    cli = HermesCLI.__new__(HermesCLI)
    cli.show_reasoning = False
    cli.show_timestamps = False
    cli.final_response_markdown = "render"
    cli._stream_text_ansi = ""
    cli._stream_box_opened = True  # skip header; exercise the emit loop
    cli._reasoning_box_opened = False
    cli._reasoning_buf = ""
    cli._reasoning_shown_this_turn = False
    cli._deferred_content = ""
    cli._stream_buf = ""
    cli._stream_table_buf = []
    cli._in_stream_table = False
    return cli


def _body(printed):
    """Streamed body lines (skip box frames / blank)."""
    return [p for p in printed if p.strip() and "╭" not in p and "╰" not in p]


# ── _wrap_stream_line unit behavior ──────────────────────────────────────

def test_wrap_short_line_unchanged():
    from cli import HermesCLI
    assert HermesCLI._wrap_stream_line("short", 80) == ["short"]


def test_wrap_breaks_on_space_within_width():
    from cli import HermesCLI
    segs = HermesCLI._wrap_stream_line("aaaa bbbb cccc dddd", 9)
    # Each segment within width; break-space consumed (not leading next seg).
    assert all(len(s) <= 9 for s in segs)
    assert all(not s.startswith(" ") for s in segs)
    # Content preserved when rejoined with the break-spaces.
    assert " ".join(segs) == "aaaa bbbb cccc dddd"


def test_wrap_hard_breaks_unbroken_run():
    from cli import HermesCLI
    segs = HermesCLI._wrap_stream_line("xxxxxxxxxxxxxxxxxxxx", 6)  # no spaces
    assert all(len(s) <= 6 for s in segs)
    assert "".join(segs) == "xxxxxxxxxxxxxxxxxxxx"


def test_wrap_preserves_leading_indent_whitespace():
    from cli import HermesCLI
    # Content-preserving: code indentation must survive (no textwrap collapse).
    line = "    indented code that is quite long and needs to wrap here ok"
    segs = HermesCLI._wrap_stream_line(line, 20)
    assert segs[0].startswith("    "), segs


# ── complete-line wrapping through _emit_stream_text ─────────────────────

def test_long_complete_line_wraps_with_indent():
    from cli import _STREAM_PAD

    cli = _make_cli_stub()
    printed = []
    long_line = "word " * 60  # ~300 chars, far over any terminal width
    with patch("cli._terminal_width_for_streaming", return_value=40), \
         patch("cli._cprint", side_effect=lambda s="": printed.append(s)):
        cli._emit_stream_text(long_line + "\n")

    body = _body(printed)
    assert len(body) > 1, f"long line should wrap into multiple segments: {body!r}"
    for seg in body:
        assert _STREAM_PAD in seg, f"segment lost indent: {seg!r}"


# ── over-long partial-line force-flush ───────────────────────────────────

def test_overlong_partial_line_force_flushes():
    """A long partial line (no newline) emits its complete segments mid-stream
    rather than waiting for end-of-stream."""
    cli = _make_cli_stub()
    printed = []
    partial = "word " * 30  # ~150 chars, no trailing newline
    with patch("cli._terminal_width_for_streaming", return_value=40), \
         patch("cli._cprint", side_effect=lambda s="": printed.append(s)):
        cli._emit_stream_text(partial)

    assert _body(printed), "over-long partial should force-flush, not stay buffered"
    # A tail remains buffered for the next delta (not everything dumped).
    assert cli._stream_buf
    assert len(cli._stream_buf) <= 40


def test_short_partial_line_stays_buffered():
    """A short partial line must NOT flush early (normal line-at-a-time)."""
    cli = _make_cli_stub()
    printed = []
    with patch("cli._terminal_width_for_streaming", return_value=80), \
         patch("cli._cprint", side_effect=lambda s="": printed.append(s)):
        cli._emit_stream_text("a short partial sentence")

    assert _body(printed) == [], f"short partial should stay buffered: {printed!r}"
    assert cli._stream_buf == "a short partial sentence"


def test_partial_table_row_not_force_flushed():
    """An over-long partial that looks like a table row stays buffered so the
    whole table can be realigned together."""
    cli = _make_cli_stub()
    printed = []
    row = "| " + "cell | " * 30  # long, table-shaped, no newline
    with patch("cli._terminal_width_for_streaming", return_value=40), \
         patch("cli._cprint", side_effect=lambda s="": printed.append(s)):
        cli._emit_stream_text(row)

    assert _body(printed) == [], "table-shaped partial must not force-flush"
    assert cli._stream_buf == row


def test_no_content_loss_across_partial_flush_then_newline():
    """Everything emitted across a force-flush + later newline reconstructs
    the original text (no dropped or duplicated content)."""
    cli = _make_cli_stub()
    printed = []
    with patch("cli._terminal_width_for_streaming", return_value=30), \
         patch("cli._cprint", side_effect=lambda s="": printed.append(s)):
        text = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
        cli._emit_stream_text(text)          # force-flushes complete segments
        cli._emit_stream_text(" lambda mu\n")  # completes the line

    body = _body(printed)
    # Rejoin emitted segments (strip the _STREAM_PAD indent) and compare words.
    from cli import _STREAM_PAD
    emitted_words = []
    for seg in body:
        emitted_words.extend(seg.replace(_STREAM_PAD, "", 1).split())
    assert emitted_words == (text + " lambda mu").split()
    assert cli._stream_buf == ""
