"""``HermesCLI._session_db`` backed by the shared gateway session (CALLMAP §3.5).

Main's classic CLI holds a ``SessionDB`` writer. Rewired, the gateway is the single writer:

- reads (resume lookup, recap history, titles, the /resume picker, safety checks) go to a
  read-only ``state.db`` reader in this process, which the maintainer allows;
- every write is a gateway verb (``session.create`` / ``session.resume`` / ``session.mutate`` /
  ``session.detach``) or a documented no-op where the gateway already owns the work
  (transcript flushes, token accounting, maintenance);
- anything not yet on the gateway raises ``GatewayClientError`` (a ``ValueError``), so main's
  existing ``except`` paths print it instead of writing locally.

The session id is the gateway's: :meth:`open_session` creates or resumes the session before the
banner and adopts the id it returns.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import uuid

from hermes_cli.gateway_client import GatewayClientError
from hermes_cli.gateway_connection import GatewayConnection

logger = logging.getLogger(__name__)

# Read-only SessionDB methods main's CLI calls (served by the WAL reader).
_READS = frozenset({
    "get_session", "get_session_title", "get_session_by_title", "resolve_resume_session_id",
    "get_messages_as_conversation", "get_resume_conversations", "assert_resume_safe",
    "is_kanban_owned_session", "list_sessions_rich", "search_sessions", "get_next_title_in_lineage",
    "get_handoff_state", "get_meta", "get_compression_tip", "resolve_session_by_title", "get_messages",
    "session_count", "sanitize_title", "session_yolo_enabled",
})
# Writes the gateway already performs itself (persistence, accounting, maintenance).
_GATEWAY_OWNED = frozenset({
    "create_session", "reopen_session", "append_messages_batch", "flush_token_counts",
    "update_session_model", "patch_session_model_config", "maybe_auto_archive",
    "maybe_auto_prune_and_vacuum", "set_meta", "update_token_counts", "close",
})
_COUNTED_REPAIRS = frozenset({"prune_empty_ghost_sessions", "finalize_orphaned_compression_sessions"})
# Launch options main's ``cli.main`` takes that are creation policy on the gateway session.
_LAUNCH_POLICY = ("model", "provider", "toolsets", "reasoning", "api_key", "base_url", "max_turns",
                  "ignore_rules", "checkpoints", "pass_session_id", "skills")


def _noop(*_a, **_k):
    return None


def _zero(*_a, **_k):
    return 0


class GatewaySessionStore:
    """Truthy while the gateway connection is usable (main's ``if self._session_db:`` guards)."""

    def __init__(self, connection=None):
        self.connection = connection or GatewayConnection.shared()
        self.snapshots: dict[str, dict] = {}
        self.current: dict | None = None
        self._reader = None

    def __bool__(self):
        return not self.connection.closed

    # -- reads -----------------------------------------------------------------------------
    def _db(self):
        """The read-only reader, opened on first use. No state.db yet (the gateway creates it on its
        first start, after the CLI's startup maintenance probe) is retried on the next read."""
        if self._reader is None:
            from hermes_constants import get_hermes_home
            path = get_hermes_home() / "state.db"
            if not path.exists():
                return None
            try:
                from hermes_state import SessionDB
                self._reader = SessionDB(db_path=path, read_only=True)
            except Exception as exc:  # health: allow BLE001 -- reads find nothing; retried on the next read
                logger.debug("read-only state.db unavailable: %s", exc)
        return self._reader

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        if name in _READS:
            if name in {"sanitize_title", "session_yolo_enabled"}:
                from hermes_state import SessionDB
                return getattr(SessionDB, name)
            db = self._db()
            if db is None:
                return _noop
            return getattr(db, name)
        if name in _GATEWAY_OWNED:
            return _noop
        if name in _COUNTED_REPAIRS:
            return _zero

        def refused(*_a, **_k):
            raise GatewayClientError(f"{name} is not on the gateway session yet; nothing was written")
        return refused

    # -- session lifecycle -----------------------------------------------------------------
    def call(self, method, **params):
        return self.connection.call(method, **params)

    def _adopt(self, snapshot):
        sid = snapshot["stored_session_id"]
        self.snapshots[sid] = snapshot
        self.current = snapshot
        self.connection.state(sid).adopt_snapshot(snapshot)
        return sid

    def attach(self, session_id):
        """``session.resume`` once per session id this viewer uses (idempotent)."""
        if session_id in self.snapshots:
            return self.snapshots[session_id]
        snapshot = self.call("session.resume", session_id=session_id)
        self._adopt(snapshot)
        return snapshot

    def open_session(self, cli, launch=None):
        """Create (or resume) the gateway session for this CLI and adopt its id. A resume target the
        gateway does not know is left as is: main's own "Session not found" path reports it."""
        if getattr(cli, "_resumed", False):
            try:
                return self._adopt(self.attach(cli.session_id))
            except GatewayClientError as exc:
                if str(exc) in {"not_found", "permission_denied"}:
                    return None
                raise
        params = _creation_policy(launch or {})
        params["cwd"] = os.path.realpath(os.getenv("TERMINAL_CWD") or os.getcwd())
        # Main's label for this row (a finite `-q`/`-Q` run is ``oneshot``, which `hermes -c` and
        # `--resume latest` count as CLI history; `--source` wins), the rule run_agent applies.
        from agent.session_source import session_source_for
        params["source"] = session_source_for("cli")
        snapshot = self.call("session.create", request_id=uuid.uuid4().hex, **params)
        return self._adopt(snapshot)

    def _revision(self, session_id):
        return self.call("session.resume", session_id=session_id)["revision"]

    def _mutate(self, session_id, operation, payload):
        return self.call("session.mutate", session_id=session_id, request_id=uuid.uuid4().hex,
                         expected_revision=self._revision(session_id), operation=operation, payload=payload)

    def set_session_title(self, session_id, title):
        self._mutate(session_id, "rename", {"title": title})
        if session_id in self.snapshots:
            self.connection.state(session_id).title = title
        return True

    def delete_session(self, session_id, sessions_dir=None):
        self.snapshots.pop(session_id, None)
        self._mutate(session_id, "delete", {})
        return True

    def end_session(self, session_id, reason=None):
        """Detach this viewer; the gateway ends an idle session itself."""
        snapshot = self.snapshots.pop(session_id, None)
        subscription = (snapshot or {}).get("subscription_id")
        if subscription and not self.connection.closed:
            try:
                self.call("session.detach", session_id=session_id, subscription_id=subscription)
            except GatewayClientError as exc:
                logger.debug("session.detach failed: %s", exc)

    def delete_session_if_empty(self, session_id, sessions_dir=None):
        """Main's quit-immediately prune, as a gateway delete: only a row with no messages, no title
        and no children (checked on the read-only reader; the gateway deletes the lineage)."""
        db = self._db()
        if db is None:
            return False
        try:
            with db._read_ctx() as conn:
                empty = conn.execute(
                    "SELECT 1 FROM sessions WHERE id = ? AND title IS NULL"
                    " AND NOT EXISTS (SELECT 1 FROM messages WHERE messages.session_id = sessions.id)"
                    " AND NOT EXISTS (SELECT 1 FROM sessions child WHERE child.parent_session_id = sessions.id)",
                    (session_id,)).fetchone()
        except Exception as exc:  # health: allow BLE001 -- a failed probe keeps the row (never deletes on doubt)
            logger.debug("empty-session probe failed: %s", exc)
            return False
        if not empty:
            return False
        try:
            return self.delete_session(session_id)
        except GatewayClientError as exc:
            logger.debug("empty session prune refused: %s", exc)
            return False

    # -- banner --------------------------------------------------------------------------
    def banner_inventory(self):
        """The current session snapshot's banner fields (``gateway/session_local_inventory``), as
        ``build_welcome_banner`` kwargs; None until a session is open."""
        info = (self.current or {}).get("info") or {}
        if "tools" not in info:
            return None
        grouped = info.get("tools") or {}
        toolset_of = {name: toolset for toolset, names in grouped.items() for name in names or []}
        availability = info.get("tool_availability") or {
            "available_toolsets": [], "unavailable_toolsets": [], "lazy_tools": [], "disabled_tools": []}
        for item in availability.get("unavailable_toolsets", []):
            for name in item.get("tools", []):
                toolset_of.setdefault(name, item.get("id", item.get("name", "")))
        return {
            "tools": [{"type": "function", "function": {"name": name}} for names in grouped.values()
                      for name in names or []],
            "get_toolset_for_tool": toolset_of.get,
            "availability": availability,
            "skills_by_category": info.get("skills") or {},
            "mcp_status": list(info.get("mcp_servers") or []),
            "enabled_toolsets": info.get("enabled_toolsets"),
            "context_length": info.get("context_length"),
            "cwd": info.get("cwd"),
            # Main's startup lines under the banner, computed in the gateway (``source: cli`` only).
            "tool_availability_warnings": info.get("tool_availability_warnings"),
            "browser_backend_notice": info.get("browser_backend_notice"),
        }


_CANONICAL_LOCAL_ID = re.compile(r"^local-([0-9a-f]{8})[0-9a-f]{56}$")


def session_label(session_id):
    """Main's 8-character banner label for a canonical durable id (``local-`` + 64 hex); every other
    id is shown as is. Display only: RPCs, /resume hints and the exit summary keep the full id."""
    match = _CANONICAL_LOCAL_ID.match(session_id or "")
    return match.group(1) if match else session_id


def _creation_policy(launch):
    """The explicit launch flags as ``session.create`` policy (the thin client's own mapping);
    unset flags leave the gateway to resolve the profile default, as main's CLI did."""
    from hermes_cli.gateway_chat import _requested_policy, env_launch_flags
    fields = {key: launch.get(key) for key in _LAUNCH_POLICY}
    if isinstance(fields.get("toolsets"), (list, tuple)):
        fields["toolsets"] = ",".join(str(item) for item in fields["toolsets"])
    policy = _requested_policy(argparse.Namespace(**fields, source=None))
    policy.pop("source", None)
    policy.update(env_launch_flags())
    return policy


def session_store():
    return GatewaySessionStore()


def create_titled_session(title):
    """``chat -c <title> --create-if-missing`` for the classic CLI: the gateway's ``session.create``
    with ``title`` (it resolves-or-creates under one owner lock), so this process writes no row.
    Main's ``_create_titled_session`` minted the id and wrote the row itself. Returns the id, or
    None when the gateway refused (the caller prints main's "could not be created" copy)."""
    params = _creation_policy({})
    params["cwd"] = os.path.realpath(os.getenv("TERMINAL_CWD") or os.getcwd())
    params["source"] = "cli"  # main's titled row is `cli` (hermes_cli.main._create_titled_session)
    try:
        snapshot = GatewayConnection.shared().call("session.create", request_id=uuid.uuid4().hex,
                                                   title=title, **params)
    except GatewayClientError as exc:
        logger.warning("Failed to create titled session %r on the gateway: %s", title, exc)
        return None
    return snapshot["stored_session_id"]
