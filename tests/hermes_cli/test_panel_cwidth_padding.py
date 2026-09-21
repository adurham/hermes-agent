"""Tests for cwidth-aware panel padding (cli.HermesCLI._panel_ljust / _panel_cwidth).

Bug: the clarify / approval / sudo / secret modal panels padded row content
with plain ``str.ljust()``, which counts Python codepoints, not terminal
display cells. Wide glyphs (emoji, CJK, box-drawing characters) render as 2
terminal cells but are 1 Python character, so a row containing one undercounts
its real width by however many wide glyphs it has. Because the panel's top/
bottom border width is computed independently (via ``_panel_box_width``), an
under-padded content row's right border lands short of where the border rules
were drawn -- visually shifting/clipping that row relative to its neighbors.
This is the reported "garbled/truncated clarify panel" symptom, most commonly
triggered by LLM-emitted emoji in clarify choices (e.g. "✅ Yes" / "❌ No") or
CJK question text.

``_panel_ljust``/``_panel_cwidth`` fix this by using prompt_toolkit's
``get_cwidth`` (the same helper ``_status_bar_display_width`` already used
for the status bar) instead of ``len()``.
"""

from cli import HermesCLI


class TestPanelCwidth:
    def test_ascii_matches_len(self):
        assert HermesCLI._panel_cwidth("plain ascii text") == len("plain ascii text")

    def test_emoji_counts_as_two_cells(self):
        # A single emoji codepoint renders as 2 terminal cells.
        assert HermesCLI._panel_cwidth("✅") == 2

    def test_cjk_counts_as_two_cells_per_char(self):
        assert HermesCLI._panel_cwidth("日本語") == 6

    def test_mixed_ascii_and_emoji(self):
        # "✅ Yes" = emoji(2) + space(1) + Yes(3) = 6 cells, but len() = 5 chars.
        text = "✅ Yes"
        assert len(text) == 5
        assert HermesCLI._panel_cwidth(text) == 6

    def test_empty_string(self):
        assert HermesCLI._panel_cwidth("") == 0


class TestPanelLjust:
    def test_pads_ascii_like_str_ljust(self):
        assert HermesCLI._panel_ljust("abc", 10) == "abc".ljust(10)

    def test_pads_by_display_width_not_char_count(self):
        # "✅ Yes" is 6 display cells; padding to 10 cells needs 4 trailing
        # spaces, NOT 5 (which plain str.ljust(10) would add since len==5).
        text = "✅ Yes"
        result = HermesCLI._panel_ljust(text, 10)
        assert result == text + "    "  # 4 spaces, not 5
        assert HermesCLI._panel_cwidth(result) == 10

    def test_str_ljust_would_overpad_wide_glyph_text(self):
        """Regression guard: demonstrates the exact bug being fixed.

        Naive str.ljust() undercounts an emoji's cell width, so it pads with
        ONE TOO MANY spaces relative to the fixed cwidth-aware version --
        the row then renders one cell too wide, pushing the right border out
        of alignment with neighboring rows (the reported garbling).
        """
        text = "✅ Yes"
        naive = text.ljust(10)  # pads assuming len()==5, adds 5 spaces
        fixed = HermesCLI._panel_ljust(text, 10)  # adds 4 spaces (cwidth==6)
        assert len(naive) != len(fixed)
        assert HermesCLI._panel_cwidth(naive) != 10  # naive overshoots the target
        assert HermesCLI._panel_cwidth(fixed) == 10  # fixed hits the target exactly

    def test_no_negative_padding_when_text_already_wider(self):
        # Text wider than inner_width (shouldn't happen in practice given
        # upstream wrapping, but must not raise or truncate).
        text = "a" * 20
        result = HermesCLI._panel_ljust(text, 5)
        assert result == text  # no padding added, text unchanged

    def test_pure_ascii_choice_unaffected(self):
        # No wide glyphs -> identical to str.ljust, confirming no regression
        # for the common case (plain-text clarify choices).
        text = "staging"
        assert HermesCLI._panel_ljust(text, 20) == text.ljust(20)


class TestPanelBoxWidthUsesCwidth:
    """The three _panel_box_width closures (clarify/approval/sudo panels)
    must size using cwidth, not len(), so a wide-glyph title/content line
    doesn't compute a box narrower than what it actually needs."""

    def test_panel_cwidth_static_method_exists_and_is_callable(self):
        # Smoke test that the shared helper is reachable the way the three
        # panel closures in cli.py call it (HermesCLI._panel_cwidth(...)).
        assert callable(HermesCLI._panel_cwidth)
        assert HermesCLI._panel_cwidth("🧭 consult") == len("🧭 consult") + 1
