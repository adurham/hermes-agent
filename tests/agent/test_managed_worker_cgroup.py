"""The managed worker adopts its own cgroup, and never lets that fail the turn."""
from __future__ import annotations

import pytest

from agent import managed_worker


def test_adopt_helper_reports_the_scope(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    monkeypatch.setattr("hermes_cli.worker_cgroup.adopt_worker_cgroup",
                        lambda: {"status": "adopted", "unit": "hermes-worker-managed-1-aa.scope",
                                 "path": "/x/hermes-worker-managed-1-aa.scope",
                                 "memory_max": 512 * 1024 ** 2, "oom_score_adj": 500})

    with caplog.at_level("INFO", logger=managed_worker.logger.name):
        info = managed_worker._adopt_worker_cgroup()

    assert info["status"] == "adopted"
    assert "hermes-worker-managed-1-aa.scope" in caplog.text


def test_adopt_helper_logs_a_host_refusal_and_keeps_running(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    monkeypatch.setattr("hermes_cli.worker_cgroup.adopt_worker_cgroup",
                        lambda: {"status": "failed", "reason": "Failed to connect to bus"})

    with caplog.at_level("WARNING", logger=managed_worker.logger.name):
        info = managed_worker._adopt_worker_cgroup()

    assert info["status"] == "failed"
    assert "NOT isolated" in caplog.text


def test_adopt_helper_swallows_an_unexpected_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom():
        raise RuntimeError("busctl exploded")

    monkeypatch.setattr("hermes_cli.worker_cgroup.adopt_worker_cgroup", boom)

    info = managed_worker._adopt_worker_cgroup()

    assert info["status"] == "failed"
    assert "busctl exploded" in info["reason"]
