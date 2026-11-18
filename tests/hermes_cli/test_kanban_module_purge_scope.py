"""The kanban fresh-home fixtures purge ``hermes_cli*`` for a WINDOW, not for the session.

Pinned here because the leak is silent and ordering-dependent: a purge left in ``sys.modules``
makes every later test file patch the PRE-purge module object while production code's
function-local imports bind the fresh one. Measured 2026-11-17: 27 later ``test_kanban_worker_*``
tests dialled the real gateway and failed only in a full-directory run
(``DiscoveryError: invalid_control_response`` / the live-system guard blocking ``gateway run``).

The two fixtures are driven directly (``__wrapped__``) so the assertions can see both halves of
the contract: a fresh import DURING the fixture, the session's own objects after it.
"""

import importlib
import sys

import pytest

from tests.hermes_cli.hermes_module_purge import (
    fresh_hermes_modules,
    is_hermes_module,
    snapshot_hermes_modules,
)
from tests.hermes_cli.test_kanban_cli_dispatch_passthrough import isolated_kanban_home
from tests.hermes_cli.test_kanban_per_profile_cap import isolated_kanban_home_with_profiles


def _hermes_keys():
    return {name for name in sys.modules if is_hermes_module(name)}


def test_the_window_purges_forwards_and_restores_backwards():
    import hermes_cli.kanban_db  # make sure the session has a tree to restore

    before = snapshot_hermes_modules()
    before_keys = _hermes_keys()

    with fresh_hermes_modules() as saved:
        assert saved == before
        assert _hermes_keys() == set()          # the test really gets a clean import
        fresh = importlib.import_module("hermes_cli.kanban_db")
        assert fresh is not before["hermes_cli.kanban_db"]   # ... and a genuinely fresh one

    assert _hermes_keys() == before_keys        # nothing first imported inside the window leaked
    for name, module in before.items():
        assert sys.modules[name] is module      # the session's own objects are back by identity


@pytest.mark.parametrize("fixture", [isolated_kanban_home, isolated_kanban_home_with_profiles],
                         ids=["cli-dispatch-passthrough", "per-profile-cap"])
def test_the_two_kanban_fixtures_restore_the_modules_they_purged(fixture, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    importlib.import_module("hermes_cli.kanban_db")

    before = snapshot_hermes_modules()
    before_keys = _hermes_keys()

    gen = fixture.__wrapped__(monkeypatch)
    next(gen)                                   # fixture setup
    try:
        # Isolation is real while the test runs: the session's tree is no longer what an import
        # finds (this is what the fixtures are for).
        assert sys.modules.get("hermes_cli") is not before["hermes_cli"]
        fresh = importlib.import_module("hermes_cli.kanban_db")
        assert fresh is not before["hermes_cli.kanban_db"]
    finally:
        with pytest.raises(StopIteration):
            next(gen)                           # fixture teardown

    assert _hermes_keys() == before_keys
    for name, module in before.items():
        assert sys.modules[name] is module


def test_a_later_import_sees_the_module_the_production_code_reads():
    """The seam the leak broke: ``run`` imports function-locally, the test patches at module level.

    ``kanban_worker_client.run`` does ``from hermes_cli.gateway_client import connect_gateway``
    inside the call, so a patch only reaches it if ``sys.modules`` still holds the module the
    test file imported at collection. A purge left behind breaks exactly this identity.
    """
    import hermes_cli.gateway_client as collection_time

    def _the_way_the_worker_imports_it():
        from hermes_cli.gateway_client import connect_gateway
        return connect_gateway

    assert collection_time is sys.modules["hermes_cli.gateway_client"]
    assert _the_way_the_worker_imports_it() is collection_time.connect_gateway
