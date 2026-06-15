"""Idle-flush of the reasoning trailing partial line.

Reasoning lines flush on a newline; a trailing partial (no newline) normally
waits for the box to close (content token or turn boundary). Some providers
(exo/DeepSeek-V4) go silent for ~1s+ between the last reasoning token and the
tool-call chunk — during which the partial would sit unflushed and look hung
("2nd-to-last line sits there before a tool call"). The agent poll loop calls
_maybe_idle_flush_reasoning() to push the partial once reasoning has been quiet
past the idle threshold, without abandoning line-at-a-time rendering.
"""
import os
import sys
import threading
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


def _make_cli_stub(intraline: bool):
    from cli import HermesCLI

    cli = HermesCLI.__new__(HermesCLI)
    cli.show_reasoning = True
    cli.intraline_streaming = intraline
    cli._stream_box_opened = False
    cli._reasoning_box_opened = False
    cli._reasoning_buf = ""
    cli._reasoning_shown_this_turn = False
    cli._reasoning_line_open = False
    cli._reasoning_col = 0
    cli._reasoning_last_partial_flush = 0.0
    cli._reasoning_lock = threading.Lock()
    cli._reasoning_last_delta_ts = 0.0
    cli._reasoning_idle_flush_secs = 0.25
    cli._deferred_content = ""
    return cli


def test_idle_flush_pushes_partial_legacy_mode():
    """Legacy line-at-a-time: a short partial (< 80 chars, no newline) sits
    buffered, then idle-flush emits it once reasoning goes quiet."""
    cli = _make_cli_stub(intraline=False)
    full = []
    # monotonic: delta arrives at t=10; poll checks at t=10.5 (idle 0.5 > 0.25)
    times = iter([10.0, 10.5, 10.5])
    with patch("cli._cprint", side_effect=lambda s="": full.append(s)), \
         patch("cli.time.monotonic", side_effect=lambda: next(times)):
        cli._stream_reasoning_delta("a trailing thought with no newline")
        # Nothing flushed yet (short partial, no newline).
        assert not any("trailing thought" in s for s in full), full
        n_before = len(full)
        cli._maybe_idle_flush_reasoning()

    assert any("trailing thought" in s for s in full), f"idle flush did not emit: {full!r}"
    assert len(full) > n_before
    assert cli._reasoning_buf == ""


def test_idle_flush_holds_when_not_yet_idle():
    """If reasoning is still active (delta just arrived), the partial is NOT
    flushed — we only push a line that has stopped growing."""
    cli = _make_cli_stub(intraline=False)
    full = []
    # delta at t=10; poll at t=10.1 (idle 0.1 < 0.25 threshold)
    times = iter([10.0, 10.1])
    with patch("cli._cprint", side_effect=lambda s="": full.append(s)), \
         patch("cli.time.monotonic", side_effect=lambda: next(times)):
        cli._stream_reasoning_delta("still being written")
        cli._maybe_idle_flush_reasoning()

    assert not any("still being written" in s for s in full), \
        "must not flush while reasoning is still active"
    assert "still being written" in cli._reasoning_buf


def test_idle_flush_noop_when_box_closed():
    cli = _make_cli_stub(intraline=False)
    full = []
    with patch("cli._cprint", side_effect=lambda s="": full.append(s)):
        # Box never opened.
        cli._maybe_idle_flush_reasoning()
    assert full == []


def test_idle_flush_noop_when_buffer_empty():
    cli = _make_cli_stub(intraline=False)
    full = []
    times = iter([10.0, 20.0])
    with patch("cli._cprint", side_effect=lambda s="": full.append(s)), \
         patch("cli.time.monotonic", side_effect=lambda: next(times)):
        cli._stream_reasoning_delta("line with newline\n")  # flushes whole line
        n_before = len(full)
        cli._maybe_idle_flush_reasoning()  # nothing left buffered
    assert len(full) == n_before


def test_idle_flush_intraline_mode_emits_via_partial():
    """Intraline mode: idle flush pushes the pending tail through
    _cprint_partial without closing the box."""
    cli = _make_cli_stub(intraline=True)
    partials = []
    # Feed via the cadence-gated path; force everything to buffer by gating
    # the partial flush, then idle-flush pushes it.
    times = iter([10.0, 10.0, 10.5, 10.5, 10.5])
    with patch("cli._cprint", side_effect=lambda s="": None), \
         patch("cli._cprint_partial", side_effect=lambda t, newline=False: partials.append((t, newline))), \
         patch("cli.time.monotonic", side_effect=lambda: next(times)):
        cli._stream_reasoning_delta("tok")  # opens box; partial may flush
        n_before = len(partials)
        # Stuff a tail directly to simulate a gated remainder, then idle-flush.
        cli._reasoning_buf = "pending tail"
        cli._maybe_idle_flush_reasoning()

    joined = "".join(t for t, _ in partials)
    assert "pending tail" in joined, f"intraline idle flush did not emit tail: {partials!r}"
    assert cli._reasoning_buf == ""
    assert cli._reasoning_box_opened, "idle flush must NOT close the box"


def test_idle_flush_lock_contention_skips():
    """If the worker holds the reasoning lock (actively emitting), the
    non-blocking idle flush returns without doing anything."""
    cli = _make_cli_stub(intraline=False)
    cli._reasoning_box_opened = True
    cli._reasoning_buf = "contended"
    full = []
    cli._reasoning_lock.acquire()  # simulate worker holding it
    try:
        with patch("cli._cprint", side_effect=lambda s="": full.append(s)):
            cli._maybe_idle_flush_reasoning()
    finally:
        cli._reasoning_lock.release()
    assert full == [], "must not emit while lock is held by worker"
    assert cli._reasoning_buf == "contended"
