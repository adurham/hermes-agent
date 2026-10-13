"""``agent/onboarding_flow.py``: the beats, the run record and every event, across machine fixtures.

Sequences named after lab runs replay what those runs did; the lab bugs they showed are the
assertions (``.scratch/onboarding-plan/NOTES-onboarding-flow.md``).
"""

from __future__ import annotations

import pytest

from agent import onboarding_flow as flow
from agent.onboarding_flow import (
    DONE, PENDING, SHOWN, SKIPPED, Answered, Applied, Done, Facts, Recorded, Said, Shown, SkipAll, Skipped,
    TimedOut, apply, expire, new_record, next_beat,
)
from agent.onboarding_first_tasks import eligible_tasks
from agent.onboarding_text import opening_prompt, reminder, turn_note

NOTES = [{"id": "notes", "label": "Notes to wiki"}]

MACHINES = {
    "mac": Facts(os="macos", arch="arm64", machine_kind="Mac", plugins=("blender",)),
    "windows_rtx": Facts(os="windows", arch="arm64", machine_kind="Spark", nvidia_platform="rtx_spark",
                         plugins=("nvidia-app", "nvidia-broadcast", "blender")),
    "spark": Facts(os="linux", arch="arm64", machine_kind="Spark", nvidia_platform="dgx_spark", plugins=("blender",)),
    "linux": Facts(os="linux", arch="amd64", machine_kind="computer", plugins=()),
    "windows_laptop": Facts(os="windows", arch="amd64", machine_kind="PC", nvidia_platform="discrete",
                            plugins=("nvidia-app", "nvidia-broadcast", "blender")),
    "remote": Facts(os="linux", arch="amd64", machine_kind="computer", facts_source="remote", tours=False),
}


def run(facts: Facts, *events):
    """Apply events in order from a fresh record; returns every step."""
    record, steps = new_record(facts, now=0), []
    for clock, event in enumerate(events, start=1):
        step = apply(record, event, facts, now=float(clock))
        record = step.record
        steps.append(step)
    return steps


def answer_all(facts: Facts) -> list:
    """The happy path for a machine: every beat it gets, shown and answered on its card."""
    answers = {"name": {"name": "Sid"}, "theme": {"accent": "Ultraviolet"}, "layout": {"layout": "terminal-deck"},
               "connectors": {"connected": ["slack"], "newly_connected": ["slack"]},
               "plugins": {"installed": list(facts.plugins[:1])}, "tour": {"tour": "quick"}}
    record, events = new_record(facts), []
    while (beat := next_beat(record)) is not None:
        first = eligible_tasks(facts, record["picks"])[0]
        answer = answers.get(beat) or {"build": first.id}
        options = [{"id": first.id, "label": first.label}] if beat == "build" else None
        for event in (Shown(beat, f"call_{beat}", options), Answered(beat, answer)):
            events.append(event)
            record = apply(record, event, facts).record
    return [*events, Done()]


@pytest.mark.parametrize("machine", sorted(MACHINES))
def test_happy_path_settles_every_beat_this_machine_gets_and_completes(machine):
    facts = MACHINES[machine]
    steps = run(facts, *answer_all(facts))

    final = steps[-1]
    assert final.result == {"completed": True} and final.reminder == "complete"
    assert final.record["phase"] == flow.COMPLETE and final.record["ended_by"] == "done"
    assert set(final.record["beats"].values()) <= {DONE, SKIPPED}
    assert (final.record["beats"]["plugins"] == SKIPPED) is not bool(facts.plugins)
    assert (final.record["beats"]["tour"] == SKIPPED) is not facts.tours
    # Every answer names the next beat, and every reminder renders.
    for step in steps:
        assert ("next" in step.result) is isinstance(step.result.get("answer"), dict)
        assert isinstance(reminder(step.reminder, step.record, facts), str)
    # Each after_<beat> reminder names the card that really comes next.
    afters = [step for step in steps if step.reminder.startswith("after_")]
    assert afters
    for step in afters:
        upcoming = next_beat(step.record)
        expected = f"shows the {upcoming} card" if upcoming else 'setup_beat(beat="done")'
        assert expected in reminder(step.reminder, step.record, facts), step.reminder


@pytest.mark.parametrize("machine", sorted(MACHINES))
def test_the_full_name_never_reaches_the_record_or_the_opening(machine):
    facts = Facts(**{**MACHINES[machine].__dict__, "full_name": "Ada Lovelace"})

    assert "Ada Lovelace" not in repr(new_record(facts))
    assert "Ada Lovelace" not in opening_prompt(facts)


def test_run_hrs0_typed_answer_is_applied_and_never_reshows_the_card():
    """hrs0: "elite sounds like my type ;)" got a second layout picker. Typed words close the card;
    the model applies them with set_theme and the hook settles the beat."""
    facts = MACHINES["mac"]
    steps = run(facts, Shown("name"), Answered("name", {"name": "Sid"}), Shown("theme"),
                Answered("theme", {"accent": "Ultraviolet"}), Shown("layout"),
                Said("layout", "elite sounds like my type ;)"),
                Applied("set_theme", {"layout": "terminal-deck"}, {"success": True, "applied": {"layout": "terminal-deck"}}))

    said, applied = steps[-2], steps[-1]
    assert said.result == {"beat": "layout", "said": "elite sounds like my type ;)"}
    assert said.record["waiting"] is None and said.record["beats"]["layout"] == SHOWN
    assert "apply it yourself" in reminder("said", said.record, facts, "layout")
    assert applied.record["beats"]["layout"] == DONE and applied.record["picks"]["layout"] == "terminal-deck"
    assert next_beat(applied.record) == "connectors"


def test_set_theme_settling_the_layout_beat_tells_the_model_the_next_beat():
    """Without a reminder the model recorded the applied layout with setup_beat and was refused."""
    facts = MACHINES["mac"]
    steps = run(facts, Shown("name"), Answered("name", {"name": "Sid"}), Shown("theme"),
                Answered("theme", {"accent": "Ultraviolet"}), Shown("layout"), Said("layout", "basic"),
                Applied("set_theme", {"layout": "sidebar-left"}, {"success": True, "applied": {"layout": "sidebar-left"}}),
                Applied("set_theme", {"layout": "terminal-deck"}, {"success": True, "applied": {"layout": "terminal-deck"}}))

    settled, again = steps[-2], steps[-1]
    assert settled.reminder == "after_layout"
    text = reminder(settled.reminder, settled.record, facts)
    assert text.startswith("Layout Basic is on, already applied.") and "Do not call set_theme" in text
    assert 'setup_beat(beat="next") shows the connectors card' in text
    # A second set_theme on the settled layout steers forward instead of returning nothing.
    assert again.reminder == "already_layout" and again.record["picks"]["layout"] == "terminal-deck"


def test_live_run_a_set_theme_after_the_theme_card_is_told_it_is_already_set():
    """Sid's run: the theme card applied green, then the model called set_theme(accent=green) again and a
    second receipt drew. The card's reminder says it is applied; a repeat call names the next step."""
    facts = MACHINES["mac"]
    steps = run(facts, Shown("name"), Answered("name", {"name": "Sid"}), Shown("theme"),
                Answered("theme", {"accent": "green"}),
                Applied("set_theme", {"accent": "green"}, {"success": True, "applied": {"accent": "green"}}))

    answered, repeat = steps[-2], steps[-1]
    card_text = reminder(answered.reminder, answered.record, facts)
    assert "already applied" in card_text and "Do not call set_theme" in card_text
    assert repeat.reminder == "already_theme"
    text = reminder(repeat.reminder, repeat.record, facts)
    assert "The theme was already set" in text and "Do not apply it again" in text
    assert 'setup_beat(beat="next") shows the layout card' in text


def test_run_hrs0_off_script_questions_keep_one_card_and_continue_settles_it():
    """hrs0: each off-script question re-rendered the apps cards (four open copies), and a typed
    "continue" left the beat shown. Bringing a card back is a new Shown of the same beat; a value
    given in words settles it."""
    facts = MACHINES["mac"]
    events = [Shown("name"), Answered("name", {"name": "Sid"}), Skipped("theme"), Skipped("layout")]
    for question in ("tell me about SpaceX first", "who runs it?"):
        events += [Shown("connectors", question), Said("connectors", question)]
    events += [Shown("connectors"), Said("connectors", "continue"), Recorded("connectors", "continue")]
    steps = run(facts, *events)

    assert all(step.record["waiting"] is None for step in steps if isinstance(step.result.get("said"), str))
    assert steps[-1].result == {"recorded": "connectors", "next": "plugins"}
    assert steps[-1].record["beats"]["connectors"] == DONE


def test_run_3ofn_pick_for_me_applies_layout_and_offers_the_look_around_once():
    """3ofn: "look around and pick one for me" ended in Elite through set_theme; the look-around
    offer rides the layout result once."""
    facts = MACHINES["mac"]
    steps = run(facts, Shown("name"), Answered("name", {"name": "Sid"}), Shown("theme"),
                Answered("theme", {"accent": "Ember"}), Shown("layout"),
                Said("layout", "look around and pick one for me"),
                Applied("set_theme", {"layout": "terminal-deck"}, {"success": True, "applied": {"layout": "terminal-deck"}}),
                Shown("connectors"))

    layout_done = steps[-2].record
    assert "look around" in reminder("after_layout", layout_done, facts)
    assert steps[-1].record["look_around"] == "offered"
    assert "look around" not in reminder("after_layout", steps[-1].record, facts)


def test_skip_setup_while_a_card_waits_ends_the_run_and_refuses_later_beats():
    facts = MACHINES["windows_laptop"]
    steps = run(facts, Shown("name"), Answered("name", {"name": "Sid"}), Shown("theme"), SkipAll(), Shown("theme"))

    skipped, refused = steps[-2], steps[-1]
    assert skipped.result == {"skipped_all": True} and skipped.reminder == "skip_all"
    assert skipped.record["phase"] == flow.PHASE_SKIPPED and skipped.record["waiting"] is None
    assert skipped.record["beats"]["name"] == DONE
    assert all(skipped.record["beats"][beat] == SKIPPED for beat in flow.BEAT_IDS if beat != "name")
    assert "error" in refused.result and refused.record == skipped.record


def test_walks_away_closes_the_card_and_the_turn_note_brings_it_back():
    facts = MACHINES["linux"]
    step = run(facts, Shown("name"), Answered("name", {"name": "Sid"}), Shown("theme"), TimedOut("theme"))[-1]

    assert step.result == {"beat": "theme", "timed_out": True}
    assert step.record["beats"]["theme"] == SHOWN and step.record["waiting"] is None
    assert 'setup_beat(beat="theme")' in turn_note(step.record)


def test_out_of_order_card_leaves_earlier_pending_beats_for_next():
    facts = MACHINES["mac"]
    step = run(facts, Shown("connectors"), Answered("connectors", {"connected": [], "newly_connected": []}))[-1]

    assert step.record["beats"]["name"] == PENDING
    assert step.result["next"] == "name"


def test_a_later_card_settles_an_earlier_card_left_open():
    facts = MACHINES["mac"]
    step = run(facts, Shown("name"), Said("name", "hi"), Shown("theme"))[-1]

    assert step.record["beats"]["name"] == DONE and step.record["beats"]["theme"] == SHOWN


def test_a_done_beat_shown_again_stays_done_and_records_the_new_pick():
    facts = MACHINES["mac"]
    step = run(facts, Shown("theme"), Answered("theme", {"accent": "Ember"}), Shown("theme"),
               Answered("theme", {"accent": "Ultraviolet"}))[-1]

    assert step.record["beats"]["theme"] == DONE and step.record["picks"]["accent"] == "Ultraviolet"


def test_done_is_refused_before_the_build_card_was_reached():
    step = run(MACHINES["mac"], Shown("name"), Answered("name", {"name": "Sid"}), Done())[-1]

    assert "error" in step.result and step.result["next"] == "theme"
    assert step.record["phase"] == flow.ONBOARDING


def test_a_value_outside_the_cards_options_is_refused():
    step = run(MACHINES["mac"], Recorded("tour", "everything"))[-1]

    assert "quick" in step.result["error"] and step.record["beats"]["tour"] == PENDING


def test_a_beat_this_machine_skips_is_never_carded():
    step = run(MACHINES["linux"], Shown("plugins"))[-1]

    assert step.result["skipped"] is True and step.record["waiting"] is None


@pytest.mark.parametrize(("tool", "args", "result", "beat", "pick"), [
    ("set_theme", {"accent": "violet"}, {"success": True, "applied": {"accent": "custom:#8a2be2"}}, "theme",
     ("accent", "custom:#8a2be2")),
    ("set_theme", {"picker": "accent"}, {"success": True, "picked": {"accent": None, "mode": "dark"}}, "theme",
     ("mode", "dark")),
    ("manage_connections", {"action": "display"}, {"connected": ["gmail"], "newly_connected": [], "suggested": []},
     "connectors", ("connected", ["gmail"])),
    ("gui_tour", {"action": "start", "preset": "quick"}, {"success": True}, "tour", ("tour", "quick")),
])
def test_general_tool_results_settle_their_beat(tool, args, result, beat, pick):
    step = run(MACHINES["mac"], Applied(tool, args, result))[-1]

    assert step.record["beats"][beat] == DONE
    assert step.record["picks"][pick[0]] == pick[1]


@pytest.mark.parametrize(("tool", "args", "result"), [
    ("set_theme", {"accent": "nope"}, {"success": False, "error": "unknown", "available": []}),
    ("set_theme", {"picker": "layout"}, {"said": "hmm"}),
    ("manage_connections", {"action": "display"}, {"timed_out": True}),
    ("manage_connections", {"action": "status"}, {"connected": ["gmail"]}),
    ("gui_tour", {"action": "next"}, {"success": True}),
])
def test_results_that_pick_nothing_settle_nothing(tool, args, result):
    step = run(MACHINES["mac"], Applied(tool, args, result))[-1]

    assert set(step.record["beats"].values()) <= {PENDING, SKIPPED}


@pytest.mark.parametrize("options", [
    None, [], [{"id": "games", "label": "Games"}], [{"id": "scan", "label": "Scan"}],
    [{"id": "hermes", "label": "Explain how Hermes works"}], [{"id": x, "label": x} for x in ("notes", "site", "repo", "watch", "explain")],
    [{"id": "notes", "label": "A"}, {"id": "notes", "label": "B"}], [{"id": "notes", "label": "x" * 33}],
    [{"id": "notes", "label": " "}], ["notes"],
])
def test_build_picks_are_refused_with_the_eligible_ids_and_no_card(options):
    step = run(MACHINES["linux"], Shown("build", "call_b", options))[-1]

    assert "notes, explain, youtube, watch, site, repo" in step.result["error"]
    assert step.record["beats"]["build"] == PENDING and step.record["waiting"] is None


def test_valid_picks_are_the_card_with_how_hermes_works_last_and_any_card_id_answers_it():
    facts = MACHINES["mac"]
    picks = [{"id": "mac", "label": "Your Mac, sorted"}, {"id": "repo", "label": "Map your repo"}]
    shown = run(facts, Shown("build", "call_b", picks))[-1]
    card = flow.BEAT_BY_ID["build"].card(facts, shown.record["picks"])

    assert card == {"options": [*picks, {"id": "hermes", "label": "Explain how Hermes works"}]}
    # Brought back without picks, the same card returns.
    assert apply(shown.record, Shown("build", "call_c"), facts).result == {}
    for value in ("repo", "hermes", "own"):
        assert apply(shown.record, Recorded("build", value), facts).record["picks"]["build"] == value
    assert "error" in apply(shown.record, Recorded("build", "notes"), facts).result


@pytest.mark.parametrize(("machine", "present", "absent"), [
    ("mac", "brew install --cask", "winget"),
    ("windows_laptop", "--accept-package-agreements", "brew"),
    ("windows_rtx", "x64 only", "brew"),
    ("linux", "flatpak install", "winget"),
    ("remote", "another computer", "brew"),
])
def test_build_guidance_carries_only_this_hosts_commands(machine, present, absent):
    text = reminder("build_notes", new_record(MACHINES[machine]), MACHINES[machine])

    assert present in text and absent not in text
    assert 'setup_beat(beat="done")' in text


def test_quiet_spell_after_the_build_card_completes_the_run():
    facts = MACHINES["mac"]
    record = run(facts, Shown("build", "", NOTES))[-1].record

    assert expire(record, now=record["updated_at"] + flow.QUIET_COMPLETE_S - 1)["phase"] == flow.ONBOARDING
    ended = expire(record, now=record["updated_at"] + flow.QUIET_COMPLETE_S)
    assert ended["phase"] == flow.COMPLETE and ended["ended_by"] == "timeout"
    assert expire(new_record(facts), now=10**9)["phase"] == flow.ONBOARDING


def test_a_card_answer_outside_the_cards_options_is_refused_and_the_beat_stays_open():
    facts = MACHINES["mac"]
    record = run(facts, Shown("build", "call_b", NOTES))[-1].record
    step = apply(record, Answered("build", {"build": "games"}), facts, now=2.0)

    assert "error" in step.result and step.record["beats"]["build"] == SHOWN
    assert step.record["picks"]["build"] is None and step.record["waiting"] is None
    for beat, answer in [("name", {"name": "  "}), ("layout", {"layout": "x"}), ("theme", {"accent": 3}),
                         ("plugins", {"installed": ["nvidia-app"]}), ("connectors", {"installed": ["blender"]})]:
        assert "error" in apply(new_record(facts), Answered(beat, answer), facts, now=1.0).result


def test_a_click_carries_the_option_label_for_the_transcript_and_only_the_id_is_recorded():
    facts = MACHINES["mac"]
    record = run(facts, Shown("tour", "call_t"))[-1].record
    step = apply(record, Answered("tour", {"tour": "quick", "label": "Quick look"}), facts, now=2.0)

    assert step.result["answer"] == {"tour": "quick", "label": "Quick look"} and step.reminder == "tour_quick"
    assert step.record["picks"]["tour"] == "quick" and "label" not in step.record["picks"]
    assert "error" in apply(record, Answered("tour", {"tour": "quick", "label": 3}), facts, now=2.0).result


def test_a_layout_or_theme_given_in_words_is_applied_by_set_theme_not_recorded():
    facts = MACHINES["mac"]
    for beat, value in [("layout", "terminal-deck"), ("theme", "Ultraviolet")]:
        step = apply(new_record(facts), Recorded(beat, value), facts, now=1.0)
        assert "set_theme" in step.result["error"] and step.record["beats"][beat] == PENDING
    assert '"name":"set_theme"' in reminder("said", new_record(facts), facts, "layout")


def test_no_turn_note_once_every_card_is_settled():
    facts = MACHINES["linux"]
    steps = run(facts, *answer_all(facts)[:-1])

    assert next_beat(steps[-1].record) is None and turn_note(steps[-1].record) == ""
