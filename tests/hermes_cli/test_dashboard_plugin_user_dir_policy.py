"""The dashboard plugin API honors ``plugins.allow_user_dir`` / ``user_dir_allowlist`` / ``plugins.disabled``.

A closed user plugins directory (``plugins.allow_user_dir: false``) kept the plugin manager's loaders from
importing anything in ``<HERMES_HOME>/plugins``, but the dashboard plugin API (``hermes dashboard`` /
``hermes serve`` and the routes a gateway mounts) still imported a user plugin's ``api`` file once
``plugins.enabled`` listed it, served its JS, and handed it to a plugin host; ``plugins.disabled`` matched
exact names only. Every dashboard path shares ``_plugin_api_mount_skip_reason``, which now applies the
plugin manager's own checks first, in the profile the request acts for and against the plugins directory
the plugin was found in (a secondary profile also sees the root's).

Driven through the real app on the consent-gate tests' multiplexed daemon stand-in; the plugin host is a
recorder there.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from unittest.mock import patch

import pytest
from fastapi import FastAPI

from hermes_cli import web_server
from hermes_cli import web_server_dashboard as dashboard
from tests.hermes_cli import test_plugin_api_consent_gate as consent_gate

daemon = consent_gate.daemon  # the multiplexed daemon stand-in fixture
_install = consent_gate._install

NAME = "closed-probe"
REFUSAL = "plugins.allow_user_dir is false"
PING, ASSET = f"/api/plugins/{NAME}/ping", f"/dashboard-plugins/{NAME}/index.js"


def _config(home, **plugins):
    plugins.setdefault("isolation", "in_process")
    (home / "config.yaml").write_text(json.dumps({"plugins": plugins}))


def _closed(home, *, allowlist=(), **plugins):
    _config(home, allow_user_dir=False, user_dir_allowlist=list(allowlist), **plugins)


def _entry(home, name=NAME):
    return next(p for p in dashboard._discover_dashboard_plugins([(home / "plugins", "user")]) if p["name"] == name)


def _imported(marker, name=NAME) -> bool:
    return marker.exists() or f"hermes_dashboard_plugin_{name}" in sys.modules


# -- the shared predicate ---------------------------------------------------------------------------------------

def test_closed_user_dir_is_refused_first_with_its_own_reason(daemon):
    _install(daemon.launch, NAME)
    _closed(daemon.launch, enabled=[NAME], disabled=[NAME])
    entry = _entry(daemon.launch)

    reason = dashboard._plugin_api_mount_skip_reason(entry, {NAME}, {NAME}) or ""

    assert REFUSAL in reason and f"'{NAME}'" in reason  # not "explicitly disabled" / "not in plugins.enabled"
    assert entry["_root"] == str(daemon.launch / "plugins")


def test_allowlisted_and_unset_keys_keep_todays_verdicts(daemon):
    _install(daemon.launch, NAME)
    entry = _entry(daemon.launch)
    _closed(daemon.launch, allowlist=[NAME], enabled=[NAME])
    assert dashboard._plugin_api_mount_skip_reason(entry, {NAME}, set()) is None
    _config(daemon.launch, enabled=[NAME])  # allow_user_dir unset
    assert dashboard._plugin_api_mount_skip_reason(entry, {NAME}, set()) is None
    assert dashboard._plugin_api_mount_skip_reason(entry, set(), set()) == "not in plugins.enabled"
    assert dashboard._plugin_api_mount_skip_reason(entry, {NAME}, {NAME}) == "explicitly disabled"
    # an entry outside every user plugins directory (bundled, project, hand-built) is not affected
    _closed(daemon.launch, enabled=[NAME])
    for source in ("bundled", "project"):
        assert dashboard._plugin_api_mount_skip_reason(dict(entry, source=source), {NAME}, set()) is None


def test_the_directory_it_was_found_in_and_the_given_config_decide(daemon, tmp_path):
    """A plugin found in a second plugins directory (the root's, scanned for a profile) is judged
    relative to that directory, by the config passed in (the requesting profile's)."""
    other = tmp_path / "root"
    _install(other, NAME)
    _config(daemon.launch, enabled=[NAME])  # the active scope's own file leaves the directory open
    entry = _entry(other)
    closed = {"plugins": {"allow_user_dir": False, "user_dir_allowlist": []}}

    assert REFUSAL in (dashboard._plugin_api_mount_skip_reason(entry, {NAME}, set(), config=closed) or "")
    allow = {"plugins": {"allow_user_dir": False, "user_dir_allowlist": [NAME]}}
    assert dashboard._plugin_api_mount_skip_reason(entry, {NAME}, set(), config=allow) is None
    assert dashboard._plugin_api_mount_skip_reason(entry, {NAME}, set()) is None


def test_a_linked_plugin_dir_and_an_undiscovered_name_are_judged_too(daemon, tmp_path):
    """The name is the entry as found in the plugins directory (a symlink to a checkout elsewhere is
    still that entry), and a request for a name discovery has not seen is judged by that name."""
    _install(tmp_path / "checkout", NAME)
    (daemon.launch / "plugins").mkdir(parents=True, exist_ok=True)
    (daemon.launch / "plugins" / NAME).symlink_to(tmp_path / "checkout" / "plugins" / NAME)
    _closed(daemon.launch, enabled=[NAME, "not-installed"])

    assert REFUSAL in (dashboard._plugin_api_mount_skip_reason(_entry(daemon.launch), {NAME}, set()) or "")
    undiscovered = {"name": "not-installed", "source": "user"}
    assert REFUSAL in (dashboard._plugin_api_mount_skip_reason(undiscovered, {"not-installed"}, set()) or "")
    _closed(daemon.launch, allowlist=[NAME, "not-installed"], enabled=[NAME, "not-installed"])
    assert dashboard._plugin_api_mount_skip_reason(_entry(daemon.launch), {NAME}, set()) is None
    assert dashboard._plugin_api_mount_skip_reason(undiscovered, {"not-installed"}, set()) is None


def test_disabled_globs_match_as_for_the_plugin_manager(daemon):
    _install(daemon.launch, NAME)
    _config(daemon.launch, enabled=[NAME])
    entry = _entry(daemon.launch)
    assert dashboard._plugin_api_mount_skip_reason(entry, {NAME}, {"closed-*"}) == "explicitly disabled"
    assert dashboard._plugin_api_mount_skip_reason(dict(entry, source="bundled"), set(), {"closed-*"}) == (
        "explicitly disabled")
    # a path-shaped glob (``platforms/*``) never matches a bundled dashboard plugin's bare name
    bundled = [p for p in dashboard._discover_dashboard_plugins() if p["source"] == "bundled"]
    assert bundled
    assert all(dashboard._plugin_api_mount_skip_reason(p, set(), {"platforms/*"}) is None for p in bundled)


# -- every path -------------------------------------------------------------------------------------------------

def test_startup_mount_skips_a_closed_user_plugin_with_the_reason(daemon, caplog):
    marker = _install(daemon.launch, NAME)
    _closed(daemon.launch, enabled=[NAME])
    web_server._dashboard_plugins_cache = None

    with caplog.at_level(logging.DEBUG, logger="hermes_cli.web_server"):
        dashboard._mount_plugin_api_routes()

    assert not dashboard._plugin_api_mounted(web_server.app, NAME)
    assert not _imported(marker)
    assert any(REFUSAL in r.getMessage() and NAME in r.getMessage() for r in caplog.records)


def test_hot_mount_and_requests_refuse_a_closed_user_plugin(daemon):
    marker = _install(daemon.launch, NAME)
    _closed(daemon.launch, enabled=[NAME])

    assert dashboard._try_hot_mount_plugin_api(NAME) is False
    assert asyncio.run(dashboard._plugin_api_request_refused(NAME)) is True  # the runtime gate, HTTP and WS
    assert not _imported(marker)


def test_host_isolation_does_not_hand_a_closed_user_plugin_to_the_plugin_host(daemon):
    marker = _install(daemon.launch, NAME)
    _closed(daemon.launch, enabled=[NAME], isolation="host")

    with patch.object(dashboard, "_mount_hosted_plugin_api") as hosted:
        mounted = dashboard._mount_one_plugin_api(FastAPI(), _entry(daemon.launch), {NAME}, set())

    assert mounted is False
    hosted.assert_not_called()
    assert dashboard._hosted_plugin_for_request(NAME) is None
    assert not _imported(marker)


def test_assets_and_plugin_list_refuse_a_closed_user_plugin(daemon):
    _install(daemon.launch, NAME)
    _closed(daemon.launch, enabled=[NAME])

    assert daemon.client.get(ASSET).status_code == 404
    assert dashboard._plugin_consent_withheld(_entry(daemon.launch))  # the filter of the plugin list

    _closed(daemon.launch, allowlist=[NAME], enabled=[NAME])  # allowlisted: served as before
    assert daemon.client.get(ASSET).status_code == 200


@pytest.mark.parametrize("where", ["own", "root"])
def test_secondary_profile_forward_judges_by_the_requesting_profile(daemon, where):
    """A secondary profile's own plugin, or the root's copy it also sees, is never forwarded to its
    plugin host once that profile's config closes the directory, whatever the launch profile says."""
    marker = _install(daemon.beta if where == "own" else daemon.launch, NAME)
    _config(daemon.launch, enabled=[NAME], isolation="host")
    _closed(daemon.beta, enabled=[NAME])

    assert daemon.client.get(PING, headers=daemon.beta_ticket()).status_code == 404
    assert daemon.client.get(ASSET, params={"profile": "beta"}).status_code == 404
    assert NAME not in consent_gate.listed(daemon)
    assert daemon.calls == [] and not _imported(marker)

    _closed(daemon.beta, allowlist=[NAME], enabled=[NAME])  # the same profile allowlists it: forwarded again
    assert daemon.client.get(PING, headers=daemon.beta_ticket()).json() == {"hosted": True}
    assert [c.home for c in daemon.calls] == [daemon.beta]
    assert not _imported(marker)
