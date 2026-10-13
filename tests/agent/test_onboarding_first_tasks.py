"""``agent/onboarding_first_tasks.py``: which first tasks the model may put on the build card."""

from __future__ import annotations

import pytest

from agent.onboarding_first_tasks import FIRST_TASKS, eligible_tasks
from agent.onboarding_flow import Facts, new_record
from agent.onboarding_text import reminder

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


def test_a_task_reminder_opens_with_its_skills_and_drops_a_bundled_skill_this_install_lacks():
    text = reminder("build_explain", new_record(MAC), MAC, has_skill=lambda name: name != "p5js")

    assert text.startswith("Load these skills with skill_view first: grounded-citations, claude-design, "
                           "blocked-page-recovery, humanizer.\n")
    # Plugin skills come with the plugin, which is the row's gate; a typed task starts from hermes-agent.
    assert reminder("build_games", new_record(PC), PC, has_skill=lambda name: False).startswith(
        "Load these skills with skill_view first: nvidia-app:nvidia-app.")
    assert reminder("build_own", new_record(MAC), MAC).startswith(
        "Load these skills with skill_view first: hermes-agent, humanizer.")


@pytest.mark.parametrize(("facts", "opens"), [
    (MAC, "`open <file>`"), (PC, '`start "" <file>`'), (Facts(os="linux"), "`xdg-open <file>`"),
    (Facts(os="linux", facts_source="remote"), "show it with desktop_preview"),
])
def test_how_hermes_works_opens_in_the_default_browser_unless_hermes_runs_elsewhere(facts, opens):
    assert opens in reminder("build_hermes", new_record(facts), facts)


def test_the_app_tasks_carry_the_connectors_playbook_and_the_menu_lists_only_eligible_rows():
    assert "connectors__<slug>__<ACTION>" in reminder("build_week", new_record(PC), PC)
    assert "connectors__" not in reminder("build_site", new_record(PC), PC)
    menu = reminder("tour_skip", new_record(MAC), MAC)
    assert "- mac (Mac helpers)" in menu and "- brief" not in menu and "- hermes" not in menu
