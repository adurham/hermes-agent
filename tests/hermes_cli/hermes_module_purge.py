"""``sys.modules`` purge of the ``hermes_cli`` tree as a WINDOW, not a side effect.

A few fixtures need modules imported *against a fresh ``HERMES_HOME``*: config paths are resolved
at import time (and several modules cache the answers), so an already-imported
``hermes_cli.config`` / ``hermes_cli.kanban_db`` answers for the previous home. The way those
fixtures got a fresh import was to delete every ``hermes_cli*`` / ``hermes_state`` /
``hermes_constants`` entry from ``sys.modules`` — and never put them back.

Deleting the entries is not enough, because ``sys.modules`` is shared by the whole pytest session:

  * every test file that did ``from hermes_cli import gateway_client`` at COLLECTION keeps the
    pre-purge module object;
  * production code that imports function-locally (``kanban_worker_client.run`` does
    ``from hermes_cli.gateway_client import connect_gateway`` inside the call) binds the FRESH
    module.

A ``monkeypatch.setattr(gateway_client, "connect_gateway", fake)`` is then invisible to the code
under test, so the test dials the REAL gateway. Measured 2026-11-17 on ``pytest tests/hermes_cli/
test_kanban*.py``: 27 later tests (``test_kanban_worker_socket_recovery`` 24,
``test_kanban_worker_owner_wait`` 2, ``test_kanban_worker_session_source`` 1) failed ONLY in the
directory run — the live-system guard blocked their ``gateway run`` spawn or a real discovery
answer came back as ``DiscoveryError: invalid_control_response``. Exceptions built in
``@pytest.mark.parametrize`` are worse still: they are instances of the STALE class, which the
fresh ``except`` clause does not catch.

So the purge is scoped: callers use the context manager (or ``purge_hermes_modules`` with their
own restore), and the rest of the session sees the objects it imported. A test that genuinely
needs the fresh modules to stay must rebind its own callers (``_rebind_modules`` in
``test_kanban_worker_restart_patience.py``), never leave the purge behind.
"""

from __future__ import annotations

from contextlib import contextmanager
import sys
from typing import Any, Iterator

_HERMES_PREFIXES = ("hermes_cli", "hermes_state")
_HERMES_EXACT = ("hermes_constants",)


def is_hermes_module(name: str) -> bool:
    """True for the modules a fresh-home fixture must forget (and later restore)."""
    return name.startswith(_HERMES_PREFIXES) or name in _HERMES_EXACT


def snapshot_hermes_modules() -> dict[str, Any]:
    """The session's current ``hermes_cli`` tree: what ``restore_hermes_modules`` puts back."""
    return {name: mod for name, mod in sys.modules.items() if is_hermes_module(name)}


def restore_hermes_modules(saved: dict[str, Any]) -> None:
    """Undo a purge: drop anything first imported since, then put the saved objects back.

    Both halves matter. ``sys.modules.update(saved)`` alone would leave a module that was
    first imported inside the window in place, so a later ``import`` would still get the fresh
    copy; dropping first makes the key set exactly what it was.
    """
    purge_hermes_modules()
    sys.modules.update(saved)


def purge_hermes_modules() -> None:
    """Forget every hermes package module, so the next import answers for the CURRENT home."""
    for name in [n for n in sys.modules if is_hermes_module(n)]:
        del sys.modules[name]


@contextmanager
def fresh_hermes_modules() -> Iterator[dict[str, Any]]:
    """Purge the hermes module tree for the duration of the block.

    Inside the block ``import hermes_cli.kanban_db`` builds a new module object against the
    caller's environment (``HERMES_HOME`` etc.); on exit the session's own objects are back in
    ``sys.modules``, so later test files patch the modules the production code reads.

    Code that needs the fresh object after the block must keep its own reference to it: on exit
    the fresh modules are forgotten, exactly as they would be in a process that never imported
    them. That is deliberate — the block is a window, not a fork.
    """
    saved = snapshot_hermes_modules()
    purge_hermes_modules()
    try:
        yield saved
    finally:
        restore_hermes_modules(saved)
