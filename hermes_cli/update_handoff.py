"""Frozen compat surface for releases that finish `hermes update` via the post-swap hand-off.

Releases from 2026-09-16 (94ced1a2b2) lazily import ``hermes_cli.update_handoff``
from the NEW tree after the checkout swap. Like every other retired updater
hook, this module keeps that import working by routing into the historical
takeover (``hermes_cli._old_updater`` → ``_update_takeover.py``) instead of
re-executing ``hermes update --post-swap``; the pulled tree is never imported
into the pre-pull interpreter.

Guarded by tests/compat/old_updater_surface.json — do not remove a public name
without regenerating the frozen surface and proving no shipped release loads it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# Set on the post-swap child: the receipt header says "continued", the lock is the parent's.
POST_SWAP_ENV = "HERMES_UPDATE_POST_SWAP"


def is_post_swap_child() -> bool:
    return os.environ.get(POST_SWAP_ENV) == "1"


def write_handoff(payload: dict[str, Any]) -> Path:
    """Persist the post-swap payload under HERMES_HOME; returns its path."""
    from hermes_constants import get_hermes_home

    directory = get_hermes_home() / "logs" / "update_receipts"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"post_swap_{os.getpid()}.json"
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def read_handoff(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"post-swap hand-off {path} is not a JSON object")
    return payload


def continue_update_in_fresh_interpreter(payload: dict[str, Any], *, argv_tail: list[str] | None = None) -> int | None:
    """Run the post-swap tail in a child interpreter on the pulled code.

    The old caller's payload carries the same fields the completion request
    needs (receipt, plan, windows_resume, gateway_mode, ...); ``argv_tail`` is
    accepted and ignored because the takeover tail does not re-parse update
    flags. Returns the child's exit code, or ``None`` when no child could be
    started.
    """
    from hermes_cli._old_updater import _run_child

    write_handoff(payload)
    try:
        code, _completed = _run_child(dict(payload))
        return int(code)
    except OSError as exc:
        print(f"  ⚠ Could not start the post-update interpreter: {exc}")
        print("  The code update is applied. Finish it with: hermes update")
        return None
