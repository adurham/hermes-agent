"""Bordered overlay panels (approval, clarify, sudo, pickers) for the classic CLI's live region."""

from hermes_cli.cli_render import _append_blank_panel_line, _append_panel_line, _panel_cwidth
from hermes_cli.portable_glyphs import normalize_for_chrome


class _Panel:
    """Fragment accumulator for one bordered overlay panel (``(style, text)`` tuples)."""

    def __init__(self, border: str, box_width: int, title: str = "", title_style: str = ""):
        self.lines, self.border, self.width = [], border, box_width
        self._row, self._blank = _append_panel_line, _append_blank_panel_line
        # FORK: normalize sequence machinery out of the title (model-generated titles
        # reach here; clusters change painted width vs pt's model — see
        # _append_panel_line).
        title = normalize_for_chrome(title)
        if title:
            # Title inlined into the top rule: ``╭─ Title ───╮``. The dash run is sized by
            # the title's DISPLAY width (cwidth), not ``len()``: a wide-glyph title (emoji,
            # CJK) is fewer Python characters than cells, so a ``len()``-sized rule would
            # overrun the rows below and detach the ╮ corner from the box's right edge.
            self.lines.append((border, "╭─ "))
            self.lines.append((title_style, title))
            self.lines.append((border, " " + ("─" * max(0, box_width - _panel_cwidth(title) - 3)) + "╮\n"))
        else:
            self.lines.append((border, "╭" + ("─" * box_width) + "╮\n"))

    def row(self, style: str, text: str) -> None:
        self._row(self.lines, self.border, style, text, self.width)

    def blank(self) -> None:
        self._blank(self.lines, self.border, self.width)

    def close(self) -> list:
        self.lines.append((self.border, "╰" + ("─" * self.width) + "╯\n"))
        return self.lines
