"""Tests for the display-side guard that strips leaked model control-token
markup (e.g. DeepSeek ``<｜DSML｜…>``) from the streamed response box.

Backend tool-call parsers (exo's DSv4 DSML parser) are the primary fix site,
but if a malformed tool-call block leaks raw control tokens into the content
stream, the CLI must never paint them. See cli._strip_special_token_markup.
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

# Fullwidth vertical bar U+FF5C — the DSML token separator.
BAR = "\uff5c"
TC_START = f"<{BAR}DSML{BAR}tool_calls>"
TC_END = f"</{BAR}DSML{BAR}tool_calls>"


class TestStripSpecialTokenMarkup:
    """Unit tests for the pure helper."""

    def test_strips_wellformed_dsml_tags(self):
        from cli import _strip_special_token_markup

        text = (
            f"{TC_START}"
            f'<{BAR}DSML{BAR}invoke name="terminal">'
            f'<{BAR}DSML{BAR}parameter name="command" string="true">ls</{BAR}DSML{BAR}parameter>'
            f"</{BAR}DSML{BAR}invoke>"
            f"{TC_END}"
        )
        out = _strip_special_token_markup(text)
        assert BAR not in out
        assert "DSML" not in out
        # the inner non-markup text survives
        assert "ls" in out

    def test_strips_malformed_block_keeps_residue(self):
        """The observed leak: model opens tool_calls then parrots a tool result
        instead of an invoke body. The DSML tokens must go; prose stays."""
        from cli import _strip_special_token_markup

        text = (
            f"{TC_START}\n"
            f"<{BAR}DSML{BAR}_cli.py | 6 ++++++ "
            '6 files changed, 37 insertions(+)", "exit_code": 0, "error": null}'
            f"{TC_END}"
        )
        out = _strip_special_token_markup(text)
        assert BAR not in out
        assert f"<{BAR}DSML" not in out
        # Human-readable residue preserved, not silently dropped.
        assert "6 files changed" in out

    def test_strips_orphan_sentinel(self):
        from cli import _strip_special_token_markup

        out = _strip_special_token_markup(f"answer text {BAR}DSML{BAR} leftover")
        assert BAR not in out
        assert "answer text" in out
        assert "leftover" in out

    def test_plain_text_untouched(self):
        from cli import _strip_special_token_markup

        plain = "The weather is 72°F and sunny. <x, y> where x > 0."
        assert _strip_special_token_markup(plain) == plain

    def test_empty_safe(self):
        from cli import _strip_special_token_markup

        assert _strip_special_token_markup("") == ""


class TestStreamDisplayGuard:
    """End-to-end: leaked DSML in the content stream must not reach _cprint."""

    def _make_cli_stub(self):
        from cli import HermesCLI

        cli = HermesCLI.__new__(HermesCLI)
        cli.show_reasoning = False
        cli._stream_buf = ""
        cli._stream_started = False
        cli._stream_box_opened = True  # box already open: skip header path
        cli._stream_prefilt = ""
        cli._in_reasoning_block = False
        cli._reasoning_box_opened = False
        cli._deferred_content = ""
        cli._stream_text_ansi = ""
        cli._in_stream_table = False
        cli._stream_table_buf = []
        cli.final_response_markdown = "off"
        return cli

    def test_leaked_dsml_not_printed(self):
        cli = self._make_cli_stub()
        captured = []
        with patch("cli._cprint", side_effect=lambda s="": captured.append(s)):
            # A malformed leaked block arriving as content, newline-terminated
            # so _emit_stream_text flushes the complete line. Mirrors the real
            # observed leak: the model parroted a git-diff result (no space
            # after the bogus tag-name token) inside a tool_calls wrapper.
            cli._emit_stream_text(
                f"{TC_START}<{BAR}DSML{BAR}_cli.py | 6 files changed{TC_END}\n"
            )
        printed = "\n".join(captured)
        assert BAR not in printed, f"DSML token leaked to display: {printed!r}"
        assert "DSML" not in printed
        assert "6 files changed" in printed
