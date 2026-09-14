"""Advance protected R2 heads from the release transaction's accepted native bytes."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile

from hermes_cli.release_channels import (
    ChannelError, build_prefix, canonical_json, validate_identity, validate_manifest,
)
from scripts.releases import handoff, r2, stable
from scripts.releases.channels import ChannelPublisher, R2ChannelStore

NATIVE_LEGS = ("darwin-arm64", "darwin-x64", "win32-arm64", "win32-x64", "windows-universal")
STABLE_NEEDS = ("admit", "ci", "docker", "acceptance", "candidates", "publication", "promote-docker", "promote-bundles")
CANARY_NEEDS = ("build-win32", "build-darwin", "build-linux", "builds-table", "assemble-win32-bundle",
                "smoke-darwin", "smoke-win32", "smoke-win32-universal", "publish-win32-updater", "publish-darwin-updater")


def select_channel(publisher: ChannelPublisher, policy: str) -> str:
    """R2 records select the name; historical labels are first-rollout defaults only."""
    matches = [record for record in publisher.list() if record["policy"] == policy]
    if len(matches) > 1:
        raise ChannelError("Ambiguous protected policy in R2; select one authority before publishing")
    if matches:
        if matches[0]["state"] != "active":
            raise ChannelError("Protected channel is retired")
        return matches[0]["name"]
    return {"stable-release": "stable", "canary-release": "canary"}[policy]


def product_identity(tag: str, run=subprocess.check_output) -> dict:
    """Consume the packager's identity, not a Python copy of its naming rules."""
    env = dict(os.environ, HERMES_DESKTOP_VARIANT="bundled", HERMES_PAYLOAD_TAG=tag)
    for key in ("HERMES_BUILD_COMMIT", "_HERMES_CHANNEL_REQUEST_JSON"):
        env.pop(key, None)
    raw = run(["node", "-e", "console.log(JSON.stringify(require('./apps/desktop/product-identity.cjs')))"],
              env=env, text=True, encoding="utf-8", timeout=30)
    identity = json.loads(raw)
    # This token reserves existing native identity; it does not create a new app.
    identity = {key: value for key, value in identity.items() if key not in {"store", "light", "channel"}}
    identity["token"] = hashlib.sha256(canonical_json(identity)).hexdigest()[:16]
    return validate_identity(identity)


def read_native_receipts(root: Path, tag: str, commit: str) -> dict:
    files = {}
    for name in NATIVE_LEGS:
        receipt = json.loads((root / handoff.receipt_name(name)).read_text(encoding="utf-8-sig"))
        for row in handoff.validate_receipt(receipt, tag, commit, name):
            if row["path"] in files and files[row["path"]] != row:
                raise ChannelError("Native release receipts disagree")
            file = root / row["path"]
            # Only native metadata, distributables and blockmaps are downloaded.
            if file.is_file() and (file.stat().st_size != row["size"] or r2.file_sha256(file) != row["sha256"]):
                raise ChannelError("Native artifact differs from its receipt")
            files[row["path"]] = row
    rows = []
    for platform in ("macos", "windows"):
        for arch in ("arm64", "x64"):
            name = f"metadata-{platform}-{arch}.json"
            if name not in files or not (root / name).is_file():
                raise ChannelError(f"Missing receipt-bound native metadata: {name}")
            row = json.loads((root / name).read_text(encoding="utf-8-sig"))
            if any(row.get(k) != v for k, v in {"platform": platform, "arch": arch, "tag": tag, "commit": commit}.items()):
                raise ChannelError("Native metadata release identity mismatch")
            rows.append(row)
    return {"packages": rows, "files": files}


def assemble(request: dict, native: dict, root: Path) -> tuple[dict, list[Path]]:
    from scripts.bundles.release_artifacts import single, write_appinstaller

    files = native["files"]
    tag_prefix = f"releases/tag/{request['releaseTag']}/"
    prefix = build_prefix(request["buildId"])
    base = request["publicBase"]
    windows = [row for row in native["packages"] if row["platform"] == "windows"]
    for field in ("identity", "publisher", "version", "applicationId"):
        if len(windows) != 2 or not windows[0].get(field) or windows[0][field] != windows[1].get(field):
            raise ChannelError(f"Windows native metadata disagrees on {field}")
    if windows[0]["applicationId"] != request["identity"]["appNamePascal"]:
        raise ChannelError("Windows application identity mismatch")
    bundle_name = single(name for name in files if name.endswith(".msixbundle") and not name.startswith("Store-"))
    with zipfile.ZipFile(root / bundle_name) as archive:
        envelope = ET.fromstring(archive.read("AppxMetadata/AppxBundleManifest.xml"))
        identity = envelope.find("{*}Identity")
        if identity is None or any(identity.get(attr) != windows[0][field] for attr, field in
                                   (("Name", "identity"), ("Publisher", "publisher"), ("Version", "version"))):
            raise ChannelError("Universal bundle differs from native metadata")
        if sorted(p.get("Architecture", "") for p in envelope.findall("{*}Packages/{*}Package")
                  if p.get("Type") == "application") != ["arm64", "x64"]:
            raise ChannelError("Universal bundle must cover both architectures")
    descriptor = root / "win32" / "stable.appinstaller"
    write_appinstaller(descriptor, identity=windows[0]["identity"], publisher=windows[0]["publisher"],
                       version=windows[0]["version"], self_uri=f"{base}/{prefix}win32/stable.appinstaller",
                       artifact_uri=f"{base}/{tag_prefix}{bundle_name}", update_policy="pinned")
    packages, mac_files = [], []
    for row in native["packages"]:
        mac = row["platform"] == "macos"
        filename = row["filename"] if mac else bundle_name
        receipt = files.get(filename)
        if receipt is None or not (root / filename).is_file():
            raise ChannelError("Missing receipt-bound native artifact")
        signing = "teamId" if mac else "publisher"
        packages.append({"platform": "darwin" if mac else "win32", "arch": row["arch"], "variant": "bundled",
                         "identity": row["identity"], "version": row["version"], signing: row.get(signing),
                         "artifact": {"key": tag_prefix + filename, "sha256": receipt["sha256"], "size": receipt["size"]},
                         "feed": {"key": prefix + ("darwin/stable-mac.yml" if mac else "win32/stable.appinstaller"),
                                  "channel": "stable"}})
        if mac:
            for name in (filename, filename.removesuffix(".zip") + ".dmg"):
                if any(item not in files or not (root / item).is_file() for item in (name, name + ".blockmap")):
                    raise ChannelError("Missing receipt-bound macOS package or blockmap")
                with (root / name).open("rb") as stream:
                    digest = base64.b64encode(hashlib.file_digest(stream, "sha512").digest()).decode("ascii")
                mac_files.append({"url": f"{base}/{tag_prefix}{name}", "sha512": digest, "size": files[name]["size"]})
    feed = root / "darwin" / "stable-mac.yml"
    feed.parent.mkdir(parents=True, exist_ok=True)
    first = single(row for row in mac_files if row["url"].endswith("-arm64.zip"))
    feed.write_bytes(canonical_json({"version": request["version"], "files": mac_files,
                                     "path": first["url"], "sha512": first["sha512"]}))
    manifest = {"schema": 1, "request": request, "packages": packages}
    record = {"name": request["channel"], "repository": request["repository"], "identity": request["identity"],
              "policy": "canary-release" if "-canary." in request["releaseTag"] else "stable-release", "head": None}
    validate_manifest(manifest, record, base)
    return manifest, [feed, descriptor]


def match_accepted_packages(manifest: dict, accepted: dict) -> None:
    by_target = {(row["platform"], row["arch"]): row for row in accepted["packages"]}
    for package in manifest["packages"]:
        platform = "macos" if package["platform"] == "darwin" else "windows"
        row = by_target.get((platform, package["arch"]), {})
        signing = "teamId" if platform == "macos" else "publisher"
        if (any(row.get(key) != package.get(key) for key in ("identity", "version", signing))
                or row.get("artifact") != {"url": manifest["request"]["publicBase"] + "/" + package["artifact"]["key"],
                                           "sha256": package["artifact"]["sha256"]}):
            raise ChannelError("Protected package differs from the accepted release")


def admit_transaction(policy: str, env: dict, *, run=stable.output) -> tuple[str, str]:
    """A callable CLI is not permission to bypass the existing workflow gate."""
    from scripts.releases.semver import is_valid_version
    from hermes_cli.release_channels import require_commit, validate_repository

    repository = validate_repository(env.get("GITHUB_REPOSITORY"))
    tag = env.get("RELEASE_TAG", "")
    if not tag.startswith("v") or not is_valid_version(tag[1:]):
        raise ChannelError("Invalid protected release tag")
    if (policy == "canary-release") != ("-canary." in tag):
        raise ChannelError("Protected release policy/tag mismatch")
    if env.get("GITHUB_ACTIONS") != "true" or env.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
        raise ChannelError("Protected heads require the accepted release workflow")
    default = ""
    if policy == "stable-release":
        expected = f"{repository}/.github/workflows/stable-release.yml@refs/tags/{tag}"
        required = STABLE_NEEDS
    elif policy == "canary-release":
        default = run(["gh", "api", f"repos/{repository}", "--jq", ".default_branch"])
        expected = f"{repository}/.github/workflows/desktop-bundled-release.yml@refs/heads/{default}"
        required = CANARY_NEEDS
    else:
        raise ChannelError("Invalid protected release policy")
    if env.get("GITHUB_WORKFLOW_REF") != expected:
        raise ChannelError("Protected publication requires its existing release workflow")
    stable.require_success(json.loads(env.get("RELEASE_NEEDS", "{}")), list(required))
    if policy == "stable-release":
        tag, commit = stable.check_tag(env, run=run)
    else:
        commit = require_commit(env.get("RELEASE_COMMIT"))
        actual = run(["git", "rev-parse", f"refs/tags/{tag}^{{commit}}"])
        remote = dict(line.split()[::-1] for line in run(
            ["git", "ls-remote", "origin", f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"] ).splitlines())
        if actual != commit or remote.get(f"refs/tags/{tag}^{{}}", remote.get(f"refs/tags/{tag}")) != commit:
            raise ChannelError("Canary release tag moved")
        run(["git", "merge-base", "--is-ancestor", commit, f"origin/{default}"])
    release = json.loads(run(["gh", "release", "view", tag, "--repo", repository,
                              "--json", "tagName,isDraft,isPrerelease"]))
    if (release.get("tagName") != tag or release.get("isDraft") is not False
            or release.get("isPrerelease") is not (policy == "canary-release")):
        raise ChannelError("Protected head requires the published GitHub release transaction")
    return tag, commit


def accepted_stable(publisher: ChannelPublisher, env: dict, tag: str, commit: str) -> dict:
    from hermes_cli.release_channels import decode_json, require_sha256

    digest = require_sha256(env.get("CANDIDATE_MANIFEST_SHA256"))
    key = f"releases/tag/{tag}/release-candidates.json"
    if env.get("CANDIDATE_MANIFEST_URL") != publisher.public_base + "/" + key:
        raise ChannelError("Accepted candidate URL differs from release archive")
    candidate = decode_json(publisher.reader.read_bytes(key, digest))
    stable.validate_candidates(candidate, tag, commit, publisher.public_base)
    accepted = publisher.store.get("releases/stable/release-candidates.json")
    if accepted is None or decode_json(accepted[0]) != candidate:
        raise ChannelError("Stable accepted manifest transaction has not completed")
    if decode_json(publisher.reader.read_bytes("releases/stable/release-candidates.json")) != candidate:
        raise ChannelError("Stable accepted manifest is not publicly visible")
    return candidate


def publish_release(policy: str, env: dict, root: Path) -> dict:
    from scripts.releases.commit_build import version_at

    tag, commit = admit_transaction(policy, env)
    publisher = ChannelPublisher(R2ChannelStore(*r2.credentials()), env["GITHUB_REPOSITORY"],
                                 r2.public_base_url(), authorize=lambda action, record: admit_transaction(policy, env))
    name = select_channel(publisher, policy)
    identity = product_identity(tag)
    current = publisher._read(name)
    if current:
        # Explicit bootstrap may have reserved another opaque token for the same app.
        identity["token"] = current[0]["identity"]["token"]
        if identity != current[0]["identity"]:
            raise ChannelError("Protected R2 identity differs from the existing product")
    accepted = accepted_stable(publisher, env, tag, commit) if policy == "stable-release" else None
    handoff.fetch(tag, commit, list(NATIVE_LEGS), root,
                  ["metadata-*.json", "*.zip", "*.dmg", "*.blockmap", "*.msixbundle"], public_base=publisher.public_base)
    native = read_native_receipts(root, tag, commit)
    windows = next(row for row in native["packages"] if row["platform"] == "windows")

    def release_gate(request: dict) -> bool:
        if admit_transaction(policy, env) != (tag, commit):
            return False
        if policy == "stable-release":
            return accepted_stable(publisher, env, tag, commit) == accepted
        return True

    request = publisher.allocate_protected(name, commit, version_at(None, commit), release_tag=tag,
                                           version=tag[1:], windows_version=windows["version"],
                                           identity=identity, policy=policy, release_gate=release_gate)
    manifest, feeds = assemble(request, native, root)
    if accepted is not None:
        match_accepted_packages(manifest, accepted)
    prefix = build_prefix(request["buildId"])
    for file in feeds:
        key = prefix + file.relative_to(root).as_posix()
        r2.put(tag=tag, key=key, file=str(file), key_is_full=True, immutable=True)
        if publisher.reader.read_bytes(key) != file.read_bytes():
            raise ChannelError("Immutable protected feed public read-back differs")
    publisher._write(prefix + "build.json", manifest)

    def qualified(pinned: dict, actual: dict) -> bool:
        # Rehash local receipt-bound bytes rather than trusting caller-supplied claims.
        expected, _ = assemble(pinned, read_native_receipts(root, tag, commit), root)
        if accepted is not None:
            match_accepted_packages(expected, accepted)
        return pinned == request and actual == expected

    publisher.verify_build = qualified
    return publisher.promote_protected(request["buildId"], policy=policy, release_gate=release_gate)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=("stable", "canary"), help="Protected release policy, not a channel name")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory() as directory:
        result = publish_release(args.role + "-release", dict(os.environ), Path(directory))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
