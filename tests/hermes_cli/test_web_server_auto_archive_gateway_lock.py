"""Invariant: `hermes serve`'s opportunistic auto-archive never opens a WRITABLE
SessionDB for a profile whose gateway holds the runtime lock (#110405).

The lock holder is a real subprocess taking the same ``flock(LOCK_EX)`` on
``gateway.lock`` that ``gateway.status`` uses, so the guard is exercised against
kernel lock state rather than a patched predicate.
"""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from gateway.status import is_gateway_runtime_lock_active
from hermes_cli import web_server_sessions as wss

pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"), reason="POSIX flock holder"
)

_HOLDER = (
    "import fcntl, sys, time\n"
    "handle = open(sys.argv[1], 'a+')\n"
    "fcntl.flock(handle.fileno(), fcntl.LOCK_EX)\n"
    "sys.stdout.write('locked\\n')\n"
    "sys.stdout.flush()\n"
    "time.sleep(300)\n"
)


class _FakeDB:
    def __init__(self, archived):
        self._archived = archived

    def maybe_auto_archive(self, **kwargs):
        self._archived.append(kwargs)

    def close(self):
        pass


@pytest.fixture
def archive_probe(tmp_path, monkeypatch):
    """Route the sweep at an isolated profile home and record every open."""
    db_path = tmp_path / "state.db"
    db_path.touch()
    opens: list[bool] = []
    archived: list[dict] = []

    monkeypatch.setattr(wss, "_session_db_path_for_profile", lambda profile: db_path)
    monkeypatch.setattr(wss, "_last_auto_archive_check", {})

    def _open(profile, *, read_only):
        opens.append(read_only)
        return _FakeDB(archived)

    monkeypatch.setattr(wss, "_open_session_db_for_profile", _open)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda *a, **k: {"sessions": {"auto_archive": True, "min_interval_hours": 0}},
    )
    return tmp_path, opens, archived


def test_serve_auto_archive_defers_to_the_gateway_holding_the_runtime_lock(archive_probe):
    tmp_path, opens, archived = archive_probe
    lock_path = tmp_path / "gateway.lock"
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(lock_path)],
        stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True,
    )
    stdout = holder.stdout
    assert stdout is not None
    try:
        assert stdout.readline().strip() == "locked"

        wss._maybe_auto_archive_for_profile(None)

        assert opens == [], "serve opened the store while the gateway owned it"
        assert archived == []
    finally:
        holder.terminate()  # by PID: the process this test spawned
        holder.wait(timeout=10)
        stdout.close()

    # Lock released: the serve-side sweep is the only archiver left and must run.
    deadline = time.monotonic() + 5
    while is_gateway_runtime_lock_active(lock_path) and time.monotonic() < deadline:
        time.sleep(0.05)
    wss._last_auto_archive_check.clear()

    wss._maybe_auto_archive_for_profile(None)

    assert opens == [False], "serve must still archive when no gateway holds the lock"
    assert len(archived) == 1
