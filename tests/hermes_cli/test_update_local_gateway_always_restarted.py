"""The local gateway is always brought onto the updated code, gently.

An update that the fleet guards call "already current" can still leave THIS host's gateway
serving pre-update modules — the identity they compare is stamped by the update, not read
from the running process — so the completion tail always drains the local unit: SIGUSR1
refuses new turns, in-flight work finishes, and only a drain past five minutes is forced.
"""

from __future__ import annotations

import subprocess

import hermes_cli.update_cmd_fleet as fleet


def _targets(monkeypatch, units):
    monkeypatch.setattr(fleet, "_local_gateway_targets", lambda: list(units))


def test_drains_with_at_least_a_five_minute_window(monkeypatch):
    _targets(monkeypatch, [("user", ["systemctl", "--user"], "hermes-gateway")])
    monkeypatch.setattr(fleet, "_systemd_unit_owned_by_update", lambda cmd, svc: True)
    monkeypatch.setattr(fleet, "_unit_main_pid", lambda cmd, svc: 4242)
    monkeypatch.setattr(fleet, "_wait_for_service_active", lambda cmd, svc: True)

    signalled: list[tuple[int, float]] = []

    def fake_sigusr1(pid, drain_timeout, *, on_progress=None):
        signalled.append((pid, drain_timeout))
        return True

    import hermes_cli.gateway as gateway_mod

    monkeypatch.setattr(gateway_mod, "_graceful_restart_via_sigusr1", fake_sigusr1)

    forced: list[tuple] = []
    monkeypatch.setattr(fleet, "_systemctl_reset_and_restart", lambda *a, **k: forced.append((a, k)))

    assert fleet.restart_local_gateways_gently(drain_seconds=5) is True
    assert signalled == [(4242, fleet.GATEWAY_DRAIN_FLOOR_SECONDS)]
    assert fleet.GATEWAY_DRAIN_FLOOR_SECONDS >= 300
    assert forced == []


def test_forces_the_restart_only_after_the_drain_window_expires(monkeypatch):
    _targets(monkeypatch, [("user", ["systemctl", "--user"], "hermes-gateway")])
    monkeypatch.setattr(fleet, "_systemd_unit_owned_by_update", lambda cmd, svc: True)
    monkeypatch.setattr(fleet, "_unit_main_pid", lambda cmd, svc: 4242)
    monkeypatch.setattr(fleet, "_wait_for_service_active", lambda cmd, svc: True)

    import hermes_cli.gateway as gateway_mod

    monkeypatch.setattr(gateway_mod, "_graceful_restart_via_sigusr1", lambda pid, t, **k: False)

    forced: list[str] = []

    class _Done:
        returncode = 0

    monkeypatch.setattr(
        fleet, "_systemctl_reset_and_restart",
        lambda cmd, svc, scope_cmd=None: (forced.append(svc), _Done())[1],
    )

    assert fleet.restart_local_gateways_gently() is True
    assert forced == ["hermes-gateway"]


def test_serve_units_skip_the_signal_and_go_straight_to_restart(monkeypatch):
    """SIGUSR1 only means something to ``gateway/run.py``; a serve unit would just die."""
    _targets(monkeypatch, [("user", ["systemctl", "--user"], "hermes-desktop-serve")])
    monkeypatch.setattr(fleet, "_systemd_unit_owned_by_update", lambda cmd, svc: True)
    monkeypatch.setattr(fleet, "_unit_main_pid", lambda cmd, svc: 99)
    monkeypatch.setattr(fleet, "_wait_for_service_active", lambda cmd, svc: True)

    import hermes_cli.gateway as gateway_mod

    signalled: list[int] = []
    monkeypatch.setattr(
        gateway_mod, "_graceful_restart_via_sigusr1",
        lambda pid, t, **k: (signalled.append(pid), True)[1],
    )

    class _Done:
        returncode = 0

    monkeypatch.setattr(fleet, "_systemctl_reset_and_restart", lambda cmd, svc, scope_cmd=None: _Done())

    assert fleet.restart_local_gateways_gently() is True
    assert signalled == []


def test_no_units_is_not_a_failure(monkeypatch):
    _targets(monkeypatch, [])
    assert fleet.restart_local_gateways_gently() is True
