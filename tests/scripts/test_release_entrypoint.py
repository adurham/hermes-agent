"""The release entrypoint claims an attempt, cuts a draft, and dispatches the gate.

The claim is the push of one attempt ref, ``rc.<N>-vX.Y.Z``, and it pushes
exactly that ref. A version is spent only by publication; an abandoned attempt
frees its version for attempt N+1. At most one attempt, of any version, is
outstanding. A release that never starts is an error, not a warning the
operator has to notice.
"""
import json
import subprocess
import threading

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


def _advance(repo, message):
    git(repo, "commit", "--allow-empty", "--quiet", "-m", message)
    git(repo, "push", "--quiet", "origin", "main")
    return git(repo, "rev-parse", "HEAD")


def _claim(repo, version, commit, attempt=1):
    metadata = json.dumps({
        "schema": 1, "version": version, "attempt": attempt, "commit": commit,
        "autopublish": False, "claimEpoch": 1_790_000_000,
    }, sort_keys=True, separators=(",", ":"))
    ref = f"rc.{attempt}-v{version}"
    git(repo, "tag", "-a", ref, commit, "-m", metadata)
    git(repo, "push", "--quiet", "origin", f"refs/tags/{ref}")


def _mark(repo, version, attempt=1):
    ref = f"abandoned-rc.{attempt}-v{version}"
    git(repo, "tag", "-a", ref, f"rc.{attempt}-v{version}^{{commit}}", "-m", "abandoned")
    git(repo, "push", "--quiet", "origin", f"refs/tags/{ref}")


def _publish_tag(repo, version, commit):
    git(repo, "tag", "-a", f"v{version}", commit, "-m", "published")
    git(repo, "push", "--quiet", "origin", f"refs/tags/v{version}")


def _release(repo, commit, **overrides):
    from scripts.releases.entrypoint import release

    arguments = {"bump": "patch", "repo": repo, "remote": "origin",
                 "repository": "example/hermes-agent", "execute": lambda _command: None}
    return release(commit, **{**arguments, **overrides})


def _must_not_execute(command):
    if command[:3] != ["gh", "run", "list"]:
        pytest.fail(f"a refused release must not run {command}")
    return "[]"


def test_release_claims_the_first_attempt_creates_a_draft_and_dispatches(source):
    commit = git(source, "rev-parse", "HEAD")
    calls = []

    def execute(command):
        calls.append(command)
        if command[:3] == ["gh", "run", "list"]:
            return json.dumps([{"databaseId": 7, "url": "https://github.com/example/hermes-agent/actions/runs/7",
                                "headBranch": "rc.1-v0.21.5", "status": "queued"}])
        return ""

    result = _release(source, commit, execute=execute, autopublish=True)

    assert result["version"] == "0.21.5"
    assert result["tag"] == "rc.1-v0.21.5"
    assert result["commit"] == commit
    assert result["url"] == "https://github.com/example/hermes-agent/releases/tag/rc.1-v0.21.5"
    assert result["final_url"] == "https://github.com/example/hermes-agent/releases/tag/v0.21.5"
    assert git(source, "rev-parse", "rc.1-v0.21.5^{commit}") == commit
    claim = json.loads(git(source, "tag", "-l", "rc.1-v0.21.5", "--format=%(contents)"))
    assert isinstance(claim.pop("claimEpoch"), int)
    assert claim == {
        "attempt": 1,
        "autopublish": True,
        "commit": commit,
        "schema": 1,
        "version": "0.21.5",
    }
    assert calls == [
        ["gh", "release", "create", "rc.1-v0.21.5", "--repo", "example/hermes-agent",
         "--verify-tag", "--draft", "--generate-notes", "--title", "Hermes Agent v0.21.5"],
        ["gh", "workflow", "run", "stable-release.yml", "--ref", "rc.1-v0.21.5",
         "--repo", "example/hermes-agent", "--raw-field", "tag=rc.1-v0.21.5"],
        ["gh", "run", "list", "--repo", "example/hermes-agent", "--workflow", "stable-release.yml",
         "--branch", "rc.1-v0.21.5", "--json", "databaseId,url,headBranch,status"],
    ]
    assert result["run_url"] == "https://github.com/example/hermes-agent/actions/runs/7"
    # The claim push names the claim ref and nothing else.
    pushed = git(source, "ls-remote", "origin", "refs/tags/*")
    assert pushed.splitlines() == [
        f"{git(source, 'rev-parse', 'rc.1-v0.21.5')}\trefs/tags/rc.1-v0.21.5",
        f"{commit}\trefs/tags/rc.1-v0.21.5^{{}}",
    ]


def test_release_output_names_the_wait_and_the_publish_step():
    from scripts.releases.entrypoint import next_steps

    result = {"version": "0.21.5", "tag": "rc.1-v0.21.5", "autopublish": False,
              "run_url": "https://github.com/example/hermes-agent/actions/runs/7",
              "final_url": "https://github.com/example/hermes-agent/releases/tag/v0.21.5"}
    text = next_steps(result)
    assert "Workflow: " + result["run_url"] in text
    assert "The release workflow started on rc.1-v0.21.5." in text
    assert "Wait for that workflow to finish." in text
    assert result["final_url"] in text
    assert "python scripts/release.py publish --version 0.21.5 --remote origin" in text

    automatic = next_steps({**result, "autopublish": True})
    assert "Autopublish is on." in automatic
    assert "publish --version" not in automatic


def test_second_cut_after_abandon_is_attempt_two_of_the_same_version(source):
    first = git(source, "rev-parse", "HEAD")
    _claim(source, "0.21.5", first)
    _mark(source, "0.21.5")

    result = _release(source, _advance(source, "fix"))

    assert result["tag"] == "rc.2-v0.21.5"
    assert result["version"] == "0.21.5"


@pytest.mark.parametrize("bump", ["patch", "minor"])
def test_an_outstanding_attempt_of_any_version_blocks_the_next_cut(source, bump):
    from scripts.releases.entrypoint import ReleaseRefused

    _claim(source, "0.21.5", git(source, "rev-parse", "HEAD"))

    with pytest.raises(ReleaseRefused, match="publish or abandon it first"):
        _release(source, _advance(source, "later"), bump=bump, execute=_must_not_execute)
    assert git(source, "tag", "--list", "rc.*") == "rc.1-v0.21.5"


def test_a_published_attempt_no_longer_blocks(source):
    commit = git(source, "rev-parse", "HEAD")
    _claim(source, "0.21.5", commit)
    _publish_tag(source, "0.21.5", commit)

    result = _release(source, _advance(source, "later"), published=("0.21.5", commit))

    assert result["tag"] == "rc.1-v0.21.6"


def test_new_attempt_must_descend_from_the_published_head(source):
    from scripts.releases.entrypoint import ReleaseRefused

    earlier = git(source, "rev-parse", "HEAD")
    published = _advance(source, "published")

    with pytest.raises(ReleaseRefused, match="does not descend from the published stable head"):
        _release(source, earlier, published=("0.21.4", published), execute=_must_not_execute)
    assert git(source, "tag", "--list", "rc.*") == ""


def test_an_abandoned_attempt_does_not_constrain_the_next_cut(source):
    root = git(source, "rev-parse", "HEAD")
    good = _advance(source, "good")
    bad = _advance(source, "bad")
    _claim(source, "0.21.5", bad)
    _mark(source, "0.21.5")

    result = _release(source, good, published=("0.21.4", root))

    assert result["tag"] == "rc.2-v0.21.5"
    assert result["commit"] == good


def test_refusal_names_the_run_and_both_ways_out(source, capsys):
    from scripts.releases.entrypoint import ReleaseRefused

    blocked = git(source, "rev-parse", "HEAD")
    _claim(source, "0.21.5", blocked)

    def execute(command):
        assert command == ["gh", "run", "list", "--repo", "example/hermes-agent", "--workflow",
                           "stable-release.yml", "--branch", "rc.1-v0.21.5",
                           "--json", "databaseId,url,headBranch,headSha,status"]
        return json.dumps([
            {"databaseId": 98, "url": "https://github.com/example/hermes-agent/actions/runs/98",
             "headBranch": "rc.1-v0.21.5", "headSha": "0" * 40, "status": "completed"},
            {"databaseId": 99, "url": "https://github.com/example/hermes-agent/actions/runs/99",
             "headBranch": "rc.1-v0.21.5", "headSha": blocked, "status": "completed"},
        ])

    with pytest.raises(ReleaseRefused):
        _release(source, _advance(source, "later"), bump="minor", execute=execute)
    text = capsys.readouterr().err
    assert "https://github.com/example/hermes-agent/actions/runs/99" in text
    assert "runs/98" not in text
    # The abandon command names the outstanding attempt's version, not the derived 0.22.0.
    assert "python scripts/release.py abandon --version 0.21.5 --remote origin" in text
    assert "gh run rerun 99 --failed --repo example/hermes-agent" in text


def test_refusal_for_an_attempt_that_never_started_offers_only_abandon(source, capsys):
    from scripts.releases.entrypoint import ReleaseRefused

    _claim(source, "0.21.5", git(source, "rev-parse", "HEAD"))

    with pytest.raises(ReleaseRefused):
        _release(source, _advance(source, "later"), execute=_must_not_execute)
    text = capsys.readouterr().err
    assert "No workflow run is listed for rc.1-v0.21.5." in text
    assert "python scripts/release.py abandon --version 0.21.5 --remote origin" in text
    assert "gh run rerun" not in text


def test_successive_attempts_reserve_increasing_native_epochs(source):
    _release(source, git(source, "rev-parse", "HEAD"))
    _mark(source, "0.21.5")
    _release(source, _advance(source, "next"))

    first_epoch = int(git(source, "for-each-ref", "refs/tags/rc.1-v0.21.5",
                          "--format=%(taggerdate:unix)"))
    second_epoch = int(git(source, "for-each-ref", "refs/tags/rc.2-v0.21.5",
                           "--format=%(taggerdate:unix)"))
    assert second_epoch > first_epoch


def test_a_dispatch_that_never_starts_is_an_error(source):
    from scripts.releases.entrypoint import ReleaseRefused

    def refuse(command):
        if command[1:3] == ["workflow", "run"]:
            raise RuntimeError("workflow dispatch rejected")

    with pytest.raises(ReleaseRefused, match="never started"):
        _release(source, git(source, "rev-parse", "HEAD"), execute=refuse)
    # The attempt stands outstanding until someone abandons it.
    assert "rc.1-v0.21.5" in git(source, "tag", "--list")


def test_publish_and_abandon_output_name_the_result():
    from scripts.releases.entrypoint import abandon_steps, publish_steps

    published = publish_steps({"version": "0.21.5", "repository": "example/hermes-agent",
                               "run_url": "https://github.com/example/hermes-agent/actions/runs/9"})
    assert "Requested publication of v0.21.5." in published
    assert "Workflow: https://github.com/example/hermes-agent/actions/runs/9" in published
    assert "moves the stable channel" in published

    abandoned = abandon_steps({"burned": "0.21.5", "tag": "v0.21.5-rc"})
    assert "Deleted the draft for v0.21.5." in abandoned
    assert "v0.21.5-rc" in abandoned
    assert "cannot be reused" in abandoned


def test_publish_dispatches_the_sequencer_and_abandon_keeps_the_claim(source):
    from scripts.releases.entrypoint import ReleaseRefused, abandon, publish

    commit = git(source, "rev-parse", "HEAD")
    _claim(source, "0.21.5", commit)
    calls = []
    published = publish("0.21.5", repository="example/hermes-agent", dispatch=calls.append)
    assert published["requested"] == "v0.21.5"
    assert published["version"] == "0.21.5"
    assert published["repository"] == "example/hermes-agent"
    assert calls == [[
        "gh", "workflow", "run", "stable-release-publication.yml",
        "--repo", "example/hermes-agent", "--raw-field", "version=0.21.5",
    ]]

    def inspect(command):
        if "v0.21.5" in command:
            return json.dumps({"tagName": "v0.21.5", "isDraft": True, "isPrerelease": False})
        raise ReleaseRefused("not found")

    abandoned = abandon("0.21.5", repo=source, repository="example/hermes-agent",
                        delete=calls.append, inspect=inspect)
    assert abandoned["burned"] == "0.21.5"
    assert abandoned["tag"] == "v0.21.5"
    assert calls[-1] == ["gh", "release", "delete", "v0.21.5", "--repo", "example/hermes-agent", "--yes"]
    assert "rc.1-v0.21.5" in git(source, "tag", "--list")

    with pytest.raises(ReleaseRefused, match="burned or superseded by 0\\.21\\.6"):
        publish(
            "0.21.5", repository="example/hermes-agent",
            dispatch=lambda _command: pytest.fail("superseded publish must not dispatch"),
            inspect=lambda _command: pytest.fail("superseded publish must not inspect drafts"),
            head_version=lambda: "0.21.6",
        )


def _clones(source, tmp_path):
    origin = git(source, "remote", "get-url", "origin")
    clones = []
    for name in ("left", "right"):
        clone = tmp_path / name
        git(tmp_path, "clone", "--quiet", origin, str(clone))
        git(clone, "config", "user.name", "Test")
        git(clone, "config", "user.email", "test@example.test")
        clones.append(clone)
    return origin, clones


def test_concurrent_claim_loser_reports_the_remote_winner_and_the_version_stays_free(
        source, tmp_path, monkeypatch):
    from scripts.releases import entrypoint
    from scripts.releases.versioning import derive_next_version, next_attempt

    old = git(source, "rev-parse", "HEAD")
    new = _advance(source, "later")
    origin, (left, right) = _clones(source, tmp_path)
    git(left, "checkout", "--quiet", old)

    barrier = threading.Barrier(2)
    winner_pushed = threading.Event()
    original_git = entrypoint._git

    def racing_git(repo, *args):
        if args[:2] == ("push", "origin") and args[-1] == "refs/tags/rc.1-v0.21.5":
            barrier.wait(timeout=10)
            if repo == left:
                if not winner_pushed.wait(timeout=10):
                    raise TimeoutError("winning claim did not finish")
            else:
                try:
                    return original_git(repo, *args)
                finally:
                    winner_pushed.set()
        return original_git(repo, *args)

    monkeypatch.setattr(entrypoint, "_git", racing_git)
    outcomes = {}

    def claim(name, repo, commit):
        try:
            outcomes[name] = _release(repo, commit)
        except Exception as error:
            outcomes[name] = error

    threads = [
        threading.Thread(target=claim, args=("old", left, old)),
        threading.Thread(target=claim, args=("new", right, new)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert outcomes["new"]["commit"] == new
    assert isinstance(outcomes["old"], entrypoint.ReleaseRefused)
    assert "was claimed by Test" in str(outcomes["old"])
    assert new in str(outcomes["old"])
    assert git(left, "rev-parse", "rc.1-v0.21.5^{commit}") == new

    fresh = tmp_path / "fresh"
    git(tmp_path, "clone", "--quiet", origin, str(fresh))
    refs = git(fresh, "tag", "--list", "rc.*").splitlines()
    assert derive_next_version(published=None, bump="patch") == "0.21.5"
    assert next_attempt("0.21.5", refs) == 2


def test_a_concurrent_cut_of_another_version_is_stopped_before_dispatch(
        source, tmp_path, monkeypatch):
    from scripts.releases import entrypoint

    commit = git(source, "rev-parse", "HEAD")
    _origin, (left, right) = _clones(source, tmp_path)
    original_git = entrypoint._git

    def interleaved_git(repo, *args):
        # The other maintainer cuts 0.22.0 after this cut passed its pre-check.
        if repo == left and args[:2] == ("push", "origin") and args[-1] == "refs/tags/rc.1-v0.21.5":
            monkeypatch.setattr(entrypoint, "_git", original_git)
            _release(right, commit, bump="minor")
        return original_git(repo, *args)

    monkeypatch.setattr(entrypoint, "_git", interleaved_git)
    with pytest.raises(entrypoint.ReleaseRefused, match="more than one outstanding attempt"):
        _release(left, commit, execute=lambda command: pytest.fail(f"must not run {command}"))
    assert {"rc.1-v0.21.5", "rc.1-v0.22.0"} <= set(git(left, "tag", "--list", "rc.*").splitlines())
