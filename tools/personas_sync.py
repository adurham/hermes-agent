#!/usr/bin/env python3
"""
Personas Sync -- Manifest-based seeding and updating of bundled personas.

Copies bundled delegation personas from the repo's personas/ directory into
~/.hermes/personas/ and uses a manifest to track which personas have been
synced and their origin hash.

This mirrors tools/skills_sync.py exactly, but for personas. The key
structural difference: bundled skills are DIRECTORIES (skills/<name>/SKILL.md)
whereas bundled personas are FLAT FILES in a category subdir
(personas/delegation/<role>.md). Persona identity is therefore the path
relative to the personas root, e.g. ``delegation/pm.md``.

Manifest format: each line is "persona_name:origin_hash" where origin_hash is
the MD5 of the bundled persona file at the time it was last synced to the user
dir. The manifest lives at ~/.hermes/personas/.bundled_manifest.

Update logic (identical semantics to skills_sync):
  - NEW personas (not in manifest): copied to user dir, origin hash recorded.
  - EXISTING personas (in manifest, present in user dir):
      * If bundled still matches origin hash: no update → skip without reading
        the user copy.
      * If bundled changed and user copy matches origin hash: safe to update.
      * If bundled changed and user copy differs: user customized it → SKIP.
  - DELETED by user (in manifest, absent from user dir): respected, not re-added.
  - REMOVED from bundled (in manifest, gone from repo): cleaned from manifest.
"""

import hashlib
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Force stdout/stderr to UTF-8 (same rationale as skills_sync.py).
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, TypeError):
            pass
from hermes_constants import get_bundled_personas_dir, get_hermes_home
from typing import Set
from utils import atomic_write_text

logger = logging.getLogger(__name__)


HERMES_HOME = get_hermes_home()
PERSONAS_DIR = HERMES_HOME / "personas"
MANIFEST_FILE = PERSONAS_DIR / ".bundled_manifest"

# Import-time snapshots backing the call-time accessors below. Same bug class
# and same fix as skills_sync.py: long-lived multi-profile runtimes import
# this module once under the launch HERMES_HOME and later scope requests to a
# different profile via set_hermes_home_override(). Frozen module constants
# would then resolve — and for reset_bundled_persona() DELETE — against the
# wrong profile's personas root. The accessors honor an explicitly patched
# module global (tests) and otherwise re-resolve from the live profile-scoped
# HERMES_HOME on every call.
_HERMES_HOME_AT_IMPORT = HERMES_HOME
_PERSONAS_DIR_AT_IMPORT = PERSONAS_DIR
_MANIFEST_FILE_AT_IMPORT = MANIFEST_FILE


def _hermes_home() -> Path:
    """Return the active profile's HERMES_HOME at call time."""
    configured = Path(HERMES_HOME)
    if configured != _HERMES_HOME_AT_IMPORT:
        return configured
    return get_hermes_home()


def _personas_dir() -> Path:
    """Return the active profile's personas directory at call time."""
    configured = Path(PERSONAS_DIR)
    if configured != _PERSONAS_DIR_AT_IMPORT:
        return configured
    return _hermes_home() / "personas"


def _manifest_file() -> Path:
    """Return the active profile's bundled-personas manifest at call time."""
    configured = Path(MANIFEST_FILE)
    if configured != _MANIFEST_FILE_AT_IMPORT:
        return configured
    return _personas_dir() / ".bundled_manifest"


# Marker file written by `hermes profile create --no-skills` (named profiles)
# and by the installer's `--no-skills` flag (the default ~/.hermes profile).
# When present in HERMES_HOME, sync_personas() is a no-op so neither the
# installer, `hermes update`, nor a direct sync re-injects bundled personas.
# Delete the file to opt back in. Mirrors skills_sync.NO_BUNDLED_SKILLS_MARKER.
NO_BUNDLED_PERSONAS_MARKER = ".no-bundled-personas"


def _get_bundled_dir() -> Path:
    """Locate the bundled personas/ directory.

    Checks HERMES_BUNDLED_PERSONAS env var first (set by Nix wrapper),
    then falls back to the relative path from this source file.
    """
    return get_bundled_personas_dir(Path(__file__).parent.parent / "personas")


def _read_manifest() -> Dict[str, str]:
    """
    Read the manifest as a dict of {persona_name: origin_hash}.

    Persona names are paths relative to the personas root, e.g.
    ``delegation/pm.md``. Lines are "name:hash".
    """
    if not _manifest_file().exists():
        return {}
    try:
        result = {}
        for line in _manifest_file().read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            if ":" in line:
                name, _, hash_val = line.partition(":")
                result[name.strip()] = hash_val.strip()
            else:
                # Plain name (no hash) — empty hash triggers re-baseline.
                result[line] = ""
        return result
    except (OSError, IOError):
        return {}


def _write_manifest(entries: Dict[str, str]):
    """Write the manifest file atomically in "name:hash" format."""
    _manifest_file().parent.mkdir(parents=True, exist_ok=True)
    data = "\n".join(f"{name}:{hash_val}" for name, hash_val in sorted(entries.items())) + "\n"

    try:
        atomic_write_text(
            _manifest_file(),
            data,
            tmp_prefix=".bundled_manifest_",
            preserve_mode=True,
        )
    except Exception as e:
        logger.debug("Failed to write personas manifest %s: %s", _manifest_file(), e, exc_info=True)


def _discover_bundled_personas(bundled_dir: Path) -> List[Tuple[str, Path]]:
    """
    Find all persona files in the bundled directory.

    Personas are FLAT FILES (unlike skills, which are directories). Each
    ``.md`` file under the bundled personas root is a persona whose identity
    is its path relative to the root, e.g. ``delegation/pm.md``.

    Returns list of (persona_name, persona_file_path) tuples.
    """
    personas = []
    if not bundled_dir.exists():
        return personas

    for persona_file in sorted(bundled_dir.rglob("*.md")):
        if not persona_file.is_file():
            continue
        rel = persona_file.relative_to(bundled_dir)
        # Skip the manifest if it ever lands inside the bundled tree.
        if rel.name == ".bundled_manifest":
            continue
        personas.append((rel.as_posix(), persona_file))

    return personas


def _compute_relative_dest(persona_file: Path, bundled_dir: Path) -> Path:
    """
    Compute the destination path in the personas dir preserving the category structure.
    e.g., bundled/personas/delegation/pm.md -> ~/.hermes/personas/delegation/pm.md
    """
    rel = persona_file.relative_to(bundled_dir)
    return _personas_dir() / rel


def _file_hash(path: Path) -> str:
    """Compute the MD5 of a single file's contents for change detection."""
    hasher = hashlib.md5()
    try:
        hasher.update(path.read_bytes())
    except (OSError, IOError):
        pass
    return hasher.hexdigest()


def _is_tracked_user_modification(origin_hash: str, user_hash: str) -> bool:
    """Whether an on-disk persona counts as a user modification sync keeps.

    Shared by the sync loop (which decides what to skip) and
    ``list_user_modified_bundled_personas`` (which surfaces the names) so the
    two can never drift. A persona is a tracked modification only when it has
    a recorded origin hash and its current content hash differs from that origin.
    """
    return bool(origin_hash) and user_hash != origin_hash


def sync_personas(quiet: bool = False) -> dict:
    """
    Sync bundled personas into ~/.hermes/personas/ using the manifest.

    Returns:
        dict with keys: copied (list), updated (list), skipped (int),
                        user_modified (list), cleaned (list), total_bundled (int)
    """
    # Opt-out: a profile (named or the default ~/.hermes) that wrote the
    # .no-bundled-personas marker gets zero bundled-persona seeding.
    if (_hermes_home() / NO_BUNDLED_PERSONAS_MARKER).exists():
        if not quiet:
            print("  (skipped — profile opted out of bundled personas via .no-bundled-personas)")
        return {
            "copied": [], "updated": [], "skipped": 0,
            "user_modified": [], "cleaned": [], "total_bundled": 0,
            "skipped_opt_out": True,
        }

    bundled_dir = _get_bundled_dir()
    if not bundled_dir.exists():
        return {
            "copied": [], "updated": [], "skipped": 0,
            "user_modified": [], "cleaned": [], "total_bundled": 0,
        }

    _personas_dir().mkdir(parents=True, exist_ok=True)
    manifest = _read_manifest()
    bundled_personas = _discover_bundled_personas(bundled_dir)
    bundled_names = {name for name, _ in bundled_personas}

    copied = []
    updated = []
    user_modified = []
    skipped = 0

    for persona_name, persona_src in bundled_personas:
        dest = _compute_relative_dest(persona_src, bundled_dir)
        bundled_hash = _file_hash(persona_src)

        if persona_name not in manifest:
            # ── New persona — never offered before ──
            try:
                if dest.exists():
                    # User already has a persona with the same name — don't overwrite.
                    # Only baseline in the manifest when the on-disk copy is
                    # byte-identical to bundled; otherwise skip the manifest write
                    # so user_hash != origin_hash doesn't read as "user-modified"
                    # forever, permanently blocking bundled updates.
                    skipped += 1
                    if _file_hash(dest) == bundled_hash:
                        manifest[persona_name] = bundled_hash
                    elif not quiet:
                        print(
                            f"  ⚠ {persona_name}: bundled version shipped but you "
                            f"already have a local persona by this name — yours "
                            f"was kept. Run `hermes personas reset {persona_name}` "
                            f"to replace it with the bundled version."
                        )
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(persona_src, dest)
                    copied.append(persona_name)
                    manifest[persona_name] = bundled_hash
                    if not quiet:
                        print(f"  + {persona_name}")
            except (OSError, IOError) as e:
                if not quiet:
                    print(f"  ! Failed to copy {persona_name}: {e}")
                # Do NOT add to manifest — next sync should retry

        elif dest.exists():
            # ── Existing persona — in manifest AND on disk ──
            origin_hash = manifest.get(persona_name, "")

            # If the bundled source still matches the version recorded when it
            # was installed, there is no update to apply.
            if origin_hash and bundled_hash == origin_hash:
                skipped += 1
                continue

            user_hash = _file_hash(dest)

            if not origin_hash:
                # No origin hash recorded. Set baseline from user's current
                # copy so future syncs can detect modifications.
                manifest[persona_name] = user_hash
                skipped += 1
                continue

            if _is_tracked_user_modification(origin_hash, user_hash):
                # User modified this persona — don't overwrite their changes
                user_modified.append(persona_name)
                if not quiet:
                    print(f"  ~ {persona_name} (user-modified, skipping)")
                continue

            # User copy matches origin — check if bundled has a newer version
            if bundled_hash != origin_hash:
                try:
                    # Move old copy to a backup so we can restore on failure
                    backup = dest.with_suffix(".bak")
                    if backup.exists():
                        backup.unlink()
                    shutil.move(str(dest), str(backup))
                    try:
                        shutil.copy2(persona_src, dest)
                        manifest[persona_name] = bundled_hash
                        updated.append(persona_name)
                        if not quiet:
                            print(f"  ↑ {persona_name} (updated)")
                        # Remove backup after successful copy
                        try:
                            backup.unlink()
                        except (OSError, IOError):
                            logger.debug("Could not remove backup %s", backup, exc_info=True)
                    except (OSError, IOError):
                        # Restore from backup. A partially-written dest must
                        # not shadow the user's copy or block the restore.
                        if backup.exists():
                            if dest.exists():
                                try:
                                    dest.unlink()
                                except (OSError, IOError):
                                    logger.warning(
                                        "Could not clear partial copy %s during restore",
                                        dest, exc_info=True,
                                    )
                            if not dest.exists():
                                shutil.move(str(backup), str(dest))
                        raise
                except (OSError, IOError) as e:
                    if not quiet:
                        print(f"  ! Failed to update {persona_name}: {e}")
            else:
                skipped += 1  # bundled unchanged, user unchanged

        else:
            # ── In manifest but not on disk — user deleted it ──
            skipped += 1

    # Clean stale manifest entries (personas removed from bundled dir)
    cleaned = sorted(set(manifest.keys()) - bundled_names)
    for name in cleaned:
        del manifest[name]

    _write_manifest(manifest)

    return {
        "copied": copied,
        "updated": updated,
        "skipped": skipped,
        "user_modified": user_modified,
        "cleaned": cleaned,
        "total_bundled": len(bundled_personas),
    }


def reset_bundled_persona(name: str, restore: bool = False) -> dict:
    """
    Reset a bundled persona's manifest tracking so future syncs work normally.

    When a user edits a bundled persona, subsequent syncs mark it as
    ``user_modified`` and skip it forever — even if the user later copies the
    bundled version back into place, because the manifest still holds the
    *old* origin hash. This function breaks that loop.

    Args:
        name: The persona name (path relative to the personas root, e.g.
              ``delegation/pm.md`` — matches the manifest key).
        restore: If True, also delete the user's copy in the personas dir and
                 let the next sync re-copy the current bundled version. If
                 False (default), only clear the manifest entry — the user's
                 current copy is preserved but future updates work again.

    Returns:
        dict with keys:
          - ok: bool, whether the reset succeeded
          - action: one of "manifest_cleared", "restored", "not_in_manifest",
                    "bundled_missing"
          - message: human-readable description
          - synced: dict from sync_personas() if a sync was triggered, else None
    """
    manifest = _read_manifest()
    bundled_dir = _get_bundled_dir()
    bundled_personas = _discover_bundled_personas(bundled_dir)
    bundled_by_name = dict(bundled_personas)

    in_manifest = name in manifest
    is_bundled = name in bundled_by_name

    if not in_manifest and not is_bundled:
        return {
            "ok": False,
            "action": "not_in_manifest",
            "message": (
                f"'{name}' is not a tracked bundled persona. Nothing to reset."
            ),
            "synced": None,
        }

    # Step 1 (optional): delete the user's copy so next sync re-copies bundled.
    # Must happen BEFORE manifest deletion so that a failed unlink does not
    # leave the persona in a manifest-less limbo state.
    deleted_user_copy = False
    if restore:
        if not is_bundled:
            return {
                "ok": False,
                "action": "bundled_missing",
                "message": (
                    f"'{name}' has no bundled source — manifest entry preserved "
                    f"but cannot restore from bundled (persona was removed upstream)."
                ),
                "synced": None,
            }
        dest = _compute_relative_dest(bundled_by_name[name], bundled_dir)
        if dest.exists():
            try:
                dest.unlink()
                deleted_user_copy = True
            except (OSError, IOError) as e:
                return {
                    "ok": False,
                    "action": "not_reset",
                    "message": (
                        f"Could not delete user copy at {dest}: {e}. "
                        f"Manifest entry preserved — nothing was changed."
                    ),
                    "synced": None,
                }

    # Step 2: drop the manifest entry so next sync treats it as new
    if in_manifest:
        del manifest[name]
        _write_manifest(manifest)

    # Step 3: run sync to re-baseline (or re-copy if we deleted)
    synced = sync_personas(quiet=True)

    if restore and deleted_user_copy:
        action = "restored"
        message = f"Restored '{name}' from bundled source."
    elif restore:
        # Nothing on disk to delete, but we re-synced — acts like a fresh install
        action = "restored"
        message = f"Restored '{name}' (no prior user copy, re-copied from bundled)."
    else:
        action = "manifest_cleared"
        message = (
            f"Cleared manifest entry for '{name}'. Future `hermes update` runs "
            f"will re-baseline against your current copy and accept upstream changes."
        )

    return {"ok": True, "action": action, "message": message, "synced": synced}


def list_user_modified_bundled_personas() -> List[dict]:
    """Return the bundled personas that sync keeps because the user edited them locally.

    A persona counts as user-modified when its on-disk copy no longer matches
    the origin hash recorded in the manifest the last time it was synced — the
    exact same test the sync loop uses to decide what to skip.

    Returns a list (sorted by name) of dicts:
        ``{"name": str, "dest": Path, "bundled_src": Path}``
    where ``dest`` is the user's copy and ``bundled_src`` is the current stock
    copy (so callers can diff or restore).
    """
    manifest = _read_manifest()
    if not manifest:
        return []
    bundled_dir = _get_bundled_dir()
    modified: List[dict] = []
    for persona_name, persona_src in _discover_bundled_personas(bundled_dir):
        origin_hash = manifest.get(persona_name, "")
        # No entry, or an un-baselined entry (empty hash): not a tracked
        # modification — the next sync handles it.
        if not origin_hash:
            continue
        dest = _compute_relative_dest(persona_src, bundled_dir)
        if not dest.exists():
            continue
        if _is_tracked_user_modification(origin_hash, _file_hash(dest)):
            modified.append(
                {"name": persona_name, "dest": dest, "bundled_src": persona_src}
            )
    modified.sort(key=lambda e: e["name"])
    return modified


def _read_for_diff(path: Path) -> Tuple[Optional[bytes], Optional[str]]:
    """Read a file once for diffing.

    Returns ``(raw_bytes, text)`` where ``text`` is ``None`` if the file is
    binary; ``(None, None)`` if it could not be read.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return None, None
    if b"\x00" in data:
        return data, None
    try:
        return data, data.decode("utf-8")
    except UnicodeDecodeError:
        return data, None


def diff_bundled_persona(name: str) -> dict:
    """Diff a user's copy of a bundled persona against the current stock version.

    Lets a user see exactly what diverged before deciding whether to keep their
    edits or ``hermes personas reset`` back to upstream.

    Returns a dict:
        ``ok`` (bool), ``name`` (str), ``found`` (bool — bundled source exists),
        ``modified`` (bool), ``message`` (str),
        ``diffs``: list of ``{"path": str, "status": str, "diff": str}`` where
        status is one of ``modified`` / ``added`` (only in user copy) /
        ``removed`` (only in bundled) / ``binary``.
    """
    import difflib

    bundled_dir = _get_bundled_dir()
    bundled_by_name = dict(_discover_bundled_personas(bundled_dir))
    bundled_src = bundled_by_name.get(name)
    if bundled_src is None:
        return {
            "ok": False,
            "name": name,
            "found": False,
            "modified": False,
            "diffs": [],
            "message": (
                f"'{name}' is not a tracked bundled persona (no stock version to "
                f"diff against)."
            ),
        }
    dest = _compute_relative_dest(bundled_src, bundled_dir)
    if not dest.exists():
        return {
            "ok": False,
            "name": name,
            "found": True,
            "modified": False,
            "diffs": [],
            "message": f"No local copy of '{name}' found at {dest}.",
        }

    user_bytes, user_text = _read_for_diff(dest)
    stock_bytes, stock_text = _read_for_diff(bundled_src)

    diffs: List[dict] = []
    if user_text is None or stock_text is None:
        # At least one side is binary — report only if bytes differ.
        if user_bytes != stock_bytes:
            diffs.append(
                {"path": name, "status": "binary", "diff": "<binary file differs>"}
            )
    elif user_text != stock_text:
        text = "".join(
            difflib.unified_diff(
                stock_text.splitlines(keepends=True),
                user_text.splitlines(keepends=True),
                fromfile=f"stock/{name}",
                tofile=f"yours/{name}",
            )
        )
        diffs.append({"path": name, "status": "modified", "diff": text})

    modified = bool(diffs)
    return {
        "ok": True,
        "name": name,
        "found": True,
        "modified": modified,
        "diffs": diffs,
        "message": (
            f"'{name}' matches the stock version."
            if not modified
            else f"'{name}' differs from the stock version in {len(diffs)} file(s)."
        ),
    }


def set_bundled_personas_opt_out(enabled: bool) -> dict:
    """Toggle the .no-bundled-personas opt-out marker for the active profile.

    When ``enabled`` is True, writes HERMES_HOME/.no-bundled-personas so the
    installer, ``hermes update``, and any direct sync stop seeding bundled
    personas. When False, removes the marker so seeding resumes on the next
    sync.

    Returns:
        dict with keys: ok (bool), changed (bool), marker (str path),
                        message (str).
    """
    marker = _hermes_home() / NO_BUNDLED_PERSONAS_MARKER
    existed = marker.exists()
    try:
        if enabled:
            _hermes_home().mkdir(parents=True, exist_ok=True)
            marker.write_text(
                "This profile opted out of bundled-persona seeding.\n"
                "Delete this file to re-enable sync on the next `hermes update`.\n",
                encoding="utf-8",
            )
            changed = not existed
            message = (
                "Opted out of bundled personas. Future install / update / sync "
                "runs will not seed bundled personas into this profile."
                if changed
                else "Already opted out — marker was already present."
            )
        else:
            if existed:
                marker.unlink()
            changed = existed
            message = (
                "Opted back in. The next `hermes update` (or a direct sync) "
                "will re-seed bundled personas."
                if changed
                else "Not opted out — no marker to remove."
            )
    except OSError as e:
        return {
            "ok": False, "changed": False, "marker": str(marker),
            "message": f"Could not update opt-out marker at {marker}: {e}",
        }
    return {"ok": True, "changed": changed, "marker": str(marker), "message": message}


def is_bundled_personas_opt_out() -> bool:
    """Return True if the active profile carries the opt-out marker."""
    return (_hermes_home() / NO_BUNDLED_PERSONAS_MARKER).exists()


if __name__ == "__main__":
    print("Syncing bundled personas into ~/.hermes/personas/ ...")
    result = sync_personas(quiet=False)
    parts = [
        f"{len(result['copied'])} new",
        f"{len(result['updated'])} updated",
        f"{result['skipped']} unchanged",
    ]
    if result["user_modified"]:
        names = result["user_modified"]
        MAX_SHOW = 5
        shown = ", ".join(names[:MAX_SHOW])
        if len(names) > MAX_SHOW:
            shown += f", +{len(names) - MAX_SHOW} more"
        parts.append(f"{len(names)} user-modified (kept): {shown}")
    if result["cleaned"]:
        parts.append(f"{len(result['cleaned'])} cleaned from manifest")
    print(f"\nDone: {', '.join(parts)}. {result['total_bundled']} total bundled.")
