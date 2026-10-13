"""The terminal runs the user's own tools first and Hermes's after them (pm.user_facing)."""
from __future__ import annotations

import os

import pm.install
import pm.user_facing
from pm.user_facing import user_facing_path


def test_user_tools_come_before_hermes_and_python_stops_at_the_store_stub(tmp_path, monkeypatch):
    npm, node, python, rg = (str(tmp_path / "store" / name) for name in ("npm", "node", "python", "rg"))
    stub = tmp_path / "AppData" / "Local" / "Microsoft" / "WindowsApps"
    stub.mkdir(parents=True)
    monkeypatch.setattr(pm.install, "_store_path_dirs", lambda: [npm, node, python, rg])
    monkeypatch.setattr(pm.user_facing, "_python_dirs", lambda: {os.path.normcase(python)})
    path = os.pathsep.join([npm, node, python, rg, "/usr/bin", str(stub), "/home/u/bin"])

    # The stub alias would open the Store, so a machine without Python still runs ours.
    assert user_facing_path(path).split(os.pathsep) == [
        "/usr/bin", python, str(stub), "/home/u/bin", npm, node, rg]

    # A real Store Python ships python3.N.exe beside its alias: then the user's Python wins.
    (stub / "python3.13.exe").write_bytes(b"")
    assert user_facing_path(path).split(os.pathsep) == [
        "/usr/bin", str(stub), "/home/u/bin", python, npm, node, rg]

    monkeypatch.setattr(pm.install, "_store_path_dirs", list)
    assert user_facing_path(path) == path
