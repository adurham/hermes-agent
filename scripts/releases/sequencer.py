"""Discover and reconcile stable releases in version order.

The planner is pure. Production discovery reads only remote annotated tags,
GitHub releases/runs, and the protected R2 head; execution flips exact release
IDs before advancing each eligible head oldest-first.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hermes_cli.update_channel import STABLE_TAG_RE

CLAIM_TAG_RE = re.compile(r"^(v(?:0|[1-9]\d{0,2})\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))-rc$")
SHA256 = re.compile(r"[a-f0-9]{64}")
DOCKER_DIGEST = re.compile(r"sha256:[a-f0-9]{64}")
MAX_ATTEMPTS = 3
RETRY_BACKOFF = timedelta(minutes=15)


def _key(version: str) -> tuple[int, int, int]:
    major, minor, patch = version.split(".")
    return int(major), int(minor), int(patch)


def plan(claims: list[dict], *, head: str | None,
         requested_version: str | None = None) -> list[dict]:
    """Return eligible draft flips followed by each ordered head advance."""
    ordered = sorted(claims, key=lambda claim: _key(claim["version"]))
    head_key = _key(head) if head is not None else None
    flips: list[dict] = []
    advances: list[dict] = []
    flush_green_chain = False

    for index, claim in enumerate(ordered):
        version = claim["version"]
        version_key = _key(version)
        state = claim["state"]

        if head_key is not None and version_key <= head_key:
            if state in {"published", "burned"}:
                continue
            raise ValueError(f"refusing to move the head backwards from {head}")

        if state == "published":
            advances.append({"advance": version})
            continue
        if state == "burned":
            continue
        if state == "running":
            break
        if state != "green":
            raise ValueError(f"unknown claim state {state!r}")

        has_later_live_claim = any(
            later["state"] != "burned" for later in ordered[index + 1:]
        )
        eligible = (
            claim.get("autopublish", False)
            or version == requested_version
            or has_later_live_claim
            or flush_green_chain
        )
        if not eligible:
            break
        flips.append({"flip": version})
        advances.append({"advance": version})
        flush_green_chain = flush_green_chain or has_later_live_claim

    return flips + advances


def output(argv: list[str]) -> str:
    return subprocess.check_output(argv, text=True, encoding="utf-8").strip()


def _pages(argv: list[str], run=output) -> list:
    pages = json.loads(run(["gh", "api", "--paginate", "--slurp", *argv]))
    if not isinstance(pages, list):
        raise ValueError("GitHub pagination returned an invalid response")
    return pages


def _release_rows(repository: str, run=output) -> list[dict]:
    rows = []
    for page in _pages([f"repos/{repository}/releases?per_page=100"], run):
        if not isinstance(page, list):
            raise ValueError("GitHub releases response is invalid")
        rows.extend(page)
    return rows


def _workflow_runs(repository: str, run=output) -> list[dict]:
    rows = []
    endpoint = f"repos/{repository}/actions/workflows/stable-release.yml/runs?event=workflow_dispatch&per_page=100"
    for page in _pages([endpoint], run):
        if not isinstance(page, dict) or not isinstance(page.get("workflow_runs"), list):
            raise ValueError("GitHub workflow runs response is invalid")
        rows.extend(page["workflow_runs"])
    return rows


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Workflow timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def classify_runs(runs: list[dict]) -> tuple[str, dict | None]:
    """Keep failed claims live until two failed-job retries are exhausted."""
    if not runs:
        return "burned", None
    run_ids = {row.get("id") for row in runs}
    if len(run_ids) != 1 or None in run_ids:
        raise ValueError("Stable claim owns multiple workflow runs")
    latest = max(runs, key=lambda row: row.get("run_attempt", 0))
    attempt = latest.get("run_attempt")
    if not isinstance(attempt, int) or attempt < 1:
        raise ValueError("Stable workflow run attempt is invalid")
    if latest.get("status") != "completed":
        return "running", None
    if latest.get("conclusion") == "success":
        raise ValueError("Stable workflow succeeded without a final tag")
    if attempt >= MAX_ATTEMPTS:
        return "burned", None
    updated_at = latest.get("updated_at")
    if not isinstance(updated_at, str):
        raise ValueError("Failed stable workflow has no completion time")
    return "running", {
        "run_id": latest["id"],
        "attempt": attempt,
        "due_at": _utc(updated_at) + RETRY_BACKOFF,
    }


def retry_due(records: list[dict], *, now: datetime | None = None) -> list[dict]:
    """Return bounded failed-job retries whose backoff has elapsed."""
    now = now or datetime.now(timezone.utc)
    retries = []
    for record in records:
        retry = record.get("retry")
        if retry is not None and retry["due_at"] <= now:
            retries.append({
                "version": record["version"], "run_id": retry["run_id"],
                "attempt": retry["attempt"] + 1,
            })
    return retries


def _remote_tags(run=output) -> dict[str, dict[str, str]]:
    refs: dict[str, dict[str, str]] = {}
    for line in run(["git", "ls-remote", "--tags", "origin", "refs/tags/v*"]).splitlines():
        sha, ref = line.split()
        peeled = ref.endswith("^{}")
        tag = ref.removeprefix("refs/tags/").removesuffix("^{}")
        if STABLE_TAG_RE.fullmatch(tag) or CLAIM_TAG_RE.fullmatch(tag):
            refs.setdefault(tag, {})["commit" if peeled else "object"] = sha
    return refs


def _tag_message(tag: str, expected_object: str, run=output) -> dict:
    local_object = run(["git", "rev-parse", f"refs/tags/{tag}"])
    if local_object != expected_object or run(["git", "cat-file", "-t", local_object]) != "tag":
        raise ValueError(f"{tag} differs from its remote annotated object")
    try:
        message = json.loads(run(["git", "tag", "-l", tag, "--format=%(contents)"]))
    except json.JSONDecodeError as error:
        raise ValueError(f"{tag} metadata is invalid") from error
    if not isinstance(message, dict):
        raise ValueError(f"{tag} metadata is invalid")
    return message


def discover(repository: str, run=output) -> list[dict]:
    """Derive every stable claim state from remote refs and GitHub objects."""
    run(["git", "fetch", "origin", "+refs/tags/v*:refs/tags/v*"])
    refs = _remote_tags(run)
    releases = _release_rows(repository, run)
    workflow_runs = _workflow_runs(repository, run)
    records = []

    for claim_tag, claim_ref in refs.items():
        match = CLAIM_TAG_RE.fullmatch(claim_tag)
        if match is None:
            continue
        if set(claim_ref) != {"object", "commit"}:
            raise ValueError(f"{claim_tag} must be an annotated remote tag")
        tag = match.group(1)
        version = tag[1:]
        commit = claim_ref["commit"]
        claim = _tag_message(claim_tag, claim_ref["object"], run)
        expected_claim = {
            "schema": 1, "version": version, "commit": commit,
            "autopublish": claim.get("autopublish"),
        }
        if claim != expected_claim or not isinstance(claim["autopublish"], bool):
            raise ValueError(f"{claim_tag} metadata differs from its ref")

        family_releases = [row for row in releases if row.get("tag_name") in {claim_tag, tag}]
        if len(family_releases) > 1:
            raise ValueError(f"{claim_tag} owns multiple GitHub releases")
        release = family_releases[0] if family_releases else None
        final_ref = refs.get(tag)
        needs_retarget = False
        final = None
        retry = None

        if final_ref is not None:
            if set(final_ref) != {"object", "commit"}:
                raise ValueError(f"{tag} must be an annotated remote tag")
            if final_ref["commit"] != commit:
                raise ValueError(f"{tag} points at a different commit than {claim_tag}")
            final = _tag_message(tag, final_ref["object"], run)
            expected_final = {
                "schema": 1, "version": version, "commit": commit,
                "claimTag": claim_tag, "claimTagObject": claim_ref["object"],
                "autopublish": claim["autopublish"],
                "candidateManifestSha256": final.get("candidateManifestSha256"),
                "dockerManifestDigest": final.get("dockerManifestDigest"),
            }
            if (final != expected_final
                    or not SHA256.fullmatch(final["candidateManifestSha256"] or "")
                    or not DOCKER_DIGEST.fullmatch(final["dockerManifestDigest"] or "")):
                raise ValueError(f"{tag} metadata differs from {claim_tag}")
            if release is None or release.get("prerelease") is not False:
                raise ValueError(f"{tag} has no valid GitHub release")
            needs_retarget = release.get("tag_name") == claim_tag
            if release.get("tag_name") not in {claim_tag, tag}:
                raise ValueError(f"{tag} release identity changed")
            if release.get("draft") is True:
                state = "green"
            elif release.get("draft") is False and release.get("published_at"):
                if needs_retarget:
                    raise ValueError(f"{claim_tag} was published before final retargeting")
                state = "published"
            else:
                raise ValueError(f"{tag} release state is invalid")
        else:
            if release is not None and (release.get("tag_name") != claim_tag
                                        or release.get("draft") is not True
                                        or release.get("prerelease") is not False):
                raise ValueError(f"{claim_tag} draft state is invalid")
            matching_runs = [row for row in workflow_runs
                             if row.get("head_branch") == claim_tag and row.get("head_sha") == commit]
            state, retry = classify_runs(matching_runs)

        records.append({
            "version": version,
            "state": state,
            "autopublish": claim["autopublish"],
            "claim_tag": claim_tag,
            "claim_object": claim_ref["object"],
            "tag": tag,
            "commit": commit,
            "release_id": release.get("id") if release else None,
            "needs_retarget": needs_retarget,
            "candidate_manifest_sha256": final["candidateManifestSha256"] if final else None,
            "docker_manifest_digest": final["dockerManifestDigest"] if final else None,
            "retry": retry if final_ref is None else None,
        })

    return sorted(records, key=lambda record: _key(record["version"]))


def reconcile(env: dict, *, run=output, read_head=None, advance_head=None) -> list[dict]:
    """Converge GitHub publication and protected heads oldest-first."""
    from scripts.releases import channel_releases, docker, stable

    repository = env["GITHUB_REPOSITORY"]
    records = discover(repository, run)
    retries = retry_due(records)
    if retries:
        for retry in retries:
            endpoint = f"repos/{repository}/actions/runs/{retry['run_id']}"
            run([
                "gh", "api", "--method", "POST",
                f"{endpoint}/rerun-failed-jobs",
            ])
            for attempt in range(6):
                current = json.loads(run(["gh", "api", endpoint]))
                if (current.get("id") == retry["run_id"]
                        and current.get("run_attempt") == retry["attempt"]
                        and current.get("status") != "completed"):
                    break
                if attempt < 5:
                    time.sleep(5)
            else:
                raise ValueError(f"Stable retry {retry['run_id']} did not enter attempt {retry['attempt']}")
        return [{"retry": retry["version"], "attempt": retry["attempt"]} for retry in retries]
    for record in records:
        if record["needs_retarget"]:
            stable.retarget_release(repository, record["release_id"], record["tag"],
                                    record["commit"], publish=False, run=run)
    if any(record["needs_retarget"] for record in records):
        records = discover(repository, run)

    read_head = read_head or (lambda: channel_releases.stable_head_version(env))
    head = read_head()
    requested = env.get("REQUESTED_VERSION") or None
    steps = plan(records, head=head, requested_version=requested)
    by_version = {record["version"]: record for record in records}

    for step in steps:
        if "flip" not in step:
            continue
        record = by_version[step["flip"]]
        stable.retarget_release(repository, record["release_id"], record["tag"],
                                record["commit"], publish=True, run=run)

    if advance_head is None:
        def production_advance(record: dict) -> None:
            docker.promote_stable(record["tag"], record["docker_manifest_digest"])
            with tempfile.TemporaryDirectory() as directory:
                channel_releases.advance_stable(env, record, Path(directory))
        advance_head = production_advance

    for step in steps:
        if "advance" in step:
            advance_head(by_version[step["advance"]])

    final = {record["version"]: record for record in discover(repository, run)}
    for step in steps:
        if "flip" in step and final[step["flip"]]["state"] != "published":
            raise ValueError(f"Stable release {step['flip']} did not publish")
    if requested and (requested not in final or final[requested]["state"] != "published"):
        raise ValueError(f"Requested stable release {requested} did not publish")
    if any("advance" in step for step in steps):
        expected = next(step["advance"] for step in reversed(steps) if "advance" in step)
        if read_head() != expected:
            raise ValueError("Stable protected head did not reach the planned version")
    return steps


def main(argv: list[str] | None = None, env: dict | None = None) -> None:
    if argv is None:
        argv = sys.argv[1:]
    if argv:
        raise ValueError("The stable sequencer takes no positional arguments")
    print(json.dumps(reconcile(dict(os.environ if env is None else env)), sort_keys=True))


if __name__ == "__main__":
    main()
