"""A ref names a version, and the next version is derived, never read from the tree.

Derivation reads the release family in order: the published stable head, then
outstanding ``-rc`` claims, then the seed ``0.21.4`` when both are empty. CalVer
tags and receipt tags are not inputs — a CalVer tag would win every ``max()``.
"""
import pytest

from scripts.releases.versioning import derive_next_version, version_from_tag

SEED = "0.21.4"


def test_final_tag_is_its_version():
    assert version_from_tag("v0.21.5") == "0.21.5"


@pytest.mark.parametrize("ref", [
    "v0.21.5-rc",
    "v0.21.4+canary.20260922T001400Z",
    "v2026.9.21",
    "canary-0.21.4+canary.20260922T001400Z",
    "channel-preview-20260922T001400Z",
    "commit-abcdef1-20260922T001400Z",
])
def test_non_final_refs_are_not_versions(ref):
    assert version_from_tag(ref) is None


def test_claim_advances_the_line_but_is_not_a_final_tag():
    assert version_from_tag("v0.21.5-rc") is None
    assert derive_next_version(published=None, claims=["v0.21.5-rc"], bump="patch") == "0.21.6"


def test_canary_compares_equal_to_its_stable():
    from scripts.releases.semver import compare, is_canary_version
    assert is_canary_version("0.21.4+canary.20260922T001400Z")
    assert compare("0.21.4+canary.20260922T001400Z", "0.21.4") == 0


def test_calver_tag_is_never_a_derivation_input():
    assert derive_next_version(published=None, claims=["v2026.9.21"], bump="patch") == "0.21.5"


def test_empty_family_seeds_the_line():
    assert derive_next_version(published=None, claims=[], bump="patch") == "0.21.5"
    assert derive_next_version(published=None, claims=[], bump="minor") == "0.22.0"
    assert derive_next_version(published=None, claims=[], bump="major") == "1.0.0"


def test_published_head_beats_the_seed():
    assert derive_next_version(published="0.21.5", claims=[], bump="patch") == "0.21.6"


def test_claim_beats_a_lower_published_head():
    assert derive_next_version(published="0.21.5", claims=["v0.21.7-rc"], bump="patch") == "0.21.8"
