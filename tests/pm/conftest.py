"""Shared fixtures for the PM suite."""
from pathlib import Path

import pytest


@pytest.fixture
def isolated_machine_home(tmp_path, monkeypatch):
    """Machine-scoped PM state (store, caches, profiles root) lands under tmp_path.

    Opt in per module with ``pytestmark = pytest.mark.usefixtures("isolated_machine_home")``.
    Does not cross a subprocess boundary — tests that spawn set HOME/USERPROFILE themselves.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
