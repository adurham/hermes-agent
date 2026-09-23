"""Release gates and package transitions bind the intended immutable artifacts."""
import copy
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.releases.draft_warning import (
    WARNING_CLOSE, WARNING_OPEN, strip_draft_warning,
)
from scripts.releases.stable import (
    check_claim, ensure_final_tag, plan_transitions, read_manifest, require_stable_identity,
    require_success, validate_candidates,
)

BASE = "https://releases.example"
ROOT = Path(__file__).resolve().parents[2]


def candidates(tag, commit, digest, archive=None):
    """A desktop candidate manifest. `archive` is the R2 prefix ref the
    manifest itself names; the payload `tag` stays plain vX.Y.Z."""
    packages = []
    second = 100 + int(tag.rsplit('.', 1)[1])
    release_epoch = int((datetime(2026, 8, 29, 1, 0, tzinfo=timezone.utc)
                         + timedelta(seconds=second)).timestamp())
    native_version = f"2026.5761.{second}.0"
    ref = archive or tag
    for platform in ("windows", "macos"):
        for arch in ("x64", "arm64"):
            packages.append({
                "platform": platform, "arch": arch, "tag": tag, "commit": commit,
                "identity": "test.application",
                "version": native_version if platform == "windows" else tag[1:],
                **({"executableVersion": native_version} if platform == "windows" else {}),
                **({"publisher": "CN=Test", "applicationId": "App"} if platform == "windows" else {"teamId": "ABCDEFGHIJ"}),
                "artifact": {"sha256": digest,
                             "url": f"{BASE}/releases/tag/{ref}/{arch}" + (".msixbundle" if platform == "windows" else ".zip")},
            })
    return {"schema": 2, "tag": tag, "commit": commit, "releaseEpoch": release_epoch,
            "archive": ref,
            "packages": packages,
            "smoke_results": {name: {"result": "success"} for name in (
                "smoke-darwin", "smoke-win32", "smoke-win32-universal")}}


def test_gate_requires_every_success_including_real_cli(tmp_path):
    required = ["ci", "docker", "acceptance", "publication"]
    success = {name: {"result": "success"} for name in required}
    require_success(success, required)
    with pytest.raises(ValueError, match="required-job list"):
        require_success(success, [])
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
    empty = subprocess.run(argv[:4], cwd=tmp_path, env=env, capture_output=True, text=True, encoding="utf-8")
    assert empty.returncode != 0
    assert "required-job list" in empty.stderr
    env["RELEASE_NEEDS"] = json.dumps({**success, "publication": {"result": "cancelled"}})
    result = subprocess.run(argv, cwd=tmp_path, env=env, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode != 0
    assert "publication=cancelled" in result.stderr


def test_validate_candidates_keys_the_archive_by_the_attempt_ref():
    commit = "b" * 40
    manifest = candidates("v1.2.4", commit, "2" * 64, archive="rc.2-v1.2.4")
    assert validate_candidates(manifest, manifest["tag"], commit, BASE, archive="rc.2-v1.2.4")
    # The archive ref is the URL prefix; the payload tag stays the identity.
    assert manifest["packages"][0]["artifact"]["url"].startswith(f"{BASE}/releases/tag/rc.2-v1.2.4/")
    with pytest.raises(ValueError, match="archive"):
        validate_candidates(manifest, manifest["tag"], commit, BASE, archive="rc.1-v1.2.4")
    misnamed = copy.deepcopy(manifest)
    misnamed["archive"] = "rc.1-v1.2.4"
    with pytest.raises(ValueError, match="archive"):
        validate_candidates(misnamed, manifest["tag"], commit, BASE, archive="rc.2-v1.2.4")
    missing = copy.deepcopy(manifest)
    del missing["archive"]
    with pytest.raises(ValueError, match="archive"):
        validate_candidates(missing, manifest["tag"], commit, BASE, archive="rc.2-v1.2.4")
    # A canary-shaped archive ref (the payload tag itself) still validates.
    assert validate_candidates(candidates("v1.2.4", commit, "2" * 64),
                               "v1.2.4", commit, BASE, archive="v1.2.4")


def test_transitions_bind_all_arches_identity_version_and_archive():
    old = candidates("v1.2.3", "a" * 40, "1" * 64)
    old["schema"] = 1
    del old["smoke_results"]
    with pytest.raises(ValueError, match="does not match release identity"):
        validate_candidates(old, old["tag"], old["commit"], BASE, archive=old["archive"])
    old = candidates("v1.2.3", "a" * 40, "1" * 64)
    new = candidates("v1.2.4", "b" * 40, "2" * 64, archive="rc.1-v1.2.4")
    require_stable_identity(new["tag"], new["commit"])
    for tag in ("v1.2.4+canary.20260907T143420Z", "v1.2.4-rc", "rc.1-v1.2.4"):
        with pytest.raises(ValueError):
            require_stable_identity(tag, new["commit"])
    transitions = plan_transitions(old, new, BASE)
    assert {row["target"] for row in transitions} == {"windows-x64", "windows-arm64", "macos-x64", "macos-arm64"}
    assert all(row["transition"]["new"]["commit"] == new["commit"] for row in transitions)
    assert all(row["transition"]["new"]["artifact"]["url"].startswith(f"{BASE}/releases/tag/{new['archive']}/")
               for row in transitions)
    missing = copy.deepcopy(new)
    missing["packages"].pop()
    with pytest.raises(ValueError, match="both architectures"):
        plan_transitions(old, missing, BASE)
    with pytest.raises(ValueError, match="identity"):
        validate_candidates(new, new["tag"], old["commit"], BASE, archive=new["archive"])
    for key, value in [("commit", old["commit"]), ("identity", "different"), ("publisher", "CN=Other"), ("version", "9.9.9.0")]:
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
        plan_transitions(new, candidates("v1.2.3", "a" * 40, "1" * 64), BASE)


@pytest.fixture
def https_origin(tmp_path, monkeypatch):
    import datetime
    import ipaddress
    import ssl
    import threading
    import urllib.request
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc))
            .not_valid_after(datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc))
            .add_extension(x509.SubjectAlternativeName([
                x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            ]), critical=False).sign(key, hashes.SHA256()))
    cert_file, key_file = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                         serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            if self.path in ("/same", "/cross"):
                self.send_response(302)
                host = "127.0.0.1" if self.path == "/same" else "localhost"
                self.send_header("Location", f"https://{host}:{self.server.server_port}/manifest")
                self.end_headers()
            else:
                item = self.server.store.get(self.path.lstrip('/'))
                data = item[0] if item else b'not found'
                self.send_response(200 if item else 404)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.store = {'manifest': (b'{"schema":1}', '"e"')}
    server.requests = requests
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_file, key_file)
    server.socket = server_context.wrap_socket(server.socket, server_side=True)
    client_context = ssl.create_default_context(cafile=str(cert_file))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                        urllib.request.HTTPSHandler(context=client_context)).open
    base = f"https://127.0.0.1:{server.server_port}"
    server.base, server.opener = base, opener
    monkeypatch.setenv('SSL_CERT_FILE', str(cert_file))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_manifest_origin_checks_with_real_https(https_origin):
    server = https_origin
    base, opener = server.base, server.opener
    digest = hashlib.sha256(b'{"schema":1}').hexdigest()
    assert read_manifest(f'{base}/same', digest, expected_origin=base, opener=opener) == {'schema': 1}
    with pytest.raises(ValueError, match='digest'):
        read_manifest(f'{base}/manifest', 'f' * 64, opener=opener)
    with pytest.raises(ValueError, match='origin'):
        read_manifest(f'{base}/cross', opener=opener)
    server.requests.clear()
    with pytest.raises(ValueError, match='origin'):
        read_manifest(f'https://localhost:{server.server_port}/manifest', expected_origin=base, opener=opener)
    assert server.requests == []


def test_claim_object_movement_and_lightweight_tags_fail_closed(tmp_path, monkeypatch):
    commit = "a" * 40
    claim_object = "b" * 40
    ref = "refs/tags/rc.1-v1.2.3"
    env = {"RELEASE_CLAIM_TAG": "rc.1-v1.2.3", "RELEASE_CLAIM_OBJECT": claim_object,
           "GITHUB_SHA": commit, "GITHUB_REF": ref}
    message = {"schema": 1, "version": "1.2.3", "attempt": 1, "commit": commit,
               "autopublish": False, "claimEpoch": 1_790_000_000}

    def git(argv):
        if argv[1] == "ls-remote":
            return f"{claim_object}\t{ref}\n{commit}\t{ref}^{{}}"
        if argv[1:3] == ["cat-file", "-t"]:
            return "tag"
        if argv[1:3] == ["cat-file", "-p"]:
            return "tagger Fixture <fixture@example.test> 1790000000 +0000\n"
        if argv[1] == "rev-parse":
            return claim_object if argv[-1] == ref else commit
        if argv[1] == "tag":
            return json.dumps(message)
        return ""

    assert check_claim(env, git) == {
        "claim_tag": "rc.1-v1.2.3", "claim_object": claim_object,
        "tag": "v1.2.3", "version": "1.2.3", "attempt": 1, "commit": commit,
        "autopublish": False, "claim_epoch": 1_790_000_000,
    }
    # The metadata binds the attempt its ref names.
    for wrong in ({**message, "attempt": 2}, {k: v for k, v in message.items() if k != "attempt"}):
        with pytest.raises(ValueError, match="metadata is invalid"):
            check_claim(env, lambda argv, wrong=wrong: json.dumps(wrong) if argv[1] == "tag" else git(argv))
    with pytest.raises(ValueError, match="moved"):
        check_claim(env, lambda argv: f"{'c' * 40}\t{ref}\n{commit}\t{ref}^{{}}"
                    if argv[1] == "ls-remote" else git(argv))
    with pytest.raises(ValueError, match="annotated"):
        check_claim(env, lambda argv: "commit" if argv[1] == "cat-file" else git(argv))

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
    actual = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, encoding="utf-8").strip()
    subprocess.run(["git", "remote", "add", "origin", str(remote)], check=True)
    metadata = json.dumps({
        "schema": 1, "version": "1.2.3", "attempt": 1, "commit": actual,
        "autopublish": False, "claimEpoch": 1_790_000_000,
    }, sort_keys=True, separators=(",", ":"))
    subprocess.run(
        ["git", "tag", "-a", "rc.1-v1.2.3", "-m", metadata], check=True,
        env={**os.environ, "GIT_COMMITTER_DATE": "@1790000000 +0000"},
    )
    subprocess.run(["git", "push", "origin", "main", "rc.1-v1.2.3"], check=True, capture_output=True)
    env.update({"GITHUB_SHA": actual, "RELEASE_CLAIM_OBJECT": subprocess.check_output(
        ["git", "rev-parse", ref], text=True, encoding="utf-8").strip()})
    claim = check_claim(env)
    assert claim["commit"] == actual
    final_object = ensure_final_tag(
        "v1.2.3", actual, claim,
        candidate_manifest_sha256="c" * 64,
        docker_manifest_digest="sha256:" + "d" * 64,
        release_id=123,
    )
    remote_final = subprocess.check_output(
        ["git", "ls-remote", "origin", "refs/tags/v1.2.3", "refs/tags/v1.2.3^{}"],
        text=True, encoding="utf-8",
    )
    assert f"{final_object}\trefs/tags/v1.2.3" in remote_final
    assert f"{actual}\trefs/tags/v1.2.3^{{}}" in remote_final
    subprocess.run(["git", "--git-dir", str(remote), "update-ref", "-d", ref], check=True)
    with pytest.raises(ValueError, match="moved"):
        check_claim(env)


def _claim_fixture(tmp_path, *, tag, version):
    """A real checkout + bare remote carrying one annotated attempt claim."""
    from scripts.releases.versioning import parse_attempt_ref

    epoch = candidates("v" + version, "0" * 40, "0" * 64)["releaseEpoch"]
    repo, remote = tmp_path / "repo", tmp_path / "remote.git"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "fixture"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=repo, check=True)
    (repo / "input").write_text("first", encoding="utf-8")
    subprocess.run(["git", "add", "input"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "first"], cwd=repo, check=True, capture_output=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True, encoding="utf-8").strip()
    attempt = parse_attempt_ref(tag)[1]
    metadata = json.dumps({
        "schema": 1, "version": version, "attempt": attempt, "commit": commit,
        "autopublish": False, "claimEpoch": epoch,
    }, sort_keys=True, separators=(",", ":"))
    subprocess.run(
        ["git", "tag", "-a", tag, "-m", metadata], cwd=repo, check=True,
        env={**os.environ, "GIT_COMMITTER_DATE": f"@{epoch} +0000"},
    )
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)
    subprocess.run(["git", "push", "origin", "main", tag], cwd=repo, check=True, capture_output=True)
    tag_object = subprocess.check_output(["git", "rev-parse", tag], cwd=repo, text=True, encoding="utf-8").strip()
    return commit, tag_object


def test_complete_writes_no_final_tag_and_leaves_the_draft_on_the_attempt_ref(tmp_path, monkeypatch):
    from scripts.releases import stable

    commit, tag_object = _claim_fixture(tmp_path, tag="rc.1-v1.2.3", version="1.2.3")
    candidate = candidates("v1.2.3", commit, "c" * 64, archive="rc.1-v1.2.3")
    calls = []

    def record(argv):
        calls.append(argv)
        if argv[0] == "gh":
            raise AssertionError("complete must not touch GitHub")
        return subprocess.check_output(argv, text=True, encoding="utf-8").strip()

    monkeypatch.setattr(stable, "output", record)
    monkeypatch.setattr(stable, "read_candidate", lambda env: candidate)
    monkeypatch.chdir(tmp_path / "repo")
    stable.complete({
        "RELEASE_CLAIM_TAG": "rc.1-v1.2.3", "RELEASE_CLAIM_OBJECT": tag_object,
        "GITHUB_SHA": commit, "GITHUB_REF": "refs/tags/rc.1-v1.2.3",
        "RELEASE_TAG": "v1.2.3", "CANDIDATE_MANIFEST_SHA256": "c" * 64,
        "CLOUDFLARE_R2_PUBLIC_URL": BASE,
    })
    remote = subprocess.check_output(
        ["git", "ls-remote", "origin", "refs/tags/*"], text=True, encoding="utf-8")
    assert "refs/tags/v1.2.3" not in remote
    assert not [argv for argv in calls if argv[0] == "gh"]


def _fenced_body(notes="## What's changed\n- x"):
    """A draft body the way the entrypoint builds it: warning block, notes, warning block."""
    block = WARNING_OPEN + "\nDO NOT PUBLISH THIS BY HAND\n" + WARNING_CLOSE
    return block + "\n" + notes + "\n" + block


def test_strip_removes_both_blocks_and_keeps_the_notes():
    assert strip_draft_warning(_fenced_body()).strip() == "## What's changed\n- x"
    # A body without fences passes through untouched.
    assert strip_draft_warning("just notes") == "just notes"


@pytest.mark.parametrize("body", [
    WARNING_OPEN + "\nunbalanced",
    "text\n" + WARNING_CLOSE,
    WARNING_OPEN + "\n" + WARNING_OPEN + "\n" + WARNING_CLOSE,
    WARNING_OPEN + "text",
])
def test_strip_refuses_an_unbalanced_fence(body):
    with pytest.raises(ValueError, match="unbalanced"):
        strip_draft_warning(body)


def _publish_record(commit, tag_object, *, epoch, release_id=42):
    return {"claim_tag": "rc.2-v1.2.3", "claim_object": tag_object, "tag": "v1.2.3",
            "commit": commit, "version": "1.2.3", "attempt": 2, "release_id": release_id,
            "autopublish": False, "claim_epoch": epoch}


def test_publish_attempt_writes_the_receipt_retargets_and_copies_no_bytes(tmp_path, monkeypatch):
    from scripts.releases import stable

    commit, tag_object = _claim_fixture(tmp_path, tag="rc.2-v1.2.3", version="1.2.3")
    monkeypatch.chdir(tmp_path / "repo")
    epoch = json.loads(subprocess.check_output(
        ["git", "tag", "-l", "rc.2-v1.2.3", "--format=%(contents)"],
        text=True, encoding="utf-8"))["claimEpoch"]
    manifest_bytes = b'{"schema":2}\n'
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    docker_digest = "sha256:" + "d" * 64
    release = {"id": 42, "tag_name": "rc.2-v1.2.3", "draft": True, "prerelease": False,
               "body": _fenced_body(), "published_at": None}
    patches = []
    requested_keys = []

    def run(argv):
        if argv[0] == "git":
            return subprocess.check_output(argv, text=True, encoding="utf-8").strip()
        if argv[:3] == ["docker", "buildx", "imagetools"]:
            assert argv[4].endswith("nousresearch/hermes-agent:rc.2-v1.2.3")
            return json.dumps(docker_digest)
        if argv[:3] == ["gh", "api", "--method"]:
            fields = {}
            for _flag, value in zip(argv[5::2], argv[6::2]):
                name, _, raw = value.partition("=")
                fields[name] = raw
            patches.append((fields.get("tag_name"), fields.get("draft")))
            release.update({key: (raw == "true") if key in {"draft", "prerelease"} else raw
                            for key, raw in fields.items()
                            if key in {"tag_name", "draft", "prerelease", "body"}})
            if release["draft"] is False:
                release["published_at"] = "2026-09-22T00:00:00Z"
            return "{}"
        if argv[:2] == ["gh", "api"]:
            assert argv[2].endswith("/releases/42")
            return json.dumps(release)
        raise AssertionError(argv)

    def read_archive(key):
        requested_keys.append(key)
        return manifest_bytes

    digest = stable.publish_attempt(
        _publish_record(commit, tag_object, epoch=epoch),
        repository="example/project", run=run, read_archive=read_archive,
    )

    assert digest == docker_digest
    receipt = json.loads(subprocess.check_output(
        ["git", "tag", "-l", "v1.2.3", "--format=%(contents)"],
        text=True, encoding="utf-8"))
    assert receipt["claimTag"] == "rc.2-v1.2.3"
    assert receipt["archive"] == "releases/tag/rc.2-v1.2.3/"
    assert receipt["candidateManifestSha256"] == manifest_digest
    assert receipt["dockerManifestDigest"] == docker_digest
    assert receipt["releaseId"] == 42
    remote = subprocess.check_output(
        ["git", "ls-remote", "origin", "refs/tags/v1.2.3", "refs/tags/v1.2.3^{}"],
        text=True, encoding="utf-8")
    assert commit in remote
    # The digest is hashed from the attempt archive, and no v-tag path is read.
    assert requested_keys == ["releases/tag/rc.2-v1.2.3/release-candidates.json"]
    # The retarget and the strip happen while the release is still a draft;
    # draft=false is its own final call, after both read back.
    assert [draft for _tag, draft in patches] == ["true", "false"]
    assert [tag for tag, _draft in patches] == ["v1.2.3", None]
    assert release["tag_name"] == "v1.2.3" and release["draft"] is False
    assert release["body"] == "## What's changed\n- x"


def test_publish_attempt_refuses_a_release_that_is_no_longer_a_draft(tmp_path, monkeypatch):
    from scripts.releases import stable

    commit, tag_object = _claim_fixture(tmp_path, tag="rc.2-v1.2.3", version="1.2.3")
    monkeypatch.chdir(tmp_path / "repo")
    epoch = json.loads(subprocess.check_output(
        ["git", "tag", "-l", "rc.2-v1.2.3", "--format=%(contents)"],
        text=True, encoding="utf-8"))["claimEpoch"]
    release = {"id": 42, "tag_name": "rc.2-v1.2.3", "draft": False, "prerelease": False,
               "body": "notes", "published_at": "2026-09-22T00:00:00Z"}

    def run(argv):
        if argv[0] == "git":
            return subprocess.check_output(argv, text=True, encoding="utf-8").strip()
        if argv[:3] == ["docker", "buildx", "imagetools"]:
            return json.dumps("sha256:" + "d" * 64)
        if argv[:2] == ["gh", "api"]:
            return json.dumps(release)
        raise AssertionError(argv)

    with pytest.raises(ValueError, match="no longer a draft"):
        stable.publish_attempt(
            _publish_record(commit, tag_object, epoch=epoch),
            repository="example/project", run=run, read_archive=lambda _key: b"m",
        )
    # The custody receipt still exists: a public release cannot be repaired,
    # but the tag must not be skipped either.
    assert "refs/tags/v1.2.3" in subprocess.check_output(
        ["git", "ls-remote", "origin", "refs/tags/v1.2.3"], text=True, encoding="utf-8")


def test_edit_draft_release_refuses_a_body_that_still_carries_a_fence(tmp_path, monkeypatch):
    from scripts.releases import stable

    commit, _tag_object = _claim_fixture(tmp_path, tag="rc.2-v1.2.3", version="1.2.3")
    monkeypatch.chdir(tmp_path / "repo")
    release = {"id": 42, "tag_name": "rc.2-v1.2.3", "draft": True, "prerelease": False,
               "body": _fenced_body(), "published_at": None}
    # The PATCH is dropped on the floor: the read-back still shows the fence.
    def run(argv):
        if argv[0] == "git":
            return subprocess.check_output(argv, text=True, encoding="utf-8").strip()
        if argv[:3] == ["gh", "api", "--method"]:
            return "{}"
        if argv[:2] == ["gh", "api"]:
            return json.dumps(release)
        raise AssertionError(argv)

    with pytest.raises(ValueError):
        stable.edit_draft_release("example/project", 42, "v1.2.3", commit, run=run)


def test_edit_draft_release_sends_the_notes_byte_for_byte(tmp_path, monkeypatch):
    """Notes that open with a mention must not be read as a file by ``gh api``."""
    from scripts.releases import stable
    from scripts.releases.draft_warning import draft_body

    commit, _tag_object = _claim_fixture(tmp_path, tag="rc.2-v1.2.3", version="1.2.3")
    monkeypatch.chdir(tmp_path / "repo")
    notes = "@alice fixed the updater\n\n42\ntrue"
    release = {"id": 42, "tag_name": "rc.2-v1.2.3", "draft": True, "prerelease": False,
               "body": draft_body(version="1.2.3", attempt_ref="rc.2-v1.2.3", notes=notes),
               "published_at": None}

    def gh_value(flag, value):
        # gh api: -f/--raw-field is a literal string; -F/--field reads @file and
        # converts true/false/null/integers.
        if flag == "--raw-field":
            return value
        if value.startswith("@"):
            raise FileNotFoundError(value[1:])
        return {"true": True, "false": False, "null": None}.get(
            value, int(value) if value.isdigit() else value)

    def run(argv):
        if argv[0] == "git":
            return subprocess.check_output(argv, text=True, encoding="utf-8").strip()
        if argv[:3] == ["gh", "api", "--method"]:
            pairs = zip(argv[5::2], argv[6::2])
            for flag, field in pairs:
                key, _, value = field.partition("=")
                release[key] = gh_value(flag, value)
            return "{}"
        if argv[:2] == ["gh", "api"]:
            return json.dumps(release)
        raise AssertionError(argv)

    stable.edit_draft_release("example/project", 42, "v1.2.3", commit, run=run)

    assert release["body"].strip() == notes
    assert release["tag_name"] == "v1.2.3" and release["draft"] is True
