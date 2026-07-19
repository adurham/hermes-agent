"""Reasoning box text must be indented to sit inside the box frame, matching
the response box padding (_STREAM_PAD), not rendered flush at column 0.
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


def _make_cli_stub():
    from cli import HermesCLI

    cli = HermesCLI.__new__(HermesCLI)
    cli.show_reasoning = True
    cli._stream_box_opened = False
    cli._reasoning_box_opened = False
    cli._reasoning_buf = ""
    cli._reasoning_shown_this_turn = False
    cli._deferred_content = ""
    return cli


def _reasoning_lines(printed):
    """Lines that carry actual reasoning text (skip the box top/bottom rules)."""
    return [p for p in printed if "Reasoning" not in p and "\u2514" not in p
            and "\u250c" not in p and p.strip()]


def test_complete_reasoning_lines_are_indented():
    from cli import _STREAM_PAD

    cli = _make_cli_stub()
    printed = []
    with patch("cli._cprint", side_effect=lambda s="": printed.append(s)):
        cli._stream_reasoning_delta("first line\nsecond line\n")

    body = _reasoning_lines(printed)
    assert body, f"no reasoning body lines captured: {printed!r}"
    for line in body:
        # Strip leading ANSI, then require the _STREAM_PAD indent before text.
        assert _STREAM_PAD in line and line.index(_STREAM_PAD) < line.find("line"), (
            f"reasoning line not indented by _STREAM_PAD: {line!r}"
        )


def test_flushed_partial_line_on_close_is_indented():
    from cli import _STREAM_PAD

    cli = _make_cli_stub()
    printed = []
    with patch("cli._cprint", side_effect=lambda s="": printed.append(s)):
        # A short trailing partial line (no newline) only flushes on close.
        cli._stream_reasoning_delta("trailing partial")
        cli._close_reasoning_box()

    flushed = [p for p in printed if "trailing partial" in p]
    assert flushed, f"trailing partial not flushed: {printed!r}"
    assert _STREAM_PAD in flushed[0] and flushed[0].index(_STREAM_PAD) < flushed[0].find("trailing"), (
        f"flushed partial not indented: {flushed[0]!r}"
    )
