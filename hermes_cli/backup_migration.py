"""Strict, disposable full-home snapshots using the normal backup inventory.

The caller quiesces writers and owns a private staging directory. Unlike quick
snapshots, this includes skills, profiles and transcripts; unlike automatic ZIP
backups, any unreadable required entry aborts publication. Regenerable exclusions
still follow the full-backup policy. It is not a byte-for-byte package archive.
"""

import hashlib
import json
import os
import shutil
import sqlite3
import stat
from pathlib import Path

from hermes_cli.backup import _backup_operation_lock, _iter_backup_files
from hermes_cli.backup_sqlite import _safe_copy_db


LINK_INVENTORY = '.hermes-retained-links.json'


def retained_link(entry: Path, relative: Path, removal_roots: tuple[Path, ...]) -> dict:
    parts = relative.parts[2:] if relative.parts[:1] == ('profiles',) else relative.parts
    if not parts or parts[0] not in {'plugins', 'skills'}:
        raise ValueError(f'Compatibility reader input cannot be a link: {relative}')
    resolved = entry.resolve(strict=True)
    if any(resolved == root or root in resolved.parents for root in removal_roots):
        raise ValueError(f'Retained link points into the removal footprint: {relative}')
    return {'path': str(relative), 'target': os.readlink(entry), 'resolved': str(resolved)}


def verify_retained_links(source: Path, snapshot: Path, removal_roots: tuple[Path, ...]) -> None:
    file = snapshot / LINK_INVENTORY
    if file.exists():
        roots = tuple(root.resolve() for root in removal_roots)
        for row in json.loads(file.read_text(encoding='utf-8')):
            relative = Path(row['path'])
            if retained_link(source / relative, relative, roots) != row:
                raise ValueError(f'Retained link changed during migration: {relative}')


def snapshot_migration_home(source: Path, target: Path, *, retained_removal_roots: tuple[Path, ...] | None = None) -> None:
    """Fill a new private tree; never publish a partial snapshot or follow links."""
    source = source.resolve(strict=True)
    resolved_target = target.resolve()
    if source == resolved_target or source in resolved_target.parents or resolved_target in source.parents:
        raise ValueError('Migration snapshot and source overlap')
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    if (source / LINK_INVENTORY).exists():
        raise ValueError('Reserved migration inventory exists in source home')
    target.mkdir(mode=0o700)
    links = []
    roots = tuple(root.resolve() for root in (retained_removal_roots or ()))
    with _backup_operation_lock(source):
        for entry, relative in _iter_backup_files(source, target, strict=True):
            output = target / relative
            output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            info = entry.lstat()
            is_link = stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)
            if is_link:
                if retained_removal_roots is None:
                    raise ValueError(f'Relocating links requires explicit external preservation: {relative}')
                links.append(retained_link(entry, relative, roots))
                # Keep probes detached: the live link is retained and rechecked,
                # never followed by destination readers on this snapshot.
                continue
            if stat.S_ISDIR(info.st_mode):
                output.mkdir(exist_ok=True, mode=0o700)
                continue
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f'Backup entry is a special file: {relative}')
            with entry.open("rb") as stream:
                sqlite = stream.read(16) == b"SQLite format 3\0"
            if sqlite or entry.suffix == ".db":
                if not _safe_copy_db(entry, output):
                    raise RuntimeError(f"WAL-safe migration snapshot failed: {relative}")
                connection = sqlite3.connect(output)
                try:
                    if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                        raise RuntimeError(f"Migration snapshot integrity failed: {relative}")
                finally:
                    connection.close()
            else:
                # Unknown SQLite sidecars cannot be paired with a fresh image.
                if entry.name.endswith(("-wal", "-shm", "-journal")):
                    database = entry.with_name(entry.name.rsplit("-", 1)[0])
                    with database.open("rb") as stream:
                        if stream.read(16) != b"SQLite format 3\0":
                            raise ValueError(f"Unrecognized SQLite sidecar: {relative}")
                    continue
                shutil.copyfile(entry, output)
                with entry.open('rb') as original, output.open('rb') as copied:
                    if hashlib.file_digest(original, 'sha256').digest() != hashlib.file_digest(copied, 'sha256').digest():
                        raise OSError(f'Migration copy verification failed: {relative}')
            with output.open("rb+") as stream:
                os.fsync(stream.fileno())
            output.chmod(entry.stat().st_mode & 0o700)
        if retained_removal_roots is not None:
            with (target / LINK_INVENTORY).open('x', encoding='utf-8') as stream:
                json.dump(links, stream)
                stream.flush()
                os.fsync(stream.fileno())
            verify_retained_links(source, target, roots)
        if os.name != 'nt':
            # Persist directory entries bottom-up before the caller's rename.
            for directory, _, _ in os.walk(target, topdown=False):
                descriptor = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)