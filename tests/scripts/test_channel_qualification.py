"""Qualification uses real HTTP bytes and a real Git controller, never a true callback."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import subprocess
from threading import Thread
import zipfile

import pytest

from hermes_cli.release_channels import canonical_json, ChannelError
from test_release_channels import object_server, publisher, put_build


def digest(body):
    return hashlib.sha256(body).hexdigest()


@contextmanager
def github_server():
    responses = {}
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            calls.append(self.path)
            body = responses.get(self.path)
            if isinstance(body, dict):
                body = canonical_json(body)
            self.send_response(200 if body is not None else 404)
            self.end_headers()
            self.wfile.write(body or b"missing")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", responses, calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def git_controller(tmp_path):
    repo = tmp_path / "controller"
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True, stdin=subprocess.DEVNULL).strip()
    git("init", "-b", "main")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    (repo / "controller").write_text("trusted workflow fixture", encoding="utf-8")
    git("add", "controller")
    git("commit", "-m", "controller")
    return git("rev-parse", "HEAD")


def evidence(objects, responses, receipt, *, run_id=17, artifact_id=23):
    body = canonical_json(receipt)
    key = f"releases/qualifications/{digest(body)}.json"
    objects[key] = body
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("receipt.json", body)
    zipped = archive.getvalue()
    repository = receipt["repository"]
    prefix = f"/repos/{repository}"
    responses[f"{prefix}/actions/runs/{run_id}"] = {
        "id": run_id, "run_attempt": 1, "status": "completed", "conclusion": "success",
        "event": "workflow_dispatch", "head_branch": "main", "head_sha": receipt["controllerCommit"],
        "path": ".github/workflows/install-e2e.yml", "repository": {"full_name": repository},
        "head_repository": {"full_name": repository},
    }
    responses[f"{prefix}/actions/artifacts/{artifact_id}"] = {
        "id": artifact_id, "expired": False, "name": "channel-qualification",
        "digest": "sha256:" + digest(zipped), "workflow_run": {"id": run_id, "head_sha": receipt["controllerCommit"]},
    }
    responses[f"{prefix}/actions/artifacts/{artifact_id}/zip"] = zipped
    return {"key": key, "sha256": digest(body), "runId": run_id, "runAttempt": 1, "artifactId": artifact_id}


def test_retirement_verifier_binds_native_receipts_to_trusted_successful_run(tmp_path):
    from scripts.releases.channel_qualification import QualificationVerifier, GitHubEvidence, RETIREMENT_ASSERTIONS

    controller = git_controller(tmp_path)
    with object_server() as (url, objects, *_), github_server() as (api_url, responses, calls):
        pub = publisher(url)
        pub.create("preview")
        request = pub.allocate("preview", "a" * 40, "1.0.0", controller_commit=controller)
        source_manifest = put_build(objects, request)
        source = pub._read("preview")[0]
        source["head"] = {"buildId": request["buildId"], "sequence": 1,
                          "manifestKey": f"releases/channel-builds/{request['buildId']}/build.json",
                          "sha256": digest(canonical_json(source_manifest))}
        objects["releases/channels/preview.json"] = canonical_json(source)
        target = deepcopy(source)
        target.update(name="stable", policy="stable-release")
        stable_request = {**request, "channel": "stable", "buildId": "b" * 32,
                          "releaseTag": "v2.0.0", "sourceVersion": "2.0.0", "version": "2.0.0", "windowsVersion": "2.0.0.0"}
        stable_request["identity"] = {**request["identity"], "appId": "com.nousresearch.hermes-bundled"}
        target["identity"] = stable_request["identity"]
        stable_manifest = put_build(objects, stable_request)
        target["head"] = {"buildId": stable_request["buildId"], "sequence": 1,
                          "manifestKey": f"releases/channel-builds/{stable_request['buildId']}/build.json",
                          "sha256": digest(canonical_json(stable_manifest))}
        objects["releases/channels/stable.json"] = canonical_json(target)
        qualification = {"schema": 1, "source": "preview", "sourceHead": source["head"], "destination": "stable",
                         "destinationHead": target["head"], "minimumVersion": "2.0.0", "receiverProtocol": 1, "coverage": []}
        refs = []
        for i, state in enumerate(("absent", "running")):
            receipt = {"schema": 1, "kind": "channel-retirement", "repository": pub.repository,
                       "controllerCommit": controller, "runId": 17 + i, "runAttempt": 1,
                       "platform": "darwin", "arch": "arm64", "variant": "bundled", "receiverProtocol": 1,
                       "source": {"channel": "preview", "head": source["head"], "commit": request["commit"],
                                  "identity": source_manifest["packages"][0]["identity"],
                                  "artifactSha256": source_manifest["packages"][0]["artifact"]["sha256"]},
                       "destination": {"channel": "stable", "head": target["head"], "commit": stable_request["commit"],
                                       "identity": stable_manifest["packages"][0]["identity"],
                                       "artifactSha256": stable_manifest["packages"][0]["artifact"]["sha256"]},
                       "minimumVersion": "2.0.0", "scenario": {"route": "direct", "stable": state, "home": "shared"},
                       "assertions": dict.fromkeys(RETIREMENT_ASSERTIONS, True)}
            refs.append(evidence(objects, responses, receipt, run_id=17 + i, artifact_id=23 + i))
        qualification["coverage"] = [{"platform": "darwin", "arch": "arm64", "sourceIdentity": receipt["source"]["identity"],
                                    "destinationIdentity": receipt["destination"]["identity"],
                                    "cohorts": [{"buildId": request["buildId"], "commit": request["commit"], "proofs": refs}]}]
        verifier = QualificationVerifier(pub.store, pub.repository, pub.public_base, controller, "main",
                                         github=GitHubEvidence(api_base=api_url))
        pub.verify_retirement = verifier.verify_retirement
        key = "releases/qualifications/retirement.json"
        objects[key] = canonical_json(qualification)
        assert pub.retire("preview", "stable", "2.0.0", key, digest(objects[key]), publish=False)["state"] == "retired"
        assert calls and pub._read("preview")[0]["state"] == "active"
        run = responses[f"/repos/{pub.repository}/actions/runs/17"]
        for field, wrong in (("conclusion", "failure"), ("head_sha", "c" * 40), ("path", ".github/workflows/untrusted.yml"),
                             ("event", "pull_request"), ("head_repository", {"full_name": "attacker/fork"})):
            saved = run[field]
            run[field] = wrong
            with pytest.raises(ChannelError):
                verifier.verify_retirement(source, target, qualification)
            run[field] = saved
        bad = deepcopy(qualification)
        bad["coverage"][0]["cohorts"][0]["commit"] = "d" * 40
        with pytest.raises(ChannelError):
            verifier.verify_retirement(source, target, bad)
        # Every complete source cohort is required, including an older offline install.
        older = {**request, "buildId": "9" * 32}
        put_build(objects, older)
        with pytest.raises(ChannelError, match="cohort"):
            verifier.verify_retirement(source, target, qualification)
        del objects[f"releases/channel-builds/{older['buildId']}/build.json"]
        # An R2 author cannot change the acceptance assertions, even with an updated R2 hash.
        forged = json.loads(objects[refs[0]["key"]])
        forged["assertions"]["previewRemoved"] = False
        forged_ref = {**refs[0], "sha256": digest(canonical_json(forged))}
        objects[forged_ref["key"]] = canonical_json(forged)
        bad = deepcopy(qualification)
        bad["coverage"][0]["cohorts"][0]["proofs"][0] = forged_ref
        with pytest.raises(ChannelError):
            verifier.verify_retirement(source, target, bad)

def test_stable_bootstrap_requires_accepted_candidate_and_published_release(tmp_path):
    from scripts.releases.channel_qualification import QualificationVerifier, GitHubEvidence
    from scripts.releases.channels import preview_identity

    controller = git_controller(tmp_path)
    repository = "example/hermes-agent"
    archive_base = "https://archive.example.invalid"
    with object_server() as (url, objects, *_), github_server() as (api_url, responses, calls):
        pub = publisher(url)
        verifier = QualificationVerifier(pub.store, repository, archive_base, controller, "main",
                                         github=GitHubEvidence(api_base=api_url))
        # Only the transport endpoint changes; candidate URLs retain their HTTPS authority.
        verifier.reader = pub.reader
        identity = {**preview_identity("stable", "a" * 16), "appId": "com.nousresearch.hermes-bundled",
                    "msixAppIdWithOrg": "NousResearch.HermesBundled", "appNamePascal": "HermesBundled"}
        request = {"schema": 1, "buildId": "f" * 32, "channel": "stable", "repository": repository,
                   "sequence": 1, "commit": controller, "controllerCommit": controller, "sourceVersion": "2.0.0",
                   "releaseTag": "v2.0.0", "version": "2.0.0", "windowsVersion": "2.0.0.0",
                   "identity": identity, "bundleEnv": {}, "publicBase": archive_base}
        packages, candidates = [], []
        for platform, arch in ((p, a) for p in ("darwin", "win32") for a in ("arm64", "x64")):
            mac = platform == "darwin"
            name = f"{platform}-{arch}." + ("zip" if mac else "msixbundle")
            key = f"releases/tag/v2.0.0/{name}"
            native = identity["appId" if mac else "msixAppIdWithOrg"]
            signing = {"teamId": "ABCDEFGHIJ"} if mac else {"publisher": "CN=Nous Research"}
            package = {"platform": platform, "arch": arch, "variant": "bundled", "identity": native,
                       "version": "2.0.0" if mac else "2.0.0.0", **signing,
                       "artifact": {"key": key, "sha256": "e" * 64, "size": 100},
                       "feed": {"key": f"releases/tag/v2.0.0/{platform}/stable", "channel": "stable"}}
            packages.append(package)
            candidates.append({**package, "platform": "macos" if mac else "windows", "tag": "v2.0.0",
                               "commit": request["commit"], "applicationId": "HermesBundled",
                               "artifact": {"url": archive_base + "/" + key, "sha256": "e" * 64}})
        candidate = {"schema": 2, "tag": "v2.0.0", "commit": request["commit"], "packages": candidates,
                     "smoke_results": {k: {"result": "success"} for k in
                                       ("smoke-darwin", "smoke-win32", "smoke-win32-universal")}}
        key = "releases/tag/v2.0.0/release-candidates.json"
        objects[key] = canonical_json(candidate)
        manifest = {"schema": 1, "request": request, "packages": packages}
        receipt = {"schema": 1, "kind": "channel-release", "repository": repository,
                   "controllerCommit": controller, "runId": 17, "runAttempt": 1,
                   "manifestSha256": digest(canonical_json(manifest)), "candidateSha256": digest(objects[key]),
                   "tag": "v2.0.0", "commit": request["commit"],
                   "assertions": {"nativeSmoke": True, "nativeUpgrade": True, "releaseGate": True}}
        proof = evidence(objects, responses, receipt)
        responses[f"/repos/{repository}/actions/runs/17"]["path"] = ".github/workflows/stable-release.yml"
        responses[f"/repos/{repository}/releases/tags/v2.0.0"] = {
            "tag_name": "v2.0.0", "draft": False, "prerelease": False, "published_at": "2026-01-01T00:00:00Z"}
        responses[f"/repos/{repository}/commits/v2.0.0"] = {"sha": request["commit"]}
        responses[f"/repos/{repository}/compare/{controller}...{controller}"] = {"status": "identical", "merge_base_commit": {"sha": controller}}
        responses[f"/repos/{repository}/actions/runs/17"]["head_branch"] = "v2.0.0"
        manifest["releaseQualification"] = {"candidate": {"key": key, "sha256": digest(objects[key])}, "proof": proof}
        assert verifier.verify_build(request, manifest) is True
        for mutate in (lambda m: m["packages"][0]["artifact"].update(sha256="b" * 64),
                       lambda m: m["packages"][0].update(identity="attacker.app"),
                       lambda m: m["request"].update(commit="b" * 40)):
            bad = deepcopy(manifest)
            mutate(bad)
            with pytest.raises(ChannelError):
                verifier.verify_build(bad["request"], bad)
        responses[f"/repos/{repository}/releases/tags/v2.0.0"]["draft"] = True
        with pytest.raises(ChannelError):
            verifier.verify_build(request, manifest)

