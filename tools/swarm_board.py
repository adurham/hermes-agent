"""Live multi-row Rich panel for active subagents during a delegate_task batch.

Replaces the stream-of-prints UX during parallel swarm execution with a
single live region above the parent's spinner.  Each row updates in place
with the child's current status (model, tool count, last tool, last
notable note, elapsed).  Children's chatter (auto-repair lines, retry
banners, compaction notes, request-dump notices) is captured into the
row's note slot instead of being printed to stdout.

Design constraints:

* Coexists with prompt_toolkit's ``patch_stdout`` and the parent's
  ``KawaiiSpinner``.  The board renders to ``self._out`` (the captured
  stdout reference, like KawaiiSpinner) and uses ANSI cursor moves to
  redraw N lines in place — no Rich.Live (which fights prompt_toolkit's
  own line management).

* Errors and final completion summaries still flow up to stdout so they
  scroll in the conversation history and survive the board teardown.

* Single-process, parent-thread coordinator.  Children write to their
  row via thread-safe dict updates; a daemon thread on the parent
  redraws the board every ~250ms.  No locks held while writing to the
  terminal.

* Off by default.  The board only activates when the parent agent has
  ``_print_fn`` (i.e. CLI session, not gateway/library), 2+ children
  are about to run, and stdout is a TTY.  Otherwise children print
  their lines to stdout as before.

Public API:

    with SwarmBoard.maybe_start(parent_agent, n_children) as board:
        # Inside this block:
        # - board.update(subagent_id, **fields) updates one row
        # - board.note(subagent_id, text) sets the row's last note
        # - board.finish(subagent_id, status, summary) marks a row done
        # - children's _print_fn is patched to route their stdout into
        #   note() automatically
        ...

If ``maybe_start`` decides not to activate (no TTY, only one child,
quiet mode, etc.) it returns a no-op context manager so the caller's
``with`` block still works without branching.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


# ANSI cursor sequences — keep them minimal.  See KawaiiSpinner for the
# precedent of using only \r-and-spaces for line clearing because some
# terminal multiplexers + prompt_toolkit + redirected-stdout combos
# garble \033[K.  We use up-cursor + carriage-return + spaces.
_HIDE_CURSOR = "\033[?25l"
_SHOW_CURSOR = "\033[?25h"
_CLEAR_LINE = "\033[2K"
_UP = "\033[{n}A"        # n lines up
_BOL = "\r"


# Status icons — kept in lockstep with the existing KawaiiSpinner /
# subagent.complete UI so the eye doesn't have to retrain.
_STATUS_GLYPH = {
    "starting":   "⏳",
    "running":    "🔀",
    "completed":  "✅",
    "ok":         "✅",
    "failed":     "❌",
    "error":      "❌",
    "timeout":    "⏱",
    "interrupted": "⛔",
}


@dataclass
class _Row:
    subagent_id: str
    model: str = ""
    goal: str = ""
    status: str = "starting"
    tool_count: int = 0
    last_tool: str = ""
    last_note: str = ""
    started_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None

    def elapsed(self) -> float:
        end = self.ended_at if self.ended_at is not None else time.time()
        return max(0.0, end - self.started_at)


class _NoopBoard:
    """Returned from ``SwarmBoard.maybe_start`` when the board is disabled.

    The caller's ``with`` block runs unmodified; every method is a no-op.
    """

    def __enter__(self) -> "_NoopBoard":
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def register(self, *_args, **_kwargs) -> None:
        return None

    def update(self, *_args, **_kwargs) -> None:
        return None

    def note(self, *_args, **_kwargs) -> None:
        return None

    def finish(self, *_args, **_kwargs) -> None:
        return None


class SwarmBoard:
    """Multi-row live display for active subagents.

    Owned by the parent thread; updated from any thread.  Render thread
    is a daemon; it shuts down on ``__exit__``.
    """

    def __init__(
        self,
        *,
        out=sys.stdout,
        refresh_interval: float = 0.25,
        title: str = "swarm",
    ) -> None:
        self._out = out
        self._refresh_interval = refresh_interval
        self._title = title
        self._rows: Dict[str, _Row] = {}
        self._row_order: List[str] = []
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lines_drawn = 0  # how many lines the last paint occupied
        # Buffer for emergency stdout passthrough (e.g. on errors before
        # a row exists).  Currently unused but retained for future hooks.
        self._suppressed_prints: List[str] = []

    # -------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------

    @classmethod
    def maybe_start(
        cls,
        parent_agent,
        n_children: int,
        *,
        title: str = "swarm",
    ) -> "SwarmBoard | _NoopBoard":
        """Decide whether to activate; return a context manager.

        Activates only when:
          * 2+ children (single-child runs already render fine)
          * stdout is a TTY (the in-place redraws need terminal control)
          * parent has a ``_print_fn`` set or the env isn't quiet (gateway /
            library callers don't get the board — their caller manages UI)
          * not explicitly disabled via HERMES_SWARM_BOARD=0
        """
        if os.environ.get("HERMES_SWARM_BOARD", "").strip() == "0":
            return _NoopBoard()
        if n_children < 2:
            return _NoopBoard()
        # Resolve the output stream the same way KawaiiSpinner does:
        # parent's _print_fn lets us route through prompt_toolkit's
        # patch_stdout cleanly.
        out = sys.stdout
        try:
            if not out.isatty():
                return _NoopBoard()
        except (AttributeError, ValueError, OSError):
            return _NoopBoard()
        return cls(out=out, title=title)

    def __enter__(self) -> "SwarmBoard":
        try:
            self._out.write(_HIDE_CURSOR)
            self._out.flush()
        except Exception:
            pass
        self._thread = threading.Thread(target=self._render_loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        self._final_paint()
        try:
            self._out.write(_SHOW_CURSOR)
            self._out.flush()
        except Exception:
            pass
        return False  # never suppress exceptions

    # -------------------------------------------------------------------
    # Mutators (thread-safe)
    # -------------------------------------------------------------------

    def register(
        self,
        subagent_id: str,
        *,
        model: str = "",
        goal: str = "",
    ) -> None:
        with self._lock:
            if subagent_id not in self._rows:
                self._rows[subagent_id] = _Row(
                    subagent_id=subagent_id, model=model, goal=goal
                )
                self._row_order.append(subagent_id)
            else:
                row = self._rows[subagent_id]
                if model:
                    row.model = model
                if goal:
                    row.goal = goal

    def update(
        self,
        subagent_id: str,
        *,
        status: Optional[str] = None,
        tool_count: Optional[int] = None,
        last_tool: Optional[str] = None,
        last_note: Optional[str] = None,
    ) -> None:
        with self._lock:
            row = self._rows.get(subagent_id)
            if row is None:
                return
            if status is not None:
                row.status = status
            if tool_count is not None:
                row.tool_count = tool_count
            if last_tool is not None:
                row.last_tool = last_tool
            if last_note is not None:
                row.last_note = last_note

    def note(self, subagent_id: str, text: str) -> None:
        """Set the row's ``last_note`` slot.  Truncated to 60 chars."""
        if not text:
            return
        text = text.strip()
        if len(text) > 60:
            text = text[:57] + "..."
        self.update(subagent_id, last_note=text)

    def finish(
        self,
        subagent_id: str,
        status: str = "completed",
        summary: Optional[str] = None,
    ) -> None:
        with self._lock:
            row = self._rows.get(subagent_id)
            if row is None:
                return
            row.status = status
            row.ended_at = time.time()
            if summary:
                row.last_note = (
                    summary if len(summary) <= 60 else summary[:57] + "..."
                )

    # -------------------------------------------------------------------
    # Rendering
    # -------------------------------------------------------------------

    def _render_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._paint()
            except Exception:
                # Never let a render glitch take down the swarm.
                pass
            self._stop_event.wait(self._refresh_interval)

    def _format_row(self, row: _Row) -> str:
        glyph = _STATUS_GLYPH.get(row.status, "🔀")
        sid = row.subagent_id[-12:] if len(row.subagent_id) > 12 else row.subagent_id
        model = row.model or "?"
        # Strip provider prefix: "anthropic/claude-…" -> "claude-…"
        if "/" in model:
            model = model.split("/", 1)[1]
        elapsed = f"{row.elapsed():.0f}s"
        tool = row.last_tool or ""
        if tool.startswith("mcp_"):
            tool = tool[4:]
        if len(tool) > 30:
            tool = tool[:27] + "..."
        n = row.tool_count
        note = row.last_note or ""
        # Compose: GLYPH [id] model · status · n tools · last_tool · note · Ts
        parts = [
            f"{glyph} [{sid}]",
            f"{model}",
            f"{row.status}",
            f"{n} tool{'s' if n != 1 else ''}",
        ]
        if tool:
            parts.append(tool)
        if note:
            parts.append(note)
        parts.append(elapsed)
        return " · ".join(parts)

    def _paint(self) -> None:
        with self._lock:
            rows = [self._rows[sid] for sid in self._row_order]
        if not rows:
            return
        lines = [self._format_row(r) for r in rows]
        # Move cursor up over the previously drawn block, clear each line,
        # rewrite.  ANSI sequences only — we accept that this requires a TTY.
        buf = []
        if self._lines_drawn > 0:
            buf.append(_UP.format(n=self._lines_drawn))
        for line in lines:
            buf.append(_BOL + _CLEAR_LINE + line + "\n")
        try:
            self._out.write("".join(buf))
            self._out.flush()
        except Exception:
            return
        self._lines_drawn = len(lines)

    def _final_paint(self) -> None:
        """Final state paint at exit — leaves the board on screen so the
        user sees the last state, with a blank line below for clean
        separation from whatever scrolls next."""
        try:
            self._paint()
            self._out.write("\n")
            self._out.flush()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Print interception — route a child's stdout chatter to its row's note slot.
# ---------------------------------------------------------------------------


def make_child_print_fn(
    board: SwarmBoard | _NoopBoard,
    subagent_id: str,
    *,
    fallback,
) -> Callable[..., None]:
    """Build a ``_print_fn`` for a child agent that captures its prints
    into the swarm board row's note instead of writing to stdout.

    Lines that look like errors / completion summaries / request-dump
    references still pass through to ``fallback`` so they show up in
    the scrollback above the board.

    ``fallback`` is the original print function (the parent's ``_print_fn``
    or the builtin ``print``).
    """
    if isinstance(board, _NoopBoard):
        return fallback

    def _is_passthrough(line: str) -> bool:
        # Errors and request-dump references should still print to stdout.
        # Heuristic: anything containing "❌", "Final error", "Request debug
        # dump", or a leading "WARNING"/"ERROR" goes through.  The rest
        # (auto-repair, retry attempts, compaction, restored todos) gets
        # captured into the row.
        markers = (
            "❌", "💀", "Final error", "Request debug dump",
            "Max retries", "ERROR ", "WARNING ",
        )
        return any(m in line for m in markers)

    def _child_print(*args, **kwargs):
        # Reconstruct the line the same way print() does.
        sep = kwargs.get("sep", " ")
        text = sep.join(str(a) for a in args)
        if _is_passthrough(text):
            try:
                fallback(*args, **kwargs)
            except Exception:
                pass
            return
        # Capture into the row's note.
        # Strip a leading log_prefix like "[subagent-1] " — it's redundant
        # in the row.
        stripped = text.strip()
        if stripped.startswith("[subagent-") and "]" in stripped:
            stripped = stripped.split("]", 1)[1].lstrip()
        board.note(subagent_id, stripped)

    return _child_print
