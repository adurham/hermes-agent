"""The native bundle pipeline publishes only after a real staged sync succeeds."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.bundles import native


def test_bundle_stages_git_tree_and_runs_native_children_before_manifest(tmp_path, monkeypatch):
    import inspect

    from hermes_cli.runtime_paths import site_packages

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    output = tmp_path / "payload"
    canonical = tmp_path / "canonical"
    from pm.lock import Facts
    from pm.registry import get_package
    from pm.store import tree_digest
    lock, target = native._lockfile(), native.current_target()
    python_package = get_package("python")
    python_entry = python_package.store_entry(lock.version("python"), target)
    source_python = python_package.binary(canonical / python_entry, target)
    target_python = output / "tools" / source_python.relative_to(canonical)
    source_python.parent.mkdir(parents=True)
    # PM seals a payload-owned base interpreter, not an external venv launcher.
    # The POSIX host supplies its stdlib; Windows needs it beside the executable.
    if os.name == "nt":
        shutil.copytree(Path(sys.base_prefix), source_python.parent, dirs_exist_ok=True)
    else:
        shutil.copy2(Path(getattr(sys, "_base_executable")).resolve(), source_python)
    repo = tmp_path / "repo"
    repo.mkdir()
    pm_project = Path(__file__).resolve().parents[2] / "pm"
    (repo / "pm").mkdir()
    for name in ("pyproject.toml", "uv.lock", "lock.json"):
        shutil.copy2(pm_project / name, repo / "pm" / name)
    (repo / "pyproject.toml").write_text('[project]\nname="fixture"\nversion="1.0.0"\nrequires-python=">=3.11"\n[project.scripts]\nprobe="entry:main"\n[project.optional-dependencies]\npayloadtest=[]\n[tool.uv]\npackage=false\n', encoding="utf-8")
    from scripts.build.inputs import RESOURCE_ENV
    for name in RESOURCE_ENV:
        (repo / name).mkdir()
        (repo / name / "asset").write_text("required", encoding="utf-8")
    (repo / "entry.py").write_text("def main(): return 0\n", encoding="utf-8")
    uv = shutil.which("uv")
    assert uv, "native bundle test requires uv"
    uv_package = get_package("uv")
    uv_entry = canonical / uv_package.store_entry(lock.version("uv"), target)
    uv_entry.mkdir(parents=True)
    shutil.copy2(uv, uv_package.binary(uv_entry, target))
    facts = Facts(canonical / "facts.json")
    for name in ("python", "uv"):
        package = get_package(name)
        entry = canonical / package.store_entry(lock.version(name), target)
        facts.record(name, lock.version(name), entry.name, package.env(entry, target), canonical,
                     target=target, artifacts=[row["sha256"] for row in lock.artifacts(name, target)],
                     digest=tree_digest(entry))
    canonical_before = {name: tree_digest(canonical / facts.get(name)["entry"]) for name in ("python", "uv")}
    env = {**os.environ, "UV_OFFLINE": "1", "UV_PYTHON_DOWNLOADS": "never", "UV_CACHE_DIR": str(tmp_path / "cache")}
    subprocess.run([uv, "lock", "--python", sys.executable], cwd=repo, env=env, check=True, capture_output=True)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-m", "fixture"], cwd=repo, check=True, capture_output=True)
    monkeypatch.setattr("pm.paths.repo_root", lambda: repo)
    monkeypatch.setattr(native, "_bundle_package_names", lambda: ["uv"])
    monkeypatch.setattr("pm.prepare_tools", lambda names, **kwargs: kwargs["out"])
    monkeypatch.setattr("pm.client.is_runtime", lambda: True)
    monkeypatch.setattr("pm._uv._toolchain", lambda **kwargs: (Path(uv), Path(sys.executable)))

    monkeypatch.setattr("pm.extras.ANCHORS", {"payloadtest": "bundle_probe.present"})
    import pm
    real_build = pm.build_environment
    install_timeout = inspect.signature(real_build).parameters["timeout"].default
    calls = []
    witness = tmp_path / "inventory-python.json"
    fail_inventory = False

    def build(**kwargs):
        assert "uv" not in kwargs
        assert kwargs["sealed"] is True
        assert kwargs.get("timeout", install_timeout) > install_timeout
        calls.append(kwargs)
        assert not (output / "manifest.json").exists()
        marker = json.loads((output / "pm-runtime/pm-runtime.json").read_text())
        assert (output / "pm-runtime" / marker["python"]).resolve() == target_python
        assert (output / "pm-runtime" / marker["sitePackages"]).is_dir()
        result = real_build(**kwargs)
        site = site_packages(output / "venv")
        if fail_inventory:
            shutil.rmtree(site)
        else:
            package = site / "bundle_probe"
            package.mkdir()
            (package / "__init__.py").write_text(
                "import json, pathlib, sys\n"
                f"pathlib.Path({str(witness)!r}).write_text(json.dumps(sys.executable), encoding='utf-8')\n",
                encoding="utf-8",
            )
            (package / "present.py").write_text("", encoding="utf-8")
        return result

    monkeypatch.setattr(pm, "build_environment", build)
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "original"))
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "cache"))
    prepared = native.prepare_native(out=output, ref="HEAD", source=repo, cache=tmp_path / "cache", tools=canonical, env=env)
    assert {name: tree_digest(canonical / facts.get(name)["entry"]) for name in ("python", "uv")} == canonical_before
    assert not target_python.samefile(source_python)
    assert prepared == output.with_name(output.name + ".prepared.json")
    assert not prepared.is_relative_to(output)
    assert prepared.is_file()
    assert not (output / "manifest.json").exists()
    from scripts.bundles.native_prepared import load_prepared, preparation_lock
    with preparation_lock(output):
        with pytest.raises(ValueError, match="already in use"):
            native.finish_native(prepared, {})
    assert prepared.is_file()
    # Dependencies are already installed. Reject changed bytes rather than
    # healing them, including a substituted directory with identical bytes.
    inventory = json.loads(prepared.read_text())
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    assert inventory["revision"] == revision
    assert inventory["inputs"]["ref"] == "HEAD"
    changed_inputs = json.loads(prepared.read_text())
    changed_inputs["inputs"]["project"] = str(repo / "pyproject.toml")
    prepared.write_text(json.dumps(changed_inputs), encoding="utf-8")
    with pytest.raises(ValueError, match="run preparation again"):
        load_prepared(prepared)
    prepared.write_text(json.dumps(inventory), encoding="utf-8")
    for relative in ("hermes-agent/entry.py", "hermes-agent/uv.lock",
                     "venv/pyvenv.cfg", "pm-runtime/pm-runtime.json",
                     "enabled-features.json"):
        changed = output / relative
        original = changed.read_bytes()
        changed.write_bytes(original + b"\nchanged")
        with pytest.raises(ValueError, match="run preparation again"):
            load_prepared(prepared)
        changed.write_bytes(original)
    moved = output / "hermes-agent"
    outside = tmp_path / "substituted-source"
    moved.rename(outside)
    moved.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="run preparation again"):
        load_prepared(prepared)
    moved.unlink()
    outside.rename(moved)
    # A source entry linked outside the payload must never be admitted, even
    # when its link text itself is freshly covered by the stored digest.
    linked = moved / "escape"
    linked.symlink_to(witness)
    from scripts.bundles.native_prepared import _source_digest
    inventory["digests"]["hermes-agent"] = _source_digest(moved)
    prepared.write_text(json.dumps(inventory), encoding="utf-8")
    with pytest.raises(ValueError, match="run preparation again"):
        load_prepared(prepared)
    linked.unlink()
    inventory["digests"]["hermes-agent"] = _source_digest(moved)
    prepared.write_text(json.dumps(inventory), encoding="utf-8")
    frontend = tmp_path / "web-product"
    frontend.mkdir()
    (frontend / "index.html").write_text("built web", encoding="utf-8")
    with monkeypatch.context() as strict:
        def forbidden(*args, **kwargs):
            pytest.fail("strict finish entered dependency acquisition or a child process")
        strict.setattr(pm, "build_environment", forbidden)
        strict.setattr(pm, "stage_manager_runtime", forbidden)
        strict.setattr(subprocess, "run", forbidden)
        assert native.finish_native(prepared, {"web": frontend}) == 0
        (output / "hermes-agent/install-stamp.json").write_text('{"variant":"bundled"}', encoding="utf-8")
        (output / "manifest.json").write_text('{"variant":"bundled"}', encoding="utf-8")
        launcher = output / ("bin/probe.exe" if os.name == "nt" else "bin/probe")
        launcher.rename(launcher.with_name("renamed-launcher"))
        (frontend / "index.html").write_text("store web", encoding="utf-8")
        assert native.finish_native(prepared, {"web": frontend}) == 0
    assert (output / "hermes-agent/hermes_cli/web_dist/index.html").read_text() == "store web"
    assert calls[0]["all_extras"] is True
    assert calls[0]["cache"] == tmp_path / "cache"
    assert (output / "hermes-agent/pyproject.toml").is_file()
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["repo"] == "hermes-agent"
    command = "bin/probe.exe" if os.name == "nt" else "bin/probe"
    assert manifest["runtime"]["commands"] == {"probe": command}
    feature_file = output / "enabled-features.json"
    assert json.loads(feature_file.read_text(encoding="utf-8"))["extras"] == ["payloadtest"]
    assert Path(json.loads(witness.read_text(encoding="utf-8"))) == target_python
    assert os.environ["HERMES_RUNTIME_DIR"] == str(tmp_path / "original")

    before = feature_file.read_bytes()
    fail_inventory = True
    assert native._stage_native(SimpleNamespace(out=str(output), ref="HEAD")) == 1
    assert not (output / "manifest.json").exists()
    assert feature_file.read_bytes() == before
    assert os.environ["HERMES_RUNTIME_DIR"] == str(tmp_path / "original")

    from pm.package import InstallError
    def fail_build(**kwargs):
        raise InstallError("venv", "injected failure")
    monkeypatch.setattr(pm, "build_environment", fail_build)
    assert native._stage_native(SimpleNamespace(out=str(output), ref="HEAD")) == 1
    assert not (output / "manifest.json").exists()
    assert os.environ["HERMES_RUNTIME_DIR"] == str(tmp_path / "original")

    (repo / "pm/lock.json").write_text("{}", encoding="utf-8")
    subprocess.run(["git", "add", "pm/lock.json"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=f@example.test", "commit", "-m", "different pins"], cwd=repo, check=True, capture_output=True)
    monkeypatch.setattr("pm.prepare_tools", lambda names, **kwargs: pytest.fail("mismatched pins reached provisioning"))
    with pytest.raises(ValueError, match="PM lock differs"):
        native._stage_native(SimpleNamespace(out=str(output), ref="HEAD"))


@pytest.mark.parametrize("pointer, shard", [("revision.http", ""), ("revision.rev", "build-settings")])
def test_staged_cache_skips_build_inputs_before_copying(tmp_path, monkeypatch, pointer, shard):
    cache = tmp_path / "cache"
    revision = Path("sdists-v9/index/package/revision")
    wheels = revision / shard
    waste = {
        revision / "src/target/release/build.exe": b"build output",
        wheels / "cache_proof-1.0-py3-none-any.whl": b"redundant ZIP",
    }
    kept = {
        wheels / "metadata.msgpack": b"wheel metadata",
        revision.parent / pointer: b"revision pointer",
        wheels / "cache_proof-1.0-py3-none-any/src/template.whl": b"package data",
        Path("archive-v0/entry/cache_proof/src/__init__.py"): b"archive data",
        Path("other-bucket/src/keep.whl"): b"unrelated data",
    }
    for relative, data in {**waste, **kept}.items():
        path = cache / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    copyfile = shutil.copyfile

    def record_copy(source, destination, **kwargs):
        assert Path(source).relative_to(cache) not in waste, "build inputs must never be copied"
        return copyfile(source, destination, **kwargs)

    shipped = tmp_path / "payload/uv-cache"
    with monkeypatch.context() as patch:
        patch.setattr(shutil, "copyfile", record_copy)
        native.stage_uv_cache(cache, shipped)
    assert all(not (shipped / relative).exists() for relative in waste)
    assert all((shipped / relative).read_bytes() == data for relative, data in kept.items())
    assert all((cache / relative).read_bytes() == data for relative, data in {**waste, **kept}.items())


def test_staged_cache_rebuilds_venv_offline_without_build_sources_or_zips(tmp_path):
    from tests.pm._fixtures import _wheel

    uv = shutil.which("uv")
    assert uv, "native bundle test requires uv"
    package = tmp_path / "package"
    package.mkdir()
    (package / "pyproject.toml").write_text(
        '[project]\nname="cache-proof"\nversion="1.0.0"\n'
        '[build-system]\nrequires=["setuptools"]\nbuild-backend="setuptools.build_meta"\n',
        encoding="utf-8",
    )
    (package / "cache_proof.py").write_text("VALUE = 'installed from cached wheel'\n", encoding="utf-8")
    dist = tmp_path / "dist"
    dist.mkdir()
    archive = dist / "cache_proof-1.0.0.tar.gz"
    with tarfile.open(archive, "w:gz") as source:
        source.add(package, arcname="cache_proof-1.0.0")
    wheel = _wheel(dist, "wheel_proof")
    for name, artifact in (("cache-proof", archive), ("wheel-proof", wheel)):
        index = dist / "simple" / name
        index.mkdir(parents=True)
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        (index / "index.html").write_text(
            f'<a href="../../{artifact.name}#sha256={digest}">{artifact.name}</a>', encoding="utf-8",
        )
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        '[project]\nname="offline-proof"\nversion="1.0"\nrequires-python=">=3.11"\n'
        'dependencies=["cache-proof==1.0.0", "wheel-proof==1.0"]\n[tool.uv]\npackage=false\n',
        encoding="utf-8",
    )
    cache = tmp_path / "build-cache"
    env = {**os.environ, "UV_CACHE_DIR": str(cache), "UV_NO_CONFIG": "1", "UV_PYTHON_DOWNLOADS": "never"}
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(dist)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    index_url = f"http://127.0.0.1:{server.server_port}/simple"
    try:
        subprocess.run(
            [uv, "pip", "install", "--python", sys.executable, "--target", str(tmp_path / "first"),
             "--no-build-isolation", "--no-deps", "--index-url", index_url,
             "cache-proof==1.0.0", "wheel-proof==1.0"],
            env=env, cwd=tmp_path, capture_output=True, text=True, check=True, timeout=60,
        )
        locked = subprocess.run(
            [uv, "lock", "--python", sys.executable, "--index-url", index_url, "--no-build-isolation"],
            env=env, cwd=project, capture_output=True, text=True, timeout=60,
        )
        assert locked.returncode == 0, locked.stderr
        warmed = subprocess.run(
            [uv, "sync", "--python", sys.executable, "--frozen", "--index-url", index_url],
            env=env, cwd=project, capture_output=True, text=True, timeout=60,
        )
        assert warmed.returncode == 0, warmed.stderr
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    built_zips = list(cache.rglob("*.whl"))
    assert built_zips, "the actual uv build must create the redundant ZIP"
    sources = [path for bucket in cache.glob("sdists-v*") for path in bucket.rglob("src") if path.is_dir()]
    assert sources, "the actual uv build must leave its source tree in the cache"
    shipped = tmp_path / "payload/uv-cache"
    native.stage_uv_cache(cache, shipped)
    assert not list(shipped.rglob("*.whl"))
    assert all(not (shipped / path.relative_to(cache)).exists() for path in sources)
    assert all(path.exists() for path in [*built_zips, *sources]), "the build machine's cache must not change"

    # Nothing outside the shipped cache can satisfy this fresh mutable venv.
    for directory in (dist, package, cache, tmp_path / "first", project / ".venv"):
        shutil.rmtree(directory)
    result = subprocess.run(
        [uv, "sync", "--python", sys.executable, "--frozen", "--offline", "--index-url", index_url],
        env={**env, "UV_CACHE_DIR": str(shipped)}, cwd=project,
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "Building" not in result.stderr
    python = project / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    probe = subprocess.run(
        [str(python), "-I", "-c",
         "import cache_proof, wheel_proof; print(cache_proof.VALUE); print(wheel_proof.__version__)"],
        cwd=tmp_path, env=env, capture_output=True, text=True, check=True, timeout=30,
    )
    assert probe.stdout.splitlines() == ["installed from cached wheel", "1.0"]


def test_native_dispatch_reuses_pm_cache_offline(tmp_path, monkeypatch):
    import pm
    from pm.packages import uv_cache_dir
    from tests.pm._fixtures import _wheel

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "setup-pm"))
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "setup-pm/tools"))
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    monkeypatch.setattr("scripts.build.windows_deps.prepare_windows_environment", lambda **kwargs: dict(kwargs["env"]))
    uv = shutil.which("uv")
    assert uv, "native bundle test requires uv"
    monkeypatch.setattr("pm.client.is_runtime", lambda: True)
    monkeypatch.setattr("pm._uv._toolchain", lambda **kwargs: (Path(uv), Path(sys.executable)))
    cache = uv_cache_dir()
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    _wheel(wheels, "cache_probe", "1.0")
    wheel, = wheels.glob("*.whl")
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(wheels)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    requirement = f"cache-probe @ http://127.0.0.1:{server.server_port}/{wheel.name}"
    try:
        pm.build_requirements_environment(
            [requirement], out=tmp_path / "first", explicit=True,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    shutil.rmtree(wheels)
    pm.prune_cache(cache)
    original = dict(os.environ)
    run = subprocess.run
    homes = []

    def child(command, *, cwd, env):
        assert Path(env["UV_CACHE_DIR"]) == cache
        homes.append(Path(env["HOME"]))
        # The server and source wheel are gone. Only the cache restored for
        # PM can satisfy this install inside the payload's isolated HOME.
        return run([sys.executable, "-c",
                    "import os, subprocess, sys; from pathlib import Path; import pm, pm._uv; "
                    "pm.client.is_runtime = lambda: True; "
                    f"pm._uv._toolchain = lambda **kw: (Path({uv!r}), Path(sys.executable)); "
                    "python = pm.build_requirements_environment([sys.argv[1]], "
                    "out=Path(os.environ['HOME'])/'venv', "
                    "cache=Path(os.environ['UV_CACHE_DIR']), offline=True, explicit=True); "
                    "subprocess.run([str(python), '-I', '-c', 'import cache_probe'], check=True)",
                    requirement], cwd=cwd, env=env, check=True)

    monkeypatch.setattr(native.subprocess, "run", child)
    for name in ("first-payload", "second-payload"):
        assert native.stage_native(SimpleNamespace(out=tmp_path / name, ref="HEAD")) == 0
    assert all(not home.exists() for home in homes)
    assert dict(os.environ) == original


def test_native_dispatch_isolates_process_state_on_real_child_failure(tmp_path, monkeypatch):
    # Compiler provisioning has its own native test; this probe must stop
    # at the invalid revision without installing tools on a developer host.
    monkeypatch.setattr("scripts.build.windows_deps.prepare_windows_environment", lambda **kwargs: dict(kwargs["env"]))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "user-home"))
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "user-tools"))
    before = dict(os.environ)
    out = tmp_path / "output"
    assert native.stage_native(SimpleNamespace(out=str(out), ref="missing-build-test-ref", cache=tmp_path / "cache")) != 0
    assert dict(os.environ) == before
    assert not (tmp_path / "user-home").exists()
    assert not (tmp_path / "user-tools").exists()
    assert not (out / "manifest.json").exists()
    assert not list(out.glob(".build-*"))


def test_native_preparation_refuses_symlinked_output_before_writing(tmp_path, monkeypatch):
    monkeypatch.setattr("pm.prepare_tools", lambda *args, **kwargs: pytest.fail("unsafe output reached acquisition"))
    output = tmp_path / "payload"
    output.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("untouched", encoding="utf-8")
    (output / "hermes-agent").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink|escaped"):
        native.prepare_native(out=output, ref="HEAD", source=Path(__file__).resolve().parents[2],
                              cache=tmp_path / "cache")
    assert (outside / "keep").read_text() == "untouched"
    assert not (output / "native-prepared.json").exists()


def test_native_dispatch_keeps_cache_across_failed_children(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.build.windows_deps.prepare_windows_environment", lambda **kwargs: dict(kwargs["env"]))
    out = tmp_path / "payload"
    cache = tmp_path / "persistent-cache"
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "ambient-cache"))
    original = dict(os.environ)
    run = subprocess.run
    attempts = []

    def child(command, *, cwd, env):
        assert command[command.index("-m") + 1] == "scripts.bundles.native"
        assert Path(env["UV_CACHE_DIR"]) == cache
        attempts.append(Path(env["HOME"]))
        # Run a real child using the actual dispatch environment. Its cache
        # write must survive the failing process and temporary-HOME cleanup.
        return run([sys.executable, "-c",
                    "import os,sys; from pathlib import Path; "
                    "p=Path(os.environ['UV_CACHE_DIR']); p.mkdir(exist_ok=True); "
                    "f=p/'reused'; f.write_text(f.read_text()+'x' if f.exists() else 'x'); sys.exit(17)"],
                   cwd=cwd, env=env)

    monkeypatch.setattr(native.subprocess, "run", child)
    for _ in range(2):
        assert native.stage_native(SimpleNamespace(out=out, ref="HEAD", cache=cache)) == 17
    assert (cache / "reused").read_text() == "xx"
    assert all(not home.exists() for home in attempts)
    assert not (tmp_path / "ambient-cache").exists()
    assert dict(os.environ) == original


@pytest.mark.parametrize("explicit", [False, True])
def test_native_dispatch_preserves_compiler_homes_inside_isolated_home(tmp_path, monkeypatch, explicit):
    host_home = tmp_path / "host"
    monkeypatch.setattr(Path, "home", lambda: host_home)
    monkeypatch.setattr("scripts.build.windows_deps.prepare_windows_environment", lambda **kwargs: dict(kwargs["env"]))
    homes = {}
    for key, name in (("CARGO_HOME", ".cargo"), ("RUSTUP_HOME", ".rustup")):
        directory = (tmp_path / "custom" if explicit else host_home) / name
        directory.mkdir(parents=True)
        (directory / "fixture-state").write_text(key, encoding="utf-8")
        homes[key] = str(directory)
        if explicit:
            monkeypatch.setenv(key, str(directory))
        else:
            monkeypatch.delenv(key, raising=False)
    before = dict(os.environ)
    run = subprocess.run

    def child(command, *, cwd, env):
        assert env["HOME"] != str(host_home)
        assert env["USERPROFILE"] == env["HOME"]
        assert {key: env.get(key) for key in homes} == homes
        return run([sys.executable, "-c",
                    "import os; from pathlib import Path; "
                    "assert all((Path(os.environ[k]) / 'fixture-state').read_text() == k "
                    "for k in ('CARGO_HOME', 'RUSTUP_HOME'))"],
                   cwd=cwd, env=env, check=True)

    monkeypatch.setattr(native.subprocess, "run", child)
    assert native.stage_native(SimpleNamespace(out=tmp_path / "out", ref="HEAD")) == 0
    assert dict(os.environ) == before
