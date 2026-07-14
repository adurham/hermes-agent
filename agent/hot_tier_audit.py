"""Hot-tier audit — stale-path detection heuristic + live demotion (MVP).

Reads hot-tier memory entries (``MEMORY.md`` / ``USER.md``) and flags entries
that mention a filesystem path which no longer exists on disk. This is a
cheap, deterministic, local-only heuristic — no LLM review is wired up in
this pass. See ``docs/plans/2026-07-14-hot-tier-audit.md`` for the full
design and staged rollout plan.

Gated behind ``curator.hot_tier_audit`` (default OFF) and
``curator.hot_tier_audit_dry_run`` (default ON).

Live mode (``dry_run=False``) automates exactly what the existing
stale-path heuristic already flags: entries where ``is_stale_path_candidate``
is True are demoted to the warm tier and removed from the hot-tier file
they came from; everything else is left untouched. No LLM-based
keep/demote/stale/dead classification is performed — that is an explicit
future follow-up, out of scope for this pass. Before any file is mutated,
a snapshot of ``~/.hermes/memories/`` is taken via
``agent.curator_backup.snapshot_memory()``; if that snapshot cannot be
created, live mutation aborts entirely (raises ``RuntimeError``) rather
than proceeding without a backup.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List

from hermes_constants import get_hermes_home
from tools.memory_tool import ENTRY_DELIMITER
from agent.curator_backup import snapshot_memory

logger = logging.getLogger(__name__)

# Path-shaped token patterns: `~/foo/bar` and `/Users/foo/bar` style tokens.
_PATH_PATTERN = re.compile(r"(~/[\w./-]+|/Users/[\w./-]+)")

# Hot-tier filenames considered by the audit, in read order.
_HOT_TIER_FILES = ("MEMORY.md", "USER.md")


def _read_entries(path: Path) -> List[str]:
    """Read and split a hot-tier file into entries on ENTRY_DELIMITER.

    Mirrors ``scripts/migrate_memory_to_warm.py``'s ``_read_entries``.
    """
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return []
    return [e.strip() for e in raw.split(ENTRY_DELIMITER) if e.strip()]


def _write_entries(path: Path, entries: List[str]) -> None:
    """Write entries back to a hot-tier file, delimiter-joined.

    Mirrors ``scripts/migrate_memory_to_warm.py``'s ``_write_entries``.
    """
    if not entries:
        path.write_text("", encoding="utf-8")
        return
    path.write_text(ENTRY_DELIMITER.join(entries), encoding="utf-8")


def _extract_paths(entry: str) -> List[str]:
    """Extract path-shaped tokens from an entry's text (raw, unexpanded)."""
    return _PATH_PATTERN.findall(entry)


def classify_entries(entries: List[str]) -> List[Dict[str, Any]]:
    """Classify each entry for stale-path candidacy.

    Returns a list of dicts, one per entry:
      - content: the raw entry text
      - is_stale_path_candidate: True if any extracted path does not exist
      - extracted_paths: the raw (unexpanded) path-shaped tokens found

    Deterministic/heuristic-only — no LLM review in this pass.
    """
    results: List[Dict[str, Any]] = []
    for entry in entries:
        raw_paths = _extract_paths(entry)
        is_stale = False
        for raw_path in raw_paths:
            try:
                expanded = Path(raw_path).expanduser()
                if not expanded.exists():
                    is_stale = True
            except Exception as e:  # pragma: no cover — defensive
                logger.debug("hot_tier_audit: path check failed for %r: %s", raw_path, e)
                continue
        results.append(
            {
                "content": entry,
                "is_stale_path_candidate": is_stale,
                "extracted_paths": raw_paths,
            }
        )
    return results


def _run_dry_run(memories_dir: Path) -> Dict[str, Any]:
    entries: List[str] = []
    for filename in _HOT_TIER_FILES:
        entries.extend(_read_entries(memories_dir / filename))

    classified = classify_entries(entries)
    stale_candidates = [c for c in classified if c["is_stale_path_candidate"]]

    return {
        "entries_checked": len(entries),
        "stale_path_candidates": stale_candidates,
        "written_report_path": None,
    }


def _run_live(memories_dir: Path) -> Dict[str, Any]:
    """Live-mode mutation: demote stale-path candidates to the warm tier.

    Snapshot-first: a memory snapshot is taken before any file is touched.
    If the snapshot fails, the whole operation aborts with RuntimeError so
    we never mutate hot-tier files without a rollback point.
    """
    snapshot_path = snapshot_memory(reason="pre-hot-tier-audit-live")
    if snapshot_path is None:
        raise RuntimeError(
            "hot_tier_audit: aborting live mutation — snapshot_memory() failed "
            "or returned None. Refusing to mutate hot-tier files without a "
            "pre-mutation backup."
        )

    # Read + classify each file separately so we know provenance per entry
    # (which file to remove it from) while keeping a combined entries_checked
    # count for the summary.
    per_file_entries: Dict[str, List[str]] = {}
    total_checked = 0
    all_stale_candidates: List[Dict[str, Any]] = []
    demoted_count = 0

    for filename in _HOT_TIER_FILES:
        path = memories_dir / filename
        entries = _read_entries(path)
        per_file_entries[filename] = entries
        total_checked += len(entries)

        classified = classify_entries(entries)
        stale_indices = {
            i for i, c in enumerate(classified) if c["is_stale_path_candidate"]
        }
        all_stale_candidates.extend(c for c in classified if c["is_stale_path_candidate"])

        if not stale_indices:
            continue

        kept: List[str] = []
        stale_entries: List[str] = []
        for i, entry in enumerate(entries):
            if i in stale_indices:
                stale_entries.append(entry)
            else:
                kept.append(entry)

        if not stale_entries:
            continue

        # Demote to warm tier first (matches migrate_memory_to_warm.py's
        # ordering rationale: writing to warm before touching hot tier means
        # a failure here doesn't lose data from the hot tier).
        from tools.memory_warm import get_warm_store

        store = get_warm_store()
        for content in stale_entries:
            store.add(
                content=content,
                category="demoted-stale-path",
                tags="hot-tier-audit,auto-demoted",
            )
            demoted_count += 1

        if kept != entries:
            _write_entries(path, kept)

    return {
        "entries_checked": total_checked,
        "stale_path_candidates": all_stale_candidates,
        "written_report_path": None,
        "demoted_count": demoted_count,
        "snapshot_path": str(snapshot_path),
    }


def run_hot_tier_audit(dry_run: bool) -> Dict[str, Any]:
    """Run a hot-tier audit pass.

    In dry-run mode, reads ``MEMORY.md``/``USER.md`` from
    ``$HERMES_HOME/memories/``, classifies every entry with the stale-path
    heuristic, and returns a summary. No files are mutated and no
    warm-tier writes happen.

    In live mode (``dry_run=False``), a snapshot of ``~/.hermes/memories/``
    is taken first via ``agent.curator_backup.snapshot_memory()``. If the
    snapshot fails, live mutation aborts with ``RuntimeError`` and nothing
    is touched. Otherwise, every entry flagged ``is_stale_path_candidate``
    by the existing heuristic is demoted to the warm tier (via
    ``tools.memory_warm.get_warm_store().add(...)``) and removed from the
    hot-tier file it came from; entries not flagged are left untouched, in
    their original order. Hot-tier files are only rewritten when their
    content actually changes (no stale candidates → no-op rewrite is
    skipped). No LLM-based keep/demote/stale/dead classification happens in
    this pass — that is a separate future follow-up.
    """
    memories_dir = get_hermes_home() / "memories"

    if dry_run:
        return _run_dry_run(memories_dir)

    return _run_live(memories_dir)
