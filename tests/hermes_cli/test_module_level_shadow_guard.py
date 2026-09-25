"""Guard: a merge must not re-add a pre-refactor definition ON TOP of a re-export import.

Upstream keeps splitting the monoliths (``cli.py``, ``hermes_cli/gateway.py``) into
topical siblings behind facade re-export imports, e.g.::

    from hermes_cli.gateway_launchd import (  # noqa: E402,F401 — facade re-exports
        launchd_install,
        ...
    )

When a merge re-introduces the OLD in-file body BELOW that import, the ``def``
rebinds the same name and silently wins at import time: upstream's newer code
never runs, and the failure surfaces somewhere unrelated (a plist that lost its
``osascript`` wrapper, a missing ``start_now=`` kwarg, a stale ``_show_usage``
shadowing a fix). No test that merely imports the module can see it.

This guard is pure AST (no app imports) and asserts that the set of
module-level "shadow" names — names imported at module level AND defined in the
same file — never GROWS. Existing shadows are recorded in ``_KNOWN_SHADOWS``
and the test fails if a name is added, while allowing the known list to shrink
as the remaining copies are de-forked (delete entries from the set when you
fix them; never add one).

The 2026-09-24 sweep deleted 49 such shadows from ``hermes_cli/gateway.py``
(commit ``5d6dde7704``); the ``cli.py`` module-level clusters are still open
scope, hence the allowlist rather than a zero-tolerance assertion.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Modules whose module-level names must never be shadowed again.
_SCANNED = (
    "cli.py",
    "hermes_cli/gateway.py",
)

# Known, still-open shadows: file -> names. This list may only SHRINK.
_KNOWN_SHADOWS: dict[str, frozenset[str]] = {
    "cli.py": frozenset(
        {
            "_SkinAwareAnsi",
            "_apply_backslash_line_continuation",
            "_apply_bracketed_paste_timeout_patch",
            "_arm_exit_watchdog",
            "_assistant_content_as_text",
            "_assistant_copy_text",
            "_bind_prompt_submit_keys",
            "_build_cpr_disabled_output",
            "_cli_config_defaults",
            "_cli_multiline_shortcuts_enabled",
            "_collect_query_images",
            "_disable_prompt_toolkit_cpr_warning",
            "_emit_interrupted_session_end",
            "_enable_extended_enter_keys",
            "_estimate_tui_input_height",
            "_exit_watchdog_timeout",
            "_finalize_single_query",
            "_float_env",
            "_flush_logging_and_stdio",
            "_flush_one_shot_session_store",
            "_heal_cooked_mode_drift",
            "_hermes_call_output_screen_diff",
            "_hex_to_ansi",
            "_invoke_interrupted_session_end",
            "_is_backslash_line_continuation",
            "_is_ghostty_terminal",
            "_load_prefill_messages",
            "_luminance_from_hex",
            "_merge_file_config",
            "_mirror_config_to_env",
            "_notify_session_finalize",
            "_notify_single_query_session_finalize",
            "_oneshot_agent_and_session",
            "_parse_reasoning_config",
            "_parse_service_tier_config",
            "_preserve_ctrl_enter_newline",
            "_query_osc11_background",
            "_resolve_prefill_messages_file",
            "_run_checkpoint_auto_maintenance",
            "_run_state_db_auto_maintenance",
            "_select_classic_cli_pt_output",
            "_should_emit_cleanup_session_finalize",
            "_status_bar_visible_from_display_config",
            "_strip_leaked_terminal_responses_with_meta",
            "_strip_reasoning_tags",
            "_terminal_may_leak_cpr",
            "_terminal_supports_extended_enter_keys",
            "_wait_for_oneshot_background_completions",
            "load_cli_config",
        }
    ),
    "hermes_cli/gateway.py": frozenset(),
}


def _module_level_shadows(path: Path) -> set[str]:
    """Names imported at module level that the same module also re-defines."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return set()

    imported: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.add((alias.asname or alias.name).split(".")[0])

    defined: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)

    return imported & defined


def test_no_new_module_level_shadows() -> None:
    """A shadow that is not on the known list means a merge re-added a def."""
    offenders: list[str] = []
    for rel in _SCANNED:
        found = _module_level_shadows(_REPO_ROOT / rel)
        known = _KNOWN_SHADOWS.get(rel, frozenset())
        new = found - set(known)
        if new:
            offenders.append(f"{rel}: {sorted(new)}")

    assert not offenders, (
        "Module-level shadow(s) appeared — a definition now rebinds a name that "
        "the same module imports from a topical sibling, so the sibling's code "
        "never runs (see this module's docstring). Delete the in-file def and "
        "port any fork-only lines into the sibling, or fix the merge. "
        + "; ".join(offenders)
    )


def test_known_shadow_list_has_no_stale_entries() -> None:
    """Keep the allowlist honest: drop entries once the copy is deleted."""
    stale: list[str] = []
    for rel, known in _KNOWN_SHADOWS.items():
        found = _module_level_shadows(_REPO_ROOT / rel)
        gone = set(known) - found
        if gone:
            stale.append(f"{rel}: {sorted(gone)}")

    assert not stale, (
        "These names are on the known-shadow allowlist but no longer shadow — "
        "delete them from _KNOWN_SHADOWS so the list keeps shrinking: "
        + "; ".join(stale)
    )
