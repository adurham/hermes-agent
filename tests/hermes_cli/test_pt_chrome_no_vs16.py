"""Guard: prompt_toolkit-rendered chrome must never emit VARIATION SELECTOR-16.

The recurring "impossible timer" corruption (``⏱70s`` beside a stopwatch at
~7 s; ``80s`` when the true elapsed was 8 s — user report 2026-10-06,
recurrence N of this class) was composed on the terminal GRID, not in any
formatter. Mechanism, proven by byte-level replay + on-screen metrology:

* A VS-16 sequence (``"🗜️"`` = U+1F5DC + U+FE0F) paints **2 cells** in
  kitty/iTerm2/Terminal.app but scores **1 cell** in prompt_toolkit's wcwidth
  model (and 2 vs 1 the other way on xterm.js's Unicode-11 table).
* That model-vs-paint divergence desyncs pt's diff-repaint bookkeeping for
  every cell to the RIGHT of the glyph: cells whose *model* content is
  unchanged are never rewritten, so stale cells survive beside fresh writes.
* The stopwatch field sits right of the ``🗜️`` compressions badge, and the
  turn's first frame seeds ``"⏲ 0s"``; the stranded ``0`` then renders next
  to every single-digit second — ``8s`` displays as ``80s``, ``7s`` as
  ``70s`` — until 10s self-heals it. The historic ``4m170s`` is the same
  divergence at a wrap boundary.

The durable fix (this fork's established doctrine for its tool emoji, see
``tests/agent/test_display_cwidth_vs16.py``): emit only glyphs every width
table agrees on — bare base codepoints, no VS-16 — in every string that
feeds the prompt_toolkit grid (status bar, docks, panels).

Scrollback strings (``print`` / ``_cprint``) are exempt: they are written
raw to the terminal with no pt layout model, so a VS-16 there cannot desync
a grid.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Files whose string constants feed prompt_toolkit FormattedText controls /
# fragments (the pt grid). Extend when a new chrome builder appears.
_CHROME_FILES = [
    "hermes_cli/cli_status_bar_mixin.py",
    "hermes_cli/cli_subagent_monitor.py",
    "hermes_cli/cli_process_dock.py",
    "hermes_cli/cli_session_dock.py",
    "hermes_cli/cli_tui_mixin.py",
    "hermes_cli/cli_modal_mixin.py",
]

_PRINTISH_CALLS = {"print", "cprint", "_cprint", "_console_print", "console_print"}


def _is_printish(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in _PRINTISH_CALLS
    if isinstance(func, ast.Attribute):
        return func.attr in {"print", "cprint"}
    return False


def _string_constants(node: ast.AST):
    """All string Constant nodes under ``node`` (recurses through f-strings)."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            yield sub


def _vs16_offenders(path: Path) -> list[str]:
    """Non-docstring, non-scrollback string constants containing U+FE0F."""
    tree = ast.parse(path.read_text(encoding="utf-8"))

    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            ds = ast.get_docstring(node, clean=False)
            if ds is not None:
                docstrings.add(ds)

    # String constants that are arguments of print-ish calls = scrollback; exempt.
    scrollback_keys = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_printish(node):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    scrollback_keys.add((sub.lineno, sub.col_offset))

    offenders = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if "\ufe0f" not in node.value:
            continue
        if node.value in docstrings:
            continue
        if (node.lineno, node.col_offset) in scrollback_keys:
            continue
        # A LONE VS-16 literal (exactly "\ufe0f") is a sanitizer argument — the
        # text these files strip FROM dynamic content (``replace("\ufe0f", "")``),
        # never emitted render content (alone it paints nothing). Exempt it; the
        # offender shape is a base+VS-16 pair inside a rendered string.
        if node.value == "\ufe0f":
            continue
        offenders.append(f"{path.name}:{node.lineno}: {node.value[:60]!r}")
    return offenders


def test_no_vs16_in_pt_rendered_chrome_strings():
    offenders: list[str] = []
    for rel in _CHROME_FILES:
        p = _REPO_ROOT / rel
        if p.exists():
            offenders.extend(_vs16_offenders(p))
    assert not offenders, (
        "VS-16 (U+FE0F) in prompt_toolkit-rendered chrome strings desyncs "
        "pt's width model from what terminals paint (2 cells vs 1), stranding "
        "stale cells in diff repaints — the 'impossible timer' corruption "
        "class (⏱70s / 80s-at-8s). Use the bare base codepoint instead. "
        f"Offenders: {offenders!r}"
    )


def test_no_sequence_machinery_in_chrome_strings():
    """The full banned set (ZWJ, VS-15/16, keycaps, RIs, skin tones, tags, bidi).

    Broader than VS-16 alone: every one of these can change painted width
    relative to pt's per-codepoint model, or reorder the grid. Dynamic text is
    normalized through them at runtime (``normalize_for_chrome``); static
    chrome may contain none. See hermes_cli/portable_glyphs.py.
    """
    from hermes_cli.portable_glyphs import BANNED_CODEPOINTS

    offenders: list[str] = []
    for rel in _CHROME_FILES:
        p = _REPO_ROOT / rel
        if not p.exists():
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        docs = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                ds = ast.get_docstring(node, clean=False)
                if ds is not None:
                    docs.add(ds)
        scrollback = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_printish(node):
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                        scrollback.add((sub.lineno, sub.col_offset))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if node.value in docs:
                continue
            if (node.lineno, node.col_offset) in scrollback:
                continue
            for ch in node.value:
                cp = ord(ch)
                # A lone banned codepoint literal is a sanitizer argument
                # (``replace("\ufe0f", "")`` etc.), never emitted content.
                if node.value == ch and cp in BANNED_CODEPOINTS:
                    continue
                if cp in BANNED_CODEPOINTS:
                    offenders.append(f"{p.name}:{node.lineno}: U+{cp:04X} in {node.value[:50]!r}")
                    break
    assert not offenders, (
        "Sequence/format codepoints in chrome strings change painted width vs "
        "pt's model (or reorder the grid) — the corruption class this file "
        "guards. Offenders:\n" + "\n".join(offenders)
    )


def test_chrome_codepoints_are_allowlisted():
    """Every non-ASCII chrome codepoint must be a deliberate allowlist entry.

    Inverted enforcement (per external design review): instead of banning one
    bad shape at a time, chrome may only use codepoints from
    ``PORTABLE_EXTRA_CODEPOINTS`` — each with a class rationale — so a future
    glyph that diverges on some terminal cannot slip in un-reviewed. The
    width-engine matrix (test_width_engine_matrix.py) re-verifies each entry's
    widths; this is the front door.
    """
    from hermes_cli.portable_glyphs import BANNED_CODEPOINTS, is_portable_codepoint

    offenders: list[str] = []
    for rel in _CHROME_FILES:
        p = _REPO_ROOT / rel
        if not p.exists():
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        docs = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                ds = ast.get_docstring(node, clean=False)
                if ds is not None:
                    docs.add(ds)
        scrollback = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_printish(node):
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                        scrollback.add((sub.lineno, sub.col_offset))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if node.value in docs:
                continue
            if (node.lineno, node.col_offset) in scrollback:
                continue
            for ch in node.value:
                if ord(ch) < 0x80:
                    continue
                # A lone banned codepoint literal is a sanitizer argument
                # (``t(...).replace("\ufe0f", "")`` on upstream i18n titles),
                # never emitted content — same exemption as the ban sweep.
                if node.value == ch and ord(ch) in BANNED_CODEPOINTS:
                    continue
                if not is_portable_codepoint(ch):
                    offenders.append(f"{p.name}:{node.lineno}: {ch!r} (U+{ord(ch):04X}) "
                                     f"in {node.value[:40]!r}")
                    break
    assert not offenders, (
        "Chrome uses non-ASCII codepoints outside the portable allowlist "
        "(hermes_cli/portable_glyphs.py). A new chrome glyph = deliberate "
        "allowlist edit + width-matrix pass. Offenders:\n" + "\n".join(offenders)
    )


# ── behavioral: the status bar (the surface the corruption appeared on) ─────

from hermes_cli.cli_status_bar_mixin import CLIStatusBarMixin as _CLIStatusBarMixin


class _StubStatus(_CLIStatusBarMixin):
    """Minimal carrier for the real mixin's snapshot/segment builders.

    Real class inheritance so every mixin method resolves with this stub as
    ``self``; only the attrs the snapshot builder reads are stubbed.
    """

    def __init__(self):
        from datetime import datetime, timedelta
        from types import SimpleNamespace

        self.model = "deepseek-v4.1-flash"
        self.session_start = datetime.now() - timedelta(minutes=4)
        self._prompt_start_time = None
        self._prompt_duration = 8.0
        self._last_turn_finished_at = None
        self._focus_view_enabled = False
        self.reasoning_config = {"enabled": True, "effort": "xhigh"}
        self._battery_visible = False
        self._background_tasks = None
        self._pending_steer = None
        self._pending_steer_lock = None
        self._prompt_stash = SimpleNamespace(indicator=lambda: "")
        self._get_goal_manager = lambda: None
        agent = SimpleNamespace(model="deepseek-v4.1-flash", context_compressor=None,
                                compressions=2)
        for key in ("session_input_tokens", "session_output_tokens", "session_total_tokens",
                    "api_calls", "total_tokens", "turns", "tool_calls", "errors",
                    "cache_hits", "cache_misses", "cost_usd", "active_background_tasks",
                    "active_background_processes", "active_background_subagents"):
            setattr(agent, key, 0)
        self.agent = agent

    def _is_session_yolo_active(self):
        return False

    def _format_context_delta(self, snapshot):
        return "Δ+1.67K new"


def _maximal_plain_bar_text() -> str:
    st = _StubStatus()
    snapshot = st._get_status_bar_snapshot()
    snapshot.update({
        "context_tokens": 478_000,
        "context_length": 1_048_576,
        "context_percent": 46,
        "context_estimated": True,
        "avg_latency_label": "6.3s",
        "avg_velocity_label": "333 t/s",
        "avg_ttft_label": "4.45",
        "compressions": 2,
        "active_background_processes": 1,
        "steer_pending": True,
    })
    segs = st._status_bar_segments(snapshot, 220, None, False, styled=False)
    return " │ ".join("".join(t for _, t in seg) for seg in segs)


def test_status_bar_plain_text_contains_no_vs16():
    text = _maximal_plain_bar_text()
    assert "\ufe0f" not in text, f"VS-16 leaked into the status bar: {text!r}"
    # The badge itself must be present as the bare base codepoint.
    assert "🗜 2" in text


def test_status_bar_fragment_text_contains_no_vs16():
    text = _maximal_plain_bar_text()
    assert not any(ord(ch) == 0xFE0F for ch in text)


# ── dynamic ingress: model/user text must not smuggle VS-16 into pt windows ──
#
# The static sweep above only covers string constants. VS-16 can also arrive at
# runtime — think-stream previews, tool rows, session titles, /goal text, queue
# previews — and render INSIDE prompt_toolkit windows whose diff repaints then
# desync (the corruption class this file guards). These pin the runtime
# chokepoints that sanitize dynamic text.


def test_subagent_clip_strips_vs16_from_dynamic_rows():
    from agent.display import display_cwidth
    from hermes_cli.cli_subagent_monitor import _clip

    # A goal / queue preview / command carrying a VS-16 sequence.
    row = "⚙️ deploy the thing · 12m09s"
    clipped = _clip(row, 60)
    assert "\ufe0f" not in clipped, "dock rows must not carry VS-16 into the pt grid"
    assert display_cwidth(clipped) <= 60


def test_dock_activity_timer_field_clean_of_vs16():
    from hermes_cli.cli_subagent_monitor import _clip
    from hermes_cli.cli_process_dock import process_activity

    # The row builder's timer field itself (what the corruption garbles). Every dock/
    # roster row is assembled through _clip (dock_text, collapsed preview, modal roster),
    # so this mirrors the real pipeline: the VS-16 in the process's last-output detail
    # must not survive into the rendered grid, and the elapsed field must stay exact.
    line = _clip(process_activity({"status": "running", "elapsed": 729, "detail": "⚙️ working"}), 60)
    assert "\ufe0f" not in line
    assert "12m09s" in line


def test_spinner_text_strips_vs16_from_model_output():
    from types import SimpleNamespace

    from hermes_cli.cli_status_bar_mixin import CLIStatusBarMixin

    stub = SimpleNamespace(
        _spinner_text="… drafting ⚙️ the report",   # think-stream preview
        _spinner_token_flow_enabled=False,
        _spinner_token_flow=lambda: "",
        _agent_running=False,
        _tool_start_time=0,
    )
    out = CLIStatusBarMixin._render_spinner_text(stub)
    assert "\ufe0f" not in out, f"spinner must sanitize dynamic text: {out!r}"
    assert "drafting ⚙ the report" in out


def test_session_title_badge_strips_vs16():
    from hermes_cli.cli_status_bar_mixin import CLIStatusBarMixin

    placed = CLIStatusBarMixin._status_title_badge("Fix ⚠️ the timer — 4m17s", 120)
    assert placed is not None
    badge, _left = placed
    assert "\ufe0f" not in badge, f"title badge must sanitize model-generated titles: {badge!r}"


def test_panel_lines_strip_vs16_from_dynamic_content():
    """Modal panels (approval/clarify/sudo) render model/user text in pt windows."""
    from hermes_cli.cli_render import _append_panel_line

    lines = []
    _append_panel_line(lines, "b", "c", "sudo rm -rf ⚙️ /tmp — dangerous", 40)
    rendered = "".join(text for _style, text in lines)
    assert "\ufe0f" not in rendered, f"panel content must not carry VS-16: {rendered!r}"


def test_panel_title_strips_vs16():
    from hermes_cli.cli_tui_mixin import _Panel

    panel = _Panel("class:b", 40, title="⚠️  Dangerous Command", title_style="class:t")
    rendered = "".join(text for _style, text in panel.close())
    assert "\ufe0f" not in rendered, f"panel title must not carry VS-16: {rendered!r}"


def test_output_history_line_math_counts_vs16_as_painted():
    """Scrollback replay arithmetic counts VS-16 sequences at painted width.

    ``_line_rows`` / ``_ansi_drop_cells`` measure against the real terminal grid; pt's
    table scores a "⚙️" sequence 1 cell where kitty paints 2, so a line carrying them
    must count one cell more than the raw-pt number.
    """
    from hermes_cli import cli_render

    line = "x" * 8 + "⚙️" * 4          # 8 + (2 painted cells x 4) = 16 cells
    assert cli_render._line_rows(line, 10) == 2  # 16 cells -> 2 rows at width 10
    # The drop keeps a sequence glued to its base: dropping exactly through the
    # emoji's two cells must not strand an orphan VS-16, and must not stop short.
    assert cli_render._ansi_drop_cells("ab⚙️cd", 4) == "cd"
