"""Stable publication is ordered, held by default, and idempotent.

A green draft publishes when it opted into autopublish, or when a later green
release needs it resolved. A running older claim blocks only the versions above
it; already-resolvable older green claims still make progress.
"""
import json

import pytest


def _claims(*rows):
    return [dict(version=version, state=state, autopublish=autopublish)
            for version, state, autopublish in rows]


def _flips(steps):
    return [step["flip"] for step in steps if "flip" in step]


def test_a_sole_green_draft_waits_without_autopublish():
    from scripts.releases.sequencer import plan

    assert plan(_claims(("0.21.5", "green", False)), head="0.21.4") == []


def test_autopublish_flips_the_current_green_release():
    from scripts.releases.sequencer import plan

    steps = plan(_claims(("0.21.5", "green", True)), head="0.21.4")
    assert _flips(steps) == ["0.21.5"]
    assert steps[-1] == {"advance": "0.21.5"}


def test_a_newer_green_release_flushes_the_older_waiting_draft():
    from scripts.releases.sequencer import plan

    steps = plan(_claims(
        ("0.21.5", "green", False),
        ("0.21.6", "green", False),
    ), head="0.21.4")

    assert _flips(steps) == ["0.21.5"]
    assert steps[-1] == {"advance": "0.21.5"}


def test_green_progress_before_a_running_blocker_is_preserved():
    from scripts.releases.sequencer import plan

    steps = plan(_claims(
        ("0.21.5", "green", False),
        ("0.21.6", "running", False),
        ("0.21.7", "green", True),
    ), head="0.21.4")

    assert _flips(steps) == ["0.21.5"]
    assert steps[-1] == {"advance": "0.21.5"}


def test_a_burned_claim_is_spent_and_skipped():
    from scripts.releases.sequencer import plan

    steps = plan(_claims(
        ("0.21.5", "burned", False),
        ("0.21.6", "green", True),
    ), head="0.21.4")

    assert _flips(steps) == ["0.21.6"]
    assert steps[-1] == {"advance": "0.21.6"}


def test_advances_each_published_version_and_ignores_a_later_burned_claim():
    from scripts.releases.sequencer import plan

    assert plan(_claims(
        ("0.21.5", "published", False),
        ("0.21.6", "published", False),
    ), head="0.21.4") == [
        {"advance": "0.21.5"},
        {"advance": "0.21.6"},
    ]
    assert plan(_claims(
        ("0.21.5", "green", False),
        ("0.21.6", "burned", False),
    ), head="0.21.4") == []


def test_explicit_publish_uses_the_same_oldest_first_plan():
    from scripts.releases.sequencer import plan

    assert plan(_claims(
        ("0.21.5", "green", False),
        ("0.21.6", "green", False),
    ), head="0.21.4", requested_version="0.21.6") == [
        {"flip": "0.21.5"},
        {"flip": "0.21.6"},
        {"advance": "0.21.5"},
        {"advance": "0.21.6"},
    ]


def test_published_history_at_or_below_the_head_is_an_idempotent_noop():
    from scripts.releases.sequencer import plan

    assert plan(_claims(
        ("0.21.4", "published", False),
        ("0.21.5", "green", False),
    ), head="0.21.4") == []


def test_an_unpublished_claim_below_the_head_is_refused():
    from scripts.releases.sequencer import plan

    with pytest.raises(ValueError, match="backwards"):
        plan(_claims(("0.21.4", "green", True)), head="0.21.5")


def test_reconcile_discovers_custody_flips_then_advances_oldest_first():
    from scripts.releases.sequencer import reconcile

    commit = "a" * 40
    tags = {}
    releases = []
    for index, version in enumerate(("0.21.5", "0.21.6"), start=1):
        claim_tag, tag = f"v{version}-rc", f"v{version}"
        claim_object, final_object = str(index) * 40, str(index + 2) * 40
        claim = {"schema": 1, "version": version, "commit": commit, "autopublish": False}
        final = {**claim, "claimTag": claim_tag, "claimTagObject": claim_object}
        tags[claim_tag] = (claim_object, commit, claim)
        tags[tag] = (final_object, commit, final)
        releases.append({
            "id": index, "tag_name": tag, "draft": True, "prerelease": False,
            "published_at": None,
        })

    events = []

    def run(argv):
        if argv[:2] == ["git", "fetch"]:
            return ""
        if argv[:3] == ["git", "ls-remote", "--tags"]:
            return "\n".join(
                f"{sha}\trefs/tags/{tag}\n{target}\trefs/tags/{tag}^{{}}"
                for tag, (sha, target, _message) in tags.items()
            )
        if argv[:2] == ["git", "rev-parse"]:
            return tags[argv[-1].removeprefix("refs/tags/")][0]
        if argv[:3] == ["git", "cat-file", "-t"]:
            return "tag"
        if argv[:3] == ["git", "tag", "-l"]:
            return json.dumps(tags[argv[3]][2])
        if argv[:4] == ["gh", "api", "--paginate", "--slurp"]:
            if "/releases?" in argv[-1]:
                return json.dumps([releases])
            return json.dumps([{"workflow_runs": []}])
        if argv[:3] == ["gh", "api", "--method"]:
            release_id = int(argv[4].rsplit("/", 1)[1])
            release = next(row for row in releases if row["id"] == release_id)
            release.update(tag_name=f"v0.21.{4 + release_id}", draft=False,
                           prerelease=False, published_at="2026-09-22T01:00:00Z")
            events.append(("flip", release["tag_name"]))
            return "{}"
        if argv[:2] == ["gh", "api"] and "/releases/" in argv[2]:
            release_id = int(argv[2].rsplit("/", 1)[1])
            return json.dumps(next(row for row in releases if row["id"] == release_id))
        raise AssertionError(argv)

    head = ["0.21.4"]

    def advance(record):
        events.append(("advance", record["tag"]))
        head[0] = record["version"]

    steps = reconcile(
        {"GITHUB_REPOSITORY": "example/project", "REQUESTED_VERSION": "0.21.6"},
        run=run, read_head=lambda: head[0], advance_head=advance,
    )

    assert steps == [
        {"flip": "0.21.5"}, {"flip": "0.21.6"},
        {"advance": "0.21.5"}, {"advance": "0.21.6"},
    ]
    assert events == [
        ("flip", "v0.21.5"), ("flip", "v0.21.6"),
        ("advance", "v0.21.5"), ("advance", "v0.21.6"),
    ]
