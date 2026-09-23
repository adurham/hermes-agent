"""Ref name to version, and the next version from the release family.

The family is the published stable head, then outstanding ``-rc`` claims, then
the seed when both are empty. CalVer tags and receipt tags are excluded: a
CalVer tag is a valid three-component version and would win every ``max()``.
"""
from __future__ import annotations

import re

from hermes_cli.update_channel import STABLE_TAG_RE

SEED = "0.21.4"
BUMPS = ("major", "minor", "patch")


def published_channel_identity(repository: str, channel: str, *, base_url: str | None = None,
                               reader_type=None) -> tuple[str, str] | None:
    """Resolve one validated protected channel's payload version and commit."""
    from hermes_cli.release_channels import ChannelNotFound, ChannelReader
    from scripts.releases.r2 import public_base_url

    if channel not in {"stable", "canary"}:
        raise ValueError("Expected a protected release channel")
    reader_type = reader_type or ChannelReader
    try:
        resolved = reader_type(base_url or public_base_url(), repository=repository).resolve(channel)
    except ChannelNotFound:
        return None
    if resolved.manifest is None:
        return None
    if resolved.terminal.get("policy") != f"{channel}-release":
        raise ValueError(f"{channel.title()} channel has the wrong publication policy")
    request = resolved.manifest["request"]
    version, commit = request.get("version"), request.get("commit")
    if not isinstance(version, str) or not isinstance(commit, str) or not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError(f"{channel.title()} channel has an invalid published identity")
    return version, commit


def published_stable_identity(repository: str, *, base_url: str | None = None,
                              reader_type=None) -> tuple[str, str | None]:
    """Resolve the protected stable identity, falling back only before it exists."""
    found = published_channel_identity(
        repository, "stable", base_url=base_url, reader_type=reader_type,
    )
    if found is None:
        return SEED, None
    version, commit = found
    if version_from_tag("v" + version) is None:
        raise ValueError("Stable channel has an invalid source version")
    return version, commit


def published_stable_version(repository: str, *, base_url: str | None = None, reader_type=None) -> str:
    return published_stable_identity(repository, base_url=base_url, reader_type=reader_type)[0]


def version_from_tag(ref: str) -> str | None:
    """The version a final release tag names, or None for anything else.

    ``-rc`` claims, build-metadata identities and non-``v`` receipt namespaces
    are not final tags, and a 4-digit major is a CalVer label.
    """
    if not isinstance(ref, str) or not STABLE_TAG_RE.fullmatch(ref):
        return None
    return ref[1:]


# The ref grammar only anchors; ``version_from_tag`` owns the version shape.
_ATTEMPT_REF_RE = re.compile(r"rc\.(?P<attempt>[1-9][0-9]*)-(?P<tag>v[^/]+)")
_MARKER_PREFIX = "abandoned-"


def parse_attempt_ref(ref: str) -> tuple[str, int] | None:
    """``rc.<N>-vX.Y.Z`` to ``(version, N)``, or None for anything else.

    The attempt comes first so the ref can never read as a SemVer prerelease
    of the version it claims.
    """
    match = _ATTEMPT_REF_RE.fullmatch(ref) if isinstance(ref, str) else None
    if match is None:
        return None
    version = version_from_tag(match.group("tag"))
    return (version, int(match.group("attempt"))) if version else None


def parse_marker_ref(ref: str) -> tuple[str, int] | None:
    """``abandoned-rc.<N>-vX.Y.Z``, the record that clears one attempt."""
    if not isinstance(ref, str) or not ref.startswith(_MARKER_PREFIX):
        return None
    return parse_attempt_ref(ref[len(_MARKER_PREFIX):])


def attempt_ref(version: str, attempt: int) -> str:
    ref = f"rc.{attempt}-v{version}"
    if parse_attempt_ref(ref) != (version, attempt):
        raise ValueError(f"invalid attempt ref: {version!r} attempt {attempt!r}")
    return ref


def marker_ref(version: str, attempt: int) -> str:
    return _MARKER_PREFIX + attempt_ref(version, attempt)


def _claim_version(tag: str) -> str | None:
    if isinstance(tag, str) and tag.endswith("-rc"):
        return version_from_tag(tag[:-3])
    return None


def _bump(version: str, bump: str) -> str:
    if bump not in BUMPS:
        raise ValueError(f"unknown bump {bump!r}")
    major, minor, patch = (int(part) for part in version.split("."))
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def derive_next_version(*, published: str | None, claims: list[str], bump: str) -> str:
    """The next version: max of the family, then the bump.

    ``claims`` may contain anything a tag list contains. Only ``v<semver>-rc``
    entries count; CalVer labels and receipt tags are ignored, not errors.
    """
    family = [published] if published else []
    family.extend(version for version in (_claim_version(tag) for tag in claims) if version)
    base = max(family, key=lambda version: [int(part) for part in version.split(".")]) if family else SEED
    return _bump(base, bump)
