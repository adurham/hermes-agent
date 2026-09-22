"""The release workflow's dependency graph enforces publication ordering."""
from pathlib import Path

from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[2]


def workflow(name):
    return YAML(typ="base").load((ROOT / ".github/workflows" / name).read_text(encoding="utf-8"))


def ancestors(jobs, name):
    seen = set()
    pending = [name]
    while pending:
        needs = jobs[pending.pop()].get("needs", [])
        for item in [needs] if isinstance(needs, str) else needs:
            if item not in seen:
                seen.add(item)
                pending.append(item)
    return seen


def test_release_reuses_whole_ci_and_docker_before_publication():
    jobs = workflow("stable-release.yml")["jobs"]
    assert jobs["ci"]["uses"] == "./.github/workflows/ci.yaml"
    assert jobs["ci"]["with"]["release"] == "true"
    assert "secrets" not in jobs["ci"]
    assert jobs["docker"]["uses"] == jobs["publish-docker"]["uses"] == jobs["promote-docker"]["uses"]
    assert jobs["docker"]["with"]["release-phase"] == "test"
    assert "ci" in ancestors(jobs, "docker")
    required = {"ci", "docker", "nix", "pm-bundle", "install-e2e", "windows-packaged", "macos-packaged", "termux-checks", "windows-live", "candidates"}
    assert required <= ancestors(jobs, "acceptance")
    for name in ("publish-docker", "publish-bundles"):
        assert required <= ancestors(jobs, name)
    for name in ("promote-docker", "promote-bundles"):
        assert {"publish-docker", "publish-bundles", "publication"} <= ancestors(jobs, name)
    assert {"promote-docker", "promote-bundles"} <= ancestors(jobs, "complete")
    for name in ("acceptance", "publication", "complete"):
        assert jobs[name]["if"] == "always()"


def test_all_applicable_ci_jobs_are_aggregated_and_desktop_e2e_stays_deferred():
    jobs = workflow("ci.yaml")["jobs"]
    checks = {name for name, job in jobs.items() if "uses" in job}
    assert checks <= set(jobs["all-checks-pass"]["needs"])
    assert jobs["e2e-desktop"]["if"] == "false"
    assert "workflow_call" in workflow("ci.yaml")["on"]


def test_claim_custody_and_final_payload_identity_reach_every_privileged_phase():
    release = workflow("stable-release.yml")
    jobs = release["jobs"]
    assert release["on"]["workflow_dispatch"]["inputs"]["autopublish"]["required"] == "true"
    assert {"claim-tag", "claim-object", "tag", "commit", "version", "release-id"} <= \
        set(jobs["admit"]["outputs"])
    for name in ("candidates", "publish-bundles", "promote-bundles"):
        call = jobs[name]["with"]
        assert call["tag"] == "${{ needs.admit.outputs.tag }}"
        assert call["claim-tag"] == "${{ needs.admit.outputs.claim-tag }}"
        assert call["claim-object"] == "${{ needs.admit.outputs.claim-object }}"
    for name in ("docker", "nix", "pm-bundle"):
        assert jobs[name]["with"]["version"] == "${{ needs.admit.outputs.version }}"
    complete = jobs["complete"]["steps"]
    final = next(i for i, step in enumerate(complete) if step.get("name", "").startswith("Create the final tag"))
    render = next(i for i, step in enumerate(complete) if step.get("name", "").startswith("Render the admitted"))
    assert final < render
