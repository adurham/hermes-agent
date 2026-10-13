"""``agent/onboarding_first_tasks.py``: which first tasks the model may put on the build card."""

from __future__ import annotations

import pytest

from agent.onboarding_first_tasks import FIRST_TASKS, eligible_tasks, pick_error
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

    goal, skills = text.split("\n")[:2]
    assert goal == ('They picked "Explain a topic I care about". Your job: explain a topic they care about on one '
                    "designed page with an interactive figure.")
    assert skills == ("Load these skills with skill_view first: grounded-citations, claude-design, "
                      "blocked-page-recovery, humanizer.")
    # Plugin skills come with the plugin, which is the row's gate; a typed task starts from hermes-agent.
    assert reminder("build_games", new_record(PC), PC, has_skill=lambda name: False).split("\n")[1] == (
        "Load these skills with skill_view first: nvidia-app:nvidia-app.")
    assert reminder("build_own", new_record(MAC), MAC).split("\n")[:2] == [
        "Your job: build the task they named in their own words.",
        "Load these skills with skill_view first: hermes-agent, humanizer."]


def test_a_task_prompt_names_the_label_they_clicked_and_reads_in_sections():
    record = new_record(MAC)
    record["picks"]["build_options"] = [{"id": "repo", "label": "Map the hermes repo"}]
    text = reminder("build_repo", record, MAC)

    assert text.startswith('They picked "Map the hermes repo". Your job: ')
    for section in ("\nSteps:\n1. ", "\nOutput: ", "\nQuality bar: ", "\nDone when: ", "\n\nHow to work:\n- ",
                    "\n\nThis computer:\n- This Mac", "\n\nWhen it is shown:\n"):
        assert section in text, section
    assert "\u2014" not in text


def test_every_catalog_row_is_a_full_prompt():
    for task in FIRST_TASKS:
        assert task.goal and task.output and task.quality and task.done and 2 <= len(task.steps) <= 6, task.id


def test_how_hermes_works_reads_the_docs_and_builds_three_screens():
    text = reminder("build_hermes", new_record(MAC), MAC)

    assert text.split("\n")[1] == ("Load these skills with skill_view first: hermes-agent, claude-design, p5js, "
                                   "humanizer.")
    assert "https://hermes-agent.nousresearch.com/docs" in text and "web_extract" in text
    assert all(f"Screen {n}" in text for n in (1, 2, 3)) and "~/hermes-first-task/" in text


@pytest.mark.parametrize(("facts", "opens"), [
    (MAC, "`open <file>`"), (PC, '`cmd.exe /c start "" "<file>"`'), (Facts(os="linux"), "`xdg-open <file>`"),
])
def test_a_local_backend_opens_the_page_in_the_default_browser_and_never_names_desktop_preview(facts, opens):
    text = reminder("build_hermes", new_record(facts), facts)
    site = reminder("build_site", new_record(facts), facts)

    assert f"open it in the default browser with {opens}" in text
    assert f"Open a page in their default browser with {opens}" in text
    assert "Say the page is open only after the open command succeeded." in text
    assert "desktop_preview" not in text and "desktop_preview" not in site
    assert "opened in the default browser" in site


def test_windows_opens_with_a_single_slash_cmd_and_a_banner_is_not_success():
    """Vesper run 1: Git's `start` wrapper passes a literal `//c` under MSYS_NO_PATHCONV=1, cmd printed its
    banner, exited 0 and nothing opened; the model still said the page was open."""
    text = reminder("build_hermes", new_record(PC), PC)

    assert '`start "" <file>`' not in text and "//c" not in text
    assert "full Windows path (C:/Users/...)" in text
    assert 'cmd banner ("Microsoft Windows [Version ...]") in the output is a failure' in text
    assert "cmd banner" not in reminder("build_hermes", new_record(MAC), MAC)


def test_a_remote_backend_shows_the_page_with_desktop_preview_through_tool_call():
    facts = Facts(os="linux", facts_source="remote")
    text = reminder("build_hermes", new_record(facts), facts)
    call = 'tool_call {"calls":[{"name":"desktop_preview","arguments":{"action":"open","url":"<file>"}}]}'

    assert f"show it with desktop_preview: {call}" in text
    assert "Say the page is open only after desktop_preview returned success." in text
    assert "default browser" not in text and "xdg-open" not in text
    assert "shown with desktop_preview" in reminder("build_site", new_record(facts), facts)


def test_the_app_tasks_carry_the_connectors_playbook_and_the_menu_lists_only_eligible_rows():
    assert "connectors__<slug>__<ACTION>" in reminder("build_week", new_record(PC), PC)
    assert "connectors__" not in reminder("build_site", new_record(PC), PC)
    menu = reminder("tour_skip", new_record(MAC), MAC)
    assert "- mac (Set up my Mac helpers)" in menu and "keep its energy" in menu and "- brief" not in menu and "- hermes" not in menu


def test_every_too_long_label_is_named_with_its_length_in_one_refusal():
    long_a, long_b = "Today's brief from Linear and Slack", "My whole week laid out on one page"
    error = pick_error([{"id": "brief", "label": long_a}, {"id": "week", "label": long_b},
                        {"id": "repo", "label": "Map one of my repos"}], MAC, {"connected": ["slack"]})
    assert f'"{long_a}" is {len(long_a)} characters; the limit is 32' in error
    assert f'"{long_b}" is {len(long_b)} characters' in error
