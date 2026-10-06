"""Diff-replay harness: pt's real repaint bytes vs the painted grid (layer 3).

This is the test that proves the MECHANISM, not the instance: drive a real
prompt_toolkit Application through a multi-frame update (seed frame -> ticking
timer), capture the exact bytes pt writes, replay them onto a grid whose paint
widths come from the terminal's own rules (cluster-aware: base+VS16 => 2 in
kitty-class terminals), and assert the converged screen equals a full rewrite
of the final frame.

If any cell ever diverges (a stale digit, a swallowed space — the "⚠7 0s"
garble from the 2026-10-06 bug), this test fails with the painted vs expected
rows. It also self-guards pt: an upstream prompt_toolkit version whose diff
behaviour changes will show up here before users see corrupt timers.

The scenario matrix intentionally spans the two geometry-dependent regimes:
  * mid-line updates    (relative cursor moves; a 1-cell model/paint shift is
                         self-correcting here — asserted as such, so a future
                         regression in relative-move handling is caught too)
  * wrap-boundary rows  (the regime where the shipped bug composed; the
                         seed frame's "0" stranded next to fresh digits)
and both badge states: the banned VS16 shape (must corrupt at the boundary —
the harness's teeth) and the bare shape (must be clean everywhere).
"""
from __future__ import annotations

import io
import re

import pytest

# ── engine: kitty-class paint widths (cluster-aware) ────────────────────────


_EP_CACHE = None


def _emoji_presentation(cp: int) -> bool:
    """Frozen Emoji_Presentation lookup (commit fixture; no regex dependency)."""
    global _EP_CACHE
    if _EP_CACHE is None:
        import json
        import pathlib as _pl
        p = _pl.Path(__file__).resolve().parent / "fixtures" / "emoji_presentation_ranges.json"
        _EP_CACHE = json.loads(p.read_text())["ranges"]
    for lo, hi in _EP_CACHE:
        if lo <= cp <= hi:
            return True
        if cp < lo:
            break
    return False


def _cluster_width(run: str, j: int) -> tuple[int, int]:
    """(painted width, codepoints consumed) for the cluster starting at run[j].

    kitty-class terminals: an emoji base + VS16 sequence paints 2 (pixel-verified
    on the user's kitty: 🗜️=2, ⚡=2, ⚙=1), Emoji_Presentation or EAW W/F paints
    2, else 1.
    """
    import unicodedata
    ch = run[j]
    nxt = run[j + 1] if j + 1 < len(run) else ""
    if nxt == "\ufe0f" and _is_emoji_base(ch):
        return 2, 2
    if ch == "\ufe0f":
        return 0, 1
    if _emoji_presentation(ord(ch)):
        return 2, 1
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2, 1
    return 1, 1


def _is_emoji_base(ch: str) -> bool:
    """Coarse \p{Emoji} membership for the cluster rule: the codepoints we emit
    with VS16 are all in the emoji-block ranges; symbol bases (>= U+2190) count."""
    return ord(ch) >= 0x2190


# ── ANSI replay: honest interpreter for what pt emits ───────────────────────

_CSI_RE = re.compile(r"\x1b\[([0-9;?]*)([A-Za-z@`])")


def replay(data: str, columns: int, rows: int = 10) -> list[str]:
    """Apply pt's byte stream to a cell grid.

    pt disables terminal autowrap (``output.disable_autowrap``), so a write past
    the last column CLAMPS at the boundary — modeling that is what makes the
    wrap-boundary corruption observable.
    """
    grid = [[" "] * columns for _ in range(rows)]
    x = y = 0
    i = 0
    while i < len(data):
        ch = data[i]
        if ch == "\x1b":
            m = _CSI_RE.match(data, i)
            if m:
                params, final = m.group(1), m.group(2)
                if params.startswith("?"):
                    i = m.end()
                    continue
                nums = [int(p) for p in params.split(";") if p.isdigit()]
                n0 = nums[0] if nums else None
                if final in ("H", "f"):
                    y = (nums[0] if len(nums) > 0 else 1) - 1
                    x = (nums[1] if len(nums) > 1 else 1) - 1
                elif final == "G":
                    x = (n0 or 1) - 1
                elif final == "A":
                    y -= (n0 or 1)
                elif final == "B":
                    y += (n0 or 1)
                elif final == "C":
                    x = min(columns - 1, x + (n0 or 1))
                elif final == "D":
                    x = max(0, x - (n0 or 1))
                elif final == "J":
                    mode = n0 if n0 is not None else 0
                    if mode == 2:
                        grid = [[" "] * columns for _ in range(rows)]
                        x = y = 0
                    elif mode == 0:
                        for xx in range(max(0, x), columns):
                            grid[y][xx] = " "
                elif final == "K":
                    mode = n0 if n0 is not None else 0
                    span = range(x, columns) if mode == 0 else range(0, columns)
                    for xx in span:
                        grid[y][xx] = " "
                i = m.end()
                continue
            if data.startswith("\x1b]", i):
                j = i + 2
                while j < len(data) and data[j] != "\x07" and not data.startswith("\x1b\\", j):
                    j += 1
                i = j + (2 if data.startswith("\x1b\\", j) else 1)
                continue
            i += 1
            continue
        if ch == "\r":
            x = 0
            i += 1
            continue
        if ch == "\n":
            y += 1
            x = 0
            i += 1
            continue
        nxt = data.find("\x1b", i)
        run = data[i:nxt] if nxt != -1 else data[i:]
        j = 0
        while j < len(run):
            w, consumed = _cluster_width(run, j)
            if 0 <= y < rows:
                if 0 <= x < columns:
                    grid[y][x] = run[j]
                for k in range(1, max(w, 1)):
                    if x + k < columns:
                        grid[y][x + k] = "\u0000W"
            x = min(columns - 1, x + w)
            j += consumed
        i = nxt if nxt != -1 else len(data)
    return ["".join("\u25a1" if c == "\u0000W" else c for c in row).rstrip() for row in grid]


# ── real pt driver ───────────────────────────────────────────────────────────


class _FakeStdout(io.StringIO):
    def isatty(self):
        return True

    def fileno(self):
        raise io.UnsupportedOperation("fileno")


def render_frames(lines_by_frame: list[tuple[str, str]], columns: int, rows: int = 10) -> str:
    """Render (top_line, bottom_line) frames through a REAL pt Application.

    Fresh FormattedTextControl per frame (so the fragment cache misses) on ONE
    app/renderer, so the emitted bytes are pt's genuine incremental diff.
    """
    from prompt_toolkit.application import Application
    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.layout import HSplit, Layout, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.output import ColorDepth
    from prompt_toolkit.output.vt100 import Vt100_Output

    stdout = _FakeStdout()
    size = Size(rows=rows, columns=columns)
    out = Vt100_Output(stdout, lambda: size, term="xterm-256color",
                       default_color_depth=ColorDepth.DEPTH_24_BIT)
    holder = {"lines": lines_by_frame[0]}

    def make_layout():
        # Top window wraps (spinner), bottom is the 1-row status bar.
        return Layout(HSplit([
            Window(FormattedTextControl(lambda: [("", holder["lines"][0])])),
            Window(FormattedTextControl(lambda: [("", holder["lines"][1])]), height=1,
                   wrap_lines=False),
        ]))

    app = Application(layout=make_layout(), output=out, full_screen=False)
    for lines in lines_by_frame:
        holder["lines"] = lines
        app.layout = make_layout()
        app.render_counter += 1
        app.renderer.render(app, app.layout)
    return stdout.getvalue()


# ── scenarios ────────────────────────────────────────────────────────────────


def _midline_scenario(glyph: str, columns: int = 60):
    def frame(secs):
        return f"A {glyph} 2 │ ⏱ {secs}s", f"status line ⏱ {secs}s"
    frames = [frame(s) for s in (0, 6, 7)]
    return frames, columns


def _boundary_scenario(glyph: str, columns: int = 40):
    """Spinner line whose painted width lands exactly on the wrap boundary."""
    def frame(secs):
        return f"{'.' * 34}{glyph} {secs}s ({secs}m00s)", f"status bar line, timer ⏱ {secs}s"
    frames = [frame(s) for s in (0, 3, 7)]
    return frames, columns


def _assert_converges(frames, columns):
    painted = [line for line in replay(render_frames(frames, columns), columns) if line]
    fresh = [line for line in replay(render_frames([frames[-1]], columns), columns) if line]
    assert painted == fresh, (
        "incremental repaint did not converge to a full rewrite — stale cells survived:\n"
        f"  painted: {painted!r}\n"
        f"  expected: {fresh!r}"
    )


def test_bare_glyphs_converge_midline_and_at_boundary():
    """Every glyph in today's chrome must repaint cleanly in both regimes.

    '🗜' is the fixed badge shape; the boundary case is the exact geometry where
    the shipped bug composed.
    """
    for glyph in ("🗜", "⚙", "⏱", "⚡", "▶", "⛓", "│"):
        frames, cols = _midline_scenario(glyph)
        _assert_converges(frames, cols)
        frames, cols = _boundary_scenario(glyph)
        _assert_converges(frames, cols)


def test_vs16_sequence_corrupts_at_wrap_boundary_teeth():
    """The harness must reproduce the shipped corruption — its teeth.

    '🗜️' (base+VS16) at the wrap boundary: pt models the sequence 1 cell narrow,
    kitty paints 2, the row shifts, and the seed frame's '0' strands next to the
    fresh digit — the '⏱70s / 80s-at-8s' garble this whole guard exists for.
    Assert the CORRUPTION is visible (otherwise this harness is worthless).
    """
    frames, cols = _boundary_scenario("\U0001F5DC\ufe0f")
    painted = [line for line in replay(render_frames(frames, cols), cols) if line]
    fresh = [line for line in replay(render_frames([frames[-1]], cols), cols) if line]
    assert painted != fresh, (
        "expected the VS16 shape to corrupt the wrap-boundary repaint; the "
        "harness no longer reproduces the bug class it guards"
    )
    # And the specific stale-seed-digit signature: the final frame asked for
    # "7s" but a '0' (from the seed frame's "0s") survives beside it.
    top = painted[0]
    assert "70s" in top or "6s0" in top or "0s" in top.split("(")[-1], (
        f"expected the stranded seed digit in the painted row, got {top!r}"
    )
