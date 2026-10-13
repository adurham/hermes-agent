"""Ink's session utilities on the shared owner: ``/save`` (``session.save``) and ``/reload``
(``reload.env``).

Main's per-TUI sidecar served these from its own session table, which never holds a shared-gateway
session, so the canonical TUI refused them. Each verb reuses main's helpers rather than re-deriving
them. ``dispatch`` already runs every handler under the owning profile's scope (``owner_scope``) and
``asyncio.to_thread`` carries that context into the worker thread.
"""
import asyncio
from datetime import datetime
from functools import partial
from pathlib import Path

from gateway.session_busy_controls import authorize
from hermes_state_runtime import RuntimeStoreError


def handlers(connection):
    return {'session.save': partial(save_session, connection),
            'reload.env': partial(reload_env, connection)}


def _profile_home(authority):
    """The owning profile's home (a symbolic test-peer id falls back to the scoped home)."""
    home = Path(str(authority.profile_id))
    if home.is_absolute() and home.is_dir():
        return home
    from hermes_constants import get_hermes_home
    return get_hermes_home()


def _multiplexed(authority):
    """Every turn runs under a per-profile secret scope (``owner_scope``'s own predicate)."""
    return bool(getattr(getattr(authority.runner, 'config', None), 'multiplex_profiles', False))


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

    def write():
        from hermes_cli.session_export import load_save_snapshot, render_session_for_save
        from hermes_state import SessionExportTooLargeError
        try:
            data = load_save_snapshot(authority.db, target, 'json')
        except SessionExportTooLargeError as exc:
            raise RuntimeStoreError('export_too_large') from exc
        if not data:
            raise RuntimeStoreError('not_found')
        path = (_profile_home(authority) / 'sessions' / 'saved'
                / f'hermes_conversation_{datetime.now():%Y%m%d_%H%M%S}.json')
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(render_session_for_save(data, 'json'), encoding='utf-8')
        except OSError as exc:
            raise RuntimeStoreError('storage_unavailable') from exc
        return {'file': str(path)}
    return await asyncio.to_thread(write)


async def reload_env(connection, ref, params):
    """``/reload``: re-read the profile's ``.env`` (main's ``reload.env`` / CLI ``/reload``).

    Single-profile owner: main's ``hermes_cli.config.reload_env()`` verbatim — the owner's
    ``os.environ`` is what its turns read, exactly as the TUI's own sidecar process was. Built agents
    keep their credential pool until ``/new`` (main's documented behaviour).

    Multiplexed owner: ``os.environ`` is shared by every served profile and turns never read profile
    secrets from it, so it is left alone. Each turn already re-reads ``.env`` into its own scope; what
    can be stale is the profile's external secret-source snapshot, which is re-pulled here. The count
    is the number of keys whose value in this profile's secret scope changed.
    """
    # Addressed at one session the caller controls: a sessionless frame stays refused, as it was
    # before this verb existed (a bare grant must not rewrite the owner's environment).
    _live_ref(connection, ref, params, {'session_id', 'profile'}, 'session:control')
    authority = connection.authority

    def reload():
        if not _multiplexed(authority):
            from hermes_cli.config import reload_env as reload_process_env
            return reload_process_env()
        from agent.secret_scope import build_profile_secret_scope, invalidate_env_file_cache
        from hermes_cli.env_loader import hydrate_profile_secret_sources, reset_secret_source_cache
        home = _profile_home(authority)
        before = build_profile_secret_scope(home)
        invalidate_env_file_cache(home / '.env')
        reset_secret_source_cache(home)
        hydrate_profile_secret_sources(home)
        after = build_profile_secret_scope(home)
        return sum(before.get(key) != after.get(key) for key in before.keys() | after.keys())
    return {'updated': int(await asyncio.to_thread(reload))}
