"""hermes.install.run: fresh installs through scripts/install.sh and scripts/install.ps1.

The installer runs before the user is asked about shared metrics and must never send anything, so a
full-ladder run only leaves ONE small local receipt (closed tokens plus two epoch timestamps) under
the profile's store dir. The next Hermes start that runs the process-exit reporter reads it: with
collection on it records one row per receipt id and deletes the receipt once the row is saved; with
collection off ``shared_metrics_process.begin_process`` deletes every receipt unreported (the same
rule as parked update receipts). A receipt with any value outside the contract is deleted unreported.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PENDING_DIRNAME = "pending_installs"
# One empty file per receipt id already counted: a delete that failed after the row was saved must
# not count the same install again on the next start.
RECORDED_DIRNAME = "recorded_installs"
_RECORDED_KEEP = 64
_RECEIPT_ID = re.compile(r"[0-9a-f]{32}")
_RECEIPT_KEYS = frozenset({"id", "installer", "outcome", "failed_stage", "failure_class", "started_at", "finished_at"})


def pending_installs_dir(home: Path) -> Path:
    return home / "telemetry" / "shared_metrics" / PENDING_DIRNAME


def purge_pending_installs(home: Path) -> None:
    shutil.rmtree(pending_installs_dir(home), ignore_errors=True)


def _epoch(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def install_run_fields(receipt: Any) -> dict[str, str] | None:
    """hermes.install.run dims for one receipt, or None when anything is outside the contract."""
    from . import shared_metrics_contract as contract

    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_KEYS:
        return None
    if not isinstance(receipt["id"], str) or not _RECEIPT_ID.fullmatch(receipt["id"]):
        return None
    started, finished = _epoch(receipt["started_at"]), _epoch(receipt["finished_at"])
    if started is None or finished is None or finished < started:
        return None
    fields = {
        "duration_bucket": contract.update_duration_bucket((finished - started) * 1000),
        "failed_stage": receipt["failed_stage"],
        "failure_class": receipt["failure_class"],
        "installer": receipt["installer"],
        "outcome": receipt["outcome"],
    }
    if not contract.counter_dimensions_are_valid(contract.INSTALL_RUN_METRIC, fields):
        return None
    # A success names no stage and no class; a failure names both.
    succeeded = fields["outcome"] == "success"
    if succeeded != (fields["failed_stage"] == "none") or succeeded != (fields["failure_class"] == "none"):
        return None
    return fields


def _claim_receipt_id(home: Path, receipt_id: str) -> Path | None:
    """The new latch for ``receipt_id``, or None when this profile already counted it."""
    directory = home / "telemetry" / "shared_metrics" / RECORDED_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    latch = directory / receipt_id
    try:
        os.close(os.open(latch, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        return None
    try:
        for stale in sorted(directory.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[_RECORDED_KEEP:]:
            stale.unlink(missing_ok=True)
    except OSError:  # a concurrent prune won; the next claim prunes again
        pass
    return latch


def report_pending_installs(home: Path) -> None:
    """Record each receipt once (the caller runs only with collection on). Never raises."""
    try:
        from . import shared_metrics_contract as contract
        from .shared_metrics_events import emit_saved
        from .shared_metrics_process import _claim, settle_claim

        directory = pending_installs_dir(home)
        if not directory.is_dir():
            return
        for path in sorted(directory.iterdir()):
            if path.name.startswith(".") or ".json" not in path.name:
                continue
            claimed = _claim(path)  # a concurrent start that loses the rename records nothing
            if claimed is None:
                continue
            try:
                receipt = json.loads(claimed.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                receipt = None
            fields = install_run_fields(receipt)
            if fields is None or not isinstance(receipt, dict):  # out of contract or unreadable
                settle_claim(claimed, path, True)
                continue
            latch = _claim_receipt_id(home, receipt["id"])
            if latch is None:  # counted before; only the delete had failed
                settle_claim(claimed, path, True)
                continue
            saved = emit_saved([(contract.INSTALL_RUN_MARK, fields)]) == 1
            if not saved:
                latch.unlink(missing_ok=True)  # nothing landed: the next start counts it
            settle_claim(claimed, path, saved)
    except Exception:
        logger.debug("Pending install receipts not reported", exc_info=True)
