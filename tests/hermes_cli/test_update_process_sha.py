"""The Desktop has to be able to say "this backend is serving older code" without a restart.

A ``hermes update`` moves the tree; the dashboard keeps the code it booted from. The check payload
carries both commits so the GUI can compare them instead of trusting the checkout alone.
"""

from __future__ import annotations

import subprocess

from hermes_cli import web_server
from hermes_cli.web_routers import actions


def _head() -> str:
    return subprocess.run(["git", "-C", str(web_server.PROJECT_ROOT), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()


def test_process_behind_when_the_booted_commit_differs(monkeypatch):
    monkeypatch.setattr(web_server, "STARTUP_SHA", "0" * 40)

    fields = actions._process_code_fields()

    assert fields["process_behind"] is True
    assert fields["running_sha"] == "0" * 40
    assert fields["current_sha"] == _head()


def test_current_when_the_booted_commit_matches(monkeypatch):
    monkeypatch.setattr(web_server, "STARTUP_SHA", _head())

    assert actions._process_code_fields()["process_behind"] is False


def test_unreadable_boot_commit_stays_quiet(monkeypatch):
    monkeypatch.setattr(web_server, "STARTUP_SHA", None)

    assert actions._process_code_fields()["process_behind"] is False
