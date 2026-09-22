"""The release entrypoint claims a version, cuts a draft, and dispatches the gate.

The claim is the tag push, and it pushes exactly the claim ref. A release that
never starts is an error, not a warning the operator has to notice.
"""
import subprocess

import pytest


def git(repo, *args):
    return subprocess.check_output(["git", *args], cwd=repo, text=True, encoding="utf-8").strip()


@pytest.fixture
def source(tmp_path):
    origin = tmp_path / "origin.git"
    repo = tmp_path / "source"
    origin.mkdir()
    repo.mkdir()
    git(origin, "init", "--bare", "--quiet")
    git(repo, "init", "--initial-branch=main", "--quiet")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.test")
    (repo / "pyproject.toml").write_text('[project]\nname="fixture"\nversion="0.0.0"\n', encoding="utf-8")
    git(repo, "add", "pyproject.toml")
    git(repo, "commit", "--quiet", "-m", "Initial")
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "--quiet", "-u", "origin", "main")
    return repo


def _claim(repo, version, commit, actor="release-bot", when="2026-09-22T00:14:00Z"):
    git(repo, "tag", "-a", f"v{version}-rc", commit, "-m",
        f"claim v{version}\n\nactor: {actor}\nwhen: {when}")
    git(repo, "push", "--quiet", "origin", f"refs/tags/v{version}-rc")


def test_release_claims_the_derived_version_and_dispatches(source):
    from scripts.releases.entrypoint import release

    commit = git(source, "rev-parse", "HEAD")
    calls = []
    result = release(commit, bump="patch", repo=source, remote="origin",
                     repository="example/hermes-agent", dispatch=calls.append)

    assert result["version"] == "0.21.5"
    assert result["tag"] == "v0.21.5-rc"
    assert result["commit"] == commit
    assert result["url"] == "https://github.com/example/hermes-agent/releases/tag/v0.21.5-rc"
    assert git(source, "rev-parse", "v0.21.5-rc^{commit}") == commit
    assert calls == [["gh", "workflow", "run", "stable-release.yml",
                      "--ref", "v0.21.5-rc", "--repo", "example/hermes-agent"]]
    # The claim push names the claim ref and nothing else.
    pushed = git(source, "ls-remote", "origin", "refs/tags/v0.21.5-rc")
    assert pushed.startswith(git(source, "rev-parse", "v0.21.5-rc"))


def test_a_commit_behind_an_outstanding_claim_is_refused(source):
    from scripts.releases.entrypoint import ReleaseRefused, release

    earlier = git(source, "rev-parse", "HEAD")
    git(source, "commit", "--allow-empty", "--quiet", "-m", "later")
    git(source, "push", "--quiet", "origin", "main")
    _claim(source, "0.21.5", git(source, "rev-parse", "HEAD"))

    with pytest.raises(ReleaseRefused, match="0\\.21\\.5 already claimed"):
        release(earlier, bump="patch", repo=source, remote="origin",
                repository="example/hermes-agent", dispatch=lambda _cmd: pytest.fail("must not dispatch"))
    assert "v0.21.6-rc" not in git(source, "tag", "--list")


def test_a_dispatch_that_never_starts_is_an_error(source):
    from scripts.releases.entrypoint import ReleaseRefused, release

    def refuse(_cmd):
        raise RuntimeError("workflow dispatch rejected")

    with pytest.raises(ReleaseRefused, match="never started"):
        release(git(source, "rev-parse", "HEAD"), bump="patch", repo=source, remote="origin",
                repository="example/hermes-agent", dispatch=refuse)
    # The claim stands: a failed start burns the version rather than retrying it.
    assert "v0.21.5-rc" in git(source, "tag", "--list")


def test_publish_flips_a_green_draft_and_abandon_keeps_the_claim(source):
    from scripts.releases.entrypoint import abandon, publish

    commit = git(source, "rev-parse", "HEAD")
    _claim(source, "0.21.5", commit)
    calls = []
    published = publish("0.21.5", repo=source, repository="example/hermes-agent",
                        green=lambda tag: tag == "v0.21.5", edit=calls.append)
    assert published["flipped"] == "v0.21.5"
    assert calls == [["gh", "release", "edit", "v0.21.5", "--repo", "example/hermes-agent", "--draft=false"]]

    abandoned = abandon("0.21.5", repo=source, repository="example/hermes-agent", delete=calls.append)
    assert abandoned["burned"] == "0.21.5"
    assert calls[-1] == ["gh", "release", "delete", "v0.21.5-rc", "--repo", "example/hermes-agent", "--yes"]
    assert "v0.21.5-rc" in git(source, "tag", "--list")
