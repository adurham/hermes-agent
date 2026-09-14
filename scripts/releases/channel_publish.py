"""Admit channel requests and publish only complete, native-smoked build manifests."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
import xml.etree.ElementTree as ET
import zipfile

from hermes_cli.release_channels import ChannelReader, build_prefix, canonical_json, decode_json, require_sha256, validate_request
from scripts.releases import commit_build, handoff, r2

NATIVE_LEGS = ("darwin-arm64", "darwin-x64", "win32-arm64", "win32-x64", "windows-universal")
REQUIRED_JOBS = ("validate", "build-darwin", "build-win32", "assemble-win32-bundle",
                 "smoke-darwin", "smoke-win32", "smoke-win32-universal")


def read_request(build_id: str, digest: str, public_base: str, repository: str) -> dict:
    require_sha256(digest)
    reader = ChannelReader(public_base, repository=repository)
    body = reader.read_bytes(build_prefix(build_id) + "request.json", digest)
    request = validate_request(decode_json(body), repository=repository, base_url=public_base)
    if request["buildId"] != build_id:
        raise ValueError("Channel request build ID differs from its key")
    return request


def require_success(needs: dict) -> None:
    """Skipped, absent and empty native matrices cannot qualify publication."""
    if not isinstance(needs, dict):
        raise ValueError("Missing workflow results")
    failed = [name for name in REQUIRED_JOBS
              if not isinstance(needs.get(name), dict) or needs[name].get("result") != "success"]
    if failed:
        raise ValueError("Channel publication requires successful jobs: " + ", ".join(failed))


def admit(request: dict, env: dict[str, str]) -> dict[str, str]:
    if (env.get("BUILD_COMMIT") or env.get("TAG") or env.get("RELEASE_PHASE")
            or env.get("TERMUX_UPGRADE_FROM_TAG") or env.get("TERMUX_ONLY") == "true"
            or env.get("UPLOAD_RELEASE", "false") != "false"
            or env.get("BUNDLE_ENV_JSON", "") not in ("", "{}")):
        raise ValueError("Channel requests cannot select one-off, release, Termux or bundle overrides")
    admitted = commit_build.admit({**env, "BUILD_COMMIT": request["commit"],
                                   "BUNDLE_ENV_JSON": json.dumps(request["bundleEnv"])})
    if admitted["payload-version"] != request["sourceVersion"]:
        raise ValueError("Channel request source version differs from admitted commit")
    if request.get("controllerCommit", env.get("GITHUB_SHA")) != env.get("GITHUB_SHA"):
        raise ValueError("Channel request controller differs from trusted workflow revision")
    return {"sha": request["commit"], "channel": request["channel"], "payload-version": request["version"]}


def _metadata(root: Path, request: dict, platform: str, arch: str) -> dict:
    native_platform = {"darwin": "macos", "win32": "windows"}[platform]
    row = json.loads((root / f"metadata-{native_platform}-{arch}.json").read_text(encoding="utf-8-sig"))
    identity_key, version_key = (("appId", "version") if platform == "darwin"
                                 else ("msixAppIdWithOrg", "windowsVersion"))
    if (row.get("request") != request or row.get("commit") != request["commit"]
            or row.get("arch") != arch or row.get("platform") != native_platform
            or row.get("identity") != request["identity"][identity_key]
            or row.get("version") != request[version_key]):
        raise ValueError("Native metadata differs from the admitted channel request")
    if platform == "win32" and row.get("applicationId") != request["identity"]["appNamePascal"]:
        raise ValueError("Windows application ID differs from channel identity")
    return row


def assemble(request: dict, root: Path, *, needs: dict) -> tuple[dict, list[Path]]:
    """Consume downloaded receipts; derive immutable feeds without another build."""
    from scripts.bundles.release_artifacts import single, write_appinstaller

    require_success(needs)
    validate_request(request)
    prefix = build_prefix(request["buildId"])
    files: dict[str, dict] = {}
    for name in NATIVE_LEGS:
        receipt = json.loads((root / handoff.receipt_name(name)).read_text(encoding="utf-8-sig"))
        listed = handoff.validate_channel_receipt(receipt, request, name)
        product = request["identity"]["artifactNamePascal"]
        if name == "windows-universal":
            expected = {f"{product}-{request['windowsVersion']}-win.msixbundle"}
        else:
            platform, arch = name.split("-")
            native = "macos" if platform == "darwin" else "windows"
            expected = {f"metadata-{native}-{arch}.json"}
            if platform == "darwin":
                expected.update(f"{product}-{request['version']}-mac-{arch}.{suffix}"
                                for suffix in ("zip", "dmg", "zip.blockmap", "dmg.blockmap"))
            else:
                expected.add(f"{product}-{request['version']}-win-{arch}.msix")
        if not expected.issubset({row["path"] for row in listed}):
            raise ValueError(f"Channel handoff {name} omits required native files")
        for row in listed:
            if row["path"] in files and files[row["path"]] != row:
                raise ValueError("Channel handoffs disagree on artifact receipts")
            file = root / row["path"]
            if file.stat().st_size != row["size"] or r2.file_sha256(file) != row["sha256"]:
                raise ValueError("Channel artifact differs from its receipt")
            files[row["path"]] = row
    name = request["identity"]["artifactNamePascal"]
    bundle_name = f"{name}-{request['windowsVersion']}-win.msixbundle"
    if bundle_name not in files:
        raise ValueError("Missing Windows universal bundle")
    windows = [_metadata(root, request, "win32", arch) for arch in ("arm64", "x64")]
    for field in ("publisher", "version", "identity", "applicationId"):
        if not windows[0].get(field) or windows[0][field] != windows[1].get(field):
            raise ValueError(f"Windows native metadata disagrees on {field}")
    with zipfile.ZipFile(root / bundle_name) as archive:
        manifest = ET.fromstring(archive.read("AppxMetadata/AppxBundleManifest.xml"))
        native = manifest.find("{*}Identity")
        for attr, field in (("Name", "identity"), ("Publisher", "publisher"), ("Version", "version")):
            if native is None or native.get(attr) != windows[0][field]:
                raise ValueError("Windows universal envelope differs from native metadata")
        architectures = sorted(p.get("Architecture", "") for p in manifest.findall("{*}Packages/{*}Package")
                               if p.get("Type") == "application")
        if architectures != ["arm64", "x64"]:
            raise ValueError("Universal bundle must contain both native architectures")
    descriptor = root / "win32" / "stable.appinstaller"
    base = request["publicBase"]
    write_appinstaller(descriptor, identity=windows[0]["identity"], publisher=windows[0]["publisher"],
                       version=request["windowsVersion"], self_uri=f"{base}/{prefix}win32/stable.appinstaller",
                       artifact_uri=f"{base}/{prefix}{bundle_name}", update_policy="pinned")
    packages = []
    mac_files = []
    for platform in ("darwin", "win32"):
        for arch in ("arm64", "x64"):
            metadata = _metadata(root, request, platform, arch)
            filename = f"{name}-{request['version']}-mac-{arch}.zip" if platform == "darwin" else bundle_name
            row = files[filename]
            signing_field = "teamId" if platform == "darwin" else "publisher"
            if not metadata.get(signing_field):
                raise ValueError("Missing native signing identity")
            packages.append({"platform": platform, "arch": arch, "variant": "bundled",
                             "version": metadata["version"], "identity": metadata["identity"],
                             signing_field: metadata[signing_field],
                             "artifact": {"key": prefix + filename, "sha256": row["sha256"], "size": row["size"]},
                             "feed": {"key": prefix + ("darwin/stable-mac.yml" if platform == "darwin"
                                                       else "win32/stable.appinstaller"), "channel": "stable"}})
            if platform == "darwin":
                for extension in ("zip", "dmg"):
                    artifact = f"{name}-{request['version']}-mac-{arch}.{extension}"
                    if artifact not in files or artifact + ".blockmap" not in files:
                        raise ValueError("Missing macOS native package or blockmap")
                    with (root / artifact).open("rb") as stream:
                        sha512 = base64.b64encode(hashlib.file_digest(stream, "sha512").digest()).decode("ascii")
                    mac_files.append({"url": base + "/" + prefix + artifact, "sha512": sha512, "size": files[artifact]["size"]})
    # JSON is valid YAML. A neutral filename avoids electron-updater prerelease-name parsing.
    feed = root / "darwin" / "stable-mac.yml"
    feed.parent.mkdir(parents=True, exist_ok=True)
    first_zip = single(f for f in mac_files if f["url"].endswith("-arm64.zip"))
    feed.write_text(json.dumps({"version": request["version"], "files": mac_files,
                                "path": first_zip["url"], "sha512": first_zip["sha512"]}, indent=2) + "\n", encoding="utf-8")
    return {"schema": 1, "request": request, "packages": packages}, [feed, descriptor]


def publish(request: dict, root: Path, *, needs: dict, publisher) -> dict:
    require_success(needs)
    # The protocol publisher rechecks policy/identity/retirement at CAS time.
    if publisher.request(request["buildId"]) != request:
        raise ValueError("Publisher request differs from pinned admission")
    current = publisher._read(request["channel"])
    if current is None:
        raise ValueError("Publication channel not found")
    publisher._preview("promote", current[0])
    handoff.fetch_channel_build(request, list(NATIVE_LEGS), root, public_base=request["publicBase"])
    manifest, feeds = assemble(request, root, needs=needs)
    prefix = build_prefix(request["buildId"])
    for file in feeds:
        key = prefix + file.relative_to(root).as_posix()
        r2.put(tag="", key=key, file=str(file), key_is_full=True, immutable=True)
        r2.download_public_object(request["publicBase"], key, root / "verified" / file.name,
                                  expected_size=file.stat().st_size, expected_sha256=r2.file_sha256(file))
    publisher.store.put(prefix + "build.json", canonical_json(manifest))
    return publisher.promote(request["buildId"])


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["request", "admit", "publish"])
    parser.add_argument("--build-id", default=os.environ.get("CHANNEL_BUILD", ""))
    parser.add_argument("--request-sha256", default=os.environ.get("CHANNEL_REQUEST_SHA256", ""))
    parser.add_argument("--public-base", default=os.environ.get("CLOUDFLARE_R2_PUBLIC_URL", ""))
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--out", type=Path)
    parser.add_argument("--root", type=Path)
    args = parser.parse_args(argv)
    request = read_request(args.build_id, args.request_sha256, args.public_base, args.repository)
    if args.command == "admit":
        values = admit(request, dict(os.environ))
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
            stream.write("".join(f"{key}={value}\n" for key, value in values.items()))
    elif args.command == "publish":
        from scripts.releases.channels import ChannelPublisher, R2ChannelStore
        needs = json.loads(os.environ.get("RELEASE_NEEDS", "{}"))
        require_success(needs)
        # Repeat maintainer/default-controller admission on promotion, including reruns.
        admit(request, dict(os.environ))
        def authorize(action: str, record: dict) -> None:
            if action != "promote" or record["policy"] != "preview":
                raise ValueError("This workflow only promotes preview channel builds")
            admit(request, dict(os.environ))

        def qualified(pinned: dict, manifest: dict) -> bool:
            # Re-evaluate exact downloaded receipt bytes on every CAS attempt.
            expected, _feeds = assemble(pinned, root, needs=needs)
            return pinned == request and manifest == expected

        publisher = ChannelPublisher(R2ChannelStore(*r2.credentials()), args.repository, args.public_base,
                                     authorize=authorize, verify_build=qualified)
        if args.root:
            root = args.root
            result = publish(request, args.root, needs=needs, publisher=publisher)
        else:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                result = publish(request, root, needs=needs, publisher=publisher)
        print(json.dumps(result, sort_keys=True))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(request, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
