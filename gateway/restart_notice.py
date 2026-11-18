"""Planned-restart marker: the CLI stamps it so the next boot says the gateway is back online.

The gateway writes one itself only when the restart is in-band (SIGUSR1 / pause-for-update). A CLI
``stop`` + ``start`` — always on Windows, which has no SIGUSR1 — is otherwise indistinguishable from
a plain stop, and the boot pass skips the "Gateway online" home-channel notice
(``gateway/run_notifications.py::_send_home_channel_startup_notifications``).

The reader is ``gateway/run.py::_planned_restart_notification_path``; the roundtrip test in
``tests/gateway/test_planned_restart_marker_cli.py`` pins the two together.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from hermes_constants import get_process_hermes_home
from utils import atomic_json_write

logger = logging.getLogger(__name__)

PLANNED_RESTART_MARKER_FILENAME = ".restart_pending.json"


def planned_restart_marker_path(home: Path | None = None) -> Path:
    return (home or get_process_hermes_home()) / PLANNED_RESTART_MARKER_FILENAME


def write_planned_restart_marker(*, via_service: bool = False, detached: bool = False) -> None:
    """Record that the running gateway is going down FOR A RESTART; never blocks the restart.

    One writer for both restart kinds: the gateway's own in-band shutdown (``via_service`` /
    ``detached`` from its shutdown context) and the CLI, which has no in-band path of its own.

    Best-effort like ``gateway.status.write_planned_stop_marker``: an unwritable home costs the
    notice, not the restart. Wider than ``OSError`` on purpose — a marker write that raises for any
    other reason must still not leave the caller's ``stop()`` un-run.
    """
    try:
        atomic_json_write(
            planned_restart_marker_path(),
            {
                "requested_at": time.time(),
                "via_service": bool(via_service),
                "detached": bool(detached),
            },
            indent=None,
        )
    except Exception:
        logger.debug(
            "Could not record the planned restart; home channels get no online notice", exc_info=True
        )


def stamp_planned_restart_unless_stamped(*, via_service: bool = False, detached: bool = False) -> None:
    """Stamp the marker only when none exists: ONE ``requested_at`` for the whole restart.

    ``request_restart`` stamps at the REQUEST, before the after-turn wait that can hold every
    client off for ``restart_after_turn_timeout``; the stop-time call is the fallback for a restart
    flag set on a path that did not go through ``request_restart``. Re-stamping at stop would move
    ``requested_at`` forward by the whole drain, and the age a ``worker_lost`` record reports is
    read against it ("planned restart 152s earlier") — that number has to mean the request.
    """
    try:
        if planned_restart_marker_path().exists():
            return
    except OSError:
        pass
    write_planned_restart_marker(via_service=via_service, detached=detached)
