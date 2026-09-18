"""Frozen compat surface for releases that defer manual serves via update_serve_obligations.

Releases from 2026-09-16 lazily import ``hermes_cli.update_serve_obligations``
from the NEW tree during the update tail. This tree owns the same obligation
through the durable fleet-restart-pending marker and the incarnation-based
survivor sweep (``update_abort_recovery._surviving_pre_update_serve_runtimes``),
so these names forward there instead of duplicating the write logic.
"""

from __future__ import annotations


def defer_manual_serve(runtime: dict, *, require_alive: bool = False) -> bool:
    """Transfer an identified manual runtime to its own durable restart reminder."""
    from hermes_cli.update_abort_recovery import _surviving_pre_update_serve_runtimes

    if runtime.get("kind") not in ("serve", "dashboard") or runtime.get("supervisor") != "manual-serve" or runtime.get("restart_via") != "respawn-argv":
        return False
    # Single-runtime write: the survivor sweep already persists the durable
    # marker from the whole plan; one row rides along by asking with a plan
    # shim carrying just this runtime.
    plan = type("_SingleRuntimePlan", (), {"runtimes": [_RuntimeRow(runtime)]})()
    _surviving_pre_update_serve_runtimes(plan)
    return True


def retain_receipt_manual_serves(receipt: dict) -> list[dict]:
    """Return transfers still owed so receipt rotation cannot discard failed writes."""
    rows = list((receipt.get("plan") or {}).get("runtimes") or []) + list(receipt.get("pending_manual_serves") or [])
    pending = []
    for row in rows:
        if not isinstance(row, dict) or row.get("kind") not in ("serve", "dashboard") or row.get("supervisor") != "manual-serve":
            continue
        if row not in pending:
            pending.append(row)
    return pending


def warn_pending_manual_serves(*, startup: bool = False, pending_manual: list[dict] | None = None) -> None:
    """Cheap CLI-startup hint. Never restarts; never raises."""
    from hermes_cli.update_cmd_fleet import _warn_pending_fleet_restart

    _warn_pending_fleet_restart(startup=startup)


class _RuntimeRow:
    """Attribute view over one runtime dict, for the plan-shaped shim above."""

    def __init__(self, runtime: dict):
        self.kind = runtime.get("kind")
        self.profile = runtime.get("profile", "")
        self.pid = runtime.get("pid")
        self.supervisor = runtime.get("supervisor", "")
        self.detail = runtime.get("detail") if isinstance(runtime.get("detail"), dict) else {}
