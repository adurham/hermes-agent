"""Regression tests for agent.display.display_cwidth()'s VS-16 correction.

Bug: prompt_toolkit.utils.get_cwidth() undercounts emoji base codepoint +
VARIATION SELECTOR-16 (U+FE0F) sequences by 1 cell. U+FE0F is Unicode
category Mn (nonspacing mark), so wcwidth-family width tables assign it
width 0; but VS-16's entire purpose (UTR#51) is to force emoji (wide,
2-cell) presentation, and virtually every terminal Hermes users run
(iTerm2, Kitty, WezTerm, Terminal.app, Windows Terminal) honors that and
renders 2 cells.

Several of Hermes's own registered tool emoji are exactly this shape
(process's "⚙️", browser's "⌨️"/"◀️"/"🖼️"/"👁️"/"🖥️", file's "✍️", feishu's
"✉️", skills' "⚠️"). That 1-cell undercount, fed into a reserved
prompt_toolkit Window height or a \\r-redraw pad, comes out exactly 1
row/column short right at a wrap boundary — producing the recurring
"garbled/duplicated digit" live-timer corruption reported by the user
(e.g. a process(action="wait") duration rendering as "4m170s" instead of
"4m17s"). Two earlier fixes (HermesCLI._status_bar_display_width and
KawaiiSpinner._display_width) replaced len() with get_cwidth() for the
*aggregate string vs len()* mismatch, but both called get_cwidth directly
— this glyph-level blind spot in that "trusted" width oracle survived
both patches untouched, which is why the bug kept resurfacing.

Fix: display_cwidth() adds the missing 1 cell whenever it encounters a
bare VS-16 codepoint, regardless of what precedes it.
"""

from agent.display import display_cwidth


# Every currently-registered tool emoji that is a narrow/astral base
# codepoint + VS-16 (U+FE0F) -- i.e. the exact shape get_cwidth undercounts.
VS16_TOOL_EMOJI = [
    "\u2699\ufe0f",       # process: gear
    "\u2328\ufe0f",       # browser_type/press: keyboard
    "\u25c0\ufe0f",       # browser_back: black left-pointing triangle
    "\U0001f5bc\ufe0f",   # browser_get_images: frame with picture
    "\U0001f441\ufe0f",   # browser_vision: eye
    "\U0001f5a5\ufe0f",   # read_terminal/close_terminal: desktop computer
    "\u270d\ufe0f",       # write_file: writing hand
    "\u2709\ufe0f",       # feishu: envelope
    "\u26a0\ufe0f",       # skills warning: warning sign
]

# Tool emoji that get_cwidth already reports correctly (astral wide emoji
# with no VS-16, or already-narrow glyphs with no VS-16) -- these must be
# completely unaffected by the fix.
NON_VS16_TOOL_EMOJI = [
    "\U0001f310",  # 🌐 browser_navigate
    "\U0001f4bb",  # 💻 terminal
    "\U0001f4d6",  # 📖 read_file
    "\u2705",      # kanban complete (no VS16 variant used)
    "\u26a1",      # ⚡ default tool emoji
]


class TestVS16Undercount:
    def test_vs16_sequences_measure_as_two_cells(self):
        for seq in VS16_TOOL_EMOJI:
            assert display_cwidth(seq) == 2, f"{seq!r} should measure as 2 cells"

    def test_bare_vs16_alone_adds_one_cell(self):
        # A lone VS-16 with no preceding base still contributes its forced
        # width delta rather than silently vanishing (defensive: should
        # never occur in real registered emoji, but must not crash or
        # under-report).
        assert display_cwidth("\ufe0f") == 1

    def test_non_vs16_emoji_unaffected(self):
        from prompt_toolkit.utils import get_cwidth

        for emoji in NON_VS16_TOOL_EMOJI:
            assert display_cwidth(emoji) == get_cwidth(emoji)

    def test_ascii_text_unaffected(self):
        from prompt_toolkit.utils import get_cwidth

        text = "  wait proc_e0efad4683 280s (4m17s)"
        assert display_cwidth(text) == get_cwidth(text)

    def test_full_status_line_with_vs16_tool_emoji_measures_two_more_than_get_cwidth(self):
        from prompt_toolkit.utils import get_cwidth

        line = "  \u2699\ufe0f wait proc_e0efad4683 280s (4m17s)"
        # The gear+VS16 sequence is the only wide-glyph correction in this
        # string; the fixed measurement must be exactly 1 cell higher.
        assert display_cwidth(line) == get_cwidth(line) + 1

    def test_empty_and_none_safe(self):
        assert display_cwidth("") == 0
        assert display_cwidth(None) == 0


class TestKawaiiSpinnerUsesSharedHelper:
    """KawaiiSpinner._display_width must delegate to display_cwidth so the
    process tool's spinner frame is also protected."""

    def test_delegates_to_display_cwidth(self):
        from agent.display import KawaiiSpinner

        line = "  \u2699\ufe0f preparing process (4m17s)"
        assert KawaiiSpinner._display_width(line) == display_cwidth(line)

    def test_process_gear_frame_no_longer_undercounted(self):
        from prompt_toolkit.utils import get_cwidth
        from agent.display import KawaiiSpinner

        line = "  \u2699\ufe0f wait proc_abc123 280s (4m17s)"
        assert KawaiiSpinner._display_width(line) > get_cwidth(line)
