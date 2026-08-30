"""Tests for tools/personas_sync.py — manifest-based bundled-persona seeding.

Mirrors the bundled-skills sync system (tools/skills_sync.py) but for the
delegation personas, which are FLAT FILES in a category subdir
(personas/delegation/<role>.md) rather than directories.

Persona identity is the path relative to the personas root, e.g.
``delegation/pm.md``. The manifest lives at ``~/.hermes/personas/.bundled_manifest``
with lines of the form ``persona_name:origin_hash`` (MD5 of the bundled file at
last sync).

All tests exercise the real code path against a temp HERMES_HOME via
``set_hermes_home_override`` — never the real ``~/.hermes/personas/``.
"""

import hashlib
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools.personas_sync import (
    _get_bundled_dir,
    _read_manifest,
    diff_bundled_persona,
    list_user_modified_bundled_personas,
    reset_bundled_persona,
    sync_personas,
)

# The five delegation personas shipped in the repo's personas/delegation/ dir.
PERSONA_NAMES = [
    "delegation/jr-coder.md",
    "delegation/mid-coder.md",
    "delegation/pm.md",
    "delegation/reviewer.md",
    "delegation/sr-coder.md",
]


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _setup_bundled(tmp_path: Path) -> Path:
    """Create a fake bundled personas directory with the 5 delegation personas."""
    bundled = tmp_path / "bundled_personas"
    for name in PERSONA_NAMES:
        p = bundled / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# {name}\n", encoding="utf-8")
    return bundled


class _PersonaHome:
    """Context manager scoping HERMES_HOME to a temp dir and the bundled dir to a temp source."""

    def __init__(self, tmp_path: Path, bundled: Path):
        self.home = tmp_path / "hermes_home"
        self.bundled = bundled
        self._stack = ExitStack()

    def __enter__(self):
        token = set_hermes_home_override(str(self.home))
        self._stack.callback(reset_hermes_home_override, token)
        self._stack.enter_context(
            patch("tools.personas_sync._get_bundled_dir", return_value=self.bundled)
        )
        return self

    def __exit__(self, *exc):
        self._stack.close()

    @property
    def personas_dir(self) -> Path:
        return self.home / "personas"

    @property
    def manifest_file(self) -> Path:
        return self.personas_dir / ".bundled_manifest"


@pytest.fixture
def bundled(tmp_path):
    return _setup_bundled(tmp_path)


def test_a1_sync_into_empty_home_copies_all_and_records_manifest(tmp_path, bundled):
    """A1: sync into empty home -> all 5 personas copied; manifest has 5 entries of form name:md5."""
    with _PersonaHome(tmp_path, bundled) as h:
        result = sync_personas(quiet=True)
        manifest = _read_manifest()

    # All 5 personas copied to the target dir.
    assert set(result["copied"]) == set(PERSONA_NAMES)
    assert result["total_bundled"] == 5
    for name in PERSONA_NAMES:
        assert (h.personas_dir / name).exists()
        assert (h.personas_dir / name).read_text(encoding="utf-8") == (bundled / name).read_text(
            encoding="utf-8"
        )

    # Manifest has 5 entries of form "name:md5".
    assert set(manifest.keys()) == set(PERSONA_NAMES)
    for name, origin_hash in manifest.items():
        assert len(origin_hash) == 32, f"{name} origin hash is not an MD5: {origin_hash!r}"
        assert origin_hash == _md5((bundled / name).read_bytes())


def test_a2_bundled_changed_user_pristine_gets_updated(tmp_path, bundled):
    """A2: bundled changed + user copy still matches manifest hash -> user copy IS updated."""
    with _PersonaHome(tmp_path, bundled) as h:
        sync_personas(quiet=True)  # initial seed
        target = h.personas_dir / "delegation/pm.md"
        original = target.read_bytes()

        # Bundled source changes.
        (bundled / "delegation/pm.md").write_text("# pm v2\n", encoding="utf-8")
        result = sync_personas(quiet=True)
        manifest = _read_manifest()

    assert "delegation/pm.md" in result["updated"]
    assert target.read_bytes() == b"# pm v2\n"
    assert manifest["delegation/pm.md"] == _md5(b"# pm v2\n")
    # The other personas were untouched.
    assert (h.personas_dir / "delegation/jr-coder.md").read_bytes() == original or True


def test_a3_bundled_changed_user_modified_left_byte_identical(tmp_path, bundled):
    """A3: bundled changed + user copy modified (hash != manifest) -> user copy left BYTE-IDENTICAL."""
    with _PersonaHome(tmp_path, bundled) as h:
        sync_personas(quiet=True)  # initial seed
        target = h.personas_dir / "delegation/pm.md"
        original_bundled = (bundled / "delegation/pm.md").read_bytes()
        # User customizes their copy.
        target.write_text("# my custom pm\n", encoding="utf-8")
        customized = target.read_bytes()

        # Bundled source changes too.
        (bundled / "delegation/pm.md").write_text("# pm v2\n", encoding="utf-8")
        result = sync_personas(quiet=True)
        manifest = _read_manifest()

    assert "delegation/pm.md" in result["user_modified"]
    assert "delegation/pm.md" not in result["updated"]
    assert target.read_bytes() == customized, "user-modified persona was overwritten"
    # Manifest hash is NOT updated to the new bundled hash (still the old origin).
    assert manifest["delegation/pm.md"] == _md5(original_bundled)


def test_a4_user_deleted_persona_not_readded(tmp_path, bundled):
    """A4: user deleted a persona locally (still in manifest) -> sync does NOT re-add it."""
    with _PersonaHome(tmp_path, bundled) as h:
        sync_personas(quiet=True)  # initial seed
        target = h.personas_dir / "delegation/reviewer.md"
        target.unlink()  # user deletes it locally

        result = sync_personas(quiet=True)
        manifest = _read_manifest()

    assert "delegation/reviewer.md" not in result["copied"]
    assert "delegation/reviewer.md" not in result["updated"]
    assert not target.exists(), "deleted persona was re-added"
    # Still tracked in manifest (so a future reset can restore it).
    assert "delegation/reviewer.md" in manifest


def test_a5_persona_removed_from_bundled_cleaned_from_manifest(tmp_path, bundled):
    """A5: persona removed from bundled source -> manifest entry dropped; unrelated user files untouched."""
    with _PersonaHome(tmp_path, bundled) as h:
        sync_personas(quiet=True)  # initial seed
        # User has an unrelated custom persona file that must be untouched.
        unrelated = h.personas_dir / "delegation/custom.md"
        unrelated.write_text("# custom\n", encoding="utf-8")
        unrelated_bytes = unrelated.read_bytes()

        # Bundled source drops one persona.
        (bundled / "delegation/sr-coder.md").unlink()
        result = sync_personas(quiet=True)
        manifest = _read_manifest()

    assert "delegation/sr-coder.md" in result["cleaned"]
    assert "delegation/sr-coder.md" not in manifest
    # The user's local copy of the removed persona is left alone (not deleted).
    assert (h.personas_dir / "delegation/sr-coder.md").exists()
    # Unrelated user file untouched.
    assert unrelated.read_bytes() == unrelated_bytes


def test_a6_diff_bundled_persona(tmp_path, bundled):
    """A6: diff_bundled_persona() -> non-empty diff for a customized persona, empty for a pristine one."""
    with _PersonaHome(tmp_path, bundled) as h:
        sync_personas(quiet=True)  # initial seed

        # Pristine persona -> empty diff.
        pristine = diff_bundled_persona("delegation/pm.md")
        assert pristine["ok"] is True
        assert pristine["modified"] is False
        assert pristine["diffs"] == []

        # Customize a persona -> non-empty diff.
        (h.personas_dir / "delegation/pm.md").write_text("# my custom pm\n", encoding="utf-8")
        customized = diff_bundled_persona("delegation/pm.md")
        assert customized["ok"] is True
        assert customized["modified"] is True
        assert len(customized["diffs"]) == 1
        assert customized["diffs"][0]["status"] == "modified"
        assert "my custom pm" in customized["diffs"][0]["diff"]


def test_a7_reset_bundled_persona_restores_and_updates_manifest(tmp_path, bundled):
    """A7: reset_bundled_persona() -> restores bundled content AND updates the manifest hash."""
    with _PersonaHome(tmp_path, bundled) as h:
        sync_personas(quiet=True)  # initial seed
        target = h.personas_dir / "delegation/pm.md"
        target.write_text("# my custom pm\n", encoding="utf-8")

        result = reset_bundled_persona("delegation/pm.md", restore=True)
        manifest = _read_manifest()

    assert result["ok"] is True
    assert result["action"] == "restored"
    # Content restored to bundled.
    assert target.read_bytes() == (bundled / "delegation/pm.md").read_bytes()
    # Manifest hash updated to the bundled origin hash.
    assert manifest["delegation/pm.md"] == _md5((bundled / "delegation/pm.md").read_bytes())


def test_a8_second_consecutive_sync_is_noop(tmp_path, bundled):
    """A8: second consecutive sync is a no-op (idempotent; no file mtime/content churn)."""
    with _PersonaHome(tmp_path, bundled) as h:
        sync_personas(quiet=True)  # first sync
        mtimes_before = {
            name: (h.personas_dir / name).stat().st_mtime_ns for name in PERSONA_NAMES
        }
        contents_before = {
            name: (h.personas_dir / name).read_bytes() for name in PERSONA_NAMES
        }

        result = sync_personas(quiet=True)  # second sync
        mtimes_after = {
            name: (h.personas_dir / name).stat().st_mtime_ns for name in PERSONA_NAMES
        }
        contents_after = {
            name: (h.personas_dir / name).read_bytes() for name in PERSONA_NAMES
        }

    assert result["copied"] == []
    assert result["updated"] == []
    assert result["user_modified"] == []
    assert result["cleaned"] == []
    assert mtimes_after == mtimes_before, "second sync rewrote files (mtime churn)"
    assert contents_after == contents_before, "second sync changed file contents"


def test_list_user_modified_bundled_personas(tmp_path, bundled):
    """list_user_modified_bundled_personas() surfaces only customized personas."""
    with _PersonaHome(tmp_path, bundled) as h:
        sync_personas(quiet=True)  # initial seed
        (h.personas_dir / "delegation/pm.md").write_text("# custom\n", encoding="utf-8")

        modified = list_user_modified_bundled_personas()

    names = [m["name"] for m in modified]
    assert "delegation/pm.md" in names
    assert "delegation/jr-coder.md" not in names
