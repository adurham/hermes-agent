"""A client's lock probe never refuses a gateway boot.

``gateway ensure`` (and every discovery) asks "is a reservation pending?" by taking
``gateway.lock``'s flock for an instant (``gateway.status._probe_lock_file``). A multiplexer whose
non-blocking boot reserve lands in that instant used to exit 1 with ``Cannot reserve gateway
profiles``, so the very client that started it read ``runtime_exited``. These tests hold a real
flock exactly where a probe holds it and boot through the real reserve.
"""
import fcntl
from pathlib import Path

import pytest

from gateway.run_bootstrap import _reserve_waiting_out_browse_reconcile
from gateway.runtime_ownership import OwnershipConflict, ProfileOwnership

pytestmark = pytest.mark.platforms("linux", "macos")


def _homes(tmp_path):
    root = tmp_path / "state"
    boot = root / "profiles" / "boot"
    for home in (root, boot):
        home.mkdir(parents=True, mode=0o700, exist_ok=True)
        (home / "gateway.lock").touch(mode=0o600)
    return root, boot


def _probe_hold(home):
    """What ``_probe_lock_file`` holds between its acquire and release: an exclusive flock on its
    own open file description, with no record written."""
    handle = open(home / "gateway.lock", "r+", encoding="utf-8")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    return handle


def test_boot_reserve_survives_a_probe_on_a_served_profile(tmp_path):
    """The probe holds boot's flock during the reserve and lets go before the re-check."""
    root, boot = _homes(tmp_path)

    class ProbedDuringFirstReserve(ProfileOwnership):
        attempts = 0

        def reserve(self, homes, **kwargs):
            self.attempts += 1
            if self.attempts > 1:
                return super().reserve(homes, **kwargs)
            with _probe_hold(boot):
                return super().reserve(homes, **kwargs)

    booting = ProbedDuringFirstReserve()
    try:
        _reserve_waiting_out_browse_reconcile(booting, [root, boot], wait_s=10)
        assert booting.owns(root) and booting.owns(boot)
        assert booting.attempts == 2
    finally:
        booting.close()


def test_boot_reserve_survives_a_probe_during_the_recheck(tmp_path):
    """The rolled-back attempt left this process's own record on root; a probe holding root while
    the boot re-checks must not read that record as another owner."""
    root, boot = _homes(tmp_path)
    held = []

    class ProbedAcrossRecheck(ProfileOwnership):
        attempts = 0

        def reserve(self, homes, **kwargs):
            self.attempts += 1
            while held:
                held.pop().close()
            if self.attempts > 1:
                return super().reserve(homes, **kwargs)
            try:
                with _probe_hold(boot):
                    return super().reserve(homes, **kwargs)
            finally:
                held.append(_probe_hold(root))  # still held when the boot re-checks who conflicts

    booting = ProbedAcrossRecheck()
    try:
        _reserve_waiting_out_browse_reconcile(booting, [root, boot], wait_s=10)
        assert booting.owns(root) and booting.owns(boot)
    finally:
        booting.close()
        for handle in held:
            handle.close()


def test_a_live_foreign_owner_still_refuses_at_once(tmp_path):
    """Control: a holder whose record names another live process is an owner, not a probe."""
    import subprocess
    import sys
    import time

    root, boot = _homes(tmp_path)
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, sys.argv[2]); from pathlib import Path; import time\n"
         "from gateway.runtime_ownership import ProfileOwnership\n"
         "owner = ProfileOwnership(); owner.reserve([Path(sys.argv[1])]); print('held', flush=True); time.sleep(30)",
         str(boot), str(Path(__file__).resolve().parents[2])],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        booting = ProfileOwnership()
        started = time.monotonic()
        with pytest.raises(OwnershipConflict):
            _reserve_waiting_out_browse_reconcile(booting, [root, boot], wait_s=10)
        assert time.monotonic() - started < 2
        assert not booting.owns(root)
    finally:
        holder.kill()
        holder.wait()
