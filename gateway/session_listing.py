"""Read-only native picker history; enumeration never adopts a transcript."""
from hermes_state_local_migration import validate_legacy_row
from hermes_state_mutation_binding import authorize_history
from hermes_state_runtime import RuntimeStoreError


def stored_picker_sessions(authority, actor, *, limit, include_hidden=False):
    """Offer legacy local rows only to the same native transport that may resume them.

    Canonical sessions are already in the live feed. Old local rows have no policy
    receipt until explicit resume; listing them must not claim or restore anything.
    """
    if actor.profile_id != authority.profile_id:
        raise RuntimeStoreError('profile_mismatch')
    if (getattr(authority, '_native_legacy_transports', {}).get(actor.transport_id) != actor
            or not {'session:read', 'session:create'} <= actor.capabilities):
        return {'sessions': [], 'scope': 'stored'}
    db = authority.db
    sessions, offset = [], 0
    while len(sessions) < limit:
        rows = db.list_sessions_rich(sources=['cli', 'tui', 'gui'], limit=200, offset=offset,
            order_by_last_active=True, compact_rows=True, include_hidden=include_hidden)
        for row in rows:
            sid = db.get_compression_tip(row['id']) or row['id']
            tip = db.get_session(sid)
            if tip is None:
                continue
            try:
                validate_legacy_row(db, tip)
                with db._read_ctx() as conn:
                    if not authorize_history(conn, actor, sid):
                        continue
            except RuntimeStoreError as exc:
                if exc.reason not in {'not_found', 'permission_denied'}:
                    raise
                continue
            sessions.append({'id': row['id'], 'session_id': sid, 'resolved_id': sid,
                'title': row.get('title') or '', 'source': tip['source'],
                'started_at': row.get('started_at'), 'message_count': row.get('message_count', 0),
                'running': False})
            if len(sessions) == limit:
                break
        if len(rows) < 200:
            break
        offset += len(rows)
    return {'sessions': sessions, 'scope': 'stored'}
