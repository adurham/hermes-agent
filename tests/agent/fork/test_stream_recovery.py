"""Fork-only: cold-start stale-timeout computation (agent/fork/stream_recovery.py)."""
import os

import pytest

from agent.fork.stream_recovery import effective_stale_timeout


def test_after_first_event_uses_flat_timeout():
    assert effective_stale_timeout(True, 30.0) == 30.0


def test_after_first_event_passes_inf_through():
    assert effective_stale_timeout(True, float("inf")) == float("inf")


def test_cold_start_inf_base_stays_inf():
    assert effective_stale_timeout(False, float("inf")) == float("inf")


def test_cold_start_default_floor_is_600(monkeypatch):
    monkeypatch.delenv("HERMES_STREAM_COLD_START_TIMEOUT", raising=False)
    # 3x30=90 < 600 default → 600 wins
    assert effective_stale_timeout(False, 30.0) == 600.0


def test_cold_start_triple_wins_when_larger(monkeypatch):
    monkeypatch.delenv("HERMES_STREAM_COLD_START_TIMEOUT", raising=False)
    # 3x300=900 > 600 → 900 wins
    assert effective_stale_timeout(False, 300.0) == 900.0


def test_cold_start_env_override(monkeypatch):
    monkeypatch.setenv("HERMES_STREAM_COLD_START_TIMEOUT", "1200")
    assert effective_stale_timeout(False, 30.0) == 1200.0
