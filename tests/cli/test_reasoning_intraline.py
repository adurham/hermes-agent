"""Intra-line reasoning streaming (display.intraline_streaming, default on).

Reasoning tokens are emitted sub-line via _cprint_partial (typewriter
cadence) instead of one whole line at a time, so a model that produces a
complete line only every few hundred ms doesn't visibly "step". Covers:
content preservation, the partial-flush cadence gate, soft-wrap with
re-indent, line termination, and clean close with no dropped text.
"""
import os
import re
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _make_cli_stub():
    from cli import HermesCLI

    cli = HermesCLI.__new__(HermesCLI)
    cli.show_reasoning = True
    cli.intraline_streaming = True
    cli._stream_box_opened = False
    cli._reasoning_box_opened = False
    cli._reasoning_buf = ""
    cli._reasoning_shown_this_turn = False
    cli._reasoning_line_open = False
    cli._reasoning_col = 0
    cli._reasoning_last_partial_flush = 0.0
    import threading
    cli._reasoning_lock = threading.Lock()
    cli._reasoning_last_delta_ts = 0.0
    cli._reasoning_idle_flush_secs = 0.25
    cli._deferred_content = ""
    return cli


def _visible_text(partial_calls):
    """Concatenate emitted partial fragments, strip ANSI + the pad, and turn
    the explicit newline flushes into '\n' to reconstruct what the user sees."""
    out = []
    for text, newline in partial_calls:
        out.append(_ANSI.sub("", text))
        if newline:
            out.append("\n")
    return "".join(out)


def test_intraline_routes_through_cprint_partial():
    cli = _make_cli_stub()
    partials = []
    # Force the cadence gate open every call so partials flush immediately.
    with patch("cli._cprint", side_effect=lambda s="": None), \
         patch("cli._cprint_partial", side_effect=lambda t, newline=False: partials.append((t, newline))), \
         patch("cli.time.monotonic", side_effect=[i * 1.0 for i in range(200)]):
        cli._stream_reasoning_delta("hello ")
        cli._stream_reasoning_delta("world ")
        cli._stream_reasoning_delta("again")

    vis = _visible_text(partials)
    assert "hello world again" in vis, f"sub-line text not accumulated: {vis!r}"


def test_intraline_cadence_gate_holds_then_flushes():
    """A second delta arriving within the frame interval is NOT flushed
    immediately; it stays buffered until the gate opens."""
    cli = _make_cli_stub()
    partials = []
    # monotonic: box-open delta sees t=0 (flush), next delta sees t=0.001
    # (gated), then close.
    times = iter([0.0, 0.0, 0.001, 0.001, 100.0, 100.0, 100.0])
    with patch("cli._cprint", side_effect=lambda s="": None), \
         patch("cli._cprint_partial", side_effect=lambda t, newline=False: partials.append((t, newline))), \
         patch("cli.time.monotonic", side_effect=lambda: next(times)):
        cli._stream_reasoning_delta("first ")   # t=0 → flushes
        n_after_first = len(partials)
        cli._stream_reasoning_delta("second ")  # t=0.001 → gated, buffered
        n_after_second = len(partials)
        assert "second" in cli._reasoning_buf, "gated text should stay buffered"
        cli._close_reasoning_box()              # drains the tail

    assert n_after_second == n_after_first, "gated delta must not flush early"
    vis = _visible_text(partials)
    assert "first second" in vis, f"buffered tail not drained on close: {vis!r}"


def test_intraline_complete_line_terminates_with_newline():
    cli = _make_cli_stub()
    partials = []
    with patch("cli._cprint", side_effect=lambda s="": None), \
         patch("cli._cprint_partial", side_effect=lambda t, newline=False: partials.append((t, newline))), \
         patch("cli.time.monotonic", side_effect=[i * 1.0 for i in range(200)]):
        cli._stream_reasoning_delta("line one\nline two\n")

    # Two explicit newline terminations for two complete lines.
    newlines = [p for p in partials if p[1]]
    assert len(newlines) == 2, f"expected 2 line terminations: {partials!r}"
    vis = _visible_text(partials)
    assert "line one\n" in vis and "line two\n" in vis


def test_intraline_soft_wraps_and_reindents():
    from cli import _STREAM_PAD

    cli = _make_cli_stub()
    partials = []
    inner = 19  # narrow wrap width so wrapping engages
    # Reasoning + response now share the configurable _stream_wrap_width().
    with patch("cli._stream_wrap_width", return_value=inner), \
         patch("cli._cprint", side_effect=lambda s="": None), \
         patch("cli._cprint_partial", side_effect=lambda t, newline=False: partials.append((t, newline))), \
         patch("cli.time.monotonic", side_effect=[i * 1.0 for i in range(500)]):
        cli._stream_reasoning_delta("aaaa bbbb cccc dddd eeee ffff gggg\n")

    vis = _visible_text(partials)
    content_lines = [ln for ln in vis.split("\n") if ln.strip()]
    assert len(content_lines) > 1, f"long line should wrap: {content_lines!r}"
    for ln in content_lines:
        # Strip leading pad before measuring.
        body = ln[len(_STREAM_PAD):] if ln.startswith(_STREAM_PAD) else ln
        assert len(body) <= inner + 1, f"wrapped row too wide ({len(body)}): {ln!r}"


def test_intraline_no_content_loss_on_close():
    cli = _make_cli_stub()
    partials = []
    with patch("cli._cprint", side_effect=lambda s="": None), \
         patch("cli._cprint_partial", side_effect=lambda t, newline=False: partials.append((t, newline))), \
         patch("cli.time.monotonic", side_effect=[i * 1.0 for i in range(200)]):
        # Mix of complete line + trailing partial with no newline.
        cli._stream_reasoning_delta("done thinking about it\nand the tail here")
        cli._close_reasoning_box()

    vis = _visible_text(partials)
    assert "done thinking about it" in vis
    assert "and the tail here" in vis, f"trailing partial lost on close: {vis!r}"
    assert not cli._reasoning_box_opened
    assert cli._reasoning_buf == ""


def test_intraline_disabled_uses_legacy_path():
    """With the flag off, emission goes through _cprint (whole lines), not
    _cprint_partial."""
    cli = _make_cli_stub()
    cli.intraline_streaming = False
    full, partial = [], []
    with patch("cli._cprint", side_effect=lambda s="": full.append(s)), \
         patch("cli._cprint_partial", side_effect=lambda t, newline=False: partial.append(t)):
        cli._stream_reasoning_delta("complete legacy line\n")

    assert any("complete legacy line" in s for s in full), full
    assert partial == [], "legacy path must not use _cprint_partial"
