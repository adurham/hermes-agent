"""Contract violations raise only while the test-isolation marker is set, read at report time."""

import logging

import pytest

from tui_gateway.contracts.registry import METHODS, ContractViolation, check_result

_BAD_RESULT = {"profiles": [{"name": "bot", "path": "/profiles/bot", "unknown_field": []}]}


def test_violation_raises_under_isolation_marker(monkeypatch):
    monkeypatch.setenv("HERMES_TEST_ISOLATION", "1")
    with pytest.raises(ContractViolation):
        check_result(METHODS["profiles.list"], _BAD_RESULT)


def test_violation_logs_once_marker_is_gone(monkeypatch, caplog):
    monkeypatch.delenv("HERMES_TEST_ISOLATION")
    with caplog.at_level(logging.ERROR, logger="tui_gateway.contracts.registry"):
        check_result(METHODS["profiles.list"], _BAD_RESULT)
    assert "violates its wire contract" in caplog.text
