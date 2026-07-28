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


class TestNoRegisteredEmojiUsesVS16:
    """Root-cause regression guard (2026-07-28): display_cwidth() correctly
    widens VS-16 sequences to 2 cells to match prompt_toolkit-side terminals
    (iTerm2, Kitty, Terminal.app, etc.) -- but Hermes Desktop's OWN embedded
    terminal pane (apps/desktop/.../use-agent-terminal.ts) renders via
    xterm.js + @xterm/addon-unicode11, whose shipped Unicode-11 width table
    still reports these exact sequences ("\u2699\ufe0f" gear, "\u270d\ufe0f"
    writing hand, "\u2709\ufe0f" envelope, "\u26a0\ufe0f" warning, etc.) as
    width 1, not 2 (confirmed by extracting and running xterm.js's actual
    shipped algorithm standalone). That leaves the two sides of the pty
    disagreeing about cell width for the same bytes whenever a Hermes
    session (cli.py, prompt_toolkit) runs inside Hermes Desktop's own
    terminal tab -- Python reserves height assuming 2 cells, xterm.js only
    advances the cursor 1 cell, and the wrapped continuation overlaps the
    row below. This is the same failure mode as the original bug, just
    with the mismatch moved to the other side of the boundary.

    display_cwidth() can't fix this (it only affects the Python side), and
    patching xterm.js's Unicode table only fixes Hermes's own embedded
    terminal while leaving every other xterm.js-based terminal (VS Code's
    integrated terminal, Hyper) still disagreeing, plus it would risk
    affecting every OTHER program's output rendered in that same pane.
    The durable fix is to never depend on VS-16 width agreement for
    Hermes's own tool emoji in the first place: registered tool emoji use
    bare base codepoints (no VS-16), which every measured width table
    (get_cwidth, display_cwidth, and xterm.js's Unicode-11 table) agrees
    on unambiguously. This test fails if a future change reintroduces a
    VS-16 tool emoji into the registry.
    """

    def test_no_registered_tool_emoji_contains_variation_selector_16(self):
        from tools.registry import registry

        offenders = {
            name: entry.emoji
            for name, entry in registry._tools.items()
            if getattr(entry, "emoji", None) and "\ufe0f" in entry.emoji
        }
        assert not offenders, (
            f"Registered tool emoji must not contain VARIATION SELECTOR-16 "
            f"(U+FE0F) -- xterm.js's own Unicode-11 width table disagrees "
            f"with prompt_toolkit's on these sequences, causing spinner-line "
            f"wrap/overlap corruption when Hermes runs inside its own "
            f"embedded terminal pane. Use the bare base codepoint instead. "
            f"Offenders: {offenders!r}"
        )
