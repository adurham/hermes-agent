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
