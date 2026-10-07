"""POSIX pre-swap gateway pause: what survives a killed updater, and what a paused update asks.

Real processes and a real record; the live stop/restart of real gateways is proven by
``tests/e2e/core/upgrade/git/test_hostile_pause.py`` (a real ``hermes update`` in a sandbox).
"""

from __future__ import annotations

import json
import os
import plistlib
import signal
import subprocess
import sys
import threading
import time

import pytest

from tests.hermes_cli.test_update_pause_record import _child, _reap_children  # noqa: F401 - autouse reaper

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the POSIX pause; Windows has its own")

_UNIT = {"kind": "systemd", "scope": "user", "unit": "hermes-gateway-p2probe.service", "pid": 4242}


@pytest.mark.live_system_guard_bypass
def test_a_killed_updaters_supervised_units_are_adopted_by_the_next_update(tmp_path):
    """The updater stopped a systemd unit and was SIGKILLed: the next ``hermes update`` must own
    restarting that unit (the record's only debt), never drop it."""
    owner = _child("""
        import time
        from hermes_cli import update_pause_record as r
        r.write(r.stamp_tree({"platform": "posix", "resume_needed": True, "posix_units": [%r]}),
                owner=r.identity())
        print("written", flush=True)
        time.sleep(120)
    """ % _UNIT, env={"HERMES_HOME": str(tmp_path)})
    assert owner.stdout.readline().strip() == "written"
    owner.send_signal(signal.SIGKILL)  # windows-footgun: ok — module skips on Windows
    owner.wait(timeout=10)

    nxt = _child("""
        import json
        from hermes_cli import update_pause_record as r
        adopted, claims = r.adopt_orphans()
        token = r.record_pause({"platform": "posix", "resume_needed": True, "unmapped": []}, adopted, claims)
        print(json.dumps({"units": token.get("posix_units"), "platform": token.get("platform")}))
    """, env={"HERMES_HOME": str(tmp_path)})
    out, _ = nxt.communicate(timeout=60)
    got = json.loads(out.strip().splitlines()[-1])
    assert got == {"units": [_UNIT], "platform": "posix"}, f"the adopted unit was lost: {got}"


def test_a_prompt_while_the_gateways_are_paused_takes_its_default_at_once(tmp_path, monkeypatch):
    """``/update`` relays prompts through the gateway; while this update holds it stopped nobody can
    answer, so the prompt must not park the update (and the downtime) for its 300 s timeout."""
    from hermes_cli import update_cmd, update_cmd_posix_pause

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(update_cmd_posix_pause, "_RUN", {"platform": "posix", "posix_stopped": True, "resume_needed": True})
    got: list[str] = []
    t = threading.Thread(target=lambda: got.append(update_cmd._gateway_prompt("Restore local changes now?", "n")),
                         daemon=True)
    t.start()
    t.join(timeout=10)
    assert got == ["n"], "a prompt waited for a gateway this update had stopped"
    assert not (tmp_path / ".update_prompt.json").exists()


@pytest.mark.platforms("macos")
@pytest.mark.live_system_guard_bypass
def test_a_launchd_job_is_paused_through_launchd_and_restarted_through_it(tmp_path):
    """A KeepAlive job merely killed is respawned by launchd on the old code mid-update; the pause
    boots it out (no respawn) and the restart bootstraps the same plist (a fresh PID)."""
    from hermes_cli.gateway import _launchd_print_service_pid
    from hermes_cli.update_cmd_posix_pause import _alive, _start_job, _stop_job
    label = f"ai.hermes.p2probe-{os.getpid()}"
    plist = tmp_path / f"{label}.plist"
    plist.write_bytes(plistlib.dumps({"Label": label, "ProgramArguments": ["/bin/sleep", "600"],
                                      "RunAtLoad": True, "KeepAlive": True}))
    domain = f"gui/{os.getuid()}"  # windows-footgun: ok — macOS-only test (platforms("macos"))
    if subprocess.run(["launchctl", "print", domain], capture_output=True, check=False).returncode:
        domain = f"user/{os.getuid()}"  # windows-footgun: ok — macOS-only test (platforms("macos"))

    def pid() -> int | None:
        return _launchd_print_service_pid(domain, label)[1]

    subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True, timeout=30)
    try:
        deadline = time.monotonic() + 15
        while not pid() and time.monotonic() < deadline:
            time.sleep(0.2)
        first = pid()
        assert first, "premise: launchd never started the probe job"
        job = {"kind": "launchd", "label": label, "domain": domain, "plist": str(plist), "pid": first}
        _stop_job(job)
        time.sleep(3.0)  # KeepAlive would have respawned a merely-killed job by now
        assert not _alive(first, None) and not pid(), "the paused job is running again under launchd"
        _start_job(job)
        assert pid() and pid() != first, "the restart did not bring the job back under launchd"
    finally:
        subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True, check=False)
