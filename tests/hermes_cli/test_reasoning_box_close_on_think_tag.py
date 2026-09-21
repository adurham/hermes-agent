"""Regression: a </think> close tag must close the reasoning box immediately,
not leave it open until the first content token arrives.

For tag-based reasoning (<think>...</think> in the content stream), if there
is a generation pause between </think> and the first answer token, the box
should already be closed (reasoning flushed + closer drawn) during that pause.
Closing only on first content would leave an open, unterminated reasoning box
hanging on screen through the gap.

Complements test_reasoning_box_close_on_content.py, which covers the
structured reasoning_content path (no </think> tag, e.g. DeepSeek V4 via exo).
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


def _make_cli_stub():
    from cli import HermesCLI

    cli = HermesCLI.__new__(HermesCLI)
    cli.show_reasoning = True
    cli._stream_buf = ""
    cli._stream_started = False
    cli._stream_box_opened = False
    cli._stream_prefilt = ""
    cli._in_reasoning_block = True          # already inside <think>
    cli._reasoning_box_opened = True
    cli._reasoning_buf = ""
    cli._deferred_content = ""
    cli._stream_text_ansi = ""
    cli._in_stream_table = False
    cli._stream_table_buf = []
    cli.final_response_markdown = "off"
    cli.show_timestamps = False
    cli._stream_last_was_newline = True
    return cli


def test_think_close_tag_closes_box_before_content_gap():
    """</think> with NO content yet must draw the box closer immediately."""
    cli = _make_cli_stub()
    printed = []
    with patch("cli._cprint", side_effect=lambda s="": printed.append(s)):
        # Reasoning + close tag arrive, but the answer has NOT arrived yet.
        cli._stream_delta("reasoning text</think>")

    # Box must already be closed during the gap (closer drawn, state flipped).
    assert any("\u2514" in l for l in printed), (
        f"reasoning box closer must be drawn at </think>, before content; got {printed!r}"
    )
    assert not cli._reasoning_box_opened
    assert not cli._stream_box_opened          # response box not opened yet
    assert cli._deferred_content == ""

    # Now the answer arrives after the pause — streams into the response box.
    printed.clear()
    with patch("cli._cprint", side_effect=lambda s="": printed.append(s)):
        cli._stream_delta("The answer.\n")
    assert any("The answer." in l for l in printed)
    assert cli._stream_box_opened
