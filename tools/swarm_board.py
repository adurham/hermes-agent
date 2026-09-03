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
  ``SwarmBoard`` (when a CLI host is reachable from the parent — directly for a
  top-level agent, or via the delegation weakref chain for a nested
  orchestrator subagent) or a ``_NoopBoard`` (everything else: gateway,
  library, piped runs).
* ``board.register(sid, model=..., goal=..., depth=..., parent_subagent_id=...)``
* ``board.update(sid, status=..., tool_count=..., last_tool=..., last_note=...)``
* ``board.note(sid, text)`` — convenience for setting only ``last_note``.
* ``board.finish(sid, status=..., summary=...)``
* ``board.get_rows_snapshot()`` — used by the widget's text getter.
* ``board.publish_to(agent)`` — make the board resolvable from ``agent``'s
  active-board registry (the owning parent is published automatically on
  ``__enter__``; children are published explicitly).

Per-agent board registry (replaces the old single ``agent._swarm_board`` slot):

* ``attach_agent_board(agent, board)`` / ``detach_agent_board(agent, board)`` —
  scoped registration, so a sibling or nested dispatch can never evict
  another dispatch's board.
* ``agent_boards(agent)`` — every board currently active for that agent.
* ``current_agent_board(agent)`` — the innermost one (what the retained
  legacy ``agent._swarm_board`` attribute mirrors).
* ``board_for_row(agent, subagent_id)`` — the board that OWNS a given row;
  the only correct way to pick a target for ``update()``/``note()``.
* ``any_board_active(agent)`` — is anything rendering for this agent's tree.

Rendering helpers the CLI widget composes (all pure, no lock needed):

* ``resolve_row_lineage(parent_agent)`` — the ``(depth, parent_subagent_id)``
  pair to pass to ``register()``, read off attributes delegation already sets.
* ``order_rows_for_display(rows)`` — regroups the concatenation of every
  active board's rows into parent → child order with effective depths, so a
  grandchild renders under its orchestrator even though the two live on
  different board objects.
* ``collapse_rows_to_limit(entries, max_rows)`` — renders to text with a hard
  line cap, replacing the overflow with a ``+N more subagents`` summary.
* ``resolve_max_board_rows(terminal_rows)`` — how many rows the cap allows.

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
    # Distinct from "running": this row's own delegate_task tool call is
    # blocked waiting on grandchildren it dispatched (a nested orchestrator
    # like a Fable PM fanned out to Opus workers). Without this the board
    # showed every row as "running" identically whether it was actively
    # doing work or sitting idle inside a blocking nested delegate_task
    # call — misleading when supervising a multi-level swarm (a PM's row
    # looked exactly as "busy" as its workers even while it had nothing
    # left to do but wait).
    "waiting_on_children": "👥",
    "summarizing": "📝",
    "completed":  "✅",
    "ok":         "✅",
    "failed":     "❌",
    "error":      "❌",
    "timeout":    "⏱",
    "interrupted": "⛔",
}

# Statuses that mean "this row is done" — used by the collapse summary to
# report how many HIDDEN rows are still doing work.
_TERMINAL_STATUSES = frozenset(
    {"completed", "ok", "failed", "error", "timeout", "interrupted"}
)

# Spaces of indent per nesting level.  2 is enough to read as a hierarchy
# without eating the (already tight) horizontal budget a row shares with
# model + status + tool + note.
_INDENT_WIDTH = 2

# Hard ceiling on rendered indentation.  delegation.max_spawn_depth bounds
# real nesting well below this, but a display path must not be the thing
# that breaks when a config raises it — past this level rows stack at the
# same indent instead of marching off the right edge.
_MAX_RENDER_DEPTH = 4

# Default cap on total rendered rows across ALL active boards (the panel is
# chrome above the conversation; it must not grow without bound as
# delegation breadth/depth increases).  The CLI passes a terminal-height
# derived value; this is the fallback when height can't be determined.
DEFAULT_MAX_BOARD_ROWS = 12

# Never shrink the board below this many rows even on a very short terminal —
# a 1-row board with "+11 more" is strictly less useful than no board.
MIN_MAX_BOARD_ROWS = 3


def resolve_max_board_rows(terminal_rows: Optional[int] = None) -> int:
    """Return how many subagent rows the board may render.

    Bounded by BOTH an absolute ceiling (``DEFAULT_MAX_BOARD_ROWS``) and a
    share of the terminal, so the panel can never crowd out the conversation
    on a short window.  The board is allotted at most a third of the visible
    rows: the panel also spends 2 lines on its borders, and it shares the
    area below the transcript with the todo board, spinner, and status bar.

    ``terminal_rows=None`` (height unknown / not a TTY) falls back to the
    absolute ceiling rather than guessing small.
    """
    if not terminal_rows or terminal_rows <= 0:
        return DEFAULT_MAX_BOARD_ROWS
    share = int(terminal_rows) // 3
    return max(MIN_MAX_BOARD_ROWS, min(DEFAULT_MAX_BOARD_ROWS, share))


@dataclass
class RowSnapshot:
    """Frozen view of a row, safe to render without holding the lock.

    ``depth`` / ``parent_subagent_id`` carry the delegation hierarchy so the
    renderer can nest a grandchild under the orchestrator that spawned it
    instead of painting one flat sibling list.  Both default to a top-level
    row so existing single-level callers are unaffected.

    ``depth`` is the DECLARED nesting level as stamped at registration time
    (0 = a direct child of the CLI's own agent).  ``order_rows_for_display``
    re-derives an effective depth from the parent links actually present in
    the render set, so an orphan row (parent already finished and torn its
    board down) doesn't render indented under nothing.
    """
    subagent_id: str
    model: str
    goal: str
    status: str
    tool_count: int
    last_tool: str
    last_note: str
    elapsed_seconds: float
    depth: int = 0
    parent_subagent_id: Optional[str] = None


@dataclass
class _Row:
    subagent_id: str
    model: str = ""
    goal: str = ""
    status: str = "starting"
    tool_count: int = 0
    last_tool: str = ""
    last_note: str = ""
    depth: int = 0
    parent_subagent_id: Optional[str] = None
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
            depth=self.depth,
            parent_subagent_id=self.parent_subagent_id,
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


def format_row(row: RowSnapshot, *, depth: Optional[int] = None) -> str:
    """Render a single row to a one-line status string.

    Pure function so the CLI's widget getter can call it without taking the
    board's lock.

    ``depth`` overrides ``row.depth`` for indentation — the renderer passes
    the EFFECTIVE depth computed by ``order_rows_for_display`` (which only
    counts ancestors actually present in the current render set) so an
    orphaned grandchild isn't indented under a parent that already left the
    board.  Each level adds ``_INDENT_WIDTH`` spaces plus a ``└─`` elbow, so
    nesting is legible without relying on color.
    """
    eff_depth = row.depth if depth is None else depth
    eff_depth = max(0, min(int(eff_depth or 0), _MAX_RENDER_DEPTH))
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
    # Indent + elbow marks the row as a child of the row above it.  Top-level
    # rows (depth 0) keep the exact pre-nesting format — no prefix at all.
    prefix = (" " * (_INDENT_WIDTH * eff_depth)) + "└─ " if eff_depth else ""
    parts = [
        f"{prefix}{glyph} [{sid}]",
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


def order_rows_for_display(
    rows: List[RowSnapshot],
) -> List[tuple]:
    """Group rows into parent → child order and compute effective depths.

    Returns a list of ``(row, effective_depth)`` pairs.

    Rows arrive from the CLI widget as the concatenation of EVERY active
    board's snapshot (``cli_ref._swarm_boards`` — one board per in-flight
    ``delegate_task()`` call).  A nested orchestrator's children therefore
    live on a *different* board object than the orchestrator's own row, and
    plain concatenation can interleave them with an unrelated concurrent
    top-level dispatch: correct depth, wrong neighbours.  This function
    reassembles the forest by parent link so a child always renders
    immediately beneath its parent regardless of which board it came from.

    Rules:
      * Roots (no parent, or a parent not present in ``rows``) keep their
        relative input order — that's registration order within a board and
        board-activation order across boards.
      * A child renders directly after its parent, siblings in input order.
      * Effective depth is ``parent's effective depth + 1``, computed from
        links actually present, so an orphan renders as a root at depth 0
        instead of floating at an indent with nothing above it.
      * Duplicate ids (same subagent registered on two boards) and parent
        cycles are handled defensively — every input row appears exactly
        once in the output.
    """
    if not rows:
        return []

    # First occurrence wins for duplicate ids; keep every row object though,
    # so a duplicate still renders (as a root) rather than vanishing.
    by_id: Dict[str, RowSnapshot] = {}
    for row in rows:
        by_id.setdefault(row.subagent_id, row)

    children: Dict[str, List[RowSnapshot]] = {}
    roots: List[RowSnapshot] = []
    for row in rows:
        parent = row.parent_subagent_id
        # A row whose parent isn't on the board (parent already finished, or
        # the parent is the CLI's own agent) is a root for display purposes.
        # `by_id.get(parent) is row` guards the degenerate self-parent case.
        if parent and parent in by_id and by_id[parent] is not row:
            children.setdefault(parent, []).append(row)
        else:
            roots.append(row)

    ordered: List[tuple] = []
    seen: set = set()

    def _emit(row: RowSnapshot, depth: int) -> None:
        # id() keyed, not subagent_id keyed: duplicate-id rows are distinct
        # objects that should each render once.
        marker = id(row)
        if marker in seen:
            return
        seen.add(marker)
        ordered.append((row, depth))
        if depth >= _MAX_RENDER_DEPTH:
            # Past the indent ceiling, keep descending but stop deepening —
            # runaway nesting must not push rows off the right edge.
            next_depth = depth
        else:
            next_depth = depth + 1
        for child in children.get(row.subagent_id, ()):
            _emit(child, next_depth)

    for root in roots:
        _emit(root, 0)

    # Safety net: anything unreachable from a root (a parent cycle among
    # non-root rows) still renders, as a root, so no row is ever dropped.
    for row in rows:
        if id(row) not in seen:
            _emit(row, 0)

    return ordered


def collapse_rows_to_limit(
    entries: List[tuple],
    max_rows: int,
) -> List[str]:
    """Render ``(row, depth)`` pairs to text, capped at ``max_rows`` lines.

    The panel is a fixed piece of chrome sitting above the conversation, so
    its height must be bounded: without this, one line was added per active
    subagent, summed across every concurrent board, and a deep/wide
    delegation tree could grow the panel until it crowded the transcript off
    a normal-height terminal.

    When the entry count exceeds ``max_rows``, the first ``max_rows - 1``
    rows render normally and the final line becomes a ``+N more subagents``
    summary, so the returned list NEVER exceeds ``max_rows``.  Keeping the
    head (rather than the tail) preserves the parent-before-child ordering
    that makes the tree readable — a truncated tail reads as "there's more
    below", a truncated head would orphan every remaining child.

    The summary line breaks out how many of the hidden rows are still
    running, since "12 hidden, all finished" and "12 hidden, all running"
    are very different situations for someone watching a live board.
    """
    if max_rows <= 0:
        return []
    if len(entries) <= max_rows:
        return [format_row(row, depth=depth) for row, depth in entries]

    visible = entries[: max_rows - 1]
    hidden = entries[max_rows - 1:]
    lines = [format_row(row, depth=depth) for row, depth in visible]
    hidden_active = sum(
        1 for row, _ in hidden if row.status not in _TERMINAL_STATUSES
    )
    n = len(hidden)
    suffix = f", {hidden_active} running" if hidden_active else ""
    lines.append(f"   … +{n} more subagent{'s' if n != 1 else ''}{suffix}")
    return lines


# ---------------------------------------------------------------------------
# Locating the CLI host across a nested delegation chain.
# ---------------------------------------------------------------------------

# Matches ``tools/delegate_tool.py::_is_descendant_of``'s hop bound — the
# same weakref chain, walked for the same reason (bounded so a corrupted or
# cyclic chain can't spin).
_MAX_ANCESTOR_HOPS = 8

_CLI_HOOKS = ("_swarm_board_show", "_swarm_board_hide", "_invalidate_app")


def _has_cli_hooks(cli_ref) -> bool:
    """True when ``cli_ref`` exposes every hook the board drives."""
    if cli_ref is None:
        return False
    return all(callable(getattr(cli_ref, attr, None)) for attr in _CLI_HOOKS)


def _find_cli_host(agent, max_hops: int = _MAX_ANCESTOR_HOPS):
    """Return the nearest CLI host reachable from *agent*, else ``None``.

    ``_cli_ref`` is stamped by ``cli.py`` on the TOP-LEVEL agent only.  A
    subagent built by ``delegate_tool._build_child_agent`` is a fresh
    ``AIAgent`` that never receives it, so an orchestrator subagent
    dispatching its own workers used to get a ``_NoopBoard`` and its
    grandchildren rendered nowhere at all — invisible, not merely flat.

    Delegation already stamps ``child._delegate_parent_ref =
    weakref.ref(parent_agent)`` at build time for the control plane
    (``action=list/steer/stop``).  Reuse that existing chain rather than
    threading a second parallel reference: walk up until we find an agent
    carrying a usable ``_cli_ref``.  Bounded by ``max_hops`` for the same
    reason ``_is_descendant_of`` is.
    """
    cur = agent
    for _ in range(max_hops + 1):
        if cur is None:
            return None
        cli_ref = getattr(cur, "_cli_ref", None)
        if _has_cli_hooks(cli_ref):
            return cli_ref
        ref = getattr(cur, "_delegate_parent_ref", None)
        # weakref.ref is callable and returns None once the referent dies.
        cur = ref() if callable(ref) else None
    return None


def any_board_active(agent, max_hops: int = _MAX_ANCESTOR_HOPS) -> bool:
    """Return True when a live swarm board is rendering for *agent*'s tree.

    The authoritative source is the CLI host's ``_swarm_boards`` LIST — the
    same collection the widget renders from (``cli.py::_swarm_board_show``).
    ``agent._swarm_board`` is a single per-agent slot that concurrent
    ``delegate_task()`` calls on the same agent overwrite and clear mid-flight
    (see the NOTE in ``delegate_tool._execute_and_aggregate``), so suppression
    checks that read only the slot open the emit gate while a sibling batch's
    board is still on screen.  When no CLI host carrying the list is reachable
    (TUI, test doubles, gateway/headless), fall back to the slot check — which
    also preserves the headless contract (no board anywhere → emit).
    """
    cur = agent
    for _ in range(max_hops + 1):
        if cur is None:
            break
        boards = getattr(getattr(cur, "_cli_ref", None), "_swarm_boards", None)
        if isinstance(boards, list):
            return len(boards) > 0
        ref = getattr(cur, "_delegate_parent_ref", None)
        cur = ref() if callable(ref) else None
    return any(
        getattr(b, "is_active", False) is True for b in agent_boards(agent)
    )


# ---------------------------------------------------------------------------
# Per-agent active-board registry.
# ---------------------------------------------------------------------------
#
# WHY THIS EXISTS (the structural bug it replaces)
#
# ``agent._swarm_board`` used to be a SINGLE attribute slot: one dispatch's
# board at a time, per agent.  Two separate changes turned that from a benign
# simplification into a genuine correctness bug, and neither revisited it:
#
#   * 2026-07-28 (d4fd4bfb2b) documented the slot as acceptable BECAUSE
#     "the CLI widget itself (cli_ref._swarm_board) is also single-slot so
#     only one board renders at a time regardless."  That was true then.
#   * 2026-08-09 (998aba516c) replaced the CLI host's singular slot with a
#     ``_swarm_boards`` LIST and made the widget concatenate rows from EVERY
#     board in it.  The premise above died here; the per-agent slot did not
#     follow.
#   * 2026-08-23 (f16110bd86) taught ``_find_cli_host`` to walk the
#     delegation weakref chain, so a nested orchestrator's own
#     ``delegate_task`` now gets a REAL board too.  That publishes a SECOND
#     board onto the SAME agent object — not occasionally under load, but
#     deterministically, for the entire time a PM subagent is blocked on its
#     children.
#
# So the slot is overwritten and nulled on the normal path, and because
# ``SwarmBoard.update()`` returns silently for a row id it doesn't hold, a
# misdirected write is discarded with no error — the row just freezes.
#
# The fix mirrors what the CLI host already does one level up: a keyed
# collection of currently-active boards, with attach/detach scoped to a
# specific board so a sibling or nested dispatch can never evict another's
# entry.  The legacy singular attribute is kept in sync as a derived "current
# board" view so external/legacy readers of ``agent._swarm_board`` keep
# working instead of breaking silently.

# One lock for the whole registry rather than one per agent: attach/detach
# happen a handful of times per dispatch (not per event), so contention is
# irrelevant, and a single lock keeps the "read the list, then reconcile the
# legacy slot" pair atomic across concurrent sibling dispatches.
_REGISTRY_LOCK = threading.RLock()

# Attribute holding an agent's ordered list of currently-attached boards.
# Oldest attachment first, so the LAST entry is the innermost/newest dispatch
# — which is what "current" means for a legacy single-board reader.
_BOARDS_ATTR = "_swarm_board_stack"

# The legacy single-slot attribute, retained as a derived view.
_LEGACY_ATTR = "_swarm_board"


def _legacy_slot_board(agent):
    """Return a board parked in the legacy singular slot, if any.

    Reads that never went through ``attach_agent_board`` (an external caller
    or older code doing ``agent._swarm_board = board`` directly) must still
    be discoverable, or this change would silently break them.
    """
    board = getattr(agent, _LEGACY_ATTR, None)
    return board if board is not None else None


def agent_boards(agent) -> List[Any]:
    """Return every board currently attached to *agent*, oldest first.

    Includes a board sitting in the legacy singular slot that was never
    registered here, so a direct ``agent._swarm_board = board`` assignment is
    still visible to every consumer in this module.
    """
    if agent is None:
        return []
    with _REGISTRY_LOCK:
        stack = getattr(agent, _BOARDS_ATTR, None)
        boards = list(stack) if isinstance(stack, list) else []
        legacy = _legacy_slot_board(agent)
        if legacy is not None and not any(b is legacy for b in boards):
            boards.append(legacy)
        return boards


def current_agent_board(agent):
    """Return the innermost (most recently attached) board, else ``None``.

    This is the single-board view the legacy ``agent._swarm_board`` slot
    exposes.  "Most recent" is the right answer for a caller that wants
    "the" board: a nested dispatch's board is the one that just opened.
    Callers that need to reach a SPECIFIC row must use ``board_for_row``
    instead — "current" is a convenience, never a routing decision.
    """
    boards = agent_boards(agent)
    return boards[-1] if boards else None


def _sync_legacy_slot(agent) -> None:
    """Point the legacy singular attribute at the current board.

    Caller must hold ``_REGISTRY_LOCK``.
    """
    stack = getattr(agent, _BOARDS_ATTR, None)
    current = stack[-1] if isinstance(stack, list) and stack else None
    try:
        setattr(agent, _LEGACY_ATTR, current)
    except Exception:
        # Slotted/immutable stand-ins: the registry is still authoritative,
        # only the legacy mirror is unavailable.
        pass


def attach_agent_board(agent, board) -> None:
    """Register *board* as active for *agent*.  Idempotent.

    Never replaces an existing entry — a concurrent sibling or nested
    dispatch appends alongside, and both stay reachable until each detaches
    its own board.
    """
    if agent is None or board is None:
        return
    with _REGISTRY_LOCK:
        stack = getattr(agent, _BOARDS_ATTR, None)
        if not isinstance(stack, list):
            stack = []
            try:
                setattr(agent, _BOARDS_ATTR, stack)
            except Exception:
                return  # can't register on this object; nothing else to do
        if not any(b is board for b in stack):
            stack.append(board)
        _sync_legacy_slot(agent)


def detach_agent_board(agent, board) -> None:
    """Unregister *board* from *agent*.  Scoped, idempotent, sibling-safe.

    Removes ONLY the named board.  The legacy slot is rewritten only when it
    was pointing at the board being torn down — so a caller that never
    attached (a raw ``agent._swarm_board = ...`` assignment by someone else)
    is left untouched, and a still-running sibling's board is revealed rather
    than nulled.
    """
    if agent is None or board is None:
        return
    with _REGISTRY_LOCK:
        stack = getattr(agent, _BOARDS_ATTR, None)
        if isinstance(stack, list):
            for i, existing in enumerate(stack):
                if existing is board:
                    del stack[i]
                    break
        if _legacy_slot_board(agent) is board:
            _sync_legacy_slot(agent)


def _board_owns_row(board, subagent_id: str) -> bool:
    """True when *board* is live AND actually carries a row for *subagent_id*.

    ``is_active`` is compared with a strict ``is True`` so a ``MagicMock``
    agent/board in a unit test (where every attribute autovivifies truthy)
    can't accidentally claim ownership.  ``_NoopBoard`` reports
    ``is_active = False`` and an empty snapshot, so headless runs resolve to
    ``None`` and callers take their normal fallback path.
    """
    if board is None or getattr(board, "is_active", False) is not True:
        return False
    if not subagent_id:
        return False
    try:
        return any(
            r.subagent_id == subagent_id for r in board.get_rows_snapshot()
        )
    except Exception:
        return False


def board_for_row(
    agent, subagent_id: Optional[str], max_hops: int = _MAX_ANCESTOR_HOPS
):
    """Return the live board that OWNS *subagent_id*'s row, else ``None``.

    Row updates must follow the ROW, never whichever board is "current":
    ``SwarmBoard.update()`` drops writes for an unregistered row id silently,
    so a misdirected write freezes a row with no error anywhere.

    Search order — agent's own attached boards first (the common case, no
    ancestor walk), then the CLI host's authoritative ``_swarm_boards`` list
    (covers a board owned by a different agent in the same delegation tree).

    Deliberately NOT memoised: both sources are also how teardown is
    observed, so a cached board would keep absorbing writes after it was
    hidden.
    """
    if not subagent_id:
        return None
    for board in agent_boards(agent):
        if _board_owns_row(board, subagent_id):
            return board
    try:
        cli_ref = _find_cli_host(agent, max_hops)
        for candidate in list(getattr(cli_ref, "_swarm_boards", None) or ()):
            if _board_owns_row(candidate, subagent_id):
                return candidate
    except Exception:
        pass
    return None


def resolve_row_lineage(parent_agent) -> tuple:
    """Return ``(depth, parent_subagent_id)`` for children of *parent_agent*.

    Both values are read from attributes delegation already maintains:
    ``_delegate_depth`` (0 on a top-level agent, incremented per level in
    ``_build_child_agent``) and ``_subagent_id`` (non-None only when the
    parent is ITSELF a subagent — i.e. exactly the nested-orchestrator case).

    A child of the top-level agent is depth 0, matching the pre-existing
    flat rendering.  A child of a subagent is depth 1, and so on.
    """
    raw_depth = getattr(parent_agent, "_delegate_depth", 0)
    depth = raw_depth if isinstance(raw_depth, int) and raw_depth > 0 else 0
    raw_parent_sid = getattr(parent_agent, "_subagent_id", None)
    parent_sid = raw_parent_sid if isinstance(raw_parent_sid, str) else None
    return depth, parent_sid


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

    def publish_to(self, *_args, **_kwargs) -> None:
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

    Each ``delegate_task()`` call gets its OWN ``SwarmBoard`` instance with
    its own rows and its own lock — this class never has cross-call state to
    worry about.  Concurrent batches (e.g. a second nested/background
    ``delegate_task()`` dispatched while the first is still running) are
    reconciled on the CLI side: ``cli_ref`` tracks a LIST of currently-shown
    boards (``_swarm_board_show``/``_swarm_board_hide`` append/remove), and
    the widget concatenates rows from every active board at render time.
    That keeps ownership simple — one board per call, cleaned up on that
    call's own ``__exit__`` — while still surfacing every concurrently
    running batch in one summary area instead of the most recent batch
    silently overwriting the display slot the previous one was using.
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
        # Agent that owns this board's dispatch (set by ``maybe_start``).
        # ``None`` for a directly-constructed board, which simply has nothing
        # to auto-attach to on ``__enter__``.
        self._owner_agent: Any = None
        # Every agent this board has been published to, so ``__exit__`` can
        # retract it from all of them rather than leaving a torn-down board
        # reachable (and silently absorbing writes) via a child agent.
        self._published_to: List[Any] = []

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
          * 1+ children — single-child runs used to fall back to raw
            scrollback chatter (each heartbeat/"still waiting on provider"
            tick printed a brand-new line instead of updating in place,
            making a long single delegation look like it was spamming or
            frozen). A lone child now gets the same one-row-updated-in-place
            treatment as a batch.
          * a CLI host is reachable from the parent agent — either the
            parent carries ``_cli_ref`` directly (a top-level agent; the CLI
            stamps it in ``cli.py``) or one of its ancestors does (a nested
            orchestrator subagent; see ``_find_cli_host``)
          * not explicitly disabled via ``HERMES_SWARM_BOARD=0``

        Otherwise returns a no-op board so callers don't have to branch.
        """
        if os.environ.get("HERMES_SWARM_BOARD", "").strip() == "0":
            return _NoopBoard()
        if n_children < 1:
            return _NoopBoard()

        cli_ref = _find_cli_host(parent_agent)
        if cli_ref is None:
            # Either no CLI at all (gateway / library / piped run) or a
            # wrapper CLI that doesn't expose the hooks we drive — degrade
            # rather than crash.  ``_find_cli_host`` validates the hooks.
            return _NoopBoard()

        board = cls(
            on_change=cli_ref._invalidate_app,
            on_show=cli_ref._swarm_board_show,
            title=title,
        )
        # Bind on_hide to THIS board instance via closure rather than
        # widening SwarmBoard's on_hide contract to take an argument.
        # ``_swarm_board_hide`` on the CLI side takes the board being torn
        # down (it needs to know WHICH board to drop from its active list —
        # see the class docstring's "concurrent batches" note) but
        # SwarmBoard.__exit__ still calls a plain zero-arg ``self._on_hide()``,
        # unaware that a specific board identity is threaded through.
        board._on_hide = lambda: cli_ref._swarm_board_hide(board)
        # Remember the dispatching agent so __enter__/__exit__ can attach and
        # detach this board on it without the caller having to hand-maintain
        # an attribute (the single-slot assignment this replaces).
        board._owner_agent = parent_agent
        return board

    def __enter__(self) -> "SwarmBoard":
        # Attach BEFORE showing: the show hook invalidates the app, which can
        # drive a render on another thread that expects the board reachable
        # from its owning agent.
        self.publish_to(self._owner_agent)
        if self._on_show is not None:
            try:
                self._on_show(self)
            except Exception:
                pass
        return self

    def publish_to(self, agent) -> None:
        """Make this board reachable from *agent*'s active-board registry.

        Used for the owning parent (automatically, on ``__enter__``) and for
        each child agent, whose own progress path resolves rows through the
        same registry.  Additive: publishing never displaces a board that a
        concurrent sibling or nested dispatch already attached to the same
        agent.  Every agent published to is remembered so ``__exit__`` can
        retract this board from ALL of them — a board that outlived its
        dispatch would keep absorbing writes.
        """
        if agent is None:
            return
        with self._lock:
            if not any(a is agent for a in self._published_to):
                self._published_to.append(agent)
        attach_agent_board(agent, self)

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        with self._lock:
            published = list(self._published_to)
            self._published_to.clear()
        # Detach FIRST so no consumer can resolve this board after teardown
        # has begun.  Scoped per board, so a sibling dispatch's still-active
        # board on the same agent is untouched.
        for agent in published:
            detach_agent_board(agent, self)
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
        depth: int = 0,
        parent_subagent_id: Optional[str] = None,
    ) -> None:
        """Add or refresh a row.

        ``status`` defaults to ``_Row``'s default ("starting").  Pass
        ``"queued"`` to render rows for children that have been built and
        submitted but are waiting on an executor slot — distinct from
        rows where the child has actually begun work.  The orchestrator's
        ``subagent.start`` event transitions the row to ``"running"``.

        ``depth`` / ``parent_subagent_id`` describe where this child sits
        in the delegation tree.  Both are optional and default to a
        top-level row, so a caller that doesn't know (or care about)
        nesting gets exactly the previous flat behavior.  When supplied,
        ``order_rows_for_display`` groups children under their parent and
        ``format_row`` indents them.
        """
        with self._lock:
            if subagent_id not in self._rows:
                row = _Row(
                    subagent_id=subagent_id,
                    model=model,
                    goal=goal,
                    depth=max(0, int(depth or 0)),
                    parent_subagent_id=parent_subagent_id or None,
                )
                if status:
                    row.status = status
                self._rows[subagent_id] = row
                self._row_order.append(subagent_id)
            else:
                row = self._rows[subagent_id]
                if model:
                    row.model = model
                if goal:
                    row.goal = goal
                if status:
                    row.status = status
                if depth:
                    row.depth = max(0, int(depth))
                if parent_subagent_id:
                    row.parent_subagent_id = parent_subagent_id
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
                # latency.
                #
                # The freeze must be provisional, not permanent: the
                # "summarizing" transition can come from a HEURISTIC text
                # match (TASK_THINKING's _looks_like_summary_phase — e.g. the
                # child's reasoning starts a line with "## Summary" as an
                # intermediate planning artifact, not the real final answer).
                # A false positive here used to freeze the clock forever —
                # tool_count kept climbing as the child did real work, but
                # elapsed() stayed pinned at the false-positive timestamp
                # (reported live: rows stuck at "4s" while clearly still
                # iterating).  Every real tool call reports status="running"
                # via TASK_TOOL_STARTED, which is an unambiguous "the child is
                # actively working" signal, so treat it as the unfreeze
                # trigger.  Terminal statuses never flow through this method
                # (they go through finish(), a separate code path that sets
                # ended_at directly) so they can't accidentally clear the
                # freeze here.
                if status == "summarizing":
                    if row.work_ended_at is None:
                        row.work_ended_at = time.time()
                elif status == "running":
                    row.work_ended_at = None
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
