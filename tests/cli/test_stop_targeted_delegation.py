"""Tests for the CLI's targeted `/stop <delegation_id>` — cancel ONE
in-flight delegate_task dispatch without touching any other running
background process/process registry entries.

Mirrors the gateway's targeted /stop tested in
tests/gateway/test_stop_thread_sibling.py, and the underlying registry
primitive tested in tests/tools/test_async_delegation.py::TestInterruptById.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_cli.cli_commands_mixin import CLICommandsMixin


def _stub_cli():
    return SimpleNamespace()


@pytest.fixture(autouse=True)
def _clean_async_delegation_records():
    import tools.async_delegation as ad_mod
    ad_mod._reset_for_tests()
    yield
    ad_mod._reset_for_tests()


def test_stop_no_args_falls_through_to_whole_session_stop(capsys):
    """Bare /stop (no id) must still take the original whole-session path —
    exercised via the 'no running background processes' fast exit when
    nothing is running, proving it did NOT try to parse an id."""
    from tools.process_registry import process_registry

    with patch.object(process_registry, "list_sessions", return_value=[]):
        CLICommandsMixin._handle_stop_command(_stub_cli(), "/stop")
    out = capsys.readouterr().out
    assert "no running background processes" in out.lower()


def test_stop_with_unknown_id_reports_not_found(capsys):
    CLICommandsMixin._handle_stop_command(_stub_cli(), "/stop deleg_ghost")
    out = capsys.readouterr().out
    assert "no running delegation" in out.lower()
    assert "deleg_ghost" in out


def test_stop_with_id_cancels_only_that_delegation(capsys):
    import tools.async_delegation as ad_mod

    calls_target, calls_other = [], []
    with ad_mod._records_lock:
        ad_mod._records["deleg_target"] = {
            "delegation_id": "deleg_target",
            "status": "running",
            "interrupt_fn": lambda: calls_target.append(1),
        }
        ad_mod._records["deleg_other"] = {
            "delegation_id": "deleg_other",
            "status": "running",
            "interrupt_fn": lambda: calls_other.append(1),
        }

    CLICommandsMixin._handle_stop_command(_stub_cli(), "/stop deleg_target")

    out = capsys.readouterr().out
    assert "cancelled" in out.lower()
    assert "deleg_target" in out
    assert calls_target == [1]
    assert calls_other == []  # sibling delegation untouched


def test_stop_with_id_already_done_reports_cleanly(capsys):
    import tools.async_delegation as ad_mod

    with ad_mod._records_lock:
        ad_mod._records["deleg_done"] = {
            "delegation_id": "deleg_done",
            "status": "completed",
        }

    CLICommandsMixin._handle_stop_command(_stub_cli(), "/stop deleg_done")

    out = capsys.readouterr().out
    assert "already finished" in out.lower()


def test_stop_with_id_does_not_touch_process_registry(capsys):
    """A targeted /stop <id> must return before reaching process_registry.kill_all —
    it only signals the one matching delegation."""
    import tools.async_delegation as ad_mod
    from tools.process_registry import process_registry

    with ad_mod._records_lock:
        ad_mod._records["deleg_target"] = {
            "delegation_id": "deleg_target",
            "status": "running",
            "interrupt_fn": lambda: None,
        }

    with patch.object(process_registry, "kill_all") as mock_kill_all:
        CLICommandsMixin._handle_stop_command(_stub_cli(), "/stop deleg_target")
        mock_kill_all.assert_not_called()
