#!/usr/bin/env python3
"""Interactive migration: classify hot-tier entries → keep / move to warm / delete.

Reads ``~/.hermes/memories/MEMORY.md`` and ``USER.md``, walks through each
entry, asks where it belongs, and writes the result.

Run from the repo root with the active venv:

    source .venv/bin/activate
    python scripts/migrate_memory_to_warm.py

What it does:
  1. Backs up MEMORY.md + USER.md to ``~/.hermes/memories/.backup-<timestamp>/``.
  2. Splits each file into entries on the ``§`` delimiter.
  3. For each entry, prompts:
       [h] Hot — keep in hot tier (always loaded)
       [w] Warm — move to warm tier (search-only)
       [d] Delete — drop, no longer relevant
       [s] Skip — leave alone in current location
       [q] Quit — write what's done so far, exit
  4. Hot picks: rewrite the file with only the kept entries.
  5. Warm picks: insert into ``~/.hermes/memory_store.db`` via WarmStore.
  6. Idempotent: any entry already present in the warm DB by content is
     skipped on re-run (won't double-insert).
  7. After classification, surfaces new hot-tier sizes and (if user agrees)
     updates ``memory.memory_char_limit`` / ``memory.user_char_limit`` in
     ``~/.hermes/config.yaml`` to a tight new cap.

Usage notes:
  * Run in a real interactive terminal — the prompt requires stdin.
  * Re-running after a partial migration is safe: backups are timestamped,
    warm-tier writes deduplicate on content, hot-tier files are only
    rewritten when you confirm at the end.
  * Pass ``--dry-run`` to walk through without writing anything.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import shutil
import sys
import textwrap
from pathlib import Path
from typing import List, Tuple

# Make the repo importable when run as a script.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from hermes_constants import get_hermes_home  # noqa: E402
from tools.memory_tool import ENTRY_DELIMITER, MemoryStore  # noqa: E402

# Lazy-imported below: tools.memory_warm.get_warm_store


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

# Suggested defaults after migration. The plan calls for ~1,000 chars total.
SUGGESTED_HOT_MEMORY_CAP = 600
SUGGESTED_HOT_USER_CAP = 400


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _read_entries(path: Path) -> List[str]:
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return []
    return [e.strip() for e in raw.split(ENTRY_DELIMITER) if e.strip()]


def _write_entries(path: Path, entries: List[str]) -> None:
    if not entries:
        path.write_text("", encoding="utf-8")
        return
    path.write_text(ENTRY_DELIMITER.join(entries), encoding="utf-8")


def _backup(memories_dir: Path) -> Path:
    """Snapshot MEMORY.md / USER.md to a timestamped backup directory."""
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = memories_dir / f".backup-{stamp}"
    target.mkdir(parents=True, exist_ok=True)
    for fn in ("MEMORY.md", "USER.md"):
        src = memories_dir / fn
        if src.exists():
            shutil.copy2(src, target / fn)
    return target


def _preview(entry: str, width: int = 80) -> str:
    """One-screen preview of an entry: wrap to `width` chars."""
    wrapped = textwrap.fill(entry, width=width, replace_whitespace=False, drop_whitespace=False)
    return wrapped


def _prompt_classify(entry: str, idx: int, total: int) -> str:
    """Ask the user where this entry belongs. Returns 'h'/'w'/'d'/'s'/'q'."""
    print()
    print("─" * 78)
    print(f"Entry {idx} of {total}  ({len(entry)} chars)")
    print("─" * 78)
    print(_preview(entry))
    print("─" * 78)
    while True:
        choice = input(
            "Where does this belong?\n"
            "  [h] Hot  — always loaded (use for user prefs, recurring corrections)\n"
            "  [w] Warm — searchable via memory(action='recall', ...)  [DEFAULT]\n"
            "  [d] Delete\n"
            "  [s] Skip — leave it alone in the current location\n"
            "  [q] Quit — write what's done so far\n"
            "Choice [w]: "
        ).strip().lower() or "w"
        if choice in ("h", "w", "d", "s", "q"):
            return choice
        print(f"  invalid choice {choice!r}, try again")


def _prompt_yesno(question: str, default: bool = True) -> bool:
    suffix = " [Y/n]" if default else " [y/N]"
    while True:
        ans = input(question + suffix + ": ").strip().lower()
        if not ans:
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print("  please answer y or n")


def _classify_file(
    label: str, path: Path, dry_run: bool,
) -> Tuple[List[str], List[str]]:
    """Walk through one file's entries; return (kept_hot, moved_to_warm)."""
    entries = _read_entries(path)
    if not entries:
        print(f"\n[{label}] no entries — skipping.")
        return [], []

    print(f"\n=== {label}: {len(entries)} entries ===")
    kept: List[str] = []
    warm: List[str] = []
    deleted: int = 0
    skipped: int = 0

    for i, entry in enumerate(entries, 1):
        choice = _prompt_classify(entry, i, len(entries))
        if choice == "h":
            kept.append(entry)
            print("  → kept HOT")
        elif choice == "w":
            warm.append(entry)
            print("  → moving to WARM")
        elif choice == "d":
            deleted += 1
            print("  → DELETED")
        elif choice == "s":
            kept.append(entry)
            skipped += 1
            print("  → skipped (left alone)")
        elif choice == "q":
            # Treat remaining entries as "skip" so the file isn't truncated.
            remaining = entries[i - 1:]  # current entry inclusive
            kept.extend(remaining)
            print(f"  → quitting; {len(remaining)} remaining entries left in place")
            break

    print(
        f"\n[{label}] {len(kept)} kept, {len(warm)} → warm, "
        f"{deleted} deleted, {skipped} skipped"
    )
    return kept, warm


def _write_warm(entries: List[str], category: str, dry_run: bool) -> int:
    """Insert entries into the warm tier. Returns count actually written."""
    if not entries:
        return 0
    if dry_run:
        print(f"  (dry-run) would write {len(entries)} entries to warm tier")
        return 0
    from tools.memory_warm import get_warm_store
    store = get_warm_store()
    written = 0
    for content in entries:
        result = store.add(
            content=content,
            category=category,
            tags="migrated",
        )
        if result.get("success") and result.get("status") == "created":
            written += 1
    return written


def _suggest_cap_update(
    config_path: Path, kept_hot_memory_chars: int, kept_hot_user_chars: int,
    dry_run: bool,
) -> None:
    """Offer to update memory.memory_char_limit / user_char_limit in config."""
    if not config_path.exists():
        print(f"\nNo config at {config_path}, skipping cap update.")
        return

    # Suggest tight cap = max(SUGGESTED_*, observed * 1.5) so there's headroom
    # but the limit still forces discipline.
    suggested_mem = max(SUGGESTED_HOT_MEMORY_CAP, int(kept_hot_memory_chars * 1.5) + 100)
    suggested_user = max(SUGGESTED_HOT_USER_CAP, int(kept_hot_user_chars * 1.5) + 100)

    print()
    print("─" * 78)
    print("Hot tier sizes after migration:")
    print(f"  MEMORY.md: {kept_hot_memory_chars:,} chars")
    print(f"  USER.md:   {kept_hot_user_chars:,} chars")
    print()
    print("Suggested new caps in config.yaml (forces discipline; raise later if needed):")
    print(f"  memory.memory_char_limit: {suggested_mem}")
    print(f"  memory.user_char_limit:   {suggested_user}")
    print("─" * 78)

    if not _prompt_yesno("Update ~/.hermes/config.yaml with these caps?", default=True):
        print("Skipping cap update.")
        return

    if dry_run:
        print("(dry-run) would write new caps to config.yaml")
        return

    # Minimal in-place edit so we don't depend on yaml libs (and don't
    # rewrite the user's comments / formatting).
    text = config_path.read_text(encoding="utf-8")
    new_text = _replace_cap_line(text, "memory_char_limit", suggested_mem)
    new_text = _replace_cap_line(new_text, "user_char_limit", suggested_user)
    if new_text == text:
        print("WARNING: could not find cap lines in config.yaml; please edit manually.")
        return

    # Backup the config before overwriting
    backup_path = config_path.with_suffix(config_path.suffix + ".pre-migrate")
    backup_path.write_text(text, encoding="utf-8")
    config_path.write_text(new_text, encoding="utf-8")
    print(f"Updated {config_path} (backup: {backup_path})")


def _replace_cap_line(text: str, key: str, new_value: int) -> str:
    """Find a YAML line like ``  memory_char_limit: NNN`` and rewrite it."""
    out_lines: List[str] = []
    replaced = False
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith(key + ":") and not replaced:
            indent = line[: len(line) - len(stripped)]
            out_lines.append(f"{indent}{key}: {new_value}\n")
            replaced = True
            continue
        out_lines.append(line)
    return "".join(out_lines)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Walk through entries without writing anything.",
    )
    p.add_argument(
        "--memories-dir",
        type=Path,
        default=None,
        help="Override the memories directory (default: $HERMES_HOME/memories).",
    )
    args = p.parse_args()

    hermes_home = get_hermes_home()
    memories_dir = args.memories_dir or (hermes_home / "memories")
    config_path = hermes_home / "config.yaml"

    if not memories_dir.exists():
        print(f"No memories directory at {memories_dir}; nothing to migrate.")
        return 0

    print(f"Memories directory: {memories_dir}")
    print(f"Hermes home:        {hermes_home}")
    print(f"Config:             {config_path}")
    print(f"Mode:               {'DRY-RUN' if args.dry_run else 'LIVE WRITE'}")

    if not args.dry_run:
        backup = _backup(memories_dir)
        print(f"Backup written to:  {backup}")

    print()
    print("=" * 78)
    print("Phase 1: classify each entry as HOT (always loaded), WARM (searchable),")
    print("         DELETE (drop), or SKIP (leave alone). Default is WARM.")
    print("=" * 78)

    mem_kept, mem_warm = _classify_file(
        "MEMORY.md", memories_dir / "MEMORY.md", args.dry_run,
    )
    user_kept, user_warm = _classify_file(
        "USER.md", memories_dir / "USER.md", args.dry_run,
    )

    # Write warm-tier entries first (so a failure here doesn't trash hot tier).
    print()
    print("=" * 78)
    print("Phase 2: writing to warm tier")
    print("=" * 78)
    written_mem = _write_warm(mem_warm, "memory", args.dry_run)
    written_user = _write_warm(user_warm, "user", args.dry_run)
    print(
        f"  Wrote {written_mem} new warm facts from MEMORY.md, "
        f"{written_user} from USER.md"
    )

    # Rewrite hot-tier files with kept entries only.
    print()
    print("=" * 78)
    print("Phase 3: rewriting hot-tier files")
    print("=" * 78)
    if args.dry_run:
        print(f"  (dry-run) would rewrite MEMORY.md with {len(mem_kept)} entries")
        print(f"  (dry-run) would rewrite USER.md with {len(user_kept)} entries")
    else:
        _write_entries(memories_dir / "MEMORY.md", mem_kept)
        _write_entries(memories_dir / "USER.md", user_kept)
        print(f"  MEMORY.md: {len(mem_kept)} entries")
        print(f"  USER.md:   {len(user_kept)} entries")

    # Reload via MemoryStore to verify the new sizes.
    if not args.dry_run:
        store = MemoryStore()
        store.load_from_disk()
        kept_mem_chars = len(ENTRY_DELIMITER.join(store.memory_entries))
        kept_user_chars = len(ENTRY_DELIMITER.join(store.user_entries))
    else:
        kept_mem_chars = len(ENTRY_DELIMITER.join(mem_kept))
        kept_user_chars = len(ENTRY_DELIMITER.join(user_kept))

    # Phase 4: optionally tighten caps in config.yaml.
    print()
    print("=" * 78)
    print("Phase 4: tighten hot-tier caps in config.yaml (optional)")
    print("=" * 78)
    _suggest_cap_update(config_path, kept_mem_chars, kept_user_chars, args.dry_run)

    print()
    print("=" * 78)
    print("Migration complete.")
    if args.dry_run:
        print("  (DRY RUN — no files were modified.)")
    else:
        print(f"  Backup at: {memories_dir}/.backup-...")
        print("  Restart your Hermes session to pick up the new hot-tier snapshot.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
