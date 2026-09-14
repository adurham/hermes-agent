"""Execute protected canary workflow/controller with local Git and HTTP only.

The packages and codesign/GitHub executables are transport fixtures, not native
qualification. No admission, metadata recorder or channel publisher is mocked.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import plistlib
import shlex
import subprocess
import sys
import zipfile

import hermes_yaml
import pytest

from hermes_cli.release_channels import ChannelReader
from scripts.releases import channel_releases, handoff
from tests.ci.test_desktop_release_tag_admission import _git, _seed_repo
from tests.scripts.test_release_r2 import r2_server  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.platforms("posix")


def workflow_job(name):
    return hermes_yaml.safe_load((ROOT / ".github/workflows/desktop-bundled-release.yml").read_text())["jobs"][name]


def step_script(job, name):
    return next(step["run"] for step in workflow_job(job)["steps"] if step.get("name") == name)


@pytest.fixture
def canary(tmp_path, r2_server, monkeypatch):
    _, clone = _seed_repo(tmp_path)
    commit = _git("rev-parse", "HEAD", cwd=clone)
    tag = "v0.1.3-canary.20260913001000"
    _git("tag", tag, cwd=clone)
    _git("push", "origin", tag, cwd=clone)
    desktop = clone / "apps/desktop"
    desktop.mkdir(parents=True)
    (desktop / "product-identity.cjs").symlink_to(ROOT / "apps/desktop/product-identity.cjs")
    monkeypatch.chdir(clone)
    identity = channel_releases.product_identity(tag)
    tools = tmp_path / "tools"
    tools.mkdir()
    driver = tools / "driver.py"
    driver.write_text(
        "import runpy,sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from scripts.releases import r2\n"
        f"r2.s3_endpoint=lambda _: 'http://127.0.0.1:{r2_server.server_port}'\n"
        "args=sys.argv[1:]\nsys.argv=args[1:]\n"
        "assert args[0]=='-m', args\nrunpy.run_module(args[1],run_name='__main__')\n"
    )
    python = tools / "python"
    python.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(driver))} \"$@\"\n")
    python.chmod(0o755)
    release_state = tmp_path / "github-release.json"
    release_state.write_text(json.dumps({"tagName": tag, "isDraft": False, "isPrerelease": True}))
    gh = tools / "gh"
    gh.write_text(
        f"#!{sys.executable}\nimport json,os,sys\nfrom pathlib import Path\n"
        "args=sys.argv[1:]\nstate=Path(os.environ['FIXTURE_RELEASE'])\n"
        "if args[:1]==['api']: print('main')\n"
        "elif args[:2]==['release','view']: print(state.read_text())\n"
        "elif args[:2]==['release','edit']:\n"
        " data=json.loads(state.read_text());data['isDraft']=False;state.write_text(json.dumps(data))\n"
        "else: raise SystemExit('unexpected gh call: '+repr(args))\n"
    )
    gh.chmod(0o755)
    # Only the signing command is a fixture boundary; real macOS CI owns its proof.
    codesign = tools / "codesign"
    codesign.write_text('#!/bin/sh\nprintf "TeamIdentifier=ABCDEFGHIJ\\n" >&2\n')
    codesign.chmod(0o755)
    base = f"http://127.0.0.1:{r2_server.server_port}/hermes-releases"
    env = {**os.environ, "PATH": str(tools) + os.pathsep + os.environ["PATH"],
           "FIXTURE_RELEASE": str(release_state), "GITHUB_ACTIONS": "true",
           "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REPOSITORY": "NousResearch/hermes-agent",
           "GITHUB_WORKFLOW_REF": "NousResearch/hermes-agent/.github/workflows/desktop-bundled-release.yml@refs/heads/main",
           "RELEASE_TAG": tag, "TAG": tag, "HERMES_PAYLOAD_TAG": tag,
           "RELEASE_COMMIT": commit, "RELEASE_PHASE": "", "HERMES_DESKTOP_VARIANT": "bundled",
           "HERMES_BUILD_COMMIT": "", "CHANNEL_BUILD": "", "R2_DISPOSABLE_RUN": "",
           "CLOUDFLARE_R2_PUBLIC_URL": base,
           "RELEASE_NEEDS": json.dumps({name: {"result": "success"} for name in workflow_job("publish-canary")["needs"]})}

    def run(script, **overrides):
        return subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script], cwd=clone,
                              env={**env, **overrides}, capture_output=True, text=True, timeout=60)

    return clone, identity, env, run


def native_leg(root, identity, env, platform, arch):
    root.mkdir(parents=True, exist_ok=True)
    version = env["RELEASE_TAG"][1:]
    stamp = {"tag": env["RELEASE_TAG"], "commit": env["RELEASE_COMMIT"]}
    artifact = identity["artifactNamePascal"]
    if platform == "win32":
        with zipfile.ZipFile(root / f"{artifact}-{version}-win-{arch}.msix", "w") as package:
            package.writestr("AppxManifest.xml", f'<Package><Identity Name="{identity["msixAppIdWithOrg"]}" Publisher="CN=Fixture" Version="0.1.3.10" ProcessorArchitecture="{arch}"/><Applications><Application Id="{identity["appNamePascal"]}"/><Application Id="CLI"><VisualElements AppListEntry="none"/></Application></Applications></Package>')
            package.writestr("app/resources/install-stamp.json", json.dumps(stamp))
    else:
        name = f"{artifact}-{version}-mac-{arch}"
        (root / f"mac-{arch}/{artifact}.app").mkdir(parents=True)
        with zipfile.ZipFile(root / f"{name}.zip", "w") as package:
            package.writestr(f"{artifact}.app/Contents/Info.plist", plistlib.dumps({"CFBundleIdentifier": identity["appId"], "CFBundleShortVersionString": version}))
            package.writestr(f"{artifact}.app/Contents/Resources/install-stamp.json", json.dumps(stamp))
        for suffix in ("dmg", "dmg.blockmap", "zip.blockmap"):
            (root / f"{name}.{suffix}").write_bytes(b"unsigned transport fixture")


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_canary_metadata_is_recorded_and_receipt_bound(canary, r2_server, platform):
    clone, identity, env, run = canary
    root = clone / "apps/desktop/release"
    native_leg(root, identity, env, platform, "x64")
    name = "Stage Windows packages to R2" if platform == "win32" else "Stage macOS packages and feed inputs to R2"
    script = step_script(f"build-{platform}-release", name)
    result = run(script, TARGET=f"{platform}-x64")
    assert result.returncode == 0, result.stdout + result.stderr
    prefix = f"releases/tag/{env['RELEASE_TAG']}/"
    receipt = json.loads(r2_server.store[prefix + handoff.receipt_name(f"{platform}-x64")][0])
    metadata_name = f"metadata-{'windows' if platform == 'win32' else 'macos'}-x64.json"
    entry = next(row for row in receipt["files"] if row["path"] == metadata_name)
    body = r2_server.store[prefix + metadata_name][0]
    metadata = json.loads(body)
    assert entry["sha256"] == hashlib.sha256(body).hexdigest()
    assert metadata["commit"] == env["RELEASE_COMMIT"] and metadata["tag"] == env["RELEASE_TAG"]
    assert metadata["version"] == ("0.1.3.10" if platform == "win32" else env["RELEASE_TAG"][1:])


def test_published_canary_workflow_advances_only_after_every_gate(canary, r2_server):
    clone, identity, env, run = canary
    root = clone / "apps/desktop/release"
    # Stage independent per-architecture job workspaces through the actual shell.
    import shutil
    for platform in ("darwin", "win32"):
        for arch in ("arm64", "x64"):
            if root.exists():
                shutil.rmtree(root)
            native_leg(root, identity, env, platform, arch)
            name = "Stage Windows packages to R2" if platform == "win32" else "Stage macOS packages and feed inputs to R2"
            result = run(step_script(f"build-{platform}-release", name), TARGET=f"{platform}-{arch}")
            assert result.returncode == 0, result.stdout + result.stderr
    bundle = root / f"{identity['artifactNamePascal']}-0.1.3.10-win.msixbundle"
    with zipfile.ZipFile(bundle, "w") as package:
        package.writestr("AppxMetadata/AppxBundleManifest.xml", f'<Bundle><Identity Name="{identity["msixAppIdWithOrg"]}" Publisher="CN=Fixture" Version="0.1.3.10"/><Packages><Package Type="application" Architecture="arm64"/><Package Type="application" Architecture="x64"/></Packages></Bundle>')
    result = run('python -m scripts.releases.handoff stage --tag "$RELEASE_TAG" --commit "$RELEASE_COMMIT" --name windows-universal --root apps/desktop/release --include "*.msixbundle"')
    assert result.returncode == 0, result.stdout + result.stderr
    script = step_script("publish-canary", "Advance protected canary head from the published release")
    before = dict(r2_server.store)
    for job in channel_releases.CANARY_NEEDS:
        for outcome in ("failure", "skipped", "cancelled"):
            needs = json.loads(env["RELEASE_NEEDS"])
            needs[job] = {"result": outcome}
            result = run(script, RELEASE_NEEDS=json.dumps(needs))
            assert result.returncode != 0 and job in result.stderr
            assert r2_server.store == before
    release = Path(env["FIXTURE_RELEASE"])
    published = json.loads(release.read_text())
    for bad in ({**published, "isDraft": True}, {**published, "isPrerelease": False}):
        release.write_text(json.dumps(bad))
        result = run(script)
        assert result.returncode != 0 and "published" in result.stderr
        assert r2_server.store == before
    release.write_text(json.dumps(published))
    result = run(script)
    assert result.returncode == 0, result.stdout + result.stderr
    resolved = ChannelReader(env["CLOUDFLARE_R2_PUBLIC_URL"], repository=env["GITHUB_REPOSITORY"]).resolve("canary")
    assert resolved.manifest["request"]["windowsVersion"] == "0.1.3.10"
    assert resolved.manifest["request"]["commit"] == env["RELEASE_COMMIT"]
    assert len(resolved.manifest["packages"]) == 4
    after = dict(r2_server.store)
    assert run(script).returncode == 0
    assert r2_server.store == after
    # A moved published tag must not retarget the accepted release, even on retry.
    _git("commit", "--allow-empty", "-m", "new source", cwd=clone)
    _git("tag", "-f", env["RELEASE_TAG"], cwd=clone)
    _git("push", "--force", "origin", env["RELEASE_TAG"], cwd=clone)
    result = run(script)
    assert result.returncode != 0 and "moved" in result.stderr
    assert r2_server.store == after
