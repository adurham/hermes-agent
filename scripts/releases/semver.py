"""Compare the stable and canary versions accepted by release feeds.

Two canary shapes are release versions. The current one is build metadata
(``X.Y.Z+canary.<stamp>``) and compares equal to its stable, which is the
point: "newer?" comes from the channel record. The legacy prerelease shape
(``X.Y.Z-canary.<14 digits>``) sorts before its stable, so a feed published
before the migration still orders correctly.
"""
from __future__ import annotations

import re

from hermes_cli.update_channel import _CANARY_TAG_RE, STABLE_TAG_RE

_CANARY_VERSION_RE = re.compile(r"^[0-9.]+[+]canary[.]20\d{6}T\d{6}Z$")


def is_canary_version(version: str) -> bool:
    """True for the build-metadata canary identity and nothing else."""
    if not isinstance(version, str) or not _CANARY_VERSION_RE.fullmatch(version):
        return False
    return bool(STABLE_TAG_RE.fullmatch("v" + version.split("+", 1)[0]))


def _is_legacy_canary(version: str) -> bool:
    return isinstance(version, str) and bool(_CANARY_TAG_RE.fullmatch("v" + version)) and len(
        version.partition("-")[2].split(".", 1)[1]) == 14


def is_valid_version(version: str) -> bool:
    """A stable version or a legacy canary prerelease — the grammar the
    existing feeds and gates were published with."""
    if not isinstance(version, str):
        return False
    core, sep, _tail = version.partition("-")
    if sep:
        return _is_legacy_canary(version) and bool(STABLE_TAG_RE.fullmatch("v" + core))
    return bool(STABLE_TAG_RE.fullmatch("v" + version))


def is_release_version(version: str) -> bool:
    """True for a stable version, a legacy canary, or a build-metadata canary."""
    return is_valid_version(version) or is_canary_version(version)


def _core(version: str) -> list[int]:
    return [int(part) for part in version.split("+", 1)[0].split("-", 1)[0].split(".")]


def compare(a: str, b: str) -> int:
    """Compare two release versions.

    Build metadata is ignored, so a ``+canary`` identity compares equal to the
    stable it was built from. A legacy ``-canary`` prerelease sorts before the
    stable of the same core, stamps compared numerically. ValueError on
    anything that is not a release version — feed publication fails loudly.
    """
    if not is_release_version(a) or not is_release_version(b):
        raise ValueError(f"invalid release version(s): {a!r}, {b!r}")
    ka, kb = _core(a), _core(b)
    if ka != kb:
        return -1 if ka < kb else 1
    a_pre, b_pre = "-" in a.split("+", 1)[0], "-" in b.split("+", 1)[0]
    if a_pre == b_pre:
        if not a_pre:
            return 0
        a_stamp = int(a.split("+", 1)[0].split("-", 1)[1].split(".", 1)[1])
        b_stamp = int(b.split("+", 1)[0].split("-", 1)[1].split(".", 1)[1])
        return -1 if a_stamp < b_stamp else (1 if a_stamp > b_stamp else 0)
    return -1 if a_pre else 1
