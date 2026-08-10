"""Streamed response/reasoning boxes hard-wrap to a symmetric margin.

2026-08-10: after making the left indent configurable
(``display.response_indent_width``, see test_stream_indent_config.py),
the box looked lopsided — a left margin with nothing matching on the
right (the terminal's own soft-wrap just ran lines flush to the wall).
Per explicit user preference, completed logical lines are now
hard-wrapped to the box's text width before printing, so the box shows
a blank right margin matching the left indent. This knowingly
reintroduces the mouse-copy/paste rough edge the July 2026 change
removed (a hard-wrapped paragraph pastes as several short lines instead
of one line the terminal can rejoin) — accepted tradeoff; ``/copy``
still writes the original unwrapped text via the native clipboard
regardless of on-screen wrapping.

Pre-completion buffering behavior (an in-flight line must NOT be
wrapped/printed before a real newline or the sentence-boundary early
flush) is unchanged and covered separately in
test_stream_partial_line_flush.py.
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


def _strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


@pytest.fixture
def cli_stub(monkeypatch):
    from cli import HermesCLI
    import cli as climod

    cli = HermesCLI.__new__(HermesCLI)
    cli.show_reasoning = False
    cli.final_response_markdown = "raw"
    cli.show_timestamps = False
    cli._reset_stream_state()
    cli._spinner_text = ""
    cli._invalidate = lambda *a, **kw: None

    emitted = []
    monkeypatch.setattr(climod, "_cprint", lambda s: emitted.append(s))
    monkeypatch.setattr(climod, "_terminal_width_for_streaming", lambda: 30)
    return cli, emitted


def test_wrap_stream_line_respects_configured_width(monkeypatch):
    import cli as climod

    monkeypatch.setattr(climod, "_terminal_width_for_streaming", lambda: 20)
    lines = climod._wrap_stream_line("one two three four five six seven eight")
    assert len(lines) > 1
    for line in lines:
        assert len(line) <= 20


def test_wrap_stream_line_empty_returns_single_blank_line():
    import cli as climod

    assert climod._wrap_stream_line("") == [""]


def test_completed_long_line_is_hard_wrapped_to_multiple_prints(cli_stub):
    cli, emitted = cli_stub
    long_line = " ".join(f"word{i}" for i in range(20)) + "\n"
    cli._stream_delta(long_line)
    plain = [_strip_ansi(e) for e in emitted if "word0" in _strip_ansi(e) or "word19" in _strip_ansi(e)]
    # word0 and word19 must land on DIFFERENT printed lines once wrapped.
    assert len(plain) >= 2
    assert not any("word0" in p and "word19" in p for p in plain)


def test_wrapped_lines_still_carry_the_left_indent(cli_stub):
    cli, emitted = cli_stub
    long_line = " ".join(f"word{i}" for i in range(20)) + "\n"
    cli._stream_delta(long_line)
    content = [_strip_ansi(e) for e in emitted if "word" in _strip_ansi(e)]
    assert content, "no content lines captured"
    for line in content:
        assert line.startswith("    "), f"expected 4-space indent prefix: {line!r}"


def test_short_line_not_split(cli_stub):
    cli, emitted = cli_stub
    cli._stream_delta("short line\n")
    plain = [_strip_ansi(e) for e in emitted if "short line" in _strip_ansi(e)]
    assert len(plain) == 1


def test_table_rows_are_not_word_wrapped(cli_stub, monkeypatch):
    """Table rows are column-aligned by realign_markdown_tables and must
    print verbatim (wrap=False) — word-wrapping would break padding."""
    import cli as climod

    # Use a wide-enough budget that realign_markdown_tables keeps the
    # horizontal layout instead of falling back to vertical key-value
    # rendering (its own separate, correct behavior for narrow widths —
    # not the word-wrap regression this test is guarding against).
    monkeypatch.setattr(climod, "_terminal_width_for_streaming", lambda: 200)
    cli, emitted = cli_stub
    header = "| " + " | ".join(f"col{i}" for i in range(8)) + " |"
    divider = "| " + " | ".join("---" for _ in range(8)) + " |"
    row = "| " + " | ".join(f"val{i}" for i in range(8)) + " |"
    cli._stream_delta(f"{header}\n{divider}\n{row}\n")
    cli._flush_stream()
    plain = [_strip_ansi(e) for e in emitted if "val0" in _strip_ansi(e)]
    # The full row (all 8 values) must land on ONE printed line, not split
    # across multiple wrapped lines.
    assert any("val7" in p for p in plain), "table row was wrapped instead of printed whole"


def test_reasoning_box_lines_are_also_symmetrically_wrapped(cli_stub):
    cli, emitted = cli_stub
    long_line = " ".join(f"reason{i}" for i in range(20)) + "\n"
    cli._stream_reasoning_delta(long_line)
    content = [_strip_ansi(e) for e in emitted if "reason0" in _strip_ansi(e) or "reason19" in _strip_ansi(e)]
    assert len(content) >= 2
    assert not any("reason0" in c and "reason19" in c for c in content)
