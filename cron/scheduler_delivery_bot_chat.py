"""Bot Chat delivery phases: deferred-record admission and receipt outcomes.

Phases of ``cron.scheduler_delivery._deliver_to_bot_chat``; the facade keeps discovery, the CLI
fallback and every fail-closed handler, and reaches these through the module object.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional


def delivery_key(job: dict, job_id: str, source_home: Path, home: Path,
                 deferred: Optional[dict]) -> str:
    """The delivery id: stable per (source home, job, execution, target home); a replay keeps its own."""
    import hashlib
    import json
    import uuid

    # run_one_job/claim_fire attach the durable execution id before delivery. The
    # transient fallback supports direct helper callers, never deduping recurring
    # runs by their (potentially identical) output or previous last_run timestamp.
    run_id = job.get("execution_id")
    if not run_id:
        run_id = job.setdefault("_bot_chat_run_id", uuid.uuid4().hex)
    key = hashlib.sha256(json.dumps(
        [str(source_home), job_id, str(run_id), str(home)],
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    if deferred is not None:
        key = deferred["id"]
    return key


def pin_cli_target(argv: list, env: dict, home: Path) -> None:
    """Point the CLI fallback child at ``home``'s profile and keep this install importable."""
    if home.parent.name != "profiles":
        argv += ["-p", "default"]
    if argv[1:3] == ["-m", "hermes_cli.main"]:
        # served_profile_child_env strips Hermes-owned PYTHONPATH entries; under a store-python
        # shim the bare interpreter then cannot import the package find_spec just proved (#122487).
        from cron.scheduler_worker_env import pin_hermes_tree_on_pythonpath
        pin_hermes_tree_on_pythonpath(env, Path(__file__).resolve().parents[1])


def admit_pending(key: str, job: dict, content: str, profile: str, home: Path, *,
                  for_failure: bool, suppressed: bool) -> Optional[dict]:
    """Return the deferred record that owns this delivery, recording one when it must exist."""
    from cron.bot_chat_delivery import defer, read_pending
    from tools.bot_live_delivery import find_canonical_live_owner, find_canonical_owner

    pending = read_pending(key)
    # Suppression is a durable disposition, not a send: record it under the producer
    # lock even when a live owner exists, so the deferred lane never replays it.
    if (pending is not None or suppressed
            or (find_canonical_live_owner(home) is None and find_canonical_owner(home))):
        pending = defer(key, dict(job), content, profile, home,
                        for_failure=for_failure, suppressed=suppressed)
    return pending


def pending_outcome(job: dict, pending: dict, key: str, profile_label: str) -> Optional[str]:
    """Stamp the deferred record's receipt on the job; None when it is settled or suppressed."""
    status = pending["status"]
    target = f"bot-chat:{profile_label}"
    job.setdefault("_bot_chat_delivery_receipts", {})[target] = {
        "status": status, "delivery_id": key}
    if status == "suppressed":
        job["_notification_all_targets_suppressed"] = True
    return None if status in ("settled", "suppressed") else f"{target} {status} (receipt {key}): completion unverified; do not resend"


def receipt_outcome(job: dict, receipt: dict, message: str, key: str, profile_label: str, *,
                    for_failure: bool) -> Optional[str]:
    """Stamp a live-owner receipt on the job; None only when the owner settled the turn."""
    from cron.scheduler_delivery import logger

    if (receipt["message"] != message
            or receipt.get("notification_category", "result") != ("diagnostic" if for_failure else "result")):
        raise ValueError("delivery id already belongs to a different payload")
    status = receipt["status"]
    target = f"bot-chat:{profile_label}"
    receipts = job.setdefault("_bot_chat_delivery_receipts", {})
    receipts[target] = {"status": status, "delivery_id": key}
    logger.info("Job '%s': Bot Chat %s receipt=%s status=%s",
                job.get("id", "?"), profile_label, key, status)
    if status == "settled":
        return None
    detail = ("completion unverified; do not resend" if status in ("queued", "claimed")
              else receipt.get("error") or receipt.get("reason") or "not completed")
    return f"{target} {status} (receipt {key}): {detail}"
