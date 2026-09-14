"""Native payload staging through PM's existing package authority."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import argparse
from dataclasses import asdict
from pathlib import Path

from pm.ensure import _lockfile
from pm.lock import Facts
from pm.registry import get_package, walk
from pm.store import current_target

def _bundle_package_names() -> list[str]:
    names = [
        n
        for n in _lockfile().names()
        if not get_package(n).internal or n == "uv"
    ]
    if "python" not in names:
        names.append("python")
    return names


def _arch_guard(store_dir: Path) -> list[str]:
    """Every staged binary must be built for this machine's target — a
    payload staged with a mismatched interpreter or PATH tool ships an
    artifact that cannot run. Reads facts, probes each entry binary."""
    from pm.package import machine_matches_binary

    facts = Facts(store_dir / "facts.json")
    problems = []
    target = current_target()
    for name in _lockfile().names():
        package = get_package(name)
        fact = facts.get(name)
        if fact is None or "entry" not in fact:
            continue
        binary = package.binary(store_dir / fact["entry"], target)
        if binary is None or not binary.is_file():
            continue
        verdict = machine_matches_binary(binary, target)
        # A package that declares this target as emulated (x64 binary run
        # under Windows ARM64 built-in emulation) is fine with the x64 PE.
        if verdict is False and target not in package.emulated_arch_targets:
            problems.append(f"{name}: {binary.name} is not a {target} binary")
    return problems



def stage_uv_cache(source: Path, destination: Path) -> None:
    """Keep offline wheel/index entries, without sdist build inputs.

    uv installs built wheels from extracted entries; their ZIPs and source
    trees (including Rust target/ outputs) are build-only. Scope exclusions
    to cache metadata boundaries so extracted package data stays intact.
    """
    def ignore(directory: str, names: list[str]) -> set[str]:
        path = Path(directory)
        parts = path.relative_to(source).parts
        if not parts or not parts[0].startswith("sdists-v"):
            return set()
        omitted = set()
        # Source trees live under a revision selected by a sibling pointer.
        if "src" in names and (path / "src").is_dir() and any(
            (path.parent / pointer).is_file() for pointer in ("revision.http", "revision.rev")
        ):
            omitted.add("src")
        # Build settings can put wheel entries in a shard below the revision.
        # The signer can reach extracted code, but not native code inside ZIPs.
        if "metadata.msgpack" in names:
            omitted.update(name for name in names if name.endswith(".whl") and (path / name).is_file())
        return omitted

    shutil.copytree(source, destination, ignore=ignore)


def _cache_index_dirs(cache: Path, family: str) -> list[Path]:
    """Index roots under a cache family (wheels-v*/sdists-v*), whatever the
    index name: PyPI materializes as `pypi`, a custom --index-url as a hashed
    dir under `index`. Depth is fixed, the leaf dir name is not."""
    roots: list[Path] = []
    for family_dir in sorted(cache.glob(f"{family}-v*")):
        for index_dir in family_dir.iterdir():
            if index_dir.name in ("index",) or index_dir.name == "pypi":
                roots.extend(d for d in index_dir.iterdir() if d.is_dir())
    return roots


def _lock_wheel_names(source_repo: Path) -> dict[str, set[str]]:
    """Map lock package name → wheel filenames uv downloads for it."""
    import tomllib

    lock = tomllib.loads((source_repo / "uv.lock").read_text(encoding="utf-8-sig"))
    packages: dict[str, set[str]] = {}
    for entry in lock.get("package", []):
        names = set()
        for wheel in entry.get("wheels") or []:
            url = wheel.get("url") if isinstance(wheel, dict) else wheel
            if url:
                names.add(url.rsplit("/", 1)[-1])
        packages[entry["name"]] = names
    return packages


def prune_uv_cache_to_built(cache: Path, source_repo: Path) -> dict[str, list[str]]:
    """Slim a staged uv cache to the packages that had no downloadable wheel.

    A venv rebuild from the bundle may go online — PyPI re-serving released
    wheels is fine — but it must never invoke a compiler for the dependency
    graph's built-ins: packages whose wheels uv had to build on the build
    machine (no cp314/platform wheel on the index) would need a toolchain
    the user's machine may not have. Those packages' cache entries — sdist
    records, built wheel zips, and extracted archive buckets — are the
    infra-free guarantee; everything else is re-downloadable bytes.

    Returns the kept package names (for the build log).
    """
    lock_wheels = _lock_wheel_names(source_repo)

    built = {name for name, wheels in lock_wheels.items() if not wheels}
    sdist_dirs = _cache_index_dirs(cache, "sdists")
    wheels_dirs = _cache_index_dirs(cache, "wheels")
    # uv records every source distribution it fetched (and compiled).
    built |= {entry.name for directory in sdist_dirs for entry in directory.iterdir() if entry.is_dir()}
    for pkg_dir in (entry for directory in wheels_dirs for entry in directory.iterdir() if entry.is_dir()):
        # A cached wheel uv did not download from the index is one it
        # built itself (platform gap, e.g. no win cp314 wheel).
        if any(whl.name not in lock_wheels.get(pkg_dir.name, set())
               for whl in pkg_dir.rglob("*.whl")):
            built.add(pkg_dir.name)
    built.discard("hermes-agent")  # installed from the payload's own tree

    keep_sdist = built & {e.name for directory in sdist_dirs for e in directory.iterdir()}
    keep_wheels = built & {e.name for directory in wheels_dirs for e in directory.iterdir()}

    # archive-v0 buckets are content-addressed: keep the ones holding any
    # built package (matched by its .dist-info directory).
    keep_buckets: set[str] = set()
    archive_root = cache / "archive-v0"
    if archive_root.is_dir():
        dist_info = re.compile(r"([a-zA-Z0-9_.]+?)-\d[^-]*\.dist-info")
        for bucket in archive_root.iterdir():
            if not bucket.is_dir():
                continue
            for dirpath, dirnames, _filenames in os.walk(bucket):
                if Path(dirpath).relative_to(bucket).parts.__len__() > 3:
                    dirnames[:] = []
                    continue
                for name in list(dirnames):
                    match = dist_info.match(name)
                    if match:
                        if match.group(1).lower().replace("_", "-") in built:
                            keep_buckets.add(bucket.name)
                        dirnames.remove(name)

    for directory, keep in (
        *[(d, keep_sdist) for d in sdist_dirs],
        *[(d, keep_wheels) for d in wheels_dirs],
    ):
        for entry in directory.iterdir():
            if entry.name not in keep and entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
    if archive_root.is_dir():
        for bucket in archive_root.iterdir():
            if bucket.name not in keep_buckets and bucket.is_dir():
                shutil.rmtree(bucket, ignore_errors=True)
    return {"sdists": sorted(keep_sdist), "wheels": sorted(keep_wheels), "buckets": sorted(keep_buckets)}


def stage_pm_runtime(root: Path, python: Path, repo: Path, *, offline: bool = False,
                     cache: Path | None = None) -> None:
    """Publish the same PM dependency graph as source installs, ready offline."""
    from pm import stage_manager_runtime
    from scripts.bundles.payload import seal_pm_runtime

    destination = root / "pm-runtime"
    if destination.exists():
        shutil.rmtree(destination)
    stage_manager_runtime(python=python, destination=destination, project=repo / "pm", offline=offline, cache=cache)
    seal_pm_runtime(root, python)


def stage_native(args) -> int:
    """Isolate HOME and PM state, but retain the provider's reusable build cache."""
    from pm.packages import uv_cache_dir

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").unlink(missing_ok=True)
    root = Path(__file__).resolve().parents[2]
    # Resolve before HOME isolation so the build warms the cache CI saves.
    cache = Path(getattr(args, "cache", None) or os.environ.get("UV_CACHE_DIR") or uv_cache_dir()).resolve()
    base_env = dict(os.environ)
    if current_target() == "win32-arm64":
        from scripts.build.windows_deps import prepare_windows_environment

        base_env = prepare_windows_environment(source=root, state=out.parent / ".build-deps", env=base_env)
    # Rustup resolves its installed toolchain under HOME unless these are explicit.
    # Preserve the build host's compiler and Cargo cache before isolating app state.
    for key, directory in (("CARGO_HOME", ".cargo"), ("RUSTUP_HOME", ".rustup")):
        base_env.setdefault(key, str(Path.home() / directory))
    with tempfile.TemporaryDirectory(prefix=".build-", dir=out) as work:
        env = {**base_env, "HOME": work, "USERPROFILE": work,
               "HERMES_HOME": str(Path(work) / ".hermes"),
               "HERMES_RUNTIME_DIR": str(out / "tools"),
               "HERMES_PYTHON_SRC_ROOT": str(root),
               "XDG_CACHE_HOME": str(Path(work) / "cache"),
               "XDG_CONFIG_HOME": str(Path(work) / "config"),
               "UV_CACHE_DIR": str(cache),
               "PYTHONPATH": os.pathsep.join([str(root), *filter(None, sys.path)])}
        command = [sys.executable, "-B", "-m", "scripts.bundles.native", "--out", str(out),
                   "--ref", args.ref or "HEAD", "--source", str(root)]
        for name, product in getattr(args, "frontends", {}).items():
            command += [f"--{name}", str(product)]
        return subprocess.run(command, cwd=root, env=env).returncode


def prune_staged_store(store_dir: Path, names: list[str]) -> None:
    """Prune this build's store, never the ambient user's tool store."""
    facts = Facts(store_dir / "facts.json", strict=True)
    facts.retain({package.name for package in walk(names)})
    keep = facts.entries_in_use()
    for entry in store_dir.iterdir():
        if entry.is_dir() and not entry.name.startswith(".") and entry.name not in keep:
            shutil.rmtree(entry)


def prepare_native(*, out: Path, ref: str, source: Path, cache: Path,
                   tools: Path | None = None, env: dict | None = None) -> Path:
    """Prepare final payload dependencies inside the caller's isolated PM process.

    The caller supplies its compiler environment; only the full standalone
    wrapper provisions machine prerequisites and isolates HOME.
    """
    from scripts.bundles.native_prepared import preparation_lock, prepared_path

    out = Path(out).absolute()
    if out != out.resolve():
        raise ValueError("symlinked native output")
    out.mkdir(parents=True, exist_ok=True)
    with preparation_lock(out):
        prepared_path(out).unlink(missing_ok=True)
        (out / "manifest.json").unlink(missing_ok=True)
        return _prepare_native(out=out, ref=ref, source=Path(source).resolve(),
                               cache=Path(cache).resolve(), tools=tools, env=env)


def _prepare_native(*, out: Path, ref: str, source: Path, cache: Path,
                    tools: Path | None, env: dict | None) -> Path:
    from pm import paths
    from pm.package import InstallError

    store_dir = out / "tools"
    store_dir.mkdir(parents=True, exist_ok=True)
    repo_dir = out / "hermes-agent"
    from scripts.bundles.payload import snapshot
    print(f"staging repo snapshot ({ref})…", flush=True)
    revision = subprocess.check_output(
        ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=source,
        text=True, encoding="utf-8").strip()
    snapshot(source, revision, repo_dir)
    # PM's provider code reads its adjacent lock. Never combine that tool graph
    # with a revision selecting different pins.
    if (repo_dir / "pm/lock.json").read_bytes() != paths.lockfile_path().read_bytes():
        raise ValueError("selected revision's PM lock differs from the builder; use a checkout at that revision")

    names = [
        n for n in _bundle_package_names()
        if get_package(n).missing_reason(current_target()) is None
    ]
    from pm import prepare_tools, stage_tools

    prepare_tools(names, out=Path(tools) if tools is not None else store_dir,
                  target=current_target(), cache=cache)
    if tools is not None:
        stage_tools(names, source_store=Path(tools), out=store_dir, target=current_target())

    # Prune the staged store BEFORE the venv sync and packaging: drop the
    # fetch-<sha> download-cache archives (needed only at install time — dead
    # weight in the shipped payload AND in the CI cache that restores this
    # dir) and any orphaned package versions left over from an older lock
    # the cache carried in. A lean staged store = a lean CI cache.

    # Only this build's store is ours to prune; machine-wide partials are not.
    # Cached facts may still name packages removed from the current selection.
    # Retain the dependency closure before using facts as the deletion roots.
    prune_staged_store(store_dir, names)


    facts = Facts(store_dir / "facts.json")
    python_fact = facts.get("python")
    if python_fact is None:
        raise InstallError("venv", "no staged interpreter to build on")
    python_bin = get_package("python").binary(
        store_dir / python_fact["entry"], current_target()
    )

    if python_bin is None:
        raise FileNotFoundError("staged Python executable is missing")
    stage_pm_runtime(out, python_bin, repo_dir, cache=cache)
    print("✓ pm-runtime (independent locked dependencies)", flush=True)

    # Build + sync INSIDE the staged repo: the editable project install
    # must point at the payload's own tree, not this checkout.
    venv_dir = out / "venv"
    if venv_dir.exists():
        shutil.rmtree(venv_dir)
    env = dict(os.environ if env is None else env)
    from pm import build_environment

    # Cold native wheels need a larger budget than interactive installs.
    build_environment(source=repo_dir, python=python_bin, out=venv_dir,
                      env=env, cache=cache, all_extras=True, sealed=True, explicit=True,
                      timeout=2 * 60 * 60)
    print("✓ venv (all extras, on the staged interpreter)")

    # Inventory the staged interpreter before publishing the bundle contract.
    from pm.features import installed_extras, write_features

    features = installed_extras(repo_dir, venv_dir, python_exe=python_bin)
    write_features(features, out)
    print(f"✓ enabled-features.json ({len(features)} extras recorded)")

    # Ship a slim uv cache: the venv sync on the build machine warms the
    # hermes-owned cache with every wheel this payload needs. A mutable-venv
    # rebuild from the bundle re-downloads released wheels online (seconds)
    # and reuses the shipped built-wheels for the packages that had none —
    # no compiler, no Xcode CLT, no Rust toolchain on the user machine.
    payload_cache = out / "uv-cache"
    if payload_cache.exists():
        shutil.rmtree(payload_cache, ignore_errors=True)
    src_cache = cache
    if src_cache.is_dir():
        print(f"  uv-cache: copying {src_cache} → payload...", flush=True)
        stage_uv_cache(src_cache, payload_cache)
        # Ship only what a rebuild cannot re-acquire online: the wheels uv
        # built itself for packages with no downloadable wheel. Rebuilds go
        # online for released wheels (seconds) but must never need a
        # compiler; the full 2GB cache buys offline rebuilds the bundle
        # contract does not promise.
        kept = prune_uv_cache_to_built(payload_cache, repo_dir)
        print(f"✓ uv-cache (slim — built-only: {len(kept['sdists'])} sdist records, "
              f"{len(kept['wheels'])} built wheels, {len(kept['buckets'])} archive buckets)", flush=True)
    else:
        raise InstallError("uv-cache", "runtime dependency cache is missing")

    bad = _arch_guard(store_dir)
    for line in bad:
        print(f"✗ arch: {line}")
    if bad:
        raise InstallError("tools", "native architecture verification failed")
    from scripts.bundles.payload import record_tools
    recorded = {name: fact["entry"] for name in names if (fact := facts.get(name)) and "entry" in fact}
    record_tools(out, paths.lockfile_path(), current_target(), recorded)
    from scripts.build.inputs import AgentInputs, RESOURCE_ENV, dependency_site
    from scripts.bundles.native_prepared import publish_prepared

    site = dependency_site(venv_dir, python_fact["version"], current_target())
    (site / "hermes-agent.pth").write_text(
        Path(os.path.relpath(repo_dir, site)).as_posix() + "\n", encoding="utf-8")
    from scripts.bundles.payload import relativize_links
    relativize_links(out)
    inputs = AgentInputs(
        project=repo_dir / "pyproject.toml", code=repo_dir, repo="hermes-agent",
        placement="contained", target=current_target(), python=python_bin,
        site_packages=site, environment=venv_dir,
        tools=store_dir, pm_runtime=out / "pm-runtime", ref=ref,
        resources={name: repo_dir / name for name in RESOURCE_ENV},
        features=out / "enabled-features.json",
    )
    return publish_prepared(out, source, revision, inputs)


def finish_native(prepared: Path, frontends: dict[str, Path]) -> int:
    """Consume verified job-local paths. No dependency acquisition or repair."""
    from scripts.build.agent import assemble
    from scripts.build.inputs import AgentInputs
    from scripts.bundles.native_prepared import load_prepared, preparation_lock

    prepared = Path(prepared).absolute()
    if not prepared.name.endswith(".prepared.json") or not prepared.is_file():
        raise ValueError("native preparation is missing or invalid; run preparation again")
    out = prepared.with_name(prepared.name.removesuffix(".prepared.json"))
    with preparation_lock(out):
        (out / "manifest.json").unlink(missing_ok=True)
        inputs = load_prepared(prepared)
        values = asdict(inputs)
        values["frontends"] = {name: Path(path).absolute() for name, path in frontends.items()}
        assemble(AgentInputs.from_dict(values), out)
    print(f"✓ manifest ({out / 'manifest.json'})")
    return 0


def _stage_native(args) -> int:
    from pm import paths
    from pm.features import FeatureProbeError
    from pm.package import InstallError

    try:
        prepared = prepare_native(
            out=Path(args.out), ref=args.ref or "HEAD",
            source=getattr(args, "source", None) or paths.repo_root(),
            cache=Path(getattr(args, "cache", None) or os.environ["UV_CACHE_DIR"]),
            tools=getattr(args, "tools", None),
        )
        return finish_native(prepared, getattr(args, "frontends", {}))
    except (InstallError, FeatureProbeError) as exc:
        print(f"✗ {exc}")
        return 1




def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--tui", type=Path)
    parser.add_argument("--web", type=Path)
    args = parser.parse_args()
    args.frontends = {name: path for name in ("tui", "web") if (path := getattr(args, name)) is not None}
    return _stage_native(args)


if __name__ == "__main__":
    raise SystemExit(main())
