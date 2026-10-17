"""Dashboard plugin routes resolve and stat filesystem paths off the event loop thread."""

from __future__ import annotations

import asyncio
import pathlib
import threading

import pytest
from fastapi import HTTPException

import hermes_constants
from hermes_cli.web_routers import dashboard_ui


def _record_resolve_threads(monkeypatch) -> set:
    threads: set = set()
    real_resolve = pathlib.Path.resolve

    def resolve(self, *args, **kwargs):
        threads.add(threading.current_thread())
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "resolve", resolve)
    return threads


def test_plugin_asset_path_checks_run_off_the_event_loop(monkeypatch, tmp_path):
    plugin_dir = tmp_path / "dashboard"
    plugin_dir.mkdir()
    (plugin_dir / "index.js").write_text("export {}")
    plugins = [{"name": "demo", "source": "bundled", "_dir": str(plugin_dir)}]
    monkeypatch.setattr(dashboard_ui, "_get_dashboard_plugins", lambda force_rescan=False: plugins)
    monkeypatch.setattr(dashboard_ui, "_plugin_enable_sets", lambda: (set(), set()))
    expected = (plugin_dir / "index.js").resolve()
    threads = _record_resolve_threads(monkeypatch)

    response = asyncio.run(dashboard_ui.serve_plugin_asset("demo", "index.js"))

    assert response.path == expected
    # asyncio.run drives the loop on this thread.
    assert threads and threading.current_thread() not in threads


class _Request:
    def __init__(self, body: dict):
        self._body = body

    async def json(self):
        return self._body


def test_activate_resolves_profile_home_off_the_event_loop(monkeypatch, tmp_path):
    monkeypatch.setattr(dashboard_ui, "_require_token", lambda _request: None)
    threads: set = set()

    def profile_name_for_home(_home) -> None:
        threads.add(threading.current_thread())

    monkeypatch.setattr(hermes_constants, "profile_name_for_home", profile_name_for_home)

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(dashboard_ui.post_agent_plugin_activate(_Request({"name": "demo", "home": str(tmp_path)})))
    assert excinfo.value.status_code == 400
    assert threads and threading.current_thread() not in threads
