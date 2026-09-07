"""Release gates and package transitions bind the intended immutable artifacts."""
import copy
import hashlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.releases.stable import (
    check_tag, plan_transitions, read_manifest, require_stable_identity,
    require_success, validate_candidates,
)

BASE = "https://releases.example"
ROOT = Path(__file__).resolve().parents[2]


def candidates(tag, commit, digest):
    packages = []
    for platform in ("windows", "macos"):
        for arch in ("x64", "arm64"):
            packages.append({
                "platform": platform, "arch": arch, "tag": tag, "commit": commit,
                "identity": "test.application",
                "version": f"{tag[1:]}.0" if platform == "windows" else tag[1:],
                **({"publisher": "CN=Test", "applicationId": "App"} if platform == "windows" else {"teamId": "ABCDEFGHIJ"}),
                "artifact": {"sha256": digest,
                             "url": f"{BASE}/releases/tag/{tag}/{arch}" + (".msixbundle" if platform == "windows" else ".zip")},
            })
    return {"schema": 1, "tag": tag, "commit": commit, "packages": packages}


def test_gate_requires_every_success_including_real_cli(tmp_path):
    required = ["ci", "docker", "acceptance", "publication"]
    success = {name: {"result": "success"} for name in required}
    require_success(success, required)
    for name in required:
        for result in ("failure", "cancelled", "skipped", None):
            needs = copy.deepcopy(success)
            if result:
                needs[name]["result"] = result
            else:
                del needs[name]
            with pytest.raises(ValueError, match=name):
                require_success(needs, required)
    summary = tmp_path / "summary.md"
    env = {**os.environ, "RELEASE_NEEDS": json.dumps(success), "GITHUB_STEP_SUMMARY": str(summary), "PYTHONPATH": str(ROOT)}
    argv = [sys.executable, "-m", "scripts.releases.stable", "gate", *required]
    assert subprocess.run(argv, cwd=tmp_path, env=env, capture_output=True).returncode == 0
    env["RELEASE_NEEDS"] = json.dumps({**success, "publication": {"result": "cancelled"}})
    result = subprocess.run(argv, cwd=tmp_path, env=env, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode != 0
    assert "publication=cancelled" in result.stderr


def test_transitions_bind_all_arches_identity_version_and_archive():
    old = candidates("v1.2.3", "a" * 40, "1" * 64)
    new = candidates("v1.2.4", "b" * 40, "2" * 64)
    require_stable_identity(new["tag"], new["commit"], "refs/tags/v1.2.4")
    for tag, ref in [("v1.2.4", "refs/heads/main"), ("v1.2.4-canary.20260907143420", "refs/tags/v1.2.4-canary.20260907143420")]:
        with pytest.raises(ValueError):
            require_stable_identity(tag, new["commit"], ref)
    transitions = plan_transitions(old, new, BASE)
    assert {row["target"] for row in transitions} == {"windows-x64", "windows-arm64", "macos-x64", "macos-arm64"}
    assert all(row["transition"]["new"]["commit"] == new["commit"] for row in transitions)
    missing = copy.deepcopy(new)
    missing["packages"].pop()
    with pytest.raises(ValueError, match="both architectures"):
        plan_transitions(old, missing, BASE)
    with pytest.raises(ValueError, match="identity"):
        validate_candidates(new, new["tag"], old["commit"], BASE)
    for key, value in [("commit", old["commit"]), ("identity", "different"), ("publisher", "CN=Other")]:
        changed = copy.deepcopy(new)
        changed["packages"][0][key] = value
        with pytest.raises(ValueError):
            plan_transitions(old, changed, BASE)
    mutable = copy.deepcopy(new)
    mutable["packages"][0]["artifact"]["url"] = f"{BASE}/releases/win32/stable/current.msixbundle"
    with pytest.raises(ValueError, match="immutable"):
        plan_transitions(old, mutable, BASE)
    for suffix in ("../other.zip", "%2e%2e/other.zip", "%252e%252e/other.zip"):
        traversal = copy.deepcopy(new)
        traversal["packages"][0]["artifact"]["url"] = f"{BASE}/releases/tag/{new['tag']}/{suffix}"
        with pytest.raises(ValueError, match="path encoding"):
            plan_transitions(old, traversal, BASE)
    with pytest.raises(ValueError, match="increase"):
        plan_transitions(new, old, BASE)


def test_manifest_digest_and_tag_movement_fail_closed(tmp_path, monkeypatch):
    data = b'{"schema":1}'

    class Response(io.BytesIO):
        def geturl(self):
            return BASE + "/manifest.json"

    def opener(url, timeout):
        return Response(data)

    assert read_manifest(BASE, hashlib.sha256(data).hexdigest(), opener=opener) == {"schema": 1}
    with pytest.raises(ValueError, match="digest"):
        read_manifest(BASE, "f" * 64, opener=opener)
    commit = "a" * 40
    env = {"RELEASE_TAG": "v1.2.3", "GITHUB_SHA": commit, "GITHUB_REF": "refs/tags/v1.2.3"}

    def git(argv):
        if argv[1] == "ls-remote":
            return f"{'b' * 40}\trefs/tags/v1.2.3\n{commit}\trefs/tags/v1.2.3^{{}}"
        return commit if argv[1] == "rev-parse" else ""

    assert check_tag(env, git) == ("v1.2.3", commit)
    with pytest.raises(ValueError, match="moved"):
        check_tag(env, lambda argv: f"{'c' * 40}\trefs/tags/v1.2.3" if argv[1] == "ls-remote" else git(argv))

    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    repo.mkdir()
    monkeypatch.chdir(repo)
    subprocess.run(["git", "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "fixture"], check=True)
    subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], check=True)
    (repo / "input").write_text("first", encoding="utf-8")
    subprocess.run(["git", "add", "input"], check=True)
    subprocess.run(["git", "commit", "-m", "first"], check=True, capture_output=True)
    actual = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    subprocess.run(["git", "remote", "add", "origin", str(remote)], check=True)
    subprocess.run(["git", "tag", "v1.2.3"], check=True)
    subprocess.run(["git", "push", "origin", "main", "v1.2.3"], check=True, capture_output=True)
    env["GITHUB_SHA"] = actual
    assert check_tag(env) == ("v1.2.3", actual)
    subprocess.run(["git", "--git-dir", str(remote), "update-ref", "-d", "refs/tags/v1.2.3"], check=True)
    with pytest.raises(ValueError, match="moved"):
        check_tag(env)
