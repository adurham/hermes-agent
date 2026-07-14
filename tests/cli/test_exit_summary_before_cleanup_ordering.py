"""Regression test: the CLI must print `_print_exit_summary()` (cost
report + `--resume <session_id>` hint) BEFORE calling `_run_cleanup()` on
every interactive-mode exit path.

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
"""

from __future__ import annotations

import re
from pathlib import Path

CLI_PY = Path(__file__).resolve().parents[2] / "cli.py"


def _find_all(pattern: str, text: str) -> list[int]:
    return [m.start() for m in re.finditer(re.escape(pattern), text)]


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
