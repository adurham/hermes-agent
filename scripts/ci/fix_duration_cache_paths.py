#!/usr/bin/env python3
"""Repoint stale test_durations.json keys whose files moved in the tests/ reorg.

The per-file timeout scaler (`scripts/run_tests_parallel.py::_effective_file_timeout`)
only applies headroom to a file it can find in the duration cache — keyed by the file's
path. The 2026-09 tests/ reorg (tests/cli -> tests/hermes_cli, tests/run_agent ->
tests/agent, and a batch of top-level tests/test_*.py moved into topical dirs) left many
cache keys pointing at paths that no longer exist. A miss means the flat 300s cap
applies, so a legitimately slow file (e.g. tests/tui_gateway/test_tui_gateway_server.py,
302s on this box) is SIGKILLed before collection and reported as "no tests ran".

This remaps each orphaned key onto its current path by basename, keeping the recorded
duration. Files with no unique current match are reported and left alone.

Usage:
    python scripts/ci/fix_duration_cache_paths.py [--apply] [--cache PATH]

Dry-run by default.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _index_by_basename(root: Path) -> dict[str, list[Path]]:
    out: dict[str, list[Path]] = {}
    for p in root.rglob("*.py"):
        out.setdefault(p.name, []).append(p)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default="test_durations.json")
    ap.add_argument("--apply", action="store_true", help="write the remapped cache")
    ap.add_argument("--tests-root", default="tests")
    args = ap.parse_args()

    cache_path = Path(args.cache)
    if not cache_path.exists():
        print(f"no cache at {cache_path} (nothing to do; it is CI-cache-backed, not committed)")
        return 0

    data = json.loads(cache_path.read_text(encoding="utf-8"))
    repo_root = Path(".")
    index = _index_by_basename(Path(args.tests_root))

    remapped: dict[str, tuple[str, float]] = {}
    orphans: list[str] = []
    collisions: list[str] = []

    for key, duration in data.items():
        if Path(key).exists():
            continue  # already correct
        base = Path(key).name
        matches = index.get(base, [])
        if len(matches) == 1:
            remapped[key] = (matches[0].as_posix(), duration)
        elif len(matches) > 1:
            collisions.append(key)
        else:
            orphans.append(key)

    print(f"cache entries: {len(data)}")
    print(f"stale (path no longer exists): {len(remapped) + len(collisions) + len(orphans)}")
    print(f"  remappable (unique basename match): {len(remapped)}")
    print(f"  ambiguous (multiple matches): {len(collisions)}")
    print(f"  no current match: {len(orphans)}")

    for old, (new, dur) in sorted(remapped.items())[:20]:
        print(f"    {old}  ->  {new}   ({dur}s)")
    if len(remapped) > 20:
        print(f"    ... +{len(remapped) - 20} more")
    for c in collisions[:10]:
        print(f"    AMBIGUOUS: {c}")
    for o in orphans[:10]:
        print(f"    ORPHAN: {o}")

    if not args.apply:
        print("\ndry run — pass --apply to write")
        return 0

    for old, (new, dur) in remapped.items():
        del data[old]
        data[new] = dur
    cache_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"\nwrote {len(remapped)} remapped entries to {cache_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
