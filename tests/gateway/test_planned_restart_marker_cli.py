"""A CLI ``restart`` must stamp the planned-restart marker itself.

The gateway writes ``.restart_pending.json`` only on an in-band restart (SIGUSR1 / pause-for-update).
``hermes gateway restart`` on Windows is ``stop()`` + ``start()`` — Windows has no SIGUSR1 — so
without a CLI writer the next boot reads a plain stop and never sends the "Gateway online" notice to
the configured home channels.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from gateway import run as gateway_run

MARKER = ".restart_pending.json"


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    return tmp_path


def test_writer_stamps_the_marker_the_boot_pass_reads(hermes_home):
    """Writer and reader must agree on path and payload; a rename on either side fails here."""
    from gateway.restart_notice import write_planned_restart_marker

    assert gateway_run._planned_restart_notification_pending() is False

    write_planned_restart_marker(via_service=True, detached=False)

    assert (hermes_home / MARKER).exists()
    assert gateway_run._planned_restart_notification_pending() is True


def test_a_restart_request_stamps_the_marker_before_the_after_turn_wait(hermes_home, monkeypatch):
    """The stamp must exist while the drain holds every client off — not only at ``stop()``.

    ``request_restart`` opens the after-turn wait immediately, and for up to
    ``restart_after_turn_timeout`` no client can attach; after ``stop()`` there is no owner at all.
    A Kanban worker that dials into either window has this marker as its only evidence that the
    host is restarting rather than broken: on 2026-11-17 an attempt died
    ``Gateway inaccessible: service_identity_unverified`` 40 s into an update's down window and
    read as a credential problem (t_39446095).
    """
    from gateway import run_shutdown

    class _Runner:
        _restart_task_started = False
        _restart_requested = False
        _draining = False

        def _mark_api_runs_shutdown_requested(self):
            pass

    runner = cast(Any, _Runner())
    spawned = []
    monkeypatch.setattr(run_shutdown.asyncio, "create_task",
                        lambda coro: spawned.append(coro) or object())

    assert run_shutdown.GatewayShutdownMixin.request_restart(runner, via_service=True) is True
    try:
        assert (hermes_home / MARKER).exists()
        # The wait task exists but has not run: the stamp came first.
        assert len(spawned) == 1
    finally:
        for coro in spawned:
            coro.close()


def test_the_stop_time_stamp_keeps_the_requests_earlier_timestamp(hermes_home):
    """``worker_lost`` ages are read against the REQUEST, not against the stop after the drain."""
    import json
    import time

    from gateway.restart_notice import stamp_planned_restart_unless_stamped

    requested_at = time.time() - 1700
    (hermes_home / MARKER).write_text(json.dumps({"requested_at": requested_at,
                                                  "via_service": True, "detached": False}))
    stamp_planned_restart_unless_stamped(via_service=True, detached=False)

    assert json.loads((hermes_home / MARKER).read_text())["requested_at"] == requested_at
