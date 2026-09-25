"""Tests for `hermes curator status` output.

Covers:
- y0shualee's "least recently active" semantic (view/patch/use all count as activity).
- The most-used / least-used rankings by activity_count so users can see which
  skills actually get exercised.
"""

from __future__ import annotations

import io
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path

import pytest


@pytest.fixture
def curator_status_env(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with real agent-created skills on disk."""
    home = tmp_path / ".hermes"
    skills = home / "skills"
    skills.mkdir(parents=True)
    (home / "logs").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    import importlib
    import hermes_constants
    importlib.reload(hermes_constants)
    from tools import skill_usage
    importlib.reload(skill_usage)
    from agent import curator
    importlib.reload(curator)
    from hermes_cli import curator as curator_cli
    importlib.reload(curator_cli)

    def _write_skill(name: str) -> None:
        d = skills / name
        d.mkdir()
        (d / "SKILL.md").write_text(
            "---\n"
            f"name: {name}\n"
            "description: test\n"
            "version: 1.0.0\n"
            "metadata:\n"
            "  hermes:\n"
            "    agent_created: true\n"
            "---\n"
            f"# {name}\n"
        )

    return {
        "home": home,
        "skills": skills,
        "make_skill": _write_skill,
        "skill_usage": skill_usage,
        "curator_cli": curator_cli,
    }


# ---------------------------------------------------------------------------
# Unmanaged blind spot + adopt verb
# ---------------------------------------------------------------------------


def _capture_status(curator_cli) -> str:
    """Run `hermes curator status` against the fixture's isolated HERMES_HOME and
    return its stdout.

    The merge that absorbed upstream's test-prune lanes dropped this helper (the
    fork's own version of the file defines it at ``12feabce13``), leaving the two
    blocked-writes tests below with a NameError at call time — a collection-clean
    file that fails only when run. Restored from the fork's copy.
    """
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = curator_cli._cmd_status(Namespace())
    assert rc == 0
    return buf.getvalue()


def test_list_unmanaged_itemizes_and_explains(curator_status_env):
    """`status` gives the count; this gives the names plus WHY each is
    unmanaged, so the user can decide what to adopt."""
    env = curator_status_env
    env["make_skill"]("legacy-one")
    env["make_skill"]("managed-one")
    env["skill_usage"].mark_agent_created("managed-one")

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = env["curator_cli"]._cmd_list_unmanaged(Namespace())
    out = buf.getvalue()

    assert rc == 0
    assert "legacy-one" in out
    assert "managed-one" not in out
    assert "no marker" in out or "created_by:null" in out
    assert "curator adopt" in out


def test_status_surfaces_blocked_writes_from_last_run(curator_status_env):
    """A skill_manage refusal recorded by the last LLM review pass (see
    agent.curator._extract_blocked_writes) must show up in `hermes curator
    status` durably -- not just as a scrollback line the user had to be
    watching live to see. Regression coverage for the gap where a
    'Refusing background curator patch ...' refusal was indistinguishable
    from a generic failed tool call and left no record anywhere `status`
    (or any other post-hoc surface) would show."""
    env = curator_status_env
    curator = __import__("agent.curator", fromlist=["curator"])
    state = curator.load_state()
    state["blocked_writes"] = [
        {
            "skill": "quicken-interaction",
            "action": "patch",
            "reason": (
                "Refusing background curator patch for skill "
                "'quicken-interaction': the skill is not curator-managed "
                "(created_by=None). Run `hermes curator adopt "
                "quicken-interaction` to opt it in."
            ),
            "hint": "hermes curator adopt quicken-interaction",
        }
    ]
    curator.save_state(state)

    out = _capture_status(env["curator_cli"])

    assert "blocked writes" in out.lower()
    assert "quicken-interaction" in out
    assert "hermes curator adopt quicken-interaction" in out


def test_status_omits_blocked_writes_section_when_empty(curator_status_env):
    """A clean run (no refusals) must not print a phantom 'blocked writes'
    section -- the section should only appear when there's something
    actionable to show."""
    env = curator_status_env
    out = _capture_status(env["curator_cli"])
    assert "blocked writes" not in out.lower()


