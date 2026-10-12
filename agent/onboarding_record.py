"""Where a first-run onboarding run lives: one ``onboarding_run`` key in the session's
``sessions.model_config`` (no schema change; a compression rotation carries it, see
``hermes_state_compression._CONVERSATION_MODEL_CONFIG_KEYS``).

``advance`` is the one writer after ``start``: load, ``onboarding_flow.apply``, save, and when the
run ends write the install-level flag in the root config so the next launch does not reopen it.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from typing import Any

from agent.onboarding_flow import (
    Applied, Event, Facts, Step, applies_to, apply, expire, is_active, new_record,
)

logger = logging.getLogger(__name__)

KEY = "onboarding_run"


def load(db, session_id: str) -> dict | None:
    """The run record, with the quiet-spell fallback applied (and saved) when it is due."""
    record = db.get_session_model_config_value(session_id, KEY) if db is not None and session_id else None
    if not isinstance(record, dict):
        return None
    expired = expire(record, now=time.time())
    if expired is not record:
        _save(db, session_id, record, expired)
    return expired


def _save(db, session_id: str, before: Mapping[str, Any], after: dict) -> None:
    db.patch_session_model_config(session_id, {KEY: after})
    if is_active(before) and not is_active(after):
        _close_install_flag()


def _close_install_flag() -> None:
    from hermes_cli.onboarding_run import set_run

    set_run(False, mark_profile_offered=True)


def start(db, session_id: str, facts: Facts) -> dict:
    """Write a fresh run record (the row must exist) and mark the install as mid-onboarding, so a
    crash before the end reopens this chat on the next launch."""
    from hermes_cli.onboarding_run import set_run

    record = new_record(facts, now=time.time())
    db.patch_session_model_config(session_id, {KEY: record})
    set_run(True)
    return record


def advance(db, session_id: str, event: Event) -> Step:
    """Apply one event to the stored run; LookupError when this session has no run."""
    record = load(db, session_id)
    if record is None:
        raise LookupError(f"session {session_id} has no onboarding run")
    step = apply(record, event, Facts.from_stored(record["facts"]), now=time.time())
    if step.record != record:
        _save(db, session_id, record, step.record)
    return step


def active_session_id(db) -> str | None:
    """The newest session whose run is still in progress."""
    return next((sid for sid in db.session_ids_with_model_config_key(KEY) if is_active(load(db, sid))), None)


def observe_tool_result(agent, name: str, args: Mapping[str, Any], result: Any) -> None:
    """Settle a beat from a general tool's result (``set_theme``, ``manage_connections`` display,
    ``gui_tour``). Runs for every tool result; only an onboarding chat (one whose agent carries
    ``setup_beat``) reads its record."""
    if not applies_to(name) or "setup_beat" not in (getattr(agent, "valid_tool_names", None) or ()):
        return
    db, session_id = getattr(agent, "_session_db", None), getattr(agent, "session_id", "") or ""
    try:
        parsed = json.loads(result) if isinstance(result, str) else result
        if isinstance(parsed, dict) and load(db, session_id) is not None:
            advance(db, session_id, Applied(name, dict(args or {}), parsed))
    except Exception:
        logger.warning("onboarding: %s result did not update the run record", name, exc_info=True)
