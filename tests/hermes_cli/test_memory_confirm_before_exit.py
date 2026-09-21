"""Regression tests for `_run_memory_confirm_before_exit` (fork-only,
2026-07-14).

Background: the Phase 2 memory-confirm UI (`hermes_cli.memory_confirm
.confirm_and_commit`) used to run inline inside `_run_cleanup_body`, which
put it BEHIND `self._print_exit_summary()` in source order on every
interactive-exit call site. Two problems followed from that:

  1. `confirm_and_commit` makes a real LLM call with its own timeout
     (`auxiliary.memory_extraction.timeout`, default 30s). If the exit
     watchdog (`_arm_exit_watchdog`) fired mid-`_run_cleanup`, the process
     was `os._exit(0)`'d before `_print_exit_summary()` ever printed --
     silently swallowing the cost report and `--resume <id>` hint.
  2. Even when the watchdog didn't fire, the confirm step's own LLM spend
     was never folded into `session_estimated_cost_usd`, so the printed
     cost total under-counted the true cost of ending the session.

Fix: extract the confirm-UI invocation into a standalone, idempotent
`_run_memory_confirm_before_exit()` function, call it explicitly BEFORE
`self._print_exit_summary()` at every exit call site, and have it drain
`tools.memory_extraction.extractor.get_and_reset_extraction_cost_usd()`
and fold the result into `agent.session_estimated_cost_usd`.

These tests exercise `_run_memory_confirm_before_exit` directly rather
than the full exit call sites (which require a real prompt_toolkit
app/agent) -- the ordering itself is covered by the existing
`test_exit_summary_before_cleanup_ordering.py` source-level check, which
this file complements with call-site + behavior-level assertions.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def _reset_cli_module_state(cli_mod):
    cli_mod._active_agent_ref = None
    cli_mod._memory_confirm_attempted = False


def test_folds_extraction_cost_into_session_estimated_cost_usd():
    """A nonzero drained cost must be added to the agent's running total."""
    import cli as cli_mod

    agent = MagicMock()
    agent.session_id = "sid-cost"
    agent._session_messages = [{"role": "user", "content": "hi"}]
    agent.session_estimated_cost_usd = 1.50

    cli_mod._active_agent_ref = agent
    cli_mod._memory_confirm_attempted = False
    try:
        with patch("hermes_cli.memory_confirm.confirm_and_commit") as mock_confirm, \
             patch(
                 "tools.memory_extraction.extractor.get_and_reset_extraction_cost_usd",
                 return_value=0.0042,
             ):
            cli_mod._run_memory_confirm_before_exit()
            mock_confirm.assert_called_once_with("sid-cost", [{"role": "user", "content": "hi"}])
        assert agent.session_estimated_cost_usd == 1.50 + 0.0042
    finally:
        _reset_cli_module_state(cli_mod)


def test_zero_drained_cost_leaves_total_unchanged():
    import cli as cli_mod

    agent = MagicMock()
    agent.session_id = "sid-zero"
    agent._session_messages = []
    agent.session_estimated_cost_usd = 2.0

    cli_mod._active_agent_ref = agent
    cli_mod._memory_confirm_attempted = False
    try:
        with patch("hermes_cli.memory_confirm.confirm_and_commit"), \
             patch(
                 "tools.memory_extraction.extractor.get_and_reset_extraction_cost_usd",
                 return_value=0.0,
             ):
            cli_mod._run_memory_confirm_before_exit()
        assert agent.session_estimated_cost_usd == 2.0
    finally:
        _reset_cli_module_state(cli_mod)


def test_idempotent_guard_prevents_double_invocation():
    """Calling twice in the same process must only invoke confirm_and_commit
    once -- multiple exit call sites (early-return path, main run() path,
    the _run_cleanup_body safety net) can all reach this function on a
    single exit, and the memory-confirm UI must not run twice."""
    import cli as cli_mod

    agent = MagicMock()
    agent.session_id = "sid-dup"
    agent._session_messages = []
    agent.session_estimated_cost_usd = 0.0

    cli_mod._active_agent_ref = agent
    cli_mod._memory_confirm_attempted = False
    try:
        with patch("hermes_cli.memory_confirm.confirm_and_commit") as mock_confirm, \
             patch(
                 "tools.memory_extraction.extractor.get_and_reset_extraction_cost_usd",
                 return_value=0.0,
             ):
            cli_mod._run_memory_confirm_before_exit()
            cli_mod._run_memory_confirm_before_exit()
        mock_confirm.assert_called_once()
    finally:
        _reset_cli_module_state(cli_mod)


def test_no_active_agent_is_noop():
    import cli as cli_mod

    cli_mod._active_agent_ref = None
    cli_mod._memory_confirm_attempted = False
    try:
        with patch("hermes_cli.memory_confirm.confirm_and_commit") as mock_confirm:
            cli_mod._run_memory_confirm_before_exit()
        mock_confirm.assert_not_called()
    finally:
        _reset_cli_module_state(cli_mod)


def test_confirm_and_commit_exception_is_swallowed():
    """A raising confirm_and_commit must not crash CLI exit -- extraction
    issues must never block the user from actually exiting."""
    import cli as cli_mod

    agent = MagicMock()
    agent.session_id = "sid-raise"
    agent._session_messages = []
    agent.session_estimated_cost_usd = 0.0

    cli_mod._active_agent_ref = agent
    cli_mod._memory_confirm_attempted = False
    try:
        with patch(
            "hermes_cli.memory_confirm.confirm_and_commit",
            side_effect=RuntimeError("boom"),
        ):
            cli_mod._run_memory_confirm_before_exit()  # must not raise
    finally:
        _reset_cli_module_state(cli_mod)


def test_cost_ledger_read_failure_is_swallowed():
    """A raising get_and_reset_extraction_cost_usd must not crash exit or
    prevent confirm_and_commit from having already run."""
    import cli as cli_mod

    agent = MagicMock()
    agent.session_id = "sid-ledger-fail"
    agent._session_messages = []
    agent.session_estimated_cost_usd = 5.0

    cli_mod._active_agent_ref = agent
    cli_mod._memory_confirm_attempted = False
    try:
        with patch("hermes_cli.memory_confirm.confirm_and_commit") as mock_confirm, \
             patch(
                 "tools.memory_extraction.extractor.get_and_reset_extraction_cost_usd",
                 side_effect=RuntimeError("ledger boom"),
             ):
            cli_mod._run_memory_confirm_before_exit()  # must not raise
        mock_confirm.assert_called_once()
        # Cost total is untouched since the ledger read itself failed.
        assert agent.session_estimated_cost_usd == 5.0
    finally:
        _reset_cli_module_state(cli_mod)
