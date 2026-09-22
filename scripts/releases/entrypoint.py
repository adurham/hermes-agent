"""The thin release entrypoint: claim a version, cut the draft, dispatch the gate.

Nothing here builds. The claim is an annotated ``-rc`` tag pushed as exactly
that ref, and a dispatch that never starts is an error — the claim stays,
because a burned version is never retried under the same number.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.releases.versioning import derive_next_version

WORKFLOW = "stable-release.yml"


class ReleaseRefused(RuntimeError):
    """The release cannot proceed, and nothing was silently skipped."""


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True, encoding="utf-8").strip()


def _claims(repo: Path) -> list[str]:
    listed = _git(repo, "tag", "--list", "v*-rc")
    return [tag for tag in listed.splitlines() if tag]


def _claim_commit(repo: Path, tag: str) -> str:
    return _git(repo, "rev-parse", f"{tag}^{{commit}}")


def _highest_claim(repo: Path) -> tuple[str, str] | None:
    """The highest-version outstanding claim, as (version, commit)."""
    from scripts.releases.versioning import version_from_tag

    best: tuple[list[int], str, str] | None = None
    for tag in _claims(repo):
        version = version_from_tag(tag[:-3]) if tag.endswith("-rc") else None
        if version is None:
            continue
        key = [int(part) for part in version.split(".")]
        if best is None or key > best[0]:
            best = (key, version, _claim_commit(repo, tag))
    return None if best is None else (best[1], best[2])


def _require_ancestry(repo: Path, commit: str) -> None:
    """A claim's commit must descend from the highest outstanding claim's."""
    highest = _highest_claim(repo)
    if highest is None:
        return
    version, claimed = highest
    if commit == claimed:
        return
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", claimed, commit], cwd=repo, capture_output=True)
    if ancestor.returncode != 0:
        raise ReleaseRefused(
            f"{version} already claimed at {claimed} — publish or abandon it first")


def release(commit: str, *, bump: str, repo: Path, remote: str, repository: str,
            execute, autopublish: bool = False) -> dict:
    """Claim the derived version, cut its draft, and start the gate."""
    _require_ancestry(repo, commit)
    version = derive_next_version(published=None, claims=_claims(repo), bump=bump)
    tag = f"v{version}-rc"
    _git(repo, "tag", "-a", tag, commit, "-m", f"claim v{version}")
    _git(repo, "push", remote, f"refs/tags/{tag}")
    url = f"https://github.com/{repository}/releases/tag/{tag}"
    try:
        execute([
            "gh", "release", "create", tag, "--repo", repository,
            "--verify-tag", "--draft", "--generate-notes", "--title", f"Hermes Agent v{version}",
        ])
        execute([
            "gh", "workflow", "run", WORKFLOW, "--ref", tag, "--repo", repository,
            "--raw-field", f"tag={tag}",
            "--raw-field", f"autopublish={str(autopublish).lower()}",
        ])
    except Exception as exc:
        raise ReleaseRefused(f"release {tag} never started: {exc}") from exc
    return {"version": version, "tag": tag, "commit": commit, "url": url,
            "autopublish": autopublish}


def publish(version: str, *, repo: Path, repository: str, green, edit) -> dict:
    """Flip a green waiting draft. The flip is the publish; nothing rebuilds."""
    tag = f"v{version}"
    if not green(tag):
        raise ReleaseRefused(f"{version} is not green — refusing to publish")
    edit(["gh", "release", "edit", tag, "--repo", repository, "--draft=false"])
    return {"flipped": tag}


def abandon(version: str, *, repo: Path, repository: str, delete) -> dict:
    """Delete the draft. The claim tag stays, so the version is spent."""
    delete(["gh", "release", "delete", f"v{version}-rc", "--repo", repository, "--yes"])
    return {"burned": version}


def cmd_release(args) -> None:
    """The ``release`` subcommand: claim, draft, dispatch."""
    from scripts import release as release_script

    repo = release_script.REPO_ROOT
    remote = release_script.resolve_push_remote(args.remote)
    repository = release_script.remote_github_repo(remote)
    if not repository:
        raise SystemExit(f"release: remote {remote!r} does not point at a GitHub repository")
    commit = _git(repo, "rev-parse", "--verify", f"{args.commit}^{{commit}}")

    def execute(command: list[str]) -> None:
        completed = subprocess.run(command, cwd=repo, capture_output=True, text=True, encoding="utf-8")
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or "release command failed")

    result = release(commit, bump=args.bump, repo=repo, remote=remote, repository=repository,
                     execute=execute, autopublish=args.autopublish)
    print(result["url"])
