#!/usr/bin/env python3
"""Sequence a disposable native journey; the native child has no publisher keys."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from hermes_cli.release_channels import (  # noqa: E402
    ChannelError, canonical_json, channel_key, decode_json, require_commit,
    require_sha256, validate_manifest,
)
from scripts.bundles.release_artifacts import write_appinstaller  # noqa: E402
from scripts.releases import r2  # noqa: E402
from scripts.releases.channel_disposable import require_receiver_scope  # noqa: E402
from scripts.releases.channel_releases import product_identity  # noqa: E402
from scripts.releases.channels import ChannelPublisher, R2ChannelStore  # noqa: E402
from scripts.releases.r2_scope import R2Scope, channel_public_base  # noqa: E402


def admitted_publisher(env: dict[str, str]) -> ChannelPublisher:
    repository = env["GITHUB_REPOSITORY"]
    branch = env["DEFAULT_BRANCH"]
    expected = f"{repository}/.github/workflows/install-e2e.yml@refs/heads/{branch}"
    if (env.get("GITHUB_ACTIONS") != "true" or env.get("RUNNER_ENVIRONMENT") != "github-hosted"
            or env.get("GITHUB_EVENT_NAME") != "workflow_dispatch" or env.get("GITHUB_WORKFLOW_REF") != expected
            or env.get("GITHUB_REF") != f"refs/heads/{branch}"):
        raise ChannelError("Retirement requires the default-branch hosted dispatch")
    controller = require_commit(env["GITHUB_SHA"])
    head = subprocess.check_output(["gh", "api", f"repos/{repository}/commits/{branch}", "--jq", ".sha"],
                                   encoding="utf-8", timeout=60, stdin=subprocess.DEVNULL).strip()
    if head != controller:
        raise ChannelError("Controller no longer matches the default branch")
    store = R2ChannelStore(*r2.credentials(), scope=R2Scope.configured())
    def authorize(action: str, record: dict) -> None:
        require_receiver_scope(publisher)
        if action != "retire" or record["policy"] != "preview":
            raise ChannelError("Native coordinator only retires disposable previews")
    publisher = ChannelPublisher(store, repository, channel_public_base(), authorize=authorize)
    require_receiver_scope(publisher)
    return publisher


def prepare(publisher: ChannelPublisher, journey: dict) -> None:
    require_receiver_scope(publisher)
    if journey["repository"] != publisher.repository or journey["controllerCommit"] != os.environ["GITHUB_SHA"]:
        raise ChannelError("Journey controller authority mismatch")
    for slot in ("A", "S", "T", *(["B"] if journey["route"] == "via-update" else [])):
        side = journey[slot]
        manifest = decode_json(publisher.reader.read_bytes(side["head"]["manifestKey"], side["head"]["sha256"]))
        request = manifest["request"]
        record = publisher._read(request["channel"])[0]
        validate_manifest(manifest, {**record, "head": side["head"]}, publisher.public_base)
        if manifest != side["manifest"] or publisher.request(request["buildId"]) != request:
            raise ChannelError("Journey differs from immutable admitted build")
        if slot in {"S", "T"} and (request["identity"] != product_identity(request["releaseTag"])
                or request.get("receiverCandidate") is not True or manifest.get("receiverProtocol") != 1):
            raise ChannelError("S/T require scoped official-identity receiver candidates")
    source = publisher._read(journey["A"]["manifest"]["request"]["channel"])[0]
    target = publisher._read("stable")[0]
    if source["state"] != "active" or target["head"] != journey["S"]["head"]:
        raise ChannelError("Journey requires active preview and exact S head")
    if journey["route"] == "via-update" and source["head"] not in (journey["A"]["head"], journey["B"]["head"]):
        raise ChannelError("A to B requires exact A or already-advertised B; no rewind is permitted")
    stable_feed(publisher, journey["S"], journey["platform"])


def stable_feed(publisher: ChannelPublisher, side: dict, platform: str) -> None:
    """Use the existing native descriptor writer, always under the physical scope."""
    require_receiver_scope(publisher)
    pkg = next(row for row in side["manifest"]["packages"] if row["platform"] == platform)
    key = f"releases/{platform}/stable/" + ("stable-mac.yml" if platform == "darwin" else "stable.appinstaller")
    if platform == "darwin":
        body = publisher.reader.read_bytes(pkg["feed"]["key"])
        feed = decode_json(body)
        if feed["version"] != side["manifest"]["request"]["version"] or not feed.get("files"):
            raise ChannelError("Stable feed version/files differ")
        if any(not item["url"].startswith(publisher.public_base + "/releases/") for item in feed["files"]):
            raise ChannelError("Stable feed escaped disposable authority")
    else:
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "stable.appinstaller"
            write_appinstaller(file, identity=pkg["identity"], publisher=pkg["publisher"], version=pkg["version"],
                               self_uri=f"{publisher.public_base}/{key}",
                               artifact_uri=f"{publisher.public_base}/{pkg['artifact']['key']}", update_policy="pinned")
            body = file.read_bytes()
    prior = publisher.store.get(key)
    publisher.store.put(key, body, prior[1] if prior else None)
    if publisher.reader.read_bytes(key) != body:
        raise ChannelError("Stable descriptor public readback differs")


def advance(publisher: ChannelPublisher, journey: dict, phase: str) -> dict:
    require_receiver_scope(publisher)
    source_name = journey["A"]["manifest"]["request"]["channel"]
    if phase == "retire":
        if publisher._read("stable")[0]["head"] != journey["S"]["head"]:
            raise ChannelError("Pinned S changed before retirement")
        return publisher.retire(source_name, "stable", journey["minimumVersion"])
    if phase == "preview-update" and journey["route"] == "via-update":
        name, before, after = source_name, journey["A"]["head"], journey["B"]["head"]
    elif phase == "stable-update":
        name, before, after = "stable", journey["S"]["head"], journey["T"]["head"]
    else:
        raise ChannelError("Unexpected journey stage")
    current, etag = publisher._read(name)
    # The ordinary B build may have already advertised its head. A is installed
    # from pinned bytes and the driver still waits for OLD chat before clicking.
    if phase == "preview-update" and current["state"] == "active" and current["head"] == after:
        return current
    if current["state"] != "active" or current["head"] != before or after["sequence"] >= current["nextSequence"]:
        raise ChannelError("Head changed or target was not allocated")
    if phase == "stable-update":
        stable_feed(publisher, journey["T"], journey["platform"])
    result = {**current, "head": after, "revision": current["revision"] + 1}
    publisher._write(channel_key(name), result, etag)
    return result


def run(args: argparse.Namespace) -> None:
    body = args.manifest.read_bytes()
    if hashlib.sha256(body).hexdigest() != require_sha256(args.sha256):
        raise ChannelError("Controller input digest mismatch")
    journey = json.loads(body)
    if sys.platform not in {"darwin", "win32"} or journey["platform"] != sys.platform or journey["arch"] != args.arch:
        raise ChannelError("Native host/architecture must match journey")
    args.work = args.work.resolve()
    if not args.work.is_relative_to(Path(os.environ["RUNNER_TEMP"]).resolve()):
        raise ChannelError("Native work must remain under RUNNER_TEMP")
    publisher = admitted_publisher(dict(os.environ))
    args.work.mkdir()
    # Validate/stage without credentials before making any journey writes.
    environment = {key: value for key, value in os.environ.items()
                   if not any(word in key.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"))
                   and not key.upper().startswith(("CLOUDFLARE", "AWS_", "R2_", "HERMES_", "PYTHON", "VIRTUAL_ENV", "NODE_", "ELECTRON_"))}
    subprocess.run(["node", str(ROOT / "tests/install/e2e-assets/channel-retirement-inputs.mjs"),
                    "--manifest", str(args.manifest), "--manifest-sha256", args.sha256, "--platform", sys.platform,
                    "--arch", args.arch, "--out", str(args.work / "inputs")], env=environment, check=True, stdin=subprocess.DEVNULL)
    prepare(publisher, journey)
    driver = ROOT / "tests/install" / ("macos-bundled-e2e.sh" if sys.platform == "darwin" else "windows-bundled-e2e.ps1")
    command = (["bash", str(driver), "--phase", "retirement", "--retirement-work", str(args.work), "--arch", args.arch]
               if sys.platform == "darwin" else ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(driver), "-RetirementWork", str(args.work), "-Arch", args.arch])
    child = subprocess.Popen(command, env=environment, stdin=subprocess.DEVNULL)
    try:
        for phase in (["preview-update"] if journey["route"] == "via-update" else []) + ["retire", "stable-update"]:
            request = args.work / f"{phase}.request.json"
            deadline = time.monotonic() + 1200
            while not request.exists():
                if child.poll() is not None or time.monotonic() > deadline:
                    raise ChannelError(f"Native driver did not reach {phase}")
                time.sleep(0.5)
            if json.loads(request.read_text()) != {"phase": phase, "controllerCommit": journey["controllerCommit"]}:
                raise ChannelError("Native stage gate mismatch")
            result = advance(publisher, journey, phase)
            temporary = args.work / f"{phase}.ready.partial"
            temporary.write_bytes(canonical_json(result))
            temporary.replace(args.work / f"{phase}.ready.json")
        if child.wait(timeout=1200) != 0:
            raise ChannelError("Native journey failed")
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=30)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--arch", choices=("arm64", "x64"), required=True)
    run(parser.parse_args())
