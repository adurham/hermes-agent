"""Release-admin evidence verifier. R2 assertions alone never authorize retirement.

The trusted controller uploads receipt.json as an immutable Actions artifact.
We compare those bytes with the digest-pinned R2 copy, then bind every claim to
native packages and the successful default-branch workflow that produced it.
"""
from __future__ import annotations

import hashlib
import io
import subprocess
from urllib.parse import quote
from urllib.request import Request, urlopen
import zipfile

from hermes_cli.release_channels import (
    ChannelError, ChannelReader, artifact_key, build_prefix, canonical_json,
    decode_json, public_base, require_commit, require_sha256, validate_manifest,
    validate_record, validate_repository,
)

RETIREMENT_ASSERTIONS = (
    "nativeIdentityVerified", "sourceChatCompleted", "destinationChatCompleted",
    "statePreserved", "automaticActivation", "previewRemoved", "ownedLaunchersRemoved",
    "stableSubscription", "stableUpdateCompleted", "stableUpdateChatCompleted",
)
SUPPORTED = {(p, a) for p in ("darwin", "win32") for a in ("arm64", "x64")}
MAX_EVIDENCE = 4 * 1024 * 1024


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ChannelError(reason)


def _positive(value: object) -> int:
    _require(type(value) is int and value > 0, "Invalid Actions evidence ID")
    return value


class GitHubEvidence:
    """gh owns production credentials. The explicit HTTP endpoint is for fixtures."""
    def __init__(self, *, api_base: str | None = None):
        self.api_base = public_base(api_base) if api_base else None

    def read(self, endpoint: str) -> bytes:
        try:
            if self.api_base:
                with urlopen(Request(self.api_base + "/" + endpoint), timeout=30) as response:
                    body = response.read(MAX_EVIDENCE + 1)
            else:
                body = subprocess.check_output(["gh", "api", endpoint], stdin=subprocess.DEVNULL, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ChannelError("GitHub qualification evidence unavailable") from exc
        _require(len(body) <= MAX_EVIDENCE, "Actions evidence exceeds size limit")
        return body

    def json(self, endpoint: str) -> dict:
        return decode_json(self.read(endpoint))

    def receipt(self, repository: str, proof: dict, expected: bytes, *, controller: str,
                branch: str, workflow: str, artifact_name: str) -> dict:
        run_id, attempt, artifact_id = (_positive(proof.get(k)) for k in ("runId", "runAttempt", "artifactId"))
        prefix = f"repos/{repository}/actions"
        run = self.json(f"{prefix}/runs/{run_id}")
        facts = {"id": run_id, "run_attempt": attempt, "status": "completed", "conclusion": "success",
                 "head_sha": controller, "head_branch": branch, "event": "workflow_dispatch", "path": workflow}
        _require(all(run.get(k) == v for k, v in facts.items()), "Untrusted or unsuccessful qualification workflow run")
        _require(all(isinstance(run.get(k), dict) and run[k].get("full_name", "").casefold() == repository.casefold()
                     for k in ("repository", "head_repository")), "Qualification workflow repository mismatch")
        artifact = self.json(f"{prefix}/artifacts/{artifact_id}")
        _require(artifact.get("id") == artifact_id and artifact.get("expired") is False
                 and artifact.get("name") == artifact_name, "Wrong or expired Actions receipt artifact")
        binding = artifact.get("workflow_run", {})
        _require(binding.get("id") == run_id and binding.get("head_sha") == controller,
                 "Actions artifact is not from the qualified controller run")
        archive = self.read(f"{prefix}/artifacts/{artifact_id}/zip")
        _require(artifact.get("digest") == "sha256:" + hashlib.sha256(archive).hexdigest(),
                 "Actions artifact digest mismatch")
        try:
            with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
                _require(zipped.namelist() == ["receipt.json"], "Actions artifact must contain only receipt.json")
                _require(zipped.getinfo("receipt.json").file_size <= MAX_EVIDENCE, "Actions receipt exceeds size limit")
                body = zipped.read("receipt.json")
        except (zipfile.BadZipFile, RuntimeError) as exc:
            raise ChannelError("Invalid Actions receipt archive") from exc
        _require(body == expected, "R2 receipt differs from trusted Actions artifact")
        receipt = decode_json(body)
        _require(receipt.get("repository") == repository and receipt.get("controllerCommit") == controller
                 and receipt.get("runId") == run_id and receipt.get("runAttempt") == attempt,
                 "Receipt controller/run binding mismatch")
        return receipt


class QualificationVerifier:
    def __init__(self, store, repository: str, base_url: str, controller_commit: str,
                 default_branch: str, *, github: GitHubEvidence | None = None):
        self.store = store
        self.repository = validate_repository(repository)
        self.base_url = public_base(base_url)
        self.reader = ChannelReader(base_url, repository)
        self.controller = require_commit(controller_commit)
        self.branch = default_branch
        self.github = github or GitHubEvidence()

    def verify_build(self, request: dict, manifest: dict) -> bool:
        from scripts.releases.stable import validate_candidates

        _require(manifest.get("request") == request, "Build request binding mismatch")
        tag = request.get("releaseTag")
        _require(isinstance(tag, str), "Admin bootstrap requires accepted protected release metadata")
        # Canary publication remains owned by its existing workflow, not stable bootstrap.
        _require("-" not in tag, "Stable bootstrap cannot admit a canary as stable")
        record = {"name": request["channel"], "repository": self.repository, "policy": "stable-release",
                  "identity": request["identity"], "head": None}
        validate_manifest(manifest, record, self.base_url)
        qualification = manifest.get("releaseQualification", {})
        candidate_ref = qualification.get("candidate", {})
        _require(candidate_ref.get("key") == f"releases/tag/{tag}/release-candidates.json",
                 "Bootstrap must reuse the accepted release candidate")
        candidate = self._object(candidate_ref["key"], require_sha256(candidate_ref.get("sha256")))
        try:
            rows = validate_candidates(candidate, tag, request["commit"], self.base_url)
        except ValueError as exc:
            raise ChannelError(str(exc)) from exc
        _require({(p["platform"], p["arch"]) for p in manifest["packages"]} == SUPPORTED,
                 "Bootstrap requires every native release platform")
        for package in manifest["packages"]:
            platform = "macos" if package["platform"] == "darwin" else "windows"
            admitted = rows[f"{platform}/{package['arch']}"]
            signing = "teamId" if platform == "macos" else "publisher"
            _require(all(admitted.get(k) == package.get(k) for k in ("identity", "version", signing))
                     and admitted["artifact"]["url"] == self.base_url + "/" + package["artifact"]["key"]
                     and admitted["artifact"]["sha256"] == package["artifact"]["sha256"],
                     "Bootstrap package differs from accepted stable candidate")
            if platform == "windows":
                _require(admitted["applicationId"] == request["identity"]["appNamePascal"],
                         "Bootstrap native application identity mismatch")
        release = self.github.json(f"repos/{self.repository}/releases/tags/{quote(tag, safe='')}")
        _require(release.get("tag_name") == tag and release.get("draft") is False
                 and release.get("prerelease") is False and bool(release.get("published_at")),
                 "Bootstrap destination is not a published stable release")
        _require(self.github.json(f"repos/{self.repository}/commits/{quote(tag, safe='')}").get("sha") == request["commit"],
                 "Published release commit differs from bootstrap")
        comparison = self.github.json(f"repos/{self.repository}/compare/{request['commit']}...{self.controller}")
        _require(comparison.get("status") in ("ahead", "identical")
                 and comparison.get("merge_base_commit", {}).get("sha") == request["commit"],
                 "Published stable controller is not admitted on the default branch")
        receipt = self._proof(qualification.get("proof"), workflow=".github/workflows/stable-release.yml",
                              artifact_name="channel-qualification", controller=request["commit"], branch=tag)
        content = {"schema": 1, "request": request, "packages": manifest["packages"]}
        facts = {"schema": 1, "kind": "channel-release", "tag": tag, "commit": request["commit"],
                 "manifestSha256": hashlib.sha256(canonical_json(content)).hexdigest(),
                 "candidateSha256": candidate_ref["sha256"]}
        _require(all(receipt.get(k) == v for k, v in facts.items()), "Release qualification binding mismatch")
        _require(all(receipt.get("assertions", {}).get(k) is True for k in ("nativeSmoke", "nativeUpgrade", "releaseGate")),
                 "Missing native stable release acceptance")
        return True

    def _object(self, key: str, digest: str | None = None) -> dict:
        artifact_key(key)
        if digest is not None:
            require_sha256(digest)
        found = self.store.get(key)
        _require(found is not None, "Missing immutable qualification object")
        raw = found[0]
        _require(digest is None or hashlib.sha256(raw).hexdigest() == digest, "Qualification object digest mismatch")
        return decode_json(raw)

    def _manifest(self, record: dict, head: dict) -> dict:
        return validate_manifest(self._object(head["manifestKey"], head["sha256"]),
                                 {**record, "head": head}, self.reader.base_url)

    def _proof(self, proof: dict, *, workflow: str, artifact_name: str,
               controller: str | None = None, branch: str | None = None) -> dict:
        _require(isinstance(proof, dict), "Missing native qualification proof")
        key = artifact_key(proof.get("key"))
        _require(key.startswith("releases/qualifications/"), "Receipt is outside immutable qualification namespace")
        body = self.reader.read_bytes(key, require_sha256(proof.get("sha256")))
        return self.github.receipt(self.repository, proof, body, controller=controller or self.controller,
                                   branch=branch or self.branch, workflow=workflow, artifact_name=artifact_name)

    def _cohorts(self, source: dict) -> dict[str, tuple[dict, dict]]:
        """No publication ledger exists: conservatively cover all complete builds <= head.

        Failed requests without build.json are not installable. Fully assembled but
        unpublished builds are included rather than silently losing an old cohort.
        """
        _require(source.get("head") is not None, "Cannot qualify an unpublished source")
        result = {}
        for key in self.store.keys("releases/channel-builds/"):
            if not key.endswith("/build.json"):
                continue
            value = self._object(key)
            request = value.get("request", {})
            if request.get("channel") != source["name"]:
                continue
            _require(request.get("identity") == source["identity"], "Source cohort identity changed")
            _require(type(request.get("sequence")) is int, "Invalid source cohort sequence")
            if request["sequence"] > source["head"]["sequence"]:
                continue
            head = {"buildId": request.get("buildId"), "sequence": request["sequence"],
                    "manifestKey": key, "sha256": hashlib.sha256(self.store.get(key)[0]).hexdigest()}
            _require(key == build_prefix(head["buildId"]) + "build.json", "Cohort manifest key mismatch")
            result[head["buildId"]] = (head, self._manifest(source, head))
        _require(source["head"]["buildId"] in result, "Source head missing from cohort coverage")
        return result

    @staticmethod
    def _native(manifest: dict, platform: str, arch: str) -> dict:
        matches = [p for p in manifest["packages"] if (p["platform"], p["arch"]) == (platform, arch)]
        _require(len(matches) == 1, "Missing supported native package")
        return matches[0]

    @staticmethod
    def _side(record: dict, head: dict, manifest: dict, package: dict) -> dict:
        return {"channel": record["name"], "head": head, "commit": manifest["request"]["commit"],
                "identity": package["identity"], "artifactSha256": package["artifact"]["sha256"]}

    def verify_retirement(self, source: dict, target: dict, qualification: dict) -> bool:
        validate_record(source, repository=self.repository)
        validate_record(target, repository=self.repository)
        _require(source["policy"] == "preview" and target["policy"] == "stable-release"
                 and target["state"] == "active", "Unsupported native retirement policy")
        expected = {"schema": 1, "source": source["name"], "sourceHead": source["head"],
                    "destination": target["name"], "destinationHead": target["head"], "receiverProtocol": 1}
        _require(all(qualification.get(k) == v for k, v in expected.items()), "Qualification head binding mismatch")
        destination = self._manifest(target, target["head"])
        cohorts = self._cohorts(source)
        coverage = qualification.get("coverage")
        _require(isinstance(coverage, list) and bool(coverage), "Missing per-platform native coverage")
        platforms = {(p["platform"], p["arch"]) for _, m in cohorts.values() for p in m["packages"]}
        _require(platforms <= SUPPORTED and len(coverage) == len(platforms), "Incomplete native platform coverage")
        seen = set()
        for row in coverage:
            platform, arch = row.get("platform"), row.get("arch")
            _require((platform, arch) in platforms - seen, "Duplicate or unsupported native coverage")
            seen.add((platform, arch))
            package = self._native(destination, platform, arch)
            _require(row.get("destinationIdentity") == package["identity"], "Destination native identity mismatch")
            entries = row.get("cohorts", [])
            needed = {k for k, (_, m) in cohorts.items() if any((p["platform"], p["arch"]) == (platform, arch) for p in m["packages"])}
            _require(isinstance(entries, list) and len(entries) == len(needed), "Missing source cohort coverage")
            covered = set()
            for entry in entries:
                build_id = entry.get("buildId")
                _require(build_id in needed - covered, "Unknown or duplicate source cohort")
                covered.add(build_id)
                head, manifest = cohorts[build_id]
                native = self._native(manifest, platform, arch)
                _require(entry.get("commit") == manifest["request"]["commit"]
                         and row.get("sourceIdentity") == native["identity"], "Source cohort commit or identity mismatch")
                scenarios = set()
                for proof in entry.get("proofs", []):
                    # The stable state also lives inside the independently authenticated receipt.
                    receipt = self._proof(proof, workflow=".github/workflows/install-e2e.yml",
                                          artifact_name="channel-qualification")
                    facts = {"schema": 1, "kind": "channel-retirement", "platform": platform, "arch": arch,
                             "variant": "bundled", "receiverProtocol": 1, "minimumVersion": qualification.get("minimumVersion"),
                             "source": self._side(source, head, manifest, native),
                             "destination": self._side(target, target["head"], destination, package)}
                    _require(all(receipt.get(k) == v for k, v in facts.items()), "Native acceptance receipt target mismatch")
                    assertions = receipt.get("assertions", {})
                    _require(all(assertions.get(k) is True for k in RETIREMENT_ASSERTIONS), "Incomplete native lifecycle acceptance")
                    scenario = receipt.get("scenario", {})
                    _require(scenario.get("home") in ("shared", "distinct") and scenario.get("route") in ("direct", "via-update"),
                             "Invalid native lifecycle scenario")
                    scenarios.add((scenario.get("route"), scenario.get("stable")))
                _require({("direct", "absent"), ("direct", "running")} <= scenarios,
                         "Every cohort needs direct retirement with absent and running stable")
        return True


def configured_verifier(store, repository: str, base_url: str) -> QualificationVerifier:
    github = GitHubEvidence()
    repository = validate_repository(repository)
    branch = github.json(f"repos/{repository}").get("default_branch")
    _require(isinstance(branch, str) and bool(branch), "Repository has no default controller branch")
    controller = github.json(f"repos/{repository}/commits/{quote(branch, safe='')}").get("sha")
    return QualificationVerifier(store, repository, base_url, controller, branch, github=github)
