#!/usr/bin/env python3
"""Stage the termux runtime libs into <payload>/runtime-libs/lib/.

The sealed termux deb is self-contained by contract: the device's termux
tree may have NONE of the payload interpreters' runtime libs installed
(first real-device install: `import ctypes` dlopened libffi.so and died).
The pin table (runtime_libs.json) is derived from the suppliers' own
dependency metadata -- the TUR python3.11 .deb's Depends line, uv's zstd,
nodejs's libc++/c-ares/libicu -- not from whichever lib happens to error
first.

Each .deb is downloaded + sha256-verified through pm's hardened downloader
and unpacked with pm's DebPackage (traversal/symlink-checked ar+tar, never
dpkg, never executing package content). Every *.so* from the package's
lib/ payload merges into ONE flat directory -- the trampolines then put a
single payload dir on the linker path.

Idempotent: the merged directory is rebuilt per package (missing files are
restored), so this also serves as the cache-restore path.

Usage: stage_runtime_libs.py <payload-dir>
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pm.downloader import Download, Source  # noqa: E402
from pm.package import DebPackage  # noqa: E402

PREFIX_REL = "data/data/com.termux/files/usr"


class _LibDeb(DebPackage):
    """Extraction-only view of a pinned runtime-lib .deb."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.deb_package = name

    def fetch_url(self, version: str, target: str) -> str:  # pragma: no cover
        raise NotImplementedError("pinned URLs live in runtime_libs.json")

    def verify(self, entry: Path, target: str) -> str:  # pragma: no cover
        return ""


def _extracted_sonames(work: Path, name: str) -> list[Path]:
    """The .so* files a package's extract dir holds (empty when the extract
    dir is absent -- cold cache)."""
    lib_dir = work / "extract" / name / PREFIX_REL / "lib"
    return list(lib_dir.glob("*.so*")) if lib_dir.is_dir() else []


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: stage_runtime_libs.py <payload-dir>", file=sys.stderr)
        return 2
    payload = Path(sys.argv[1]).resolve()
    payload.mkdir(parents=True, exist_ok=True)
    out = payload / "runtime-libs" / "lib"

    table = json.loads((HERE / "runtime_libs.json").read_text(encoding="utf-8"))["libs"]
    work = payload / ".work" / "runtime-libs"
    scratch = work / "dl"
    scratch.mkdir(parents=True, exist_ok=True)

    # The merged output dir IS the cache-restore surface (the CI workflow
    # caches payload/runtime-libs, not the scratch extract dirs), so the
    # skip check keys on it: a complete merged dir means every pinned .deb
    # already made it in, and the whole download+unpack pass is skipped.
    expected_sonames = {
        so.name
        for name in table
        for so in _extracted_sonames(work, name)
    }
    merged_now = {so.name for so in out.glob("*.so*")} if out.is_dir() else set()
    # Guard the empty>=empty cold-start case: with nothing staged there is
    # nothing to skip -- run the full download+unpack pass.
    if merged_now and merged_now >= expected_sonames:
        print(f"runtime libs already staged: {len(merged_now)} .so* -> {out}")
        return 0

    merged = 0
    for name, row in table.items():
        extract = work / "extract" / name
        if not any(extract.glob(f"{PREFIX_REL}/lib/*.so*")):
            archive = scratch / f"{name}.deb"
            Download([Source(row["url"], archive, row["sha256"])], partials_dir=scratch).run()
            if extract.exists():
                shutil.rmtree(extract)
            extract.mkdir(parents=True, exist_ok=True)
            _LibDeb(name).unpack(archive, extract, "linux-arm64-bionic")
        lib_dir = extract / PREFIX_REL / "lib"
        if not lib_dir.is_dir():
            print(f"  {name}: no {PREFIX_REL}/lib in the package", file=sys.stderr)
            return 1
        n = 0
        for so in sorted(lib_dir.glob("*.so*")):
            dest = out / so.name
            if dest.exists():
                continue  # co-installed packages share sonames; first wins
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(so, dest)
            n += 1
        merged += n
        print(f"  {name} {row['version']}: {n} new .so* -> runtime-libs/lib")
    if not any(out.glob("*.so*")):
        print("no shared objects staged", file=sys.stderr)
        return 1
    print(f"runtime libs staged: {len(table)} packages, {merged} new .so* -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
