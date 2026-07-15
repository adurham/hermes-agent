"""Regression test: the CLI must print `_print_exit_summary()` (cost
report + `--resume <session_id>` hint) BEFORE calling `_run_cleanup()` on
every interactive-mode exit path. It must also run
`_run_memory_confirm_before_exit()` before `_print_exit_summary()` at each
of those same sites, so the memory-confirm LLM call's cost is folded into
the total before it's printed.

Why this matters: `_run_cleanup()` can block for tens of seconds — the
Phase 2 memory-confirm LLM call (`confirm_and_commit` ->
`extractor.on_session_end`) has its own timeout (default 30s via
`auxiliary.memory_extraction.timeout`), and `shutdown_mcp_servers()` can
separately block up to 15s. Both run inside `_run_cleanup()`, which is
"protected" by `_arm_exit_watchdog()` — a daemon thread that force-exits
the process with `os._exit(0)` after `HERMES_EXIT_WATCHDOG_S` seconds
(default 60s as of this fix; was 30s) if cleanup hasn't finished.

If the watchdog fires while still inside `_run_cleanup()`, the process is
killed via `os._exit(0)` before any code AFTER `_run_cleanup()` runs. If
`_print_exit_summary()` is called after `_run_cleanup()` in source order,
a slow-but-real memory-confirm LLM call (or slow MCP teardown) can
silently swallow the cost report and resume hint with zero user-visible
error -- the user just sees "Shutting down..." and then the shell prompt.

Reproduced 2026-07-14: multiple sessions hit "Exit watchdog fired after
30s" in agent.log immediately after "Memory: reviewing proposals from
this session..." printed with nothing after it -- no cost line, no
`--resume <id>` hint. Root cause was print-after-cleanup ordering
combined with a too-tight watchdog budget.

Fix: reorder both interactive-exit call sites (the stdin-unavailable
early-return path and the main `run()` finally-block exit path) to call
`_print_exit_summary()` BEFORE `_run_cleanup()`. This test pins that
ordering at the source level so a future edit can't silently reintroduce
the swallow-on-watchdog bug. (The single-query path was already correctly
ordered and is not covered here.)

Follow-up fix (2026-07-14, same day): the memory-confirm step itself was
extracted out of `_run_cleanup_body` into `_run_memory_confirm_before_exit`
(idempotent) so its own LLM cost could be drained and folded into
`session_estimated_cost_usd` -- but only if it runs BEFORE
`_print_exit_summary()` reads that total. This file's second test pins
that ordering too.
"""

from __future__ import annotations

import re
from pathlib import Path

CLI_PY = Path(__file__).resolve().parents[2] / "cli.py"


def _find_all(pattern: str, text: str) -> list[int]:
    return [m.start() for m in re.finditer(re.escape(pattern), text)]


def _bare_call_positions(call_text: str, src: str) -> list[int]:
    """Offsets of lines that are ONLY ``call_text`` (a bare statement),
    excluding occurrences inside comments/docstrings that merely mention
    the call by name (e.g. a docstring explaining why some other function
    must run before/after it).
    """
    positions = []
    offset = 0
    for line in src.splitlines(keepends=True):
        if line.strip() == call_text:
            positions.append(offset)
        offset += len(line)
    return positions


def test_print_exit_summary_precedes_run_cleanup_on_every_interactive_exit_site():
    """Every bare ``_run_cleanup()`` call in cli.py's interactive exit
    paths must be preceded (not followed) by a ``self._print_exit_summary()``
    call within the same local block, so the watchdog can never guillotine
    the cost report / resume hint out of existence.
    """
    src = CLI_PY.read_text(encoding="utf-8")

    # Only count actual call statements (a bare `_run_cleanup()` as the
    # entirety of a stripped line), not occurrences inside comments/
    # docstrings that merely mention the function by name (e.g. the
    # watchdog's own docstring: "tests invoke _run_cleanup() directly").
    cleanup_positions = [
        pos
        for pos, line in zip(
            (m.start() for m in re.finditer(r"^.*$", src, flags=re.MULTILINE)),
            src.splitlines(),
        )
        if line.strip() == "_run_cleanup()"
    ]
    summary_positions = _find_all("self._print_exit_summary()", src)

    assert len(cleanup_positions) >= 2, (
        "Expected at least 2 bare `_run_cleanup()` call sites in cli.py "
        "(stdin-unavailable early return + main run() exit path); found "
        f"{len(cleanup_positions)}. If call sites were added/removed, "
        "update this test's expectations."
    )

    for cleanup_pos in cleanup_positions:
        # Nearest _print_exit_summary() call before this _run_cleanup(),
        # within a generous same-block window (a few hundred chars is
        # plenty -- these are adjacent lines in source, not far-apart).
        preceding = [p for p in summary_positions if p < cleanup_pos]
        assert preceding, (
            f"_run_cleanup() at offset {cleanup_pos} has no preceding "
            "self._print_exit_summary() call anywhere earlier in the file. "
            "The exit summary (cost report + --resume hint) must print "
            "BEFORE _run_cleanup(), because _run_cleanup() can be killed "
            "mid-flight by the exit watchdog (_arm_exit_watchdog), which "
            "calls os._exit(0) and skips any code written after it."
        )
        nearest_preceding = max(preceding)
        gap = cleanup_pos - nearest_preceding
        assert gap < 600, (
            f"_run_cleanup() at offset {cleanup_pos} is preceded by "
            f"self._print_exit_summary() but {gap} chars earlier -- too far "
            "to confidently be the paired call for this exit site. Verify "
            "manually that ordering is still correct for this call site."
        )


def test_memory_confirm_precedes_print_exit_summary_on_every_exit_site():
    """Every `self._print_exit_summary()` / `cli._print_exit_summary()`
    call site in cli.py's exit paths must be preceded by a
    `_run_memory_confirm_before_exit()` call within the same local block,
    so the memory-confirm step's LLM cost is folded into
    `session_estimated_cost_usd` BEFORE the summary reads and prints it.
    Covers all three exit paths: the stdin-unavailable early return, the
    main run() finally-block exit, and the single-query (`-q`) path.
    """
    src = CLI_PY.read_text(encoding="utf-8")

    summary_call_positions = _bare_call_positions(
        "self._print_exit_summary()", src
    ) + _bare_call_positions("cli._print_exit_summary()", src)
    confirm_positions = _bare_call_positions("_run_memory_confirm_before_exit()", src)

    assert len(confirm_positions) >= 3, (
        "Expected _run_memory_confirm_before_exit() to appear as a bare "
        "call at least 3 times in cli.py (stdin-unavailable exit, main "
        f"run() exit, single-query exit); found {len(confirm_positions)}."
    )

    assert len(summary_call_positions) >= 3, (
        "Expected at least 3 exit-summary call sites (self._print_exit_summary "
        f"x2 + cli._print_exit_summary x1); found {len(summary_call_positions)}."
    )

    for summary_pos in summary_call_positions:
        preceding = [p for p in confirm_positions if p < summary_pos]
        assert preceding, (
            f"_print_exit_summary() call at offset {summary_pos} has no "
            "preceding _run_memory_confirm_before_exit() call anywhere "
            "earlier in the file. The memory-confirm step (and its LLM "
            "cost) must run BEFORE the exit summary is printed, otherwise "
            "the printed cost total misses that spend."
        )
        nearest_preceding = max(preceding)
        gap = summary_pos - nearest_preceding
        assert gap < 800, (
            f"_print_exit_summary() at offset {summary_pos} is preceded by "
            f"_run_memory_confirm_before_exit() but {gap} chars earlier -- "
            "too far to confidently be the paired call for this exit site. "
            "Verify manually that ordering is still correct here."
        )


def test_curator_cost_fold_precedes_print_exit_summary_on_every_exit_site():
    """Every exit-summary call site must also be preceded by a
    `_fold_curator_cost_before_exit()` call, so a completed background
    skill-curator review's LLM cost is folded in the same way the
    memory-confirm cost is (see `test_memory_confirm_precedes_print_
    exit_summary_on_every_exit_site` above for the memory-confirm half
    of this pairing).
    """
    src = CLI_PY.read_text(encoding="utf-8")

    summary_call_positions = _bare_call_positions(
        "self._print_exit_summary()", src
    ) + _bare_call_positions("cli._print_exit_summary()", src)
    fold_positions = _bare_call_positions("_fold_curator_cost_before_exit()", src)

    assert len(fold_positions) >= 3, (
        "Expected _fold_curator_cost_before_exit() to appear as a bare "
        "call at least 3 times in cli.py (stdin-unavailable exit, main "
        f"run() exit, single-query exit); found {len(fold_positions)}."
    )

    for summary_pos in summary_call_positions:
        preceding = [p for p in fold_positions if p < summary_pos]
        assert preceding, (
            f"_print_exit_summary() call at offset {summary_pos} has no "
            "preceding _fold_curator_cost_before_exit() call anywhere "
            "earlier in the file. A completed background curator review's "
            "cost must be folded in BEFORE the exit summary is printed."
        )
        nearest_preceding = max(preceding)
        gap = summary_pos - nearest_preceding
        assert gap < 800, (
            f"_print_exit_summary() at offset {summary_pos} is preceded by "
            f"_fold_curator_cost_before_exit() but {gap} chars earlier -- "
            "too far to confidently be the paired call for this exit site. "
            "Verify manually that ordering is still correct here."
        )
