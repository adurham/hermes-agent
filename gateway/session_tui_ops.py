"""Ink's session utilities on the shared owner: ``/save`` (``session.save``).

Main's per-TUI sidecar served these from its own session table, which never holds a shared-gateway
session, so the canonical TUI refused them. Each verb here acts on one authority session (or, for
the documented process-wide ones, on this owner) and reuses main's helpers rather than re-deriving
them; blocking work runs off the loop under the owning profile's runtime scope.
"""
import asyncio
from contextlib import nullcontext
from datetime import datetime
from functools import partial
from pathlib import Path

from gateway.session_busy_controls import authorize
from hermes_state_runtime import RuntimeStoreError


def handlers(connection):
    return {'session.save': partial(save_session, connection)}


def _profile_home(authority):
    """The owning profile's home, or None for a symbolic (test peer) id."""
    home = Path(str(authority.profile_id))
    return home if home.is_absolute() and home.is_dir() else None


def _profile_scope(authority):
    """Bind the owning profile's home + secrets for one off-loop call (enter it IN the thread:
    ``to_thread`` copies the caller's context, so a scope entered on the loop would leak)."""
    home = _profile_home(authority)
    if home is None:
        return nullcontext()
    from gateway.run import _profile_runtime_scope
    return _profile_runtime_scope(home, hydrate_secrets=False)


def _live_ref(connection, ref, params, fields, capability):
    if set(params) - fields or not isinstance(ref.session_id, str) or not ref.session_id:
        raise RuntimeStoreError('invalid_params')
    authorize(connection, ref, params, capability)
    return connection.authority.sessions[ref.session_id]


async def save_session(connection, ref, params):
    """``/save``: main's ``session.save`` — the stored transcript as a ``/save json`` snapshot in the
    profile's ``sessions/saved/``. Reads the conversation's CURRENT physical row (a local session's
    reset/compression child), like main's ``_submit_row_target_key``."""
    _live_ref(connection, ref, params, {'session_id', 'profile'}, 'session:read')
    authority = connection.authority
    from gateway.session_ancillary import physical_target
    target = physical_target(authority, ref)
    home = _profile_home(authority)

    def write():
        from hermes_cli.session_export import load_save_snapshot, render_session_for_save
        from hermes_constants import get_hermes_home
        from hermes_state import SessionExportTooLargeError
        with _profile_scope(authority):
            try:
                data = load_save_snapshot(authority.db, target, 'json')
            except SessionExportTooLargeError as exc:
                raise RuntimeStoreError('export_too_large') from exc
            if not data:
                raise RuntimeStoreError('not_found')
            path = ((home or get_hermes_home()) / 'sessions' / 'saved'
                    / f'hermes_conversation_{datetime.now():%Y%m%d_%H%M%S}.json')
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(render_session_for_save(data, 'json'), encoding='utf-8')
            except OSError as exc:
                raise RuntimeStoreError('storage_unavailable') from exc
        return {'file': str(path)}
    return await asyncio.to_thread(write)
