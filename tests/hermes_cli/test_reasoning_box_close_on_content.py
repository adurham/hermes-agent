"""Regression: structured reasoning_content (no </think> close tag) must not
hang the reasoning box open until end-of-stream.

DeepSeek V4 via exo streams reasoning as structured reasoning_content deltas
routed to _stream_reasoning_delta — there is no </think> tag in the content
stream. Previously _emit_stream_text deferred ALL content into
_deferred_content while the reasoning box stayed open, and the box only closed
at _flush_stream. Symptom: the last reasoning line and the entire response
printed together at end-of-stream (2nd-to-last line appeared to hang).

Fix: the first content token closes the reasoning box (flush trailing
reasoning line + closer), then content streams live.
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


def _make_cli_stub():
    from cli import HermesCLI

    cli = HermesCLI.__new__(HermesCLI)
    cli.show_reasoning = True            # reasoning display ON (the hang case)
    cli._stream_buf = ""
    cli._stream_started = False
    cli._stream_box_opened = False
    cli._stream_prefilt = ""
    cli._in_reasoning_block = False
    cli._reasoning_box_opened = False
    cli._reasoning_buf = ""
    cli._deferred_content = ""
    cli._stream_text_ansi = ""
    cli._in_stream_table = False
    cli._stream_table_buf = []
    cli.final_response_markdown = "off"
    cli.show_timestamps = False
    return cli


def test_content_closes_reasoning_box_before_response():
    """Structured reasoning, then content: the box closer (└...┘) must be
    printed BEFORE the first response line — not deferred to end-of-stream."""
    cli = _make_cli_stub()
    printed = []
    with patch("cli._cprint", side_effect=lambda s="": printed.append(s)):
        # Reasoning streamed as structured deltas (no </think> tag).
        cli._stream_reasoning_delta("Let me think about the sprint.\n")
        # A trailing PARTIAL reasoning line (no newline, <80 chars) — exactly
        # the "(DeepSeek V4 Flash) prefill optimization. Let me break it down."
        # line that hung in the report.
        cli._stream_reasoning_delta("Let me break it down.")
        assert cli._reasoning_box_opened, "reasoning box should be open"
        # First real content token arrives.
        cli._emit_stream_text("Here is the summary.\n")

    joined = "\n".join(printed)
    # The trailing partial reasoning line must have been flushed.
    assert "Let me break it down." in joined
    # The box closer must appear, and BEFORE the response text.
    closer_idx = next((i for i, l in enumerate(printed) if "\u2514" in l), -1)
    resp_idx = next((i for i, l in enumerate(printed) if "Here is the summary." in l), -1)
    assert closer_idx != -1, f"reasoning box closer missing: {printed!r}"
    assert resp_idx != -1, f"response line missing: {printed!r}"
    assert closer_idx < resp_idx, (
        f"closer must precede response (no hang); got {printed!r}"
    )
    # Box state flipped: reasoning closed, response open.
    assert not cli._reasoning_box_opened
    assert cli._stream_box_opened
    assert cli._deferred_content == ""
