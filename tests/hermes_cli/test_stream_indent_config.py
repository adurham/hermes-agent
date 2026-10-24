"""Streamed response indent is configurable via ``display.response_indent_width``.

Upstream default was a 4-space ``_STREAM_PAD`` indent. July 2026 changed the
default to 0 (flush-left) because every mouse-copied line carried leading
whitespace, breaking paste. This fork tracked 4 from 2026-08-10 (a visually
framed response box) and returned to upstream's flush-left default on
2026-10-24 so the transcript matches stock Hermes. The setting stays
configurable — ``/copy`` remains the clean-copy path regardless of the
on-screen indent, since it writes the original message text via the native
clipboard rather than scraping the rendered box.
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

    emitted = []
    monkeypatch.setattr(climod, "_cprint", lambda s: emitted.append(s))
    monkeypatch.setattr(climod, "_terminal_width_for_streaming", lambda: 74)
    return cli, emitted


def test_stream_pad_default_is_flush_left(monkeypatch):
    """Unset config == upstream's post-July-2026 flush-left rendering."""
    import cli as climod

    monkeypatch.delitem(climod.CLI_CONFIG["display"], "response_indent_width", raising=False)
    assert climod._load_stream_pad() == ""


def test_load_stream_pad_honors_config_override(monkeypatch):
    """display.response_indent_width drives _STREAM_PAD's width."""
    import cli as climod

    monkeypatch.setitem(climod.CLI_CONFIG["display"], "response_indent_width", 0)
    assert climod._load_stream_pad() == ""

    monkeypatch.setitem(climod.CLI_CONFIG["display"], "response_indent_width", 2)
    assert climod._load_stream_pad() == "  "

    monkeypatch.setitem(climod.CLI_CONFIG["display"], "response_indent_width", 4)
    assert climod._load_stream_pad() == "    "


def test_load_stream_pad_falls_back_on_bad_config(monkeypatch):
    """A non-numeric/garbage config value falls back to the flush-left default
    rather than raising and taking down CLI startup."""
    import cli as climod

    monkeypatch.setitem(climod.CLI_CONFIG["display"], "response_indent_width", "nonsense")
    assert climod._load_stream_pad() == ""


def test_streamed_content_lines_are_flush_left_by_default(cli_stub, monkeypatch):
    import cli as climod

    monkeypatch.setattr(climod, "_STREAM_PAD", "")
    cli, emitted = cli_stub
    cli._stream_delta("First streamed line of text.\nSecond streamed line.\n")
    cli._flush_stream()
    content = [
        _strip_ansi(e)
        for e in emitted
        if "streamed line" in _strip_ansi(e)
    ]
    assert content, "no content lines captured"
    for line in content:
        assert not line.startswith(" "), f"unexpected indent prefix: {line!r}"


def test_streamed_content_lines_carry_the_configured_indent(cli_stub, monkeypatch):
    import cli as climod

    monkeypatch.setattr(climod, "_STREAM_PAD", "    ")
    cli, emitted = cli_stub
    cli._stream_delta("First streamed line of text.\nSecond streamed line.\n")
    cli._flush_stream()
    content = [
        _strip_ansi(e)
        for e in emitted
        if "streamed line" in _strip_ansi(e)
    ]
    assert content, "no content lines captured"
    for line in content:
        assert line.startswith("    "), f"expected 4-space indent prefix: {line!r}"


def test_intentional_markdown_indentation_is_preserved_after_pad(cli_stub, monkeypatch):
    """Markdown-structural indentation (nested list items) must still be
    visible AFTER the configured pad prefix, not collapsed into it."""
    import cli as climod

    monkeypatch.setattr(climod, "_STREAM_PAD", "    ")
    cli, emitted = cli_stub
    cli._stream_delta("- item\n  - nested item\n")
    cli._flush_stream()
    plain = [_strip_ansi(e) for e in emitted]
    assert any(line == "    - item" for line in plain)
    assert any(line == "      - nested item" for line in plain)
