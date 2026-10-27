"""Reclaiming an orphan install state never leaves two holders of its install lock
(``pm/install_states.py``, ``hermes_cli.runtime_state.runtime_lock``)."""

from __future__ import annotations

import os
import threading

import pm.environments
import pm.install_states
import pytest
from hermes_cli.runtime_state import runtime_lock
from pm.environments import install_key
from pm.install_states import collect_orphan_install_states


@pytest.mark.platforms("posix")
def test_launcher_waiting_on_a_reclaimed_state_lock_excludes_later_launchers(tmp_path, monkeypatch):
    """A launcher from a checkout re-created at the recorded path opens the lock while the
    collector holds it, then gets it when the collector lets go. The lock file it waited on
    is the one the collector removes; whatever it ends up holding must still exclude the
    next launcher, or two installers write one state."""
    installs = tmp_path / "home" / "installs"
    checkout = tmp_path / "home" / "cache" / "scratch" / "clone"  # provably deleted: reclaimed
    state = installs / install_key(checkout)
    (state / "inputs").mkdir(parents=True)
    (state / "inputs" / ".project-root").write_text(str(checkout), encoding="utf-8")
    (state / "environments" / "gen" / "venv").mkdir(parents=True)
    monkeypatch.setattr(pm.environments, "installs_root", lambda: installs)

    opened, held, done = threading.Event(), threading.Event(), threading.Event()
    real_open = os.open

    def observed_open(path, *args, **kwargs):
        fd = real_open(path, *args, **kwargs)
        if threading.current_thread().name == "launcher-a" and os.fspath(path).endswith(".install.lock"):
            opened.set()
        return fd

    def launcher_a():
        with runtime_lock(checkout, timeout=None) as got:
            if got:
                held.set()
            done.wait(30)

    a = threading.Thread(target=launcher_a, name="launcher-a", daemon=True)
    real_leased = pm.install_states._leased

    def checkout_recreated_mid_reclaim(path):
        # Called under the collector's lock, after its orphan re-check.
        checkout.mkdir(parents=True)
        a.start()
        assert opened.wait(30)
        return real_leased(path)

    monkeypatch.setattr(os, "open", observed_open)
    monkeypatch.setattr(pm.install_states, "_leased", checkout_recreated_mid_reclaim)
    try:
        collect_orphan_install_states(installs)
        assert held.wait(30)
        with runtime_lock(checkout, timeout=0) as launcher_b:
            assert launcher_b is False, "two launchers hold the same state's install lock"
    finally:
        done.set()
        a.join(30)
