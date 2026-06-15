"""Reasoning box closes at the deterministic reasoning→content transition.

Providers like exo/DeepSeek-V4 emit a whitespace-only ("\\n\\n") content delta
the instant reasoning ends — ~1s before the tool-call chunk. _fire_stream_delta
lstrips that to "" and returns early, so it never reaches the display; the
reasoning box used to stay open until the tool call fired (visible lag before
the bottom border). The fix fires content_started_callback BEFORE the strip,
on the first content delta of a model call, which the CLI wires to close the
reasoning box.
"""
import os
import sys
import threading
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


def _make_cli_stub():
    from cli import HermesCLI

    cli = HermesCLI.__new__(HermesCLI)
    cli.show_reasoning = True
    cli.intraline_streaming = False
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


def _make_agent_stub(cli):
    import run_agent

    ag = run_agent.AIAgent.__new__(run_agent.AIAgent)
    ag._stream_needs_break = False
    ag._stream_think_scrubber = None
    ag._stream_context_scrubber = None
    ag._current_streamed_assistant_text = ""
    ag._content_started_fired = False
    ag.stream_delta_callback = cli._stream_delta
    ag._stream_callback = None
    ag._record_streamed_assistant_text = lambda t: None
    ag._strip_think_blocks = lambda t: t
    ag.content_started_callback = cli._on_content_started
    return ag


def _border_count(emitted):
    return sum(1 for e in emitted if isinstance(e, str) and "└" in e)


def test_whitespace_content_delta_closes_reasoning_box():
    cli = _make_cli_stub()
    ag = _make_agent_stub(cli)
    emitted = []
    with patch("cli._cprint", side_effect=lambda s="": emitted.append(s)), \
         patch("cli._cprint_partial", side_effect=lambda t, newline=False: emitted.append(("p", t, newline))), \
         patch("cli._pt_print", side_effect=lambda *a, **k: None):
        # Reasoning ends with a trailing partial (no newline).
        cli._stream_reasoning_delta("Let me check. I'll call the tool now")
        assert cli._reasoning_box_opened
        assert _border_count(emitted) == 0  # border not drawn yet

        # exo's deterministic transition: first content delta is "\n\n".
        ag._fire_stream_delta("\n\n")

    assert not cli._reasoning_box_opened, "box should close on content transition"
    assert _border_count(emitted) == 1, "bottom border should be drawn"
    # The trailing partial must have been flushed (no lost text).
    assert any(isinstance(e, str) and "call the tool now" in e for e in emitted)
    # No blank response line leaked to the display.
    blanks = [e for e in emitted if isinstance(e, str) and e.strip() == ""
              and "└" not in e and "┌" not in e]
    assert blanks == [], f"blank response line leaked: {blanks!r}"


def test_content_started_fires_once_per_model_call():
    cli = _make_cli_stub()
    ag = _make_agent_stub(cli)
    calls = []
    ag.content_started_callback = lambda: calls.append(1)
    with patch("cli._pt_print", side_effect=lambda *a, **k: None):
        ag._fire_stream_delta("\n\n")     # first content → fires
        ag._fire_stream_delta("hello ")   # subsequent → must NOT refire
        ag._fire_stream_delta("world")
    assert len(calls) == 1, f"content_started must fire once per call: {calls!r}"


def test_real_content_delta_also_closes_box():
    """Non-whitespace first content also closes the box (and prints)."""
    cli = _make_cli_stub()
    ag = _make_agent_stub(cli)
    emitted = []
    with patch("cli._cprint", side_effect=lambda s="": emitted.append(s)), \
         patch("cli._cprint_partial", side_effect=lambda t, newline=False: emitted.append(("p", t, newline))), \
         patch("cli._pt_print", side_effect=lambda *a, **k: None):
        cli._stream_reasoning_delta("thinking done")
        ag._fire_stream_delta("Here is the answer.")
    assert not cli._reasoning_box_opened
    assert _border_count(emitted) == 1


def test_fired_flag_rearms_after_stream_finalize():
    """After the stream finalizer re-arms the flag, the next model call's
    first content delta fires the signal again (multi-round tool turns)."""
    cli = _make_cli_stub()
    ag = _make_agent_stub(cli)
    calls = []
    ag.content_started_callback = lambda: calls.append(1)
    with patch("cli._pt_print", side_effect=lambda *a, **k: None):
        ag._fire_stream_delta("\n\n")          # round 1 fires
        # Simulate the stream finalizer re-arming between rounds.
        ag._content_started_fired = False
        ag._fire_stream_delta("\n\n")          # round 2 fires again
    assert len(calls) == 2, f"expected re-arm across model calls: {calls!r}"
