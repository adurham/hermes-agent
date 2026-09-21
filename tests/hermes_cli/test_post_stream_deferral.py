"""Tests for _emit_or_defer_post_stream / _flush_stream message deferral.

Bug repro (the screenshot bug): user hits Enter while the agent is mid-stream
inside an open response box.  The "Queued for the next turn" confirmation
gets printed by the prompt_toolkit UI thread DIRECTLY into the open box,
visually interleaved with streamed body lines (looks like the box is
broken in half).

Fix: confirmations emitted while ``_stream_box_opened`` is True must be
deferred and printed AFTER ``_flush_stream`` closes the box.

These tests exercise the deferral path with a minimal CLI stub so the
behavior is pinned independent of the prompt_toolkit UI thread.
"""
from __future__ import annotations

import os
import sys
import threading
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pytest


def _make_cli_stub():
    """Minimal HermesCLI stub with the deferral attrs initialised."""
    from cli import HermesCLI

    cli = HermesCLI.__new__(HermesCLI)
    cli._stream_buf = ""
    cli._stream_started = False
    cli._stream_box_opened = False
    cli._stream_drained = False
    cli._stream_text_ansi = ""
    cli._stream_prefilt = ""
    cli._in_reasoning_block = False
    cli._post_stream_messages = []
    cli._post_stream_lock = threading.Lock()
    return cli


def test_emit_or_defer_prints_directly_when_no_box_open():
    """No box → message goes straight to _cprint, never queued."""
    cli = _make_cli_stub()
    with patch("cli._cprint") as cprint:
        cli._emit_or_defer_post_stream("  Queued: hello")
    cprint.assert_called_once_with("  Queued: hello")
    assert cli._post_stream_messages == []


def test_emit_or_defer_defers_while_box_open():
    """Box open → message stashed, NOT printed."""
    cli = _make_cli_stub()
    cli._stream_box_opened = True
    with patch("cli._cprint") as cprint:
        cli._emit_or_defer_post_stream("  Queued for the next turn: foo")
    cprint.assert_not_called()
    assert cli._post_stream_messages == ["  Queued for the next turn: foo"]


def test_emit_or_defer_prints_directly_after_drain():
    """Once _stream_drained is True (post-flush), bypass the queue.

    Without this, a confirmation that arrives BETWEEN _flush_stream's
    drain and the next turn's _reset_stream_state would be silently
    swallowed (queued into a list nothing will drain again).
    """
    cli = _make_cli_stub()
    cli._stream_box_opened = True   # box was opened during stream
    cli._stream_drained = True       # but flush has already drained
    with patch("cli._cprint") as cprint:
        cli._emit_or_defer_post_stream("  Queued: late arrival")
    cprint.assert_called_once_with("  Queued: late arrival")
    assert cli._post_stream_messages == []


def test_flush_stream_drains_deferred_messages_after_closing_box():
    """_flush_stream closes the ╰─╯ first, THEN prints deferred msgs.

    Visual ordering must be: streamed body lines → ╰───╯ closer →
    "Queued for the next turn" line.  A direct mid-stream print would
    have shown the queued line BETWEEN body lines, breaking the frame.
    """
    cli = _make_cli_stub()
    cli._stream_box_opened = True
    # Pre-populate as if the user pressed Enter mid-stream
    cli._post_stream_messages.append("  Queued for the next turn: bar")

    printed: list[str] = []

    def fake_cprint(text):
        printed.append(text)

    with patch("cli._cprint", side_effect=fake_cprint):
        cli._flush_stream()

    # Last printed line must be the deferred message; the closing ╰
    # must appear before it.
    assert any("╰" in line for line in printed), f"missing closer in {printed!r}"
    closer_idx = next(i for i, line in enumerate(printed) if "╰" in line)
    msg_idx = printed.index("  Queued for the next turn: bar")
    assert closer_idx < msg_idx, (
        f"closer must precede deferred message; got {printed!r}"
    )
    # Drain leaves the queue empty and marks the stream drained.
    assert cli._post_stream_messages == []
    assert cli._stream_drained is True
