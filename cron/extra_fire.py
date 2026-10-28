"""Manual runs are EXTRA fires: a run-now (``cronjob run``, ``hermes cron run``, dashboard Run,
gateway-forwarded ``trigger_job``) leaves the job's pending scheduled occurrence and its
``repeat`` budget alone. Without this a test-fire of an ``in 30m`` reminder completed the
one-shot and the real reminder never fired; a recurring ``repeat.times=N`` job lost one of N.

The claim (``claim_job_for_fire``) decides once, under the jobs lock, and stamps the pending
slot on ``fire_claim["extra_slot"]``; every later step (``claim_dispatch``, ``mark_job_run``)
reads that stamp, so a crash between claim and completion leaves ``next_run_at`` on the slot.
"""
from datetime import datetime
from typing import Any, Optional

RESTORE_KEY = "manual_run_restore_at"  # trigger_job's overwritten next_run_at
_CLAIM_KEY = "extra_slot"


def _future_slot(value: Any, now: datetime) -> Optional[str]:
    from cron.jobs import _parse_aware

    parsed = _parse_aware(value) if isinstance(value, str) else None
    return value if parsed is not None and parsed > now else None


def pending_slot(job: dict[str, Any], now: datetime, manual_fire: bool) -> Optional[str]:
    """The still-future scheduled instant an explicit run-now must leave in place, or None when
    this fire IS the scheduled occurrence (a tick, a provider fire, or a run-now once the slot
    is already due: nothing to preserve, no double fire)."""
    stored = job.get("next_run_at")
    if job.get("manual_run_at") == stored:  # trigger_job replaced next_run_at with "now"
        return _future_slot(job.get(RESTORE_KEY), now)
    return _future_slot(stored, now) if manual_fire else None


def stamp_claim(job: dict[str, Any], slot: str) -> None:
    """Mark the fresh ``fire_claim`` as an extra fire and put ``next_run_at`` back on *slot* in the
    same save, so the scheduled occurrence survives a crash before ``mark_job_run``."""
    job["fire_claim"][_CLAIM_KEY] = slot
    job["next_run_at"] = slot
    job.pop(RESTORE_KEY, None)


def is_extra(job: dict[str, Any]) -> bool:
    claim = job.get("fire_claim")
    return isinstance(claim, dict) and bool(claim.get(_CLAIM_KEY))


def settle(job: dict[str, Any], now: str) -> None:
    """After an extra fire: ``repeat.completed`` untouched, the job keeps its pending slot. A slot
    that passed while the run was in flight (the tick lost the claim race) is not dropped: a
    one-shot fires at once, a recurring job takes its next natural occurrence."""
    from cron.jobs import compute_next_run

    if _future_slot(job.get("next_run_at"), datetime.fromisoformat(now)):
        return
    if job.get("schedule", {}).get("kind") == "once":
        job["next_run_at"] = now
    else:
        job["next_run_at"] = compute_next_run(job["schedule"], now) or job.get("next_run_at")
