"""Reasoning box text carries the same left margin as the response box.

The margin is ``_STREAM_PAD`` (``display.response_indent_width``): flush-left by
default since 2026-10-24 (matching upstream), and a configurable indent when the
owner sets the key above 0. Either way reasoning rows must line up with response
rows — never one flush-left and the other padded.
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


def test_complete_reasoning_lines_are_indented(monkeypatch):
    import cli as climod

    monkeypatch.setattr(climod, "_STREAM_PAD", "    ")
    cli = _make_cli_stub()
    printed = []
    with patch("cli._cprint", side_effect=lambda s="": printed.append(s)):
        cli._stream_reasoning_delta("first line\nsecond line\n")

    body = _reasoning_lines(printed)
    assert body, f"no reasoning body lines captured: {printed!r}"
    for line in body:
        # Strip leading ANSI, then require the _STREAM_PAD indent before text.
        assert "    " in line and line.index("    ") < line.find("line"), (
            f"reasoning line not indented by _STREAM_PAD: {line!r}"
        )


def test_flushed_partial_line_on_close_is_indented(monkeypatch):
    import cli as climod

    monkeypatch.setattr(climod, "_STREAM_PAD", "    ")
    cli = _make_cli_stub()
    printed = []
    with patch("cli._cprint", side_effect=lambda s="": printed.append(s)):
        # A short trailing partial line (no newline) only flushes on close.
        cli._stream_reasoning_delta("trailing partial")
        cli._close_reasoning_box()

    flushed = [p for p in printed if "trailing partial" in p]
    assert flushed, f"trailing partial not flushed: {printed!r}"
    assert "    " in flushed[0] and flushed[0].index("    ") < flushed[0].find("trailing"), (
        f"flushed partial not indented: {flushed[0]!r}"
    )


def test_reasoning_lines_are_flush_left_by_default(monkeypatch):
    """The default pad is empty, so reasoning rows start at column 0 like answers."""
    import re
    import cli as climod

    monkeypatch.setattr(climod, "_STREAM_PAD", "")
    cli = _make_cli_stub()
    printed = []
    with patch("cli._cprint", side_effect=lambda s="": printed.append(s)):
        cli._stream_reasoning_delta("flush line\n")

    body = _reasoning_lines(printed)
    assert body, f"no reasoning body lines captured: {printed!r}"
    plain = [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in body]
    assert any(line.startswith("flush line") for line in plain), plain
    for line in plain:
        assert not line.startswith(" "), f"unexpected indent: {line!r}"
