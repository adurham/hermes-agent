"""Admit and dispatch tagless builds without changing release channels."""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
import tomllib
from pathlib import Path

WORKFLOW = "desktop-bundled-release.yml"

# Direct commit-build dispatch is the upstream-only path: the workflow's
# admission step rejects any repository != NousResearch/hermes-agent that has
# no disposable allocation (the fork CI guard). Forks must instead allocate a
# disposable channel first; commit_build.cmd_build_commit routes them there.
UPSTREAM_REPOSITORY = "NousResearch/hermes-agent"

# The allocation workflow run is created by a server-side race we do not
# observe; this bounds the wait for it to appear in `gh run list`.
_ALLOCATION_APPEAR_TIMEOUT_S = 120
# A disposable allocation is a single 10-minute-timeout GHA job (probes plus
# R2 writes, no native build); the same budget covers queueing.
_ALLOCATION_COMPLETE_TIMEOUT_S = 900
_ALLOCATION_POLL_INTERVAL_S = 15
# How recent a listed run's createdAt must be to count as ours without being
# queued/running — generous enough for gh/GitHub clock skew.
_RUN_RECENCY_WINDOW_S = 300


def require_commit(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{40}", value):
        raise ValueError("Commit builds require an exact full 40-character SHA")
    return value


def output(argv: list[str], repo: Path | None = None) -> str:
    return subprocess.check_output(argv, cwd=repo, text=True, encoding="utf-8", timeout=60).strip()


def require_pushed(commit: str, remote: str, repo: Path | None = None) -> None:
    """Require ancestry from a branch or tag currently advertised by this remote."""
    require_commit(commit)
    advertised = {line.split()[0] for line in output(
        ["git", "ls-remote", remote, "refs/heads/*", "refs/tags/*"], repo).splitlines()}
    containing = set(output(["git", "for-each-ref", f"--contains={commit}", "--format=%(objectname)",
                             f"refs/remotes/{remote}/", "refs/tags/"], repo).splitlines())
    if not advertised.intersection(containing):
        raise ValueError(f"Commit {commit} is not reachable from a pushed branch or tag on {remote}")


def version_at(repo: Path | None, commit: str) -> str:
    require_commit(commit)
    document = tomllib.loads(output(["git", "show", f"{commit}:pyproject.toml"], repo))
    version = document["project"]["version"]
    if not isinstance(version, str) or not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", version):
        raise ValueError("Commit packaging requires project.version=X.Y.Z")
    return version


def admit(env: dict[str, str]) -> dict[str, str]:
    from scripts.releases.bundle_env import decode

    commit = require_commit(env.get("BUILD_COMMIT", ""))
    if env.get("TAG") or env.get("RELEASE_PHASE") or env.get("UPLOAD_RELEASE", "false") != "false":
        raise ValueError("Commit builds cannot use tag, release-phase or upload_release")
    if env.get("TERMUX_UPGRADE_FROM_TAG"):
        raise ValueError("Commit builds do not run release-channel upgrade acceptance")
    default = env.get("DEFAULT_BRANCH", "")
    ref = f"refs/heads/{default}"
    repository = env.get("GITHUB_REPOSITORY", "")
    expected_workflow = f"{repository}/.github/workflows/{WORKFLOW}@{ref}"
    if (not default or not repository or env.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or env.get("GITHUB_REF") != ref or env.get("GITHUB_WORKFLOW_REF") != expected_workflow):
        raise ValueError("Commit builds require workflow_dispatch from the repository default-branch workflow")
    actors = {env.get("GITHUB_ACTOR", ""), env.get("GITHUB_TRIGGERING_ACTOR") or env.get("GITHUB_ACTOR", "")}
    for actor in actors:
        if not actor:
            raise ValueError("Commit builds require a repository maintainer")
        permission = output(["gh", "api", f"repos/{repository}/collaborators/{actor}/permission", "--jq", ".permission"])
        if permission not in {"write", "maintain", "admin"}:
            raise ValueError("Commit builds require repository write, maintain or admin permission")
    require_pushed(commit, "origin")
    decode(env.get("BUNDLE_ENV_JSON", ""))
    return {"sha": commit, "channel": "commit", "payload-version": version_at(None, commit)}


def resolve_revision(rev: str, remote: str, repo: Path) -> str:
    if not isinstance(rev, str) or not rev or rev.startswith("-"):
        raise ValueError("Commit builds require a Git revision")
    output(["git", "fetch", "--quiet", remote], repo)
    commit = require_commit(output(["git", "rev-parse", "--verify", "--end-of-options", f"{rev}^{{commit}}"], repo))
    require_pushed(commit, remote, repo)
    return commit


def dispatch_command(commit: str, repository: str, branch: str,
                     bundle_env: dict[str, str | None] | None = None) -> list[str]:
    from scripts.releases.bundle_env import validate

    require_commit(commit)
    command = ["gh", "workflow", "run", WORKFLOW, "--ref", branch, "--repo", repository,
            "-f", f"build_commit={commit}", "-f", "tag=", "-f", "upload_release=false",
            "-f", "termux_only=false", "-f", "termux_upgrade_from_tag="]
    if bundle_env:
        command += ["-f", "bundle_env=" + json.dumps(validate(bundle_env), sort_keys=True)]
    return command


def disposable_dispatch_command(commit: str, repository: str, branch: str,
                               bundle_env: dict[str, str | None] | None = None) -> list[str]:
    """Fork allocation dispatch: same workflow, disposable_channel inputs.

    Bundle env travels here (not on the follow-up build dispatch): the
    allocation bakes it into the immutable request, so every value must be
    present at allocation time. The follow-up command pinned to that request
    must never re-pass bundle_env.
    """
    from scripts.releases.bundle_env import validate

    require_commit(commit)
    command = ["gh", "workflow", "run", WORKFLOW, "--repo", repository, "--ref", branch,
               "-f", f"build_commit={commit}", "-f", "tag=", "-f", "upload_release=false",
               "-f", "termux_only=false", "-f", "termux_upgrade_from_tag=",
               "-f", "disposable_receivers=false",
               "-f", "disposable_channel=" + _allocation_channel_name(commit)]
    if bundle_env:
        command += ["-f", "bundle_env=" + json.dumps(validate(bundle_env), sort_keys=True)]
    return command


def _allocation_channel_name(commit: str) -> str:
    """A unique disposable preview name.

    Uniqueness matters: ChannelPublisher.create() returns an existing record
    instead of failing, so a colliding name would silently allocate into a
    previous channel and bump its sequence.
    """
    return f"commit-{commit[:12]}-{int(time.time())}"


def _fork_allocation(repository: str, command: list[str], repo_root: Path) -> None:
    """Dispatch the allocation run on a fork, then dispatch its follow-up build.

    The lease lives in the fork's Actions run (release-signing secrets exist
    only there), so the allocation itself cannot run locally. But the
    follow-up dispatch CAN: it runs with the local maintainer's gh login —
    the same identity that dispatched the allocation — unlike the CI
    controller token, which deliberately never dispatches publication runs.
    One command therefore covers both steps; if the follow-up cannot be
    recovered from the run logs, fall back to pointing at the run summary.
    """
    run = subprocess.run(command, cwd=repo_root, capture_output=True, text=True,  # windows-footgun: ok — encoding and replacement policy are on the next line.
                         encoding="utf-8", errors="replace", check=True, timeout=60)
    print((run.stdout or "").strip() or f"Dispatched disposable allocation for {repository}.")
    run_id = _await_allocation_run(repository)
    _await_completion(repository, run_id)
    follow_up = _extract_follow_up(repository, run_id)
    if not _is_pinned_follow_up(follow_up, repository):
        print(f"Disposable allocation succeeded (run {run_id}), but the follow-up build "
              "command could not be recovered from the run logs. Copy it from the "
              f"allocation run summary — https://github.com/{repository}/actions/runs/{run_id} "
              "— and run it with your maintainer login.")
        return
    assert follow_up is not None
    print("Allocation complete; dispatching the pinned channel build.")
    print(f"    {shlex.join(follow_up)}")
    result = subprocess.run(follow_up, cwd=repo_root, capture_output=True, text=True,  # windows-footgun: ok — encoding and replacement policy are on the next line.
                            encoding="utf-8", errors="replace", check=True, timeout=60)
    print((result.stdout or "").strip() or f"Dispatched channel build from allocation run {run_id}. No release was created.")


def _is_pinned_follow_up(command: object, repository: str) -> bool:
    """Only ever execute a follow-up shaped exactly like our own dispatch.

    The command is parsed from CI logs, so refuse anything that is not a
    `gh workflow run` of this workflow against the same repository.
    """
    if not (isinstance(command, list) and len(command) > 6
            and command[:4] == ["gh", "workflow", "run", WORKFLOW] and "--repo" in command):
        return False
    index = command.index("--repo")
    return command[index + 1:index + 2] == [repository]


def _await_allocation_run(repository: str) -> str:
    """Wait for the just-dispatched allocation run to appear and return its id.

    GitHub creates workflow_dispatch runs asynchronously, so the newest list
    entry right after dispatch may still be a prior run. Accept the newest
    entry once it is queued/running, or once it was created inside a recent
    window (allowing gh/GitHub clock skew); otherwise keep polling and fall
    back to the newest entry when the window closes.
    """
    list_command = ["gh", "run", "list", "--repo", repository,
                    "--workflow", WORKFLOW, "--limit", "1",
                    "--json", "databaseId,status,conclusion,createdAt"]
    recent_cutoff = _iso_utc_now_minus(_RUN_RECENCY_WINDOW_S)
    deadline = time.monotonic() + _ALLOCATION_APPEAR_TIMEOUT_S
    latest: dict = {}
    while time.monotonic() < deadline:
        result = subprocess.run(list_command, capture_output=True, text=True,  # windows-footgun: ok — encoding and replacement policy are on the next line.
                                encoding="utf-8", errors="replace", timeout=60)
        if result.returncode == 0:
            latest = (json.loads(result.stdout or "[]") or [{}])[0]
            if (latest.get("status") in {"queued", "in_progress"}
                    or str(latest.get("createdAt", "")) >= recent_cutoff):
                return str(latest["databaseId"])
        time.sleep(_ALLOCATION_POLL_INTERVAL_S)
    if latest.get("databaseId"):
        # gh's server-side lag outran the window; the newest run we saw is
        # still the best candidate for the dispatch we just made.
        return str(latest["databaseId"])
    raise ValueError(f"No workflow run appeared for {repository} within "
                     f"{_ALLOCATION_APPEAR_TIMEOUT_S} seconds")


def _iso_utc_now_minus(seconds: int) -> str:
    """A UTC ISO-8601 timestamp `seconds` in the past, for string comparison
    against gh's `createdAt` values (both share the same format and zone)."""
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _await_completion(repository: str, run_id: str) -> None:
    """Poll the allocation run until GitHub reports a terminal conclusion."""
    view_command = ["gh", "run", "view", run_id, "--repo", repository,
                    "--json", "status,conclusion"]
    deadline = time.monotonic() + _ALLOCATION_COMPLETE_TIMEOUT_S
    while True:
        result = subprocess.run(view_command, capture_output=True, text=True,  # windows-footgun: ok — encoding and replacement policy are on the next line.
                                encoding="utf-8", errors="replace", timeout=60)
        if result.returncode != 0:
            raise ValueError(f"Could not read workflow run {run_id}: {(result.stderr or '').strip()}")
        status = (json.loads(result.stdout or "{}") or {})
        if status.get("status") == "completed":
            if status.get("conclusion") != "success":
                raise ValueError(f"Disposable allocation run {run_id} finished with "
                                 f"conclusion {status.get('conclusion')!r}; see "
                                 f"https://github.com/{repository}/actions/runs/{run_id}")
            return
        if time.monotonic() >= deadline:
            raise ValueError(f"Disposable allocation run {run_id} did not complete within "
                             f"{_ALLOCATION_COMPLETE_TIMEOUT_S} seconds; see "
                             f"https://github.com/{repository}/actions/runs/{run_id}")
        time.sleep(_ALLOCATION_POLL_INTERVAL_S)


def _extract_follow_up(repository: str, run_id: str) -> list[str] | None:
    """Find the printed follow-up dispatch in the allocation run's logs.

    channel_disposable prints one single-line JSON object (sorted keys) whose
    "command" value is the exact argv list of the follow-up dispatch. gh --log
    prefixes each line with "job\\tstep\\ttimestamp ", so scan every line for
    a decodable JSON object carrying a "command" argv. Return the argv list,
    or None when the log line is unavailable.
    """
    result = subprocess.run(["gh", "run", "view", run_id, "--repo", repository, "--log"],
                            capture_output=True, text=True,  # windows-footgun: ok — encoding and replacement policy are on the next line.
                            encoding="utf-8", errors="replace", timeout=120)
    if result.returncode != 0:
        return None
    decoder = json.JSONDecoder()
    for line in (result.stdout or "").splitlines():
        index = line.find("{")
        while index != -1:
            try:
                value, _ = decoder.raw_decode(line, index)
            except json.JSONDecodeError:
                index = line.find("{", index + 1)
                continue
            command = value.get("command") if isinstance(value, dict) else None
            if isinstance(command, list) and command and all(isinstance(part, str) for part in command):
                return command
            index = line.find("{", index + 1)
    return None


def cmd_build_commit(args) -> None:
    from scripts import release
    from scripts.releases import r2
    from scripts.releases.bundle_env import parse_assignments

    try:
        bundle_env = parse_assignments(args.bundle_env, args.bundle_unset)
        remote = release.resolve_push_remote(args.remote)
        repository = release.remote_github_repo(remote)
        if not repository:
            raise ValueError("commit builds require an explicit GitHub remote")
        commit = resolve_revision(args.build_commit, remote, release.REPO_ROOT)
        branch = release._default_branch(repository)
        if not branch:
            raise ValueError("could not resolve the repository default branch")
        fork = repository.casefold() != UPSTREAM_REPOSITORY.casefold()
        command = disposable_dispatch_command(commit, repository, branch, bundle_env) if fork \
            else dispatch_command(commit, repository, branch, bundle_env)
        page = r2.public_url_for(r2.public_base_url(), r2.commit_page_key_for(commit))
        print(f"Building one-off bundle for commit {commit}")
        print(f"Builds will be available at: {page}.")
        if fork:
            # The disposable flow allocates the channel in the fork's Actions
            # run and prints the pinned follow-up dispatch there; bundle env
            # already travels inside the allocation request.
            print(f"Repository {repository} is not {UPSTREAM_REPOSITORY}; routing the "
                  "commit build through a disposable channel allocation.")
            print(f"Allocation command, running from {repository}@{branch}")
            print(f"    {shlex.join(command)}")
            if not args.publish:
                print("Dry run. Add --publish to dispatch.")
                return
            print("Starting disposable allocation workflow!")
            _fork_allocation(repository, command, release.REPO_ROOT)
            return
        print(f"Workflow command, running from {repository}@{branch}")
        print(f"    {shlex.join(command)}")
        if not args.publish:
            print("Dry run. Add --publish to dispatch.")
            return
        print("Starting workflow!")
        result = subprocess.run(command, cwd=release.REPO_ROOT, capture_output=True, text=True,  # windows-footgun: ok — encoding and replacement policy are on the next line.
                                encoding="utf-8", errors="replace", check=True, timeout=60)
        print((result.stdout or "").strip() or f"Dispatched commit build {commit}. No release was created.")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        stderr = ""
        if isinstance(exc, subprocess.CalledProcessError):
            # check_output failures carry no captured stderr; don't mask the
            # original error with a TypeError while reporting it.
            stderr = "\n" + (exc.stderr or "")
        raise SystemExit(f"release: commit build refused: {exc}{stderr}") from exc


def main() -> None:
    import sys

    if sys.argv[1:] != ["admit"]:
        raise SystemExit("usage: python -m scripts.releases.commit_build admit")
    values = admit(dict(os.environ))
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
        stream.write("".join(f"{key}={value}\n" for key, value in values.items()))
    print(json.dumps(values, sort_keys=True))


if __name__ == "__main__":
    main()
