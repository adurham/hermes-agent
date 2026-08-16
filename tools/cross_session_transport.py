"""Transport B — cross-process agent messaging backed by ``state.db``.

Design: ``docs/design/cross-session-messaging.md`` (all core mechanics) and
``docs/design/local-agent-messaging.md`` ("Transport B", which extends scope
to subagents but explicitly keeps them OUT of this transport's registry).

This module owns the durable half of local agent messaging: the
``cross_session_registry`` (who is reachable) and the ``cross_session_inbox``
(what is queued for them). It deliberately knows nothing about Transport A —
the two meet only at the ``resolve_transport`` seam in
``tools.agent_messaging_contract``.

Scope invariants this module must never violate:

* **Sessions only.** ``delegate_task`` subagents never get a registry row.
  A subagent lives seconds; a heartbeat/reap cost for it is disproportionate.
  Cross-process addressing always names a top-level session, and it is that
  session's own in-process logic (Transport A) that decides whether to relay
  further. There is no subagent-facing path here, by design.
* **Every delivered body is marker-wrapped.** Both the mid-turn and the idle
  delivery paths route through ``build_agent_message_marker()``. A bare body
  reaching a transcript is indistinguishable from operator input and defeats
  the untrusted-content framing the whole threat model rests on.
* **Policy is enforced at DRAIN time**, not send time. The recipient
  evaluates its OWN current config when claiming a row. Send-time-only
  enforcement is bypassable and races a policy change.

Delivery is at-most-once, stated deliberately rather than mislabeled: the
claim (``UPDATE ... SET status='delivered' WHERE id=? AND status='pending'``)
commits before injection, so a crash in between drops the message. Round 3
of the predecessor doc accepted that tradeoff for v1 — an occasional dropped
cross-session note is low-stakes, and true at-least-once would need an
intermediate unacked state plus a redelivery timeout.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional

from hermes_constants import get_hermes_home
from tools.agent_messaging_contract import (
    CROSS_SESSION_HEARTBEAT_SECONDS,
    CROSS_SESSION_REGISTRY_REAP_SECONDS,
    HELD_MESSAGE_EXPIRY_SECONDS,
    DeliveryOutcome,
    Participant,
    ParticipantKind,
    SendResult,
    SessionOrigin,
    TransportKind,
    build_agent_message_marker,
    check_message_size,
    register_transport,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Inbox status state machine
# ---------------------------------------------------------------------------
#
#   pending  --drain, policy=accept--> delivered
#   pending  --drain, policy=hold----> held        (awaits a human)
#   pending  --drain, policy=refuse--> denied
#   held     --`hermes agents inbox --approve`--> pending  (re-drains normally)
#   held     --`hermes agents inbox --deny`-----> denied
#   held     --expires_at elapsed---------------> expired
#   pending  --recipient reaped from registry---> expired
#
# The held -> pending transition is the one the predecessor doc flagged as
# missing: approving does NOT deliver directly, it returns the row to the
# normal pending queue so the recipient's own drain claims it as usual.

STATUS_PENDING = "pending"
STATUS_HELD = "held"
STATUS_DELIVERED = "delivered"
STATUS_DENIED = "denied"
STATUS_EXPIRED = "expired"

# Inbound policies (config.yaml ``cross_session.inbound``).
POLICY_ACCEPT = "accept"
POLICY_HOLD = "hold"
POLICY_REFUSE = "refuse"

# Per the predecessor doc's "Inbound policy" defaults, deliberately more
# conservative than Claude Code's for the surfaces where an unexpected
# message would be genuinely disruptive.
_DEFAULT_INBOUND_BY_ORIGIN = {
    SessionOrigin.CLI: POLICY_HOLD,
    SessionOrigin.GATEWAY: POLICY_REFUSE,
    SessionOrigin.ACP: POLICY_HOLD,
    SessionOrigin.CRON: POLICY_REFUSE,
}

# ---------------------------------------------------------------------------
# Throttling constants — required for v1, not optional (predecessor doc's
# "Throttling — non-optional for v1").
# ---------------------------------------------------------------------------

# Reject sends beyond this chain depth. NOTE, per Round 3 finding 8: this
# bounds TOTAL conversation depth including human-supervised exchanges, not
# only autonomous ping-pong. A real back-and-forth a human is steering hits
# the same ceiling as a runaway loop. That is a stated, accepted tradeoff.
HOP_COUNT_CEILING = 4

# Per-sender-pair rate cap: at most N messages per rolling window. Uses
# idx_inbox_sender_pair.
RATE_CAP_MESSAGES = 5
RATE_CAP_WINDOW_SECONDS = 60.0

# Identical-body repeat suppression window.
REPEAT_SUPPRESSION_SECONDS = 60.0

# Hard per-turn ceiling: one send_agent_message call per turn. A real v1
# tradeoff against a fan-out/coordinator use case, stated explicitly.
MAX_SENDS_PER_TURN = 1


# ---------------------------------------------------------------------------
# Connection handling
# ---------------------------------------------------------------------------
#
# WAL + a real busy_timeout are BOTH verified present here rather than
# assumed (predecessor doc Round 3 finding 7 asked for exactly this):
#
# * ``SessionDB``'s own connection (hermes_state.py) calls
#   ``apply_wal_with_fallback`` and ``PRAGMA foreign_keys=ON``, and routes
#   writes through ``_execute_write`` (BEGIN IMMEDIATE + jittered retry).
# * This module opens its own short-lived connections the same way
#   ``tools/async_delegation.py`` already does for the same database, and
#   additionally sets an explicit ``busy_timeout``. async_delegation relies
#   on ``sqlite3.connect(timeout=...)`` alone; we set the PRAGMA too so the
#   value holds regardless of driver mapping.


_BUSY_TIMEOUT_MS = 10_000


def _db_path():
    return get_hermes_home() / "state.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=_BUSY_TIMEOUT_MS / 1000.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        # cross_session_subagents.owner_session_id FKs to
        # cross_session_registry(session_id) ON DELETE CASCADE so a crashed
        # session's subagent rows are reaped along with its own registry row
        # -- but SQLite only enforces/cascades FKs on connections that opt in.
        # reap_stale_registry() below also does the delete explicitly (belt
        # and suspenders, matching the inbox cleanup in the same function),
        # so this PRAGMA is defense-in-depth, not the only mechanism.
        conn.execute("PRAGMA foreign_keys=ON")
        from hermes_state import apply_wal_with_fallback

        apply_wal_with_fallback(conn, db_label="state.db (cross_session)")
        _ensure_tables(conn)
    except Exception:
        conn.close()
        raise
    return conn


def _ensure_tables(conn: sqlite3.Connection) -> None:
    """Create the two tables if absent.

    ``SCHEMA_SQL`` in ``hermes_state_common.py`` is the source of truth and
    normally wins the race, but this module can be reached by a process that
    never opened a full ``SessionDB`` (the ``hermes agents inbox`` CLI, for
    one). Keeping the DDL idempotent here mirrors what async_delegation.py
    already does for its own table.
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS cross_session_registry (
            session_id      TEXT PRIMARY KEY,
            name            TEXT NOT NULL,
            cwd             TEXT,
            platform        TEXT,
            profile         TEXT NOT NULL,
            session_origin  TEXT,
            pid             INTEGER,
            last_heartbeat  REAL NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS cross_session_inbox (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            from_session_id TEXT NOT NULL,
            from_name       TEXT NOT NULL,
            to_session_id   TEXT NOT NULL,
            body            TEXT NOT NULL,
            status          TEXT NOT NULL,
            hop_count       INTEGER NOT NULL DEFAULT 0,
            created_at      REAL NOT NULL,
            delivered_at    REAL,
            expires_at      REAL
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_inbox_recipient "
        "ON cross_session_inbox(to_session_id, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_inbox_sender_pair "
        "ON cross_session_inbox(from_session_id, to_session_id, created_at)"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS cross_session_subagents (
            subagent_id      TEXT PRIMARY KEY,
            owner_session_id TEXT NOT NULL,
            goal             TEXT,
            cwd              TEXT,
            status           TEXT NOT NULL,
            started_at       REAL NOT NULL
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_subagents_owner "
        "ON cross_session_subagents(owner_session_id)"
    )


@contextmanager
def _transaction() -> Iterator[sqlite3.Connection]:
    """Commit/rollback on exit and ALWAYS close.

    ``sqlite3.Connection.__enter__`` only manages the transaction, not the
    connection, so ``with _connect()`` alone leaks the fd (and its WAL/SHM
    handles) to the garbage collector — the bug async_delegation.py documents
    at its own ``_transaction``.
    """
    conn = _connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Registry: heartbeat, reap, lookup
# ---------------------------------------------------------------------------


def _current_profile() -> str:
    """Profile name for scoping. Profiles have separate state.db files, so
    this is belt-and-suspenders rather than the actual isolation mechanism —
    per the design, profile scoping is correct by construction and is a UX
    boundary, not a security one (the real trust boundary is the OS user).
    """
    return os.environ.get("HERMES_PROFILE", "default") or "default"


def _session_id_suffix(session_id: str) -> str:
    """Short, stable, human-readable discriminator taken from a session id.

    Session ids look like ``20260815_234533_b49c59``; the trailing random
    segment is the part that actually distinguishes two sessions started in
    the same directory, so prefer it over an arbitrary hash.
    """
    tail = session_id.rsplit("_", 1)[-1] if session_id else ""
    tail = tail or session_id
    return tail[-6:] or "session"


def _disambiguated_name(
    conn: sqlite3.Connection, *, session_id: str, name: str, cutoff: float
) -> str:
    """Return ``name``, id-suffixed when another LIVE session already uses it.

    Session display names are auto-derived (``/rename`` title, else the cwd
    folder name), so two concurrently live sessions can independently land on
    the identical string — which makes ``list_agents`` output ambiguous to a
    human reading it. Addressing already fails closed on ambiguity
    (``resolve_recipient``), so this is purely discovery/UX clarity.

    Only rows whose heartbeat is still within the staleness window count as a
    collision: historical/ended sessions keep whatever name they stored, and
    a session that has since died must not permanently poison the name.
    """
    if not name:
        return name
    try:
        row = conn.execute(
            "SELECT 1 FROM cross_session_registry "
            "WHERE name = ? AND session_id <> ? AND last_heartbeat >= ? LIMIT 1",
            (name, session_id, cutoff),
        ).fetchone()
    except Exception as exc:  # cosmetic feature; never block the heartbeat
        logger.debug("cross_session name-collision check failed: %s", exc)
        return name
    if row is None:
        return name
    return f"{name} ({_session_id_suffix(session_id)})"


def heartbeat_registry(
    *,
    session_id: str,
    name: str,
    cwd: Optional[str] = None,
    platform: Optional[str] = None,
    session_origin: Optional[SessionOrigin] = None,
    now: Optional[float] = None,
) -> bool:
    """Upsert this session's registry row. Returns True if a row was written.

    Cron sessions never register: they always ``refuse`` inbound, so paying a
    heartbeat cost for a session that can never receive is pointless (the
    predecessor doc calls this out explicitly).

    Subagents must never be passed here — this transport is session-only.
    """
    if not session_id or not name:
        return False
    if session_origin == SessionOrigin.CRON:
        return False

    ts = float(now if now is not None else time.time())
    origin_value = session_origin.value if session_origin is not None else None
    try:
        with _transaction() as conn:
            name = _disambiguated_name(
                conn,
                session_id=session_id,
                name=name,
                cutoff=ts - CROSS_SESSION_REGISTRY_REAP_SECONDS,
            )
            conn.execute(
                """INSERT INTO cross_session_registry
                       (session_id, name, cwd, platform, profile,
                        session_origin, pid, last_heartbeat)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(session_id) DO UPDATE SET
                       name=excluded.name,
                       cwd=excluded.cwd,
                       platform=excluded.platform,
                       profile=excluded.profile,
                       session_origin=excluded.session_origin,
                       pid=excluded.pid,
                       last_heartbeat=excluded.last_heartbeat""",
                (
                    session_id,
                    name,
                    cwd,
                    platform,
                    _current_profile(),
                    origin_value,
                    os.getpid(),
                    ts,
                ),
            )
        return True
    except Exception as exc:  # never break the caller's turn boundary
        logger.debug("cross_session heartbeat failed: %s", exc)
        return False


def reap_stale_registry(*, now: Optional[float] = None) -> int:
    """Delete registry rows whose heartbeat is older than the reap threshold,
    and expire any ``pending`` messages addressed to them.

    That second half closes the predecessor doc's "no lifecycle for pending
    messages targeting a session that dies" gap: without it, a message to a
    session that crashed sits pending forever with no expiry.
    """
    ts = float(now if now is not None else time.time())
    cutoff = ts - CROSS_SESSION_REGISTRY_REAP_SECONDS
    try:
        with _transaction() as conn:
            dead = [
                row["session_id"]
                for row in conn.execute(
                    "SELECT session_id FROM cross_session_registry "
                    "WHERE last_heartbeat < ?",
                    (cutoff,),
                )
            ]
            if not dead:
                return 0
            conn.executemany(
                "DELETE FROM cross_session_registry WHERE session_id = ?",
                [(sid,) for sid in dead],
            )
            conn.executemany(
                "UPDATE cross_session_inbox SET status = ? "
                "WHERE to_session_id = ? AND status IN (?, ?)",
                [(STATUS_EXPIRED, sid, STATUS_PENDING, STATUS_HELD) for sid in dead],
            )
            # Explicit delete alongside the ON DELETE CASCADE FK (see the
            # _connect() PRAGMA foreign_keys=ON comment): belt-and-suspenders
            # matching the inbox cleanup above, and the only mechanism at all
            # for any older on-disk DB whose cross_session_subagents table
            # predates the FK being added to a given creator's DDL.
            conn.executemany(
                "DELETE FROM cross_session_subagents WHERE owner_session_id = ?",
                [(sid,) for sid in dead],
            )
            return len(dead)
    except Exception as exc:
        logger.debug("cross_session reap failed: %s", exc)
        return 0


def expire_held_messages(*, now: Optional[float] = None) -> int:
    """Flip ``held`` rows past their ``expires_at`` to ``expired``."""
    ts = float(now if now is not None else time.time())
    try:
        with _transaction() as conn:
            cur = conn.execute(
                "UPDATE cross_session_inbox SET status = ? "
                "WHERE status = ? AND expires_at IS NOT NULL AND expires_at < ?",
                (STATUS_EXPIRED, STATUS_HELD, ts),
            )
            return cur.rowcount or 0
    except Exception as exc:
        logger.debug("cross_session held-expiry failed: %s", exc)
        return 0


@dataclass(frozen=True)
class RegisteredSession:
    session_id: str
    name: str
    cwd: Optional[str]
    platform: Optional[str]
    session_origin: Optional[SessionOrigin]
    pid: Optional[int]
    last_heartbeat: float

    @property
    def heartbeat_age(self) -> float:
        return max(0.0, time.time() - self.last_heartbeat)


@dataclass(frozen=True)
class RegisteredSubagent:
    """Machine-wide, read-only subagent awareness row.

    Deliberately NOT addressable by ``send_agent_message`` (Finding 7,
    docs/design/local-agent-messaging.md, still applies to the SEND path) —
    this dataclass exists only so ``list_agents`` can show what other
    sessions' subagents are doing, to help a human or model operating
    concurrent sessions notice a working-directory collision before it
    happens. No transport registers a send callable against
    ``subagent_id``.
    """

    subagent_id: str
    owner_session_id: str
    goal: Optional[str]
    cwd: Optional[str]
    status: str
    started_at: float


def _row_to_registered(row: Any) -> RegisteredSession:
    origin_raw = row["session_origin"]
    origin: Optional[SessionOrigin] = None
    if origin_raw:
        try:
            origin = SessionOrigin(origin_raw)
        except ValueError:
            origin = None
    return RegisteredSession(
        session_id=row["session_id"],
        name=row["name"],
        cwd=row["cwd"],
        platform=row["platform"],
        session_origin=origin,
        pid=row["pid"],
        last_heartbeat=float(row["last_heartbeat"]),
    )


# Length cap for a subagent's free-text goal before it's written to the
# durable, machine-wide-visible registry. Every other session's model reads
# this string, so it is untrusted cross-process content the moment it's
# written -- capped the same way check_message_size bounds message bodies,
# for the same reason (module docstring: "Every delivered body is
# marker-wrapped" / caps bound flooding, not just size).
_SUBAGENT_GOAL_MAX_CHARS = 500


def register_subagent(
    *,
    subagent_id: str,
    owner_session_id: str,
    goal: Optional[str],
    cwd: Optional[str],
    status: str = "running",
    now: Optional[float] = None,
) -> bool:
    """Upsert a durable, cross-process-visible row for a live subagent.

    Called synchronously at spawn (``tools/delegate_tool.py``), not on a
    heartbeat cadence -- see the schema comment in ``hermes_state_common.py``
    for why a periodic-refresh liveness model is wrong for something that
    typically lives seconds. The owning session's OWN heartbeat is what keeps
    this row from being reaped as stale (``reap_stale_registry`` cascades the
    delete when the owner's row goes stale), not a heartbeat of its own.
    """
    if not subagent_id or not owner_session_id:
        return False
    ts = float(now if now is not None else time.time())
    goal_text = (goal or "")[:_SUBAGENT_GOAL_MAX_CHARS] or None
    try:
        with _transaction() as conn:
            conn.execute(
                """INSERT INTO cross_session_subagents
                       (subagent_id, owner_session_id, goal, cwd, status, started_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(subagent_id) DO UPDATE SET
                       owner_session_id=excluded.owner_session_id,
                       goal=excluded.goal,
                       cwd=excluded.cwd,
                       status=excluded.status""",
                (subagent_id, owner_session_id, goal_text, cwd, status, ts),
            )
        return True
    except Exception as exc:  # never block a spawn over registry bookkeeping
        logger.debug("cross_session subagent registration failed: %s", exc)
        return False


def unregister_subagent(subagent_id: str) -> None:
    """Drop a subagent's durable row. Called synchronously at completion.

    Safe to call even if the row was never written (owner session had no
    live registry row yet, or the write failed) -- mirrors
    ``tools/delegate_tool.py``'s own ``_unregister_subagent`` for the
    in-process registry.
    """
    if not subagent_id:
        return
    try:
        with _transaction() as conn:
            conn.execute(
                "DELETE FROM cross_session_subagents WHERE subagent_id = ?",
                (subagent_id,),
            )
    except Exception as exc:
        logger.debug("cross_session subagent unregistration failed: %s", exc)


def _row_to_registered_subagent(row: Any) -> RegisteredSubagent:
    return RegisteredSubagent(
        subagent_id=row["subagent_id"],
        owner_session_id=row["owner_session_id"],
        goal=row["goal"],
        cwd=row["cwd"],
        status=row["status"],
        started_at=float(row["started_at"]),
    )


def list_registered_subagents(
    *, owner_session_id: Optional[str] = None
) -> List[RegisteredSubagent]:
    """Machine-wide live subagents, optionally filtered to one owner.

    No staleness filter of its own (unlike ``list_registered_sessions``):
    rows are written/deleted synchronously by the owning process rather than
    refreshed on a heartbeat, so "the row exists" already means "as of the
    owner's last write, this subagent was running" -- a stale row only
    survives a crashed owner until that owner's OWN registry row is reaped,
    at which point ``reap_stale_registry`` cascades the delete here too.
    """
    try:
        with _transaction() as conn:
            if owner_session_id:
                rows = conn.execute(
                    "SELECT * FROM cross_session_subagents "
                    "WHERE owner_session_id = ? ORDER BY started_at DESC",
                    (owner_session_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM cross_session_subagents ORDER BY started_at DESC"
                ).fetchall()
    except Exception as exc:
        logger.debug("cross_session subagent list failed: %s", exc)
        return []
    return [_row_to_registered_subagent(row) for row in rows]


def _cwd_overlaps(a: Optional[str], b: Optional[str]) -> bool:
    """Prefix-overlap test for working-directory collision detection.

    Equality alone is the wrong granularity for this: two sessions working
    in the SAME repo -- the common case that actually stomps -- typically
    have the same top-level cwd, but a subagent's cwd can also be a
    subdirectory a sibling's covers (e.g. ``/repo`` vs ``/repo/pkg/foo``),
    which equality would miss entirely. Either direction of containment
    counts as overlap: ``a`` under ``b``, or ``b`` under ``a``. Both paths
    are normalized (resolved, trailing slash stripped) before comparing so
    ``/repo/`` and ``/repo`` aren't treated as distinct.
    """
    if not a or not b:
        return False
    try:
        norm_a = os.path.normpath(os.path.abspath(a))
        norm_b = os.path.normpath(os.path.abspath(b))
    except Exception:
        return a == b
    if norm_a == norm_b:
        return True
    return norm_a.startswith(norm_b + os.sep) or norm_b.startswith(norm_a + os.sep)


def find_cwd_collisions(
    cwd: Optional[str], *, exclude_subagent_id: Optional[str] = None
) -> List[RegisteredSubagent]:
    """Live subagents (any owner, machine-wide) whose cwd overlaps ``cwd``.

    Used at ``delegate_task`` dispatch time (``tools/delegate_tool.py``) to
    warn -- not block; see the design note on that call site for why --
    when a new subagent is about to start work in a directory another live
    subagent is already touching, regardless of which top-level session
    owns either one. Returns an empty list (never raises) on ``cwd=None``
    or on any registry read failure, so a broken collision check can never
    block a spawn.
    """
    if not cwd:
        return []
    try:
        candidates = list_registered_subagents()
    except Exception as exc:
        logger.debug("find_cwd_collisions failed: %s", exc)
        return []
    return [
        rec
        for rec in candidates
        if rec.subagent_id != exclude_subagent_id and _cwd_overlaps(cwd, rec.cwd)
    ]


def list_registered_sessions(
    *,
    exclude_session_id: Optional[str] = None,
    now: Optional[float] = None,
) -> List[RegisteredSession]:
    """Live sessions in this profile, staleness-filtered by heartbeat age."""
    ts = float(now if now is not None else time.time())
    cutoff = ts - CROSS_SESSION_REGISTRY_REAP_SECONDS
    try:
        with _transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM cross_session_registry "
                "WHERE last_heartbeat >= ? ORDER BY last_heartbeat DESC",
                (cutoff,),
            ).fetchall()
    except Exception as exc:
        logger.debug("cross_session registry list failed: %s", exc)
        return []
    out = []
    for row in rows:
        rec = _row_to_registered(row)
        if exclude_session_id and rec.session_id == exclude_session_id:
            continue
        out.append(rec)
    return out


def resolve_recipient(
    recipient: str, *, now: Optional[float] = None
) -> Optional[RegisteredSession]:
    """Resolve ``recipient`` against the CURRENT registry, by session_id or name.

    Name resolution fails CLOSED on ambiguity rather than guessing which of
    two same-named sessions was meant (Round 3 finding 4 — ``name`` has no
    uniqueness constraint, so this has to be an explicit rule).
    """
    if not recipient:
        return None
    live = list_registered_sessions(now=now)
    for rec in live:
        if rec.session_id == recipient:
            return rec
    matches = [r for r in live if r.name == recipient]
    if len(matches) == 1:
        return matches[0]
    return None  # zero matches, or ambiguous — both are "cannot deliver"


# ---------------------------------------------------------------------------
# Transport-resolution seam
# ---------------------------------------------------------------------------


def _cross_process_lookup(sender: Participant, recipient_id: str) -> Optional[Participant]:
    """Lookup registered with ``resolve_transport`` for CROSS_PROCESS_DB.

    Correct in isolation: this must not assume Transport A's lookup ran first
    (it may not be registered at all in a given test), and must return None
    rather than raising for anything it does not own, so the fan-out in
    ``resolve_transport`` keeps working.
    """
    try:
        rec = resolve_recipient(recipient_id)
    except Exception:
        return None
    if rec is None:
        return None
    return Participant(
        participant_id=rec.session_id,
        kind=ParticipantKind.SESSION,
        owner_session_id=rec.session_id,
        parent_participant_id=None,
        session_origin=rec.session_origin,
    )


_lookup_registered = False


def _lookup_own_name(session_id: str) -> Optional[str]:
    """This session's OWN registered display name, for the ``from_name`` column.

    ``Participant`` carries no name field — only ``participant_id`` — so the
    sender's display name has to come back out of the registry. Deliberately
    reads the row directly rather than going through
    ``list_registered_sessions()``, which applies a staleness filter: a sender
    whose own heartbeat has gone stale should still be able to send, and would
    otherwise silently lose its name.

    Never raises. A missing name is a cosmetic problem, not a reason to block
    a send; the caller falls back to the raw session_id.
    """
    if not session_id:
        return None
    try:
        with _transaction() as conn:
            row = conn.execute(
                "SELECT name FROM cross_session_registry WHERE session_id = ?",
                (session_id,),
            ).fetchone()
    except Exception as exc:
        logger.debug("cross_session own-name lookup failed: %s", exc)
        return None
    if row is None:
        return None
    return row["name"] or None


def _cross_process_send(
    sender: Participant, recipient: Participant, body: str
) -> SendResult:
    """``TransportSendFn`` adapter: contract shape -> ``send_message()`` shape.

    Two things worth stating explicitly, because both are deliberate:

    1. **The recipient is passed as the ALREADY-RESOLVED ``session_id``**, not
       as the free-text string the caller typed. ``send_message()`` resolves
       its ``recipient`` param a second time, so passing the original string
       would reopen a TOCTOU window where a name that resolved uniquely during
       ``resolve_transport()`` becomes ambiguous (or stale) microseconds later.
       ``_cross_process_lookup`` always builds ``participant_id`` from
       ``rec.session_id``, which is the registry primary key and matches
       ``resolve_recipient``'s exact-id branch — so the re-resolution is a
       cheap liveness recheck rather than a second chance to pick a different
       recipient.
    2. **``hop_count`` is 0 here, and that is a known gap.** An outbound send
       made directly via the tool has no turn-scoped ``TurnMessageState``
       plumbed through to it; hop tracking exists today only on the delivery
       side, where a reply during an active drain correctly goes through
       ``TurnMessageState.next_hop_count()``. Wiring turn state through the
       tool-calling convention is out of scope for this seam fix.

    Exception contract: ``send_message()`` raises only ``MessageTooLargeError``
    (from its own ``check_message_size``) and converts every internal failure
    into a ``RECIPIENT_NOT_FOUND`` SendResult, so no transport-internal
    exception type escapes this adapter. The result is returned verbatim —
    Transport B's outcome semantics are correct as designed.
    """
    from_session_id = sender.participant_id
    return send_message(
        from_session_id=from_session_id,
        from_name=_lookup_own_name(from_session_id) or from_session_id,
        recipient=recipient.participant_id,
        body=body,
        hop_count=0,
    )


def register_lookup() -> None:
    """Plug this transport into ``resolve_transport``. Idempotent.

    Registers the lookup and the send together: a transport that is
    discoverable but not sendable is exactly the failure mode this pairing
    exists to prevent.
    """
    global _lookup_registered
    if _lookup_registered:
        return
    register_transport(
        TransportKind.CROSS_PROCESS_DB, _cross_process_lookup, _cross_process_send
    )
    _lookup_registered = True


# ---------------------------------------------------------------------------
# Inbound policy (recipient side)
# ---------------------------------------------------------------------------


def resolve_inbound_policy(
    *, session_origin: Optional[SessionOrigin] = None
) -> str:
    """This session's own current inbound policy.

    Read fresh at DRAIN time by the recipient, never trusted from the sender
    (Round 3 finding 5). ``config.yaml``'s ``cross_session.inbound`` overrides
    the per-origin default when set to a valid value.
    """
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        section = cfg.get("cross_session") or {}
        configured = section.get("inbound")
        if configured in (POLICY_ACCEPT, POLICY_HOLD, POLICY_REFUSE):
            return configured
    except Exception:
        pass
    return _DEFAULT_INBOUND_BY_ORIGIN.get(session_origin or SessionOrigin.CLI, POLICY_HOLD)


# ---------------------------------------------------------------------------
# Send path
# ---------------------------------------------------------------------------


def _rate_cap_exceeded(
    conn: sqlite3.Connection,
    *,
    from_session_id: str,
    to_session_id: str,
    now: float,
) -> bool:
    """Per-sender-pair rolling-window cap. Uses idx_inbox_sender_pair."""
    since = now - RATE_CAP_WINDOW_SECONDS
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM cross_session_inbox "
        "WHERE from_session_id = ? AND to_session_id = ? AND created_at >= ?",
        (from_session_id, to_session_id, since),
    ).fetchone()
    return int(row["n"]) >= RATE_CAP_MESSAGES


def _is_repeat_body(
    conn: sqlite3.Connection,
    *,
    from_session_id: str,
    to_session_id: str,
    body: str,
    now: float,
) -> bool:
    """Identical-body repeat suppression within a short window."""
    since = now - REPEAT_SUPPRESSION_SECONDS
    row = conn.execute(
        "SELECT 1 FROM cross_session_inbox "
        "WHERE from_session_id = ? AND to_session_id = ? AND body = ? "
        "AND created_at >= ? LIMIT 1",
        (from_session_id, to_session_id, body, since),
    ).fetchone()
    return row is not None


def send_message(
    *,
    from_session_id: str,
    from_name: str,
    recipient: str,
    body: str,
    hop_count: int = 0,
    now: Optional[float] = None,
) -> SendResult:
    """Queue a message for a registered session.

    Returns the SEND-TIME outcome only, never a promise of eventual delivery
    (the predecessor doc's Round 3 correction of an earlier draft's mistake:
    delivery happens when the recipient polls, which may be much later or
    never). ``queued`` -> QUEUED_DURABLE, ``held`` -> HELD, and every refusal
    -> RECIPIENT_NOT_FOUND with a clear detail message.

    Note the status written here is a send-time PREDICTION for UX only. The
    authoritative policy decision happens at drain time, when the recipient
    evaluates its own current config.
    """
    ts = float(now if now is not None else time.time())
    check_message_size(body)  # raises MessageTooLargeError; caller surfaces it

    rec = resolve_recipient(recipient, now=ts)
    if rec is None:
        return SendResult(
            outcome=DeliveryOutcome.RECIPIENT_NOT_FOUND,
            detail=(
                f"no live session matches '{recipient}' — it may have exited, "
                f"the name may be ambiguous across two live sessions, or the "
                f"id may be incorrect. Use list_agents to see current targets."
            ),
        )

    if hop_count > HOP_COUNT_CEILING:
        return SendResult(
            outcome=DeliveryOutcome.RECIPIENT_NOT_FOUND,
            detail=(
                f"message chain depth {hop_count} exceeds the limit of "
                f"{HOP_COUNT_CEILING}; this conversation has gone too many hops "
                f"and further sends are refused. Do not retry."
            ),
        )

    policy = resolve_inbound_policy(session_origin=rec.session_origin)
    if policy == POLICY_REFUSE:
        return SendResult(
            outcome=DeliveryOutcome.RECIPIENT_NOT_FOUND,
            detail=(
                f"'{rec.name}' does not accept incoming agent messages "
                f"(inbound policy: refuse). Do not retry."
            ),
        )

    status = STATUS_HELD if policy == POLICY_HOLD else STATUS_PENDING
    expires_at = ts + HELD_MESSAGE_EXPIRY_SECONDS if status == STATUS_HELD else None

    try:
        with _transaction() as conn:
            if _rate_cap_exceeded(
                conn,
                from_session_id=from_session_id,
                to_session_id=rec.session_id,
                now=ts,
            ):
                return SendResult(
                    outcome=DeliveryOutcome.RECIPIENT_NOT_FOUND,
                    detail=(
                        f"rate cap reached: at most {RATE_CAP_MESSAGES} messages "
                        f"per {int(RATE_CAP_WINDOW_SECONDS)}s to the same recipient. "
                        f"Do not retry immediately."
                    ),
                )
            if _is_repeat_body(
                conn,
                from_session_id=from_session_id,
                to_session_id=rec.session_id,
                body=body,
                now=ts,
            ):
                return SendResult(
                    outcome=DeliveryOutcome.RECIPIENT_NOT_FOUND,
                    detail=(
                        "an identical message was already sent to this recipient "
                        "moments ago and was suppressed as a repeat."
                    ),
                )
            conn.execute(
                """INSERT INTO cross_session_inbox
                       (from_session_id, from_name, to_session_id, body,
                        status, hop_count, created_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    from_session_id,
                    from_name,
                    rec.session_id,
                    body,
                    status,
                    int(hop_count),
                    ts,
                    expires_at,
                ),
            )
    except Exception as exc:
        logger.warning("cross_session send failed: %s", exc)
        return SendResult(
            outcome=DeliveryOutcome.RECIPIENT_NOT_FOUND,
            detail=f"could not queue the message: {exc}",
        )

    if status == STATUS_HELD:
        return SendResult(
            outcome=DeliveryOutcome.HELD,
            detail=(
                f"queued for '{rec.name}', which holds inbound messages for "
                f"human approval. It will not be delivered unless approved."
            ),
        )
    return SendResult(
        outcome=DeliveryOutcome.QUEUED_DURABLE,
        detail=f"queued for '{rec.name}'. Queued is not delivered — the "
        f"recipient receives it at its next drain checkpoint.",
    )


# ---------------------------------------------------------------------------
# Hop-count tracking (in-turn state)
# ---------------------------------------------------------------------------


class TurnMessageState:
    """Per-turn bookkeeping for hop counting and the per-turn send ceiling.

    New state that has no home in the conversation loop today, so it gets an
    explicit one here. Not persisted across a session resume — acceptable for
    v1, stated rather than left implicit.

    The hop rule is the Round-2 CORRECTED one: any send made during a turn
    that itself delivered inbox messages sets
    ``hop_count = max(delivered hop_counts) + 1`` UNCONDITIONALLY, including
    the very first reply (0 + 1 = 1). The original "increment only if nonzero"
    rule was a real bug — it let two accept-mode sessions ping-pong forever at
    hop_count 0, under the rate cap the whole time.
    """

    def __init__(self) -> None:
        self._delivered_hops: List[int] = []
        self._sends_this_turn = 0

    def reset(self) -> None:
        self._delivered_hops.clear()
        self._sends_this_turn = 0

    def record_delivered(self, hop_count: int) -> None:
        self._delivered_hops.append(int(hop_count))

    def next_hop_count(self) -> int:
        if not self._delivered_hops:
            return 0  # fresh chain
        return max(self._delivered_hops) + 1

    def send_budget_exhausted(self) -> bool:
        return self._sends_this_turn >= MAX_SENDS_PER_TURN

    def record_send(self) -> None:
        self._sends_this_turn += 1


# ---------------------------------------------------------------------------
# Drain path (recipient side) — where policy is actually enforced
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DrainedMessage:
    """A claimed message, already marker-wrapped and safe to inject."""

    row_id: int
    from_session_id: str
    from_name: str
    hop_count: int
    framed_body: str


def _frame(row: Any, origin: Optional[SessionOrigin]) -> str:
    """Wrap a raw body for injection. The ONLY place this module produces
    injectable text — both the mid-turn and idle paths go through here, so
    neither can accidentally emit a bare body.
    """
    return build_agent_message_marker(
        sender_participant_id=row["from_name"] or row["from_session_id"],
        sender_kind=ParticipantKind.SESSION,
        sender_origin=origin,
        body=row["body"],
    )


def drain_inbox(
    *,
    session_id: str,
    session_origin: Optional[SessionOrigin] = None,
    limit: int = 10,
    now: Optional[float] = None,
    on_held: Optional[Any] = None,
) -> List[DrainedMessage]:
    """Claim and frame this session's pending messages.

    Policy is evaluated HERE, against this session's own current config —
    not whatever the sender computed at insert time (Round 3 finding 5:
    send-time-only enforcement is bypassable and races a policy change).

    * ``accept`` -> claim, mark delivered, return framed for injection.
    * ``hold``   -> move to ``held`` with an ``expires_at``, fire the
      attention signal via ``on_held``, return nothing.
    * ``refuse`` -> mark ``denied``, return nothing.

    The claim checks rowcount before treating a row as ours, so two
    processes racing the same row cannot both inject it.
    """
    if not session_id:
        return []
    ts = float(now if now is not None else time.time())
    policy = resolve_inbound_policy(session_origin=session_origin)
    out: List[DrainedMessage] = []
    held_count = 0

    try:
        with _transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM cross_session_inbox "
                "WHERE to_session_id = ? AND status = ? "
                "ORDER BY created_at ASC LIMIT ?",
                (session_id, STATUS_PENDING, int(limit)),
            ).fetchall()

            for row in rows:
                row_id = int(row["id"])

                if policy == POLICY_REFUSE:
                    conn.execute(
                        "UPDATE cross_session_inbox SET status = ? "
                        "WHERE id = ? AND status = ?",
                        (STATUS_DENIED, row_id, STATUS_PENDING),
                    )
                    continue

                if policy == POLICY_HOLD:
                    cur = conn.execute(
                        "UPDATE cross_session_inbox SET status = ?, expires_at = ? "
                        "WHERE id = ? AND status = ?",
                        (
                            STATUS_HELD,
                            ts + HELD_MESSAGE_EXPIRY_SECONDS,
                            row_id,
                            STATUS_PENDING,
                        ),
                    )
                    if cur.rowcount == 1:
                        held_count += 1
                    continue

                # accept — claim atomically, then frame.
                cur = conn.execute(
                    "UPDATE cross_session_inbox SET status = ?, delivered_at = ? "
                    "WHERE id = ? AND status = ?",
                    (STATUS_DELIVERED, ts, row_id, STATUS_PENDING),
                )
                if cur.rowcount != 1:
                    continue  # someone else claimed it
                out.append(
                    DrainedMessage(
                        row_id=row_id,
                        from_session_id=row["from_session_id"],
                        from_name=row["from_name"],
                        hop_count=int(row["hop_count"]),
                        framed_body=_frame(row, session_origin),
                    )
                )
    except Exception as exc:
        logger.debug("cross_session drain failed: %s", exc)
        return out

    if held_count and on_held is not None:
        # Hold-mode UX: reuse cli.py's existing _fire_attention_signals()
        # (config-gated terminal bell + macOS notification) rather than
        # building a parallel notification path.
        try:
            on_held(
                f"{held_count} cross-session message(s) held for approval — "
                f"run `hermes agents inbox` to review"
            )
        except Exception:
            pass  # fail-soft, exactly like the approval-prompt call sites

    return out


# ---------------------------------------------------------------------------
# Held-message resolution (hermes agents inbox)
# ---------------------------------------------------------------------------


def list_inbox(
    *, session_id: Optional[str] = None, status: Optional[str] = STATUS_HELD
) -> List[Dict[str, Any]]:
    """Rows for the CLI listing. ``status=None`` lists every status."""
    sql = "SELECT * FROM cross_session_inbox"
    params: List[Any] = []
    clauses = []
    if session_id:
        clauses.append("to_session_id = ?")
        params.append(session_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at ASC"
    try:
        with _transaction() as conn:
            return [dict(row) for row in conn.execute(sql, params)]
    except Exception as exc:
        logger.debug("cross_session inbox list failed: %s", exc)
        return []


def approve_held(row_id: int) -> bool:
    """Return a ``held`` row to ``pending`` so the recipient's own drain
    claims it normally.

    This is the ``held -> pending`` transition the predecessor doc flagged as
    missing from the state machine. Approving does NOT deliver directly —
    delivery stays recipient-driven, which also means the recipient's policy
    is re-evaluated at that point.
    """
    try:
        with _transaction() as conn:
            cur = conn.execute(
                "UPDATE cross_session_inbox SET status = ?, expires_at = NULL "
                "WHERE id = ? AND status = ?",
                (STATUS_PENDING, int(row_id), STATUS_HELD),
            )
            return (cur.rowcount or 0) == 1
    except Exception as exc:
        logger.debug("cross_session approve failed: %s", exc)
        return False


def deny_held(row_id: int) -> bool:
    """Mark a ``held`` row ``denied``; it is never delivered."""
    try:
        with _transaction() as conn:
            cur = conn.execute(
                "UPDATE cross_session_inbox SET status = ? "
                "WHERE id = ? AND status = ?",
                (STATUS_DENIED, int(row_id), STATUS_HELD),
            )
            return (cur.rowcount or 0) == 1
    except Exception as exc:
        logger.debug("cross_session deny failed: %s", exc)
        return False


__all__ = [
    "CROSS_SESSION_HEARTBEAT_SECONDS",
    "HOP_COUNT_CEILING",
    "MAX_SENDS_PER_TURN",
    "POLICY_ACCEPT",
    "POLICY_HOLD",
    "POLICY_REFUSE",
    "RATE_CAP_MESSAGES",
    "RATE_CAP_WINDOW_SECONDS",
    "STATUS_DELIVERED",
    "STATUS_DENIED",
    "STATUS_EXPIRED",
    "STATUS_HELD",
    "STATUS_PENDING",
    "DrainedMessage",
    "RegisteredSession",
    "RegisteredSubagent",
    "TurnMessageState",
    "approve_held",
    "deny_held",
    "drain_inbox",
    "expire_held_messages",
    "find_cwd_collisions",
    "heartbeat_registry",
    "list_inbox",
    "list_registered_sessions",
    "list_registered_subagents",
    "reap_stale_registry",
    "register_lookup",
    "register_subagent",
    "resolve_inbound_policy",
    "resolve_recipient",
    "send_message",
    "unregister_subagent",
]
