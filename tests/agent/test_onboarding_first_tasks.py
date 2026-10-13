"""``agent/onboarding_first_tasks.py``: which first tasks the model may put on the build card."""

from __future__ import annotations

import pytest

from agent.onboarding_first_tasks import FIRST_TASKS, eligible_tasks
from agent.onboarding_flow import Facts

MAC = Facts(os="macos", arch="arm64", machine_kind="Mac")
PC = Facts(os="windows", arch="amd64", machine_kind="PC", nvidia_platform="discrete")
ALWAYS = ["notes", "explain", "youtube", "watch", "site", "repo"]


def ids(facts: Facts, **picks) -> list[str]:
    return [task.id for task in eligible_tasks(facts, picks)]


def test_every_pill_fits_and_how_hermes_works_is_never_pickable():
    assert all(len(task.label) <= 32 for task in FIRST_TASKS)
    assert "hermes" not in ids(MAC, installed=["blender"], connected=["gmail"])


@pytest.mark.parametrize(("plugin", "row"), [("nvidia-app", "games"), ("nvidia-broadcast", "broadcast"),
                                            ("blender", "blender")])
def test_a_plugin_row_needs_its_plugin_installed(plugin, row):
    assert row in ids(PC, installed=[plugin])
    assert row not in ids(PC, installed=[])


def test_brief_and_week_need_an_app_connected_this_run_and_mac_helpers_need_macos():
    assert {"brief", "week"} <= set(ids(PC, connected=["gmail"]))
    assert ids(PC, connected=[]) == ALWAYS
    assert ids(MAC) == ["mac", *ALWAYS]
