"""Display-width (cwidth) padding/sizing for the shared panel renderer.

Bug: the clarify / approval / sudo / secret modal panels pad rows with
``str.ljust()`` and measure with ``len()``, both of which count Python
codepoints, not terminal display cells. Wide glyphs (emoji, CJK, emoji+VS-16)
render as 2 terminal cells but are 1 Python character, so a row containing one
under-pads relative to the panel's border width and the box's right border
lands one or more columns short of the top/bottom rules — the reported
"garbled/truncated clarify panel" symptom, most commonly triggered by
LLM-emitted emoji in clarify choices (e.g. "✅ Yes" / "❌ No") or a CJK
question forwarded from a non-English-speaking user.

``_panel_box_width`` / ``_append_panel_line`` live in ``hermes_cli/cli_render.py``
(shared by all four panels and by the mixin's ``_Panel`` title rule) and now
measure/pad via ``_panel_cwidth`` (``agent.display.display_cwidth``), which also
corrects the emoji+VS-16 undercount in plain ``get_cwidth``. The
``cli.HermesCLI._panel_cwidth / _panel_ljust`` statics are covered by
``test_panel_cwidth_padding.py``; this file pins the shared renderer.
"""

from __future__ import annotations

from cli import HermesCLI, _panel_box_width, _panel_cwidth, _panel_ljust
from hermes_cli.cli_render import _append_blank_panel_line, _append_panel_line
from hermes_cli.cli_tui_mixin import CLITuiMixin


def _rendered(fragments) -> list[str]:
    return "".join(str(text) for _style, text in fragments).split("\n")[:-1]


def test_panel_ljust_pads_by_display_width_not_char_count():
    text = "✅ Yes"  # 5 Python chars, 6 display cells
    result = _panel_ljust(text, 10)
    assert result == text + "    "  # 4 spaces, not 5
    assert _panel_cwidth(result) == 10


def test_naive_str_ljust_overshoots_wide_glyph_text():
    """The exact bug: str.ljust undercounts the emoji, overpadding by one cell."""
    text = "✅ Yes"
    assert _panel_cwidth(text.ljust(10)) == 11  # naive: one cell too wide
    assert _panel_cwidth(_panel_ljust(text, 10)) == 10


def test_ascii_padding_is_identical_to_str_ljust():
    assert _panel_ljust("staging", 20) == "staging".ljust(20)


def test_no_negative_padding_when_text_already_wider():
    text = "a" * 20
    assert _panel_ljust(text, 5) == text


def test_panel_box_width_counts_wide_glyphs_as_two_cells():
    # 20 CJK chars = 40 cells (60 with the padding/borders clamp), 20 chars to len().
    assert _panel_box_width("t", ["日本語" * 10]) > _panel_box_width("t", ["x" * 20])
    assert _panel_box_width("日本語のタイトル", ["body"]) >= _panel_cwidth("日本語のタイトル")


def test_append_panel_line_row_fills_the_box_in_display_cells():
    """A row and a blank line must both span ``box_width + 2`` cells ('│ ' + inner + ' │')."""
    lines: list = []
    _append_panel_line(lines, "b", "c", "日本語", 20)
    _append_blank_panel_line(lines, "b", 20)
    rendered = "".join(str(text) for _style, text in lines).split("\n")[:-1]
    assert [_panel_cwidth(line) for line in rendered] == [22, 22], rendered
    # The naive version pad counted the row as 3 chars and overshot the box by 3 cells.
    naive_inner = "日本語".ljust(18)
    assert _panel_cwidth(naive_inner) == 21  # 6 cells of glyph + 15 spaces


def test_sudo_panel_borders_align_with_a_wide_glyph_title():
    """End-to-end: the mixin's sudo panel ( 🔐 = 1 char, 2 cells ) must render as a box.

    Before the fix the title rule was sized with ``len(title)``, so the top rule
    measured 62 cells against 61-cell rows for '🔐 Sudo Password Required'.
    """

    class _Host(CLITuiMixin):
        pass

    fragments = _Host()._render_sudo_style_panel(
        "🔐 Sudo Password Required", ["Enter password below (hidden), or press Enter to skip"])
    lines = _rendered(fragments)
    widths = {HermesCLI._panel_cwidth(line) for line in lines}
    assert len(widths) == 1, [(_panel_cwidth(line), line) for line in lines]


def test_secret_panel_borders_align_with_wide_glyph_body_rows():
    class _Host(CLITuiMixin):
        pass

    fragments = _Host()._render_sudo_style_panel(
        "🔑 Skill Setup Required", ["日本語のラベル", "Enter secret below (hidden), ESC or Ctrl+C to skip"])
    lines = _rendered(fragments)
    assert len({HermesCLI._panel_cwidth(line) for line in lines}) == 1, lines


def test_clarify_panel_borders_align_with_cjk_question_and_emoji_choices():
    class _Host(CLITuiMixin):
        pass

    host = _Host()
    setattr(host, "_clarify_state", {
        "question": "日本語の質問: どちらにしますか?",
        "choices": ["✅ Yes", "❌ No"],
        "selected": 0,
    })
    setattr(host, "_clarify_freetext", False)
    lines = _rendered(host._get_clarify_display_fragments())
    assert len({HermesCLI._panel_cwidth(line) for line in lines}) == 1, lines
