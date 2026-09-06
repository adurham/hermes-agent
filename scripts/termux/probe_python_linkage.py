#!/usr/bin/env python3
"""Diagnose the actual anydoc wheel against the pinned bionic interpreter.

This standalone CI diagnostic builds the locked sdist, inspects the ELF
linkage, and tests the same extension with an explicit libpython dependency.
It does not change any package source or publish release artifacts.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import tomllib
import zipfile


def run(argv: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    print("+", " ".join(argv), flush=True)
    return subprocess.run(argv, check=True, env=env)


def import_anydoc(python: Path, site: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(python), "-c", "import anydoc; print('ANYDOC_IMPORT_OK', anydoc.__file__)"],
        env={**env, "PYTHONPATH": str(site)},
        capture_output=True,
        text=True,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", type=Path, required=True)
    ap.add_argument("--payload", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--built-wheel", type=Path)
    args = ap.parse_args()
    prefix = Path("/data/data/com.termux/files/usr")
    python = args.payload / "python" / prefix.relative_to("/") / "bin/python3.11"
    uv = args.payload / "uv" / prefix.relative_to("/") / "bin/uv"
    pylib = python.parent.parent / "lib"
    runtime_libs = args.payload / "runtime-libs/lib"
    env = {**os.environ, "LD_LIBRARY_PATH": f"{pylib}:{runtime_libs}:{prefix}/lib"}
    args.output.mkdir(parents=True, exist_ok=True)
    lock = tomllib.loads((args.repo / "uv.lock").read_text(encoding="utf-8"))
    package = next(p for p in lock["package"] if p["name"] == "firecrawl-anydoc")
    requirement = f"firecrawl-anydoc=={package['version']}"
    with tempfile.TemporaryDirectory(prefix="hermes-linkage-", dir=prefix / "tmp") as tmp:
        venv = Path(tmp) / "venv"
        run([str(uv), "venv", "--python", str(python), str(venv)], env=env)
        vp = venv / "bin/python"
        run([str(uv), "pip", "install", "--python", str(vp), "pip", "packaging"], env=env)
        build_env = {
            **env,
            "CARGO_BUILD_JOBS": "1",
            "MAKEFLAGS": "-j1",
            "ANDROID_API_LEVEL": "24",
            "RUSTFLAGS": f"-L{pylib}",
            "LDFLAGS": f"-L{pylib}",
        }
        if args.built_wheel:
            shutil.copy2(args.built_wheel, args.output / args.built_wheel.name)
        else:
            run([
                str(vp), "-m", "pip", "wheel", "--no-deps", "--no-cache-dir",
                "--no-binary", ":all:", "-w", str(args.output), requirement,
            ], env=build_env)
        wheels = sorted(args.output.glob("firecrawl_anydoc-*.whl"))
        if len(wheels) != 1:
            raise RuntimeError(f"expected one anydoc wheel, found {wheels}")
        site = Path(tmp) / "site"
        with zipfile.ZipFile(wheels[0]) as wheel:
            wheel.extractall(site)
        extension = next(site.glob("anydoc/_anydoc*.so"))
        library = next(pylib.glob("libpython3.11.so.*"))
        readelf = shutil.which("llvm-readelf") or shutil.which("readelf")
        if not readelf:
            raise RuntimeError("builder has no ELF inspection tool")
        run([readelf, "-d", str(extension)])
        symbols = subprocess.run([readelf, "--dyn-syms", "--wide", str(library)], check=True, capture_output=True, text=True)
        print("libpython export:", *[line for line in symbols.stdout.splitlines() if "_Py_NoneStruct" in line], sep="\n")
        baseline = import_anydoc(vp, site, env)
        print("BASELINE_EXIT", baseline.returncode, flush=True)
        print(baseline.stdout, baseline.stderr, flush=True)
        soname = subprocess.run(["patchelf", "--print-soname", str(library)], check=True, capture_output=True, text=True).stdout.strip()
        needed = subprocess.run(["patchelf", "--print-needed", str(extension)], check=True, capture_output=True, text=True).stdout.splitlines()
        from python_linkage import repair_wheel

        repaired = repair_wheel(wheels[0], library)
        if repaired == 0:
            raise RuntimeError("production wheel repair did not change the failing extension")
        shutil.rmtree(site)
        with zipfile.ZipFile(wheels[0]) as wheel:
            wheel.extractall(site)
        explicit = import_anydoc(vp, site, env)
        print("EXPLICIT_LINK_EXIT", explicit.returncode, flush=True)
        print(explicit.stdout, explicit.stderr, flush=True)
        evidence = {
            "requirement": requirement,
            "wheel": wheels[0].name,
            "libpython_soname": soname,
            "original_needed": needed,
            "baseline_exit": baseline.returncode,
            "baseline_error": baseline.stderr,
            "explicit_link_exit": explicit.returncode,
            "explicit_link_error": explicit.stderr,
        }
        (args.output / "linkage-evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        if baseline.returncode == 0:
            raise RuntimeError("baseline did not reproduce the native import failure")
        if "_Py_NoneStruct" not in baseline.stderr:
            raise RuntimeError("baseline failed for a different reason")
        if explicit.returncode:
            raise RuntimeError("explicit libpython dependency did not fix the import")
        print("LINKAGE_CAUSE_PROVEN", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
