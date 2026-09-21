"""Regression tests for `_fold_curator_cost_before_exit` (fork-only,
2026-07-14).

Background: the background skill-curator (`agent.curator.maybe_run_curator`,
kicked off at CLI/session startup) spawns a forked AIAgent in a daemon
thread (`run_curator_review`'s `_llm_pass`) that makes real LLM calls and
accumulates real cost on its own `session_estimated_cost_usd` -- but
nothing ever surfaced that spend anywhere: not in the curator's own state
file, not in the CLI's exit-summary cost report.

Unlike the (bounded, ~30s) memory-extraction call fixed by
`_run_memory_confirm_before_exit`, the curator's review pass can
legitimately run for minutes (its own docstring: "50-100 API calls
against hundreds of candidate skills"), so exit must NEVER block waiting
for it. `_fold_curator_cost_before_exit`:
  - drains `agent.curator.get_and_reset_curator_cost_usd()` and folds a
    nonzero result into `session_estimated_cost_usd` when the pass
    already finished before exit;
  - prints a one-line note (via `agent.curator.is_curator_running()`)
    when the pass is still in flight, so the printed cost total isn't
    silently incomplete without any indication.

These tests exercise `_fold_curator_cost_before_exit` directly -- mirrors
the test shape in test_memory_confirm_before_exit.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def _reset_cli_module_state(cli_mod):
    cli_mod._active_agent_ref = None
    cli_mod._curator_fold_attempted = False


def test_folds_completed_curator_cost_into_session_total():
    import cli as cli_mod

    agent = MagicMock()
    agent.session_estimated_cost_usd = 1.00

    cli_mod._active_agent_ref = agent
    cli_mod._curator_fold_attempted = False
    try:
        with patch(
            "agent.curator.get_and_reset_curator_cost_usd", return_value=0.15
        ), patch("agent.curator.is_curator_running", return_value=False):
            cli_mod._fold_curator_cost_before_exit()
        assert agent.session_estimated_cost_usd == 1.00 + 0.15
    finally:
        _reset_cli_module_state(cli_mod)


def test_zero_completed_cost_leaves_total_unchanged():
    import cli as cli_mod

    agent = MagicMock()
    agent.session_estimated_cost_usd = 2.0

    cli_mod._active_agent_ref = agent
    cli_mod._curator_fold_attempted = False
    try:
        with patch(
            "agent.curator.get_and_reset_curator_cost_usd", return_value=0.0
        ), patch("agent.curator.is_curator_running", return_value=False):
            cli_mod._fold_curator_cost_before_exit()
        assert agent.session_estimated_cost_usd == 2.0
    finally:
        _reset_cli_module_state(cli_mod)


def test_prints_note_when_curator_still_running(capsys):
    """When nothing's in the ledger yet AND the thread is still alive,
    print a visible note rather than silently under-reporting cost."""
    import cli as cli_mod

    agent = MagicMock()
    agent.session_estimated_cost_usd = 3.0

    cli_mod._active_agent_ref = agent
    cli_mod._curator_fold_attempted = False
    try:
        with patch(
            "agent.curator.get_and_reset_curator_cost_usd", return_value=0.0
        ), patch("agent.curator.is_curator_running", return_value=True):
            cli_mod._fold_curator_cost_before_exit()
        out = capsys.readouterr().out
        assert "still running" in out
        assert "hermes curator status" in out
        # Cost total is untouched -- nothing was drained.
        assert agent.session_estimated_cost_usd == 3.0
    finally:
        _reset_cli_module_state(cli_mod)


def test_no_note_when_curator_not_running_and_nothing_drained():
    """The common case (curator never ran / already folded): no note,
    no cost change, silent no-op."""
    import cli as cli_mod

    agent = MagicMock()
    agent.session_estimated_cost_usd = 0.0

    cli_mod._active_agent_ref = agent
    cli_mod._curator_fold_attempted = False
    try:
        with patch(
            "agent.curator.get_and_reset_curator_cost_usd", return_value=0.0
        ), patch("agent.curator.is_curator_running", return_value=False):
            cli_mod._fold_curator_cost_before_exit()
        assert agent.session_estimated_cost_usd == 0.0
    finally:
        _reset_cli_module_state(cli_mod)


def test_idempotent_guard_prevents_double_invocation():
    import cli as cli_mod

    agent = MagicMock()
    agent.session_estimated_cost_usd = 0.0

    cli_mod._active_agent_ref = agent
    cli_mod._curator_fold_attempted = False
    try:
        with patch(
            "agent.curator.get_and_reset_curator_cost_usd", return_value=0.10
        ) as mock_drain, patch("agent.curator.is_curator_running", return_value=False):
            cli_mod._fold_curator_cost_before_exit()
            cli_mod._fold_curator_cost_before_exit()
        mock_drain.assert_called_once()
        assert agent.session_estimated_cost_usd == 0.10
    finally:
        _reset_cli_module_state(cli_mod)


def test_no_active_agent_is_noop():
    import cli as cli_mod

    cli_mod._active_agent_ref = None
    cli_mod._curator_fold_attempted = False
    try:
        with patch("agent.curator.get_and_reset_curator_cost_usd") as mock_drain:
            cli_mod._fold_curator_cost_before_exit()
        mock_drain.assert_not_called()
    finally:
        _reset_cli_module_state(cli_mod)


def test_curator_import_failure_is_swallowed():
    """A raising get_and_reset_curator_cost_usd (or import failure) must
    never crash CLI exit -- curator cost bookkeeping is advisory."""
    import cli as cli_mod

    agent = MagicMock()
    agent.session_estimated_cost_usd = 4.0

    cli_mod._active_agent_ref = agent
    cli_mod._curator_fold_attempted = False
    try:
        with patch(
            "agent.curator.get_and_reset_curator_cost_usd",
            side_effect=RuntimeError("boom"),
        ):
            cli_mod._fold_curator_cost_before_exit()  # must not raise
        assert agent.session_estimated_cost_usd == 4.0
    finally:
        _reset_cli_module_state(cli_mod)
