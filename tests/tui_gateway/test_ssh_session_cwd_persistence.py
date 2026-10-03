"""An ssh session's launch directory is a host path: kept in memory, never persisted as its remote workspace.

A resume adopts a stored ssh cwd as the remote workspace, so a persisted launch directory (in the Docker image
``/opt/hermes``, ``/opt/data`` or ``/opt/data/home``) made every remote terminal and file call ``cd`` into a path that
only exists on the Hermes host.
"""

from __future__ import annotations

import contextlib

import pytest

import tui_gateway.server as server
from hermes_state import SessionDB


class _ImmediateThread:
    def __init__(self, *, target, **_kwargs):
        self._target = target

    def start(self):
        self._target()


@pytest.fixture
def backend(monkeypatch):
    def use(name: str) -> None:
        monkeypatch.setattr(server, "_effective_terminal_backend", lambda: name)

    use("ssh")
    return use


@pytest.fixture
def db(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / "state.db")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server.threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(server.git_probe, "branch", lambda _cwd: None)
    monkeypatch.setattr(server.git_probe, "common_repo_root", lambda _cwd: None)
    yield db
    db.close()


def _hydrate(db, key: str, session: dict) -> dict:
    sid = f"sid-{key}"
    server._sessions[sid] = session
    try:
        server._hydrate_session_cwd(sid, key, db, None)
    finally:
        server._sessions.pop(sid, None)
    return session


def _hermes_home_subdir(name: str) -> str:
    path = server.get_hermes_home() / name
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def test_ssh_launch_dir_is_not_persisted(backend, tmp_path):
    launch = str(tmp_path)
    assert server._persisted_session_cwd({"source": "tui", "cwd": launch}) is None


def test_ssh_explicit_cwd_is_persisted(backend):
    session = {"source": "tui", "cwd": "/home/me/proj", "explicit_cwd": True}
    assert server._persisted_session_cwd(session) == "/home/me/proj"


@pytest.mark.parametrize("name", ["local", "docker"])
def test_host_backends_still_persist_the_launch_dir(backend, tmp_path, name):
    backend(name)
    assert server._persisted_session_cwd({"source": "tui", "cwd": str(tmp_path)}) == str(tmp_path)


def test_resume_keeps_a_stored_remote_workspace(backend, db, tmp_path):
    db.create_session("picked", source="tui", model="m", cwd="/home/me/proj")
    session = _hydrate(db, "picked", {"session_key": "picked", "source": "tui", "cwd": str(tmp_path)})

    assert session["cwd"] == "/home/me/proj"
    assert session["explicit_cwd"] is True


def test_resume_without_a_stored_cwd_does_not_persist_the_launch_dir(backend, db, tmp_path):
    db.create_session("fresh", source="tui", model="m")
    session = _hydrate(db, "fresh", {"session_key": "fresh", "source": "tui", "cwd": str(tmp_path)})

    assert session["cwd"] == str(tmp_path)
    assert not session.get("explicit_cwd")
    assert db.get_session("fresh")["cwd"] is None


@pytest.mark.parametrize("name", ["local", "docker"])
def test_host_backends_resume_unchanged(backend, db, tmp_path, name):
    """Docker installs on the local or docker backend keep adopting a stored cwd under HERMES_HOME."""
    backend(name)
    stored = _hermes_home_subdir("home")
    db.create_session("host", source="tui", model="m", cwd=stored)
    session = _hydrate(db, "host", {"session_key": "host", "source": "tui", "cwd": str(tmp_path)})

    assert session["cwd"] == stored
    assert not session.get("explicit_cwd")

    db.create_session("host-fresh", source="tui", model="m")
    _hydrate(db, "host-fresh", {"session_key": "host-fresh", "source": "tui", "cwd": str(tmp_path)})
    assert db.get_session("host-fresh")["cwd"] == str(tmp_path)


@pytest.mark.parametrize(("explicit", "expected"), [(False, None), (True, "/home/me/proj")])
def test_ssh_branch_seed_skips_the_launch_dir(backend, monkeypatch, explicit, expected):
    seen = {}
    monkeypatch.setattr(server, "_session_db", lambda _record: contextlib.nullcontext(object()))
    monkeypatch.setattr(server, "_branch_title", lambda *_a: "t")
    monkeypatch.setattr(server, "_persist_branch", lambda *_a, cwd, **_k: seen.setdefault("cwd", cwd))
    record = {"cwd": "/home/me/proj" if explicit else "/opt/hermes", "explicit_cwd": explicit}

    server._seed_branch_row(record, "child", "parent", [], "desktop", None)

    assert seen["cwd"] == expected
