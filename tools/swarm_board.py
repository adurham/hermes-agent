"""Multi-row live status for active subagents during a delegate_task batch.

This module is a thread-safe state container.  Rendering is the responsibility
of the surrounding UI — the CLI hosts a prompt_toolkit ``FormattedTextControl``
that reads ``get_rows_snapshot()`` and re-renders whenever the board calls
its ``on_change`` hook.

Why no rendering here:

The previous implementation tried to paint multi-row live updates by writing
raw ANSI cursor-up + clear-line sequences to ``sys.stdout`` from a daemon
thread.  Under prompt_toolkit's ``patch_stdout`` (the active CLI runtime), raw
cursor-movement escapes are silently filtered by ``StdoutProxy`` while line
clears pass through as literal text — so each tick appended a fresh block of
rows instead of updating in place.  See ``cli.py::_cprint`` for the documented
note that raw ANSI through stdout doesn't survive ``patch_stdout``.

The proper fix is to surface board state as a real widget in prompt_toolkit's
own layout, where the rendering pipeline owns cursor management.  That's what
the CLI does with the ``swarm_board_widget`` hung off the root ``HSplit``.

Public surface used by ``delegate_tool.py``:

* ``SwarmBoard.maybe_start(parent_agent, n_children)`` — returns either a real
  ``SwarmBoard`` (when the parent is attached to a CLI that can host the
  widget) or a ``_NoopBoard`` (everything else: gateway, library, piped runs).
* ``board.register(sid, model=..., goal=...)``
* ``board.update(sid, status=..., tool_count=..., last_tool=..., last_note=...)``
* ``board.note(sid, text)`` — convenience for setting only ``last_note``.
* ``board.finish(sid, status=..., summary=...)``
* ``board.get_rows_snapshot()`` — used by the widget's text getter.

Both ``SwarmBoard`` and ``_NoopBoard`` are context managers; ``__enter__`` /
``__exit__`` handle showing and hiding the widget by toggling a CLI-side flag.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


# Status icons — kept in lockstep with the existing KawaiiSpinner /
# subagent.complete UI so the eye doesn't have to retrain.
_STATUS_GLYPH = {
    "queued":     "⏸",
    "starting":   "⏳",
    "running":    "🔀",
    "summarizing": "📝",
    "completed":  "✅",
    "ok":         "✅",
    "failed":     "❌",
    "error":      "❌",
    "timeout":    "⏱",
    "interrupted": "⛔",
}


@dataclass
class RowSnapshot:
    """Frozen view of a row, safe to render without holding the lock."""
    subagent_id: str
    model: str
    goal: str
    status: str
    tool_count: int
    last_tool: str
    last_note: str
    elapsed_seconds: float


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
    # Freeze point for the displayed elapsed clock once the child stops
    # doing work and just streams the final summary to text.  The model has
    # finished its tool-calling loop at this point, so the meaningful
    # "work duration" is fixed; continuing to tick the clock made finished
    # rows look like they were still iterating.  Set when status flips to
    # "summarizing"; preserved through the eventual ``finish()`` call so the
    # final completed row still displays the work-time, not the work-time +
    # summary-write-time.
    work_ended_at: Optional[float] = None

    def elapsed(self) -> float:
        # Precedence: terminal end (finish/failure) > work-finished freeze
        # (summarizing onwards) > current wall clock.
        if self.ended_at is not None and self.work_ended_at is None:
            end = self.ended_at
        elif self.work_ended_at is not None:
            end = self.work_ended_at
        else:
            end = time.time()
        return max(0.0, end - self.started_at)

    def snapshot(self) -> RowSnapshot:
        return RowSnapshot(
            subagent_id=self.subagent_id,
            model=self.model,
            goal=self.goal,
            status=self.status,
            tool_count=self.tool_count,
            last_tool=self.last_tool,
            last_note=self.last_note,
            elapsed_seconds=self.elapsed(),
        )


def _flatten_to_oneline(text: str, max_len: int) -> str:
    """Collapse text to a single visual line for row rendering.

    Newlines / carriage returns in ``last_note`` (or ``last_tool``)
    overflow the row's allocated height in the prompt_toolkit Window —
    the widget reserves ``len(rows)`` lines but a row whose text
    contains a ``\\n`` renders on multiple visual lines, pushing later
    rows out of the allocated area.  Sanitise here so format_row's
    output is guaranteed single-line.
    """
    if not text:
        return ""
    # Replace any whitespace-newline run with a single space; strip the
    # rest of the control-character range too so a stray ANSI fragment
    # doesn't leak into the board.
    flat = " ".join(text.split())
    if len(flat) > max_len:
        flat = flat[: max_len - 3] + "..."
    return flat


def format_row(row: RowSnapshot) -> str:
    """Render a single row to a one-line status string.

    Pure function so the CLI's widget getter can call it without taking the
    board's lock.
    """
    glyph = _STATUS_GLYPH.get(row.status, "🔀")
    sid = row.subagent_id[-12:] if len(row.subagent_id) > 12 else row.subagent_id
    model = row.model or "?"
    if "/" in model:
        model = model.split("/", 1)[1]
    elapsed = f"{row.elapsed_seconds:.0f}s"
    tool = _flatten_to_oneline(row.last_tool or "", 30)
    if tool.startswith("mcp_"):
        tool = tool[4:]
    n = row.tool_count
    note = _flatten_to_oneline(row.last_note or "", 60)
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


class _NoopBoard:
    """Returned from ``SwarmBoard.maybe_start`` when no CLI host is available.

    The caller's ``with`` block runs unmodified; every method is a no-op.
    Children print their chatter to stdout via the existing spinner-driven
    progress path (i.e. pre-board behavior).
    """

    is_active = False

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

    def get_rows_snapshot(self) -> List[RowSnapshot]:
        return []


class SwarmBoard:
    """Thread-safe state container for the live swarm display.

    The CLI's prompt_toolkit widget reads ``get_rows_snapshot()`` and renders.
    Mutators (``register``, ``update``, ``note``, ``finish``) call the
    ``on_change`` callback after releasing the lock so the host can invalidate
    its app and trigger a re-render.

    The class is a context manager so callers can scope show/hide cleanly:

        with SwarmBoard.maybe_start(parent_agent, n) as board:
            board.register(sid, ...)
            board.update(sid, last_tool="...")
    """

    is_active = True

    def __init__(
        self,
        *,
        on_change: Optional[Callable[[], None]] = None,
        on_show: Optional[Callable[["SwarmBoard"], None]] = None,
        on_hide: Optional[Callable[[], None]] = None,
        title: str = "swarm",
    ) -> None:
        self._on_change = on_change
        self._on_show = on_show
        self._on_hide = on_hide
        self._title = title
        self._rows: Dict[str, _Row] = {}
        self._row_order: List[str] = []
        self._lock = threading.Lock()

    @classmethod
    def maybe_start(
        cls,
        parent_agent,
        n_children: int,
        *,
        title: str = "swarm",
    ) -> "SwarmBoard | _NoopBoard":
        """Activate the board only when there's a CLI host to render it.

        Activates when:
          * 2+ children (single-child runs render fine via existing chatter)
          * the parent agent carries a ``_cli_ref`` that exposes the
            ``_swarm_board_show`` / ``_swarm_board_hide`` /
            ``_invalidate_app`` hooks
          * not explicitly disabled via ``HERMES_SWARM_BOARD=0``

        Otherwise returns a no-op board so callers don't have to branch.
        """
        if os.environ.get("HERMES_SWARM_BOARD", "").strip() == "0":
            return _NoopBoard()
        if n_children < 2:
            return _NoopBoard()

        cli_ref = getattr(parent_agent, "_cli_ref", None)
        if cli_ref is None:
            return _NoopBoard()
        # Sanity: the CLI must expose the hooks we need.  If a wrapper CLI
        # subclasses HermesCLI without these, we degrade rather than crash.
        for attr in ("_swarm_board_show", "_swarm_board_hide", "_invalidate_app"):
            if not callable(getattr(cli_ref, attr, None)):
                return _NoopBoard()

        return cls(
            on_change=cli_ref._invalidate_app,
            on_show=cli_ref._swarm_board_show,
            on_hide=cli_ref._swarm_board_hide,
            title=title,
        )

    def __enter__(self) -> "SwarmBoard":
        if self._on_show is not None:
            try:
                self._on_show(self)
            except Exception:
                pass
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        if self._on_hide is not None:
            try:
                self._on_hide()
            except Exception:
                pass
        return False  # never suppress exceptions

    def _notify(self) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change()
        except Exception:
            pass

    def register(
        self,
        subagent_id: str,
        *,
        model: str = "",
        goal: str = "",
        status: Optional[str] = None,
    ) -> None:
        """Add or refresh a row.

        ``status`` defaults to ``_Row``'s default ("starting").  Pass
        ``"queued"`` to render rows for children that have been built and
        submitted but are waiting on an executor slot — distinct from
        rows where the child has actually begun work.  The orchestrator's
        ``subagent.start`` event transitions the row to ``"running"``.
        """
        with self._lock:
            if subagent_id not in self._rows:
                row_kwargs = {
                    "subagent_id": subagent_id,
                    "model": model,
                    "goal": goal,
                }
                if status:
                    row_kwargs["status"] = status
                self._rows[subagent_id] = _Row(**row_kwargs)
                self._row_order.append(subagent_id)
            else:
                row = self._rows[subagent_id]
                if model:
                    row.model = model
                if goal:
                    row.goal = goal
                if status:
                    row.status = status
        self._notify()

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
                # Reset the elapsed clock when the row transitions out of
                # "queued" — otherwise a child that waited 30s for an
                # executor slot starts its life showing "30s" of work
                # already done.
                if row.status == "queued" and status != "queued":
                    row.started_at = time.time()
                # Freeze the elapsed clock at the moment the child enters
                # "summarizing" — the model has stopped calling tools and
                # is just streaming its final answer text, so the displayed
                # time should reflect the work duration, not the streaming
                # latency.  Only the FIRST transition into summarizing wins
                # (a later TASK_TOOL_STARTED could flip back to running and
                # then back to summarizing again; we don't reset the freeze
                # in that case — the original work end is still meaningful).
                if status == "summarizing" and row.work_ended_at is None:
                    row.work_ended_at = time.time()
                row.status = status
            if tool_count is not None:
                row.tool_count = tool_count
            if last_tool is not None:
                row.last_tool = last_tool
            if last_note is not None:
                row.last_note = last_note
        self._notify()

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
        self._notify()

    def get_rows_snapshot(self) -> List[RowSnapshot]:
        """Return frozen row snapshots in registration order.

        Callable from any thread; safe to render without further locking.
        """
        with self._lock:
            return [self._rows[sid].snapshot() for sid in self._row_order]


# ---------------------------------------------------------------------------
# Print interception — route a child's stdout chatter to its row's note slot.
# ---------------------------------------------------------------------------


def make_child_print_fn(
    board: "SwarmBoard | _NoopBoard",
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
