"""Bounded, profile-local receipts for completed terminal processes.

Receipts are read through process_manage, never replayed as notifications or
adopted as live PIDs. Each producer writes its own file so independent one-shot
parents cannot overwrite each other's results in the running-PID checkpoint.
"""

import json
import logging
import re
import sqlite3
import time
from hermes_dir_acl import make_user_dir

from hermes_constants import get_hermes_home
from utils import atomic_json_write

logger = logging.getLogger("tools.process_registry")

RESULT_RETENTION_SECONDS = 7 * 24 * 60 * 60
MAX_RETAINED_RESULTS = 64
_RESULT_FIELDS = (
    "id", "command", "cwd", "task_id", "owner_task_id", "session_key",
    "parent_session_id", "started_at", "exit_code", "completion_reason",
    "termination_source", "notify_on_complete",
)


def _result_paths():
    """Prune by completion time, not start time (jobs can take days)."""
    directory = get_hermes_home() / "logs" / "process-results"
    cutoff = time.time() - RESULT_RETENTION_SECONDS
    retained = []
    for path in directory.glob("proc_*.json"):
        try:
            modified = path.stat().st_mtime
            if modified < cutoff:
                path.unlink(missing_ok=True)
            else:
                retained.append((modified, path))
        except FileNotFoundError:
            continue  # Another producer pruned it.
    retained.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    for _, path in retained[MAX_RETAINED_RESULTS:]:
        path.unlink(missing_ok=True)
    return [path for _, path in retained[:MAX_RETAINED_RESULTS]]


def save_completed_result(session) -> None:
    from agent.redact import redact_sensitive_text, redact_terminal_output
    from tools.process_registry import MAX_OUTPUT_CHARS

    with session._lock:
        record = {key: getattr(session, key) for key in _RESULT_FIELDS}
        record["output"] = session.output_buffer[-MAX_OUTPUT_CHARS:]
    # Live-output opt-out must not persist raw credentials in durable receipts.
    record["output"] = redact_terminal_output(record["output"], record["command"], force=True)
    record["command"] = redact_sensitive_text(record["command"], code_file=True, force=True)
    directory = get_hermes_home() / "logs" / "process-results"
    try:
        from hermes_constants import assert_named_profile_home_live
        assert_named_profile_home_live(directory)
        make_user_dir(directory, mode=0o700, parents=True, exist_ok=True)
        atomic_json_write(directory / f"{session.id}.json", record, mode=0o600)
        _result_paths()
    except OSError:
        # Preserve live delivery on disk failure, but never silently claim durability.
        logger.warning("Could not retain completed process result %s", session.id, exc_info=True)


def _owns_result(owner: str, parent: str | None) -> bool:
    if not parent:
        return False
    if owner == parent:
        return True
    from hermes_state import SessionDB

    # Pure lineage read on the hot path of every retained-result load; a writable open here
    # was one more writer handle per call inside the gateway (#100896).
    db = SessionDB(read_only=True)
    try:
        return db.get_compression_tip(parent) == owner
    finally:
        db.close()


def spawner_lineage(owner: str, parent: str | None) -> bool | None:
    """Whether *owner* may consume a result spawned by *parent*: ``True``/``False`` when the
    compression lineage decides it, ``None`` when it cannot decide.

    This is the one ownership proof shared by every consumer of a completed process: the
    durable receipt reader (``_owns_result``, which fails closed on ``None``) and the live
    completion drain (``ProcessRegistry.drain_notifications``, which falls back to route-key
    routing on ``None`` so a notice is never pinned to a session that can never take it).

    Undecidable cases, all deliberate:

    * no spawner recorded (a pre-upgrade receipt, or a spawn with no session bound);
    * the spawner has no row in this profile's store (another profile's session, or a
      receipt whose session was deleted);
    * the spawner is a DELEGATED CHILD. A subagent's ``session_key`` is its parent chat's
      key and its own session is the receipt's spawner, so its process completions are
      routed by the delegation path (hold while the child runs, release to the parent
      chat with attribution) — never by lineage, which would leave them ownerless.

    A route key alone is NOT proof: every session in one compression lineage, every
    sibling continuation and every subagent of the chat hold the same key.
    """
    if not parent or not owner:
        return None
    if owner == parent:
        return True
    from hermes_state import SessionDB

    db = SessionDB(read_only=True)
    try:
        row = db.get_session(parent)
        if row is None:
            return None
        config = row.get("model_config")
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except (TypeError, ValueError):
                config = None
        if row.get("_delegate_from") or (isinstance(config, dict) and config.get("_delegate_from")):
            return None
        return db.get_compression_tip(parent) == owner
    except sqlite3.Error:
        return None
    finally:
        db.close()


def load_completed_results(prefix: str = "") -> dict:
    """Restore read-only snapshots; no process handles, watchers, or queue events."""
    from tools.process_registry import ProcessSession

    from gateway.session_context import get_session_env

    owner = get_session_env("HERMES_SESSION_ID", "")
    if not owner:
        return {}
    results = {}
    try:
        paths = _result_paths()
    except OSError:
        logger.warning("Could not read retained process results", exc_info=True)
        return results
    for path in paths:
        if not path.stem.startswith(prefix):
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8-sig"))
            if record["id"] != path.stem or not re.fullmatch(r"proc_[\w]+", record["id"]):
                continue
            if not _owns_result(owner, record.get("parent_session_id")):
                continue
            session = ProcessSession(
                **{key: record[key] for key in _RESULT_FIELDS},
                exited=True, output_buffer=record["output"],
            )
            session._completion_event.set()
            results[session.id] = session
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            logger.debug("Skipping unreadable process result %s", path.name, exc_info=True)
    return results
