"""Hot-tier audit — dry-run-only MVP (stale-path detection heuristic).

Reads hot-tier memory entries (``MEMORY.md`` / ``USER.md``) and flags entries
that mention a filesystem path which no longer exists on disk. This is a
cheap, deterministic, local-only heuristic — no LLM review is wired up in
this pass. See ``docs/plans/2026-07-14-hot-tier-audit.md`` for the full
design and staged rollout plan.

Gated behind ``curator.hot_tier_audit`` (default OFF) and
``curator.hot_tier_audit_dry_run`` (default ON). Live mutation (moving
`demote`/`stale` entries out of hot tier) is explicitly OUT OF SCOPE for this
pass and raises ``NotImplementedError`` — it lands in a follow-up once
dry-run reports have been validated against real hot-tier content.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List

from hermes_constants import get_hermes_home
from tools.memory_tool import ENTRY_DELIMITER

logger = logging.getLogger(__name__)

# Path-shaped token patterns: `~/foo/bar` and `/Users/foo/bar` style tokens.
_PATH_PATTERN = re.compile(r"(~/[\w./-]+|/Users/[\w./-]+)")


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


def run_hot_tier_audit(dry_run: bool) -> Dict[str, Any]:
    """Run a hot-tier audit pass.

    In dry-run mode (the only supported mode this pass), reads
    ``MEMORY.md``/``USER.md`` from ``$HERMES_HOME/memories/``, classifies
    every entry with the stale-path heuristic, and returns a summary. No
    files are mutated and no warm-tier writes happen.

    Live mode (``dry_run=False``) mutates hot-tier files (removing stale
    entries / demoting others to warm tier) and is OUT OF SCOPE for this
    pass — raises ``NotImplementedError``. It lands in a follow-up PR once
    dry-run reports have been reviewed and trusted (staged rollout per the
    design doc).
    """
    if not dry_run:
        raise NotImplementedError(
            "hot-tier audit live mode (dry_run=False) is not implemented yet — "
            "this MVP ships dry-run-only reporting. Live mutation (removing "
            "stale entries / demoting to warm tier) lands in a follow-up PR "
            "once dry-run reports have been reviewed and trusted. See "
            "docs/plans/2026-07-14-hot-tier-audit.md for the staged rollout."
        )

    memories_dir = get_hermes_home() / "memories"
    entries: List[str] = []
    entries.extend(_read_entries(memories_dir / "MEMORY.md"))
    entries.extend(_read_entries(memories_dir / "USER.md"))

    classified = classify_entries(entries)
    stale_candidates = [c for c in classified if c["is_stale_path_candidate"]]

    return {
        "entries_checked": len(entries),
        "stale_path_candidates": stale_candidates,
        "written_report_path": None,
    }
