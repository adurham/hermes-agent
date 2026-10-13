"""The first-task catalog of onboarding's build beat: what the model may put on the build card.

Pure. Gates read only hard facts (this OS, plugins installed this run, apps connected this run);
whether a row fits the person is the model's call. ``agent/onboarding_flow.py`` checks the model's
picks against ``eligible_tasks``; ``agent/onboarding_text.py`` renders each row's reminder.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent.onboarding_flow import Facts

MAX_PICKS = 4
MAX_LABEL = 32
# The one fixed row: always last on the card, never one of the model's picks.
FIXED_ID = "hermes"
# The value a task the user names in words records; it is never on the card.
OWN_ID = "own"


@dataclass(frozen=True)
class FirstTask:
    """``skills`` load first, before the work; an entry ``a|b`` means one of them, whichever fits.
    Plugin skills use the ``<plugin>:<skill>`` form skill_view resolves. ``goal`` finishes the
    sentence "Your job: ..."; ``<open>`` in a step is replaced with how this host shows a page."""

    id: str
    label: str
    gate: Callable[[Facts, Mapping[str, Any]], bool]
    skills: tuple[str, ...]
    goal: str
    steps: tuple[str, ...]
    output: str
    quality: str
    done: str


def _always(facts: Facts, picks: Mapping[str, Any]) -> bool:
    return True


def _plugin(name: str) -> Callable[[Facts, Mapping[str, Any]], bool]:
    # The plugins card answers with every onboarding plugin installed, including ones from before this run.
    return lambda facts, picks: name in (picks.get("installed") or ())


def _connected(facts: Facts, picks: Mapping[str, Any]) -> bool:
    return bool(picks.get("connected"))


def _macos(facts: Facts, picks: Mapping[str, Any]) -> bool:
    return facts.os == "macos"


FIRST_TASKS: tuple[FirstTask, ...] = (
    FirstTask(
        "hermes", "Explain how Hermes works", lambda facts, picks: False, ("hermes-agent", "claude-design", "p5js"),
        goal="build a designed page of at least three screens that shows how Hermes works, from the real docs "
             "and real facts about this install",
        steps=(
            "Read the docs with web_extract, starting at https://hermes-agent.nousresearch.com/docs (the "
            "hermes-agent skill links the pages): the agent loop, skills and the learning loop, cron and "
            "automations, tools and toolsets, memory.",
            "Read facts from this install with your own tools: how many skills are installed, which apps are "
            "connected, which model is configured, and which skills this chat used.",
            "Write a first version within a minute and <open>. Then refine the same file.",
            "Screen 1, the agent loop: model, tools, results, back to the model, as an animated diagram.",
            "Screen 2, the skill learning loop: Hermes notices a procedure, saves it as a skill and loads it next "
            "time. Name the skills this chat used.",
            "Screen 3, automations and what they can do: cron jobs, webhooks, connectors, messaging, with "
            "examples that fit them.",
        ),
        output="One self-contained HTML file in ~/hermes-first-task/, at least three full-screen sections, each "
               "with its own graphic (a p5js animation or an SVG diagram).",
        quality="Designed with claude-design, not a plain document. Every name and number comes from the docs or "
                "this install. Copy is short and plain.",
        done="The page is open, every screen has its graphic and real facts, and you said where the file is."),
    FirstTask(
        "games", "Tune my games for this PC", _plugin("nvidia-app"), ("nvidia-app:nvidia-app",),
        goal="optimise the games on this PC with NVIDIA App and show what changed",
        steps=(
            "Check NVIDIA App is running, as the skill says. If it is not, say how to start it and wait.",
            "Read the installed games and their current settings.",
            "Ask once before changing anything, naming the games you will change.",
            "Apply the optimised settings, then read them back.",
        ),
        output="A before and after table in chat: one row per game, what changed and why.",
        quality="Only settings NVIDIA App reports and really changed. Never guess a value.",
        done="The changes are applied and read back, and the table shows each one."),
    FirstTask(
        "broadcast", "Make my mic and camera pro", _plugin("nvidia-broadcast"), ("nvidia-broadcast:nvidia-broadcast",),
        goal="set up NVIDIA Broadcast so their mic and camera are ready for a call",
        steps=(
            "Check NVIDIA Broadcast is running, as the skill says. If it is not, say how to start it and wait.",
            "Read the mic, speaker and camera devices and the effects this machine offers.",
            "Ask in one clarify what the setup is for, with three options such as a meeting, a stream or a "
            "noisy room.",
            "Turn on the effects that fit, then read them back.",
        ),
        output="A short list in chat: each effect, its device, before and after.",
        quality="Only effects Broadcast reports on this machine.",
        done="The effects are on and they know how to turn each one off."),
    FirstTask(
        "blender", "Build a 3D scene in Blender", _plugin("blender"), ("blender:blender",),
        goal="make one small 3D scene in Blender from a script, render it and show it",
        steps=(
            "Check Blender is running, as the skill says. If it is not, say how to start it and wait.",
            "Offer three scene ideas that fit them in one clarify.",
            "Write a short Python script, run it in Blender and render one still.",
            "Show the render, then offer one variation.",
        ),
        output="The script and the PNG render in ~/hermes-first-task/; the render shown with desktop_preview.",
        quality="A real Blender render, lit and framed. Never a placeholder.",
        done="The render is shown and you said where the script and image are."),
    FirstTask(
        "brief", "Brief me on today", _connected, ("claude-design",),
        goal="write today's brief from their connected apps",
        steps=(
            "Check the connected apps with manage_connections(action=\"status\").",
            "Read today from them: mail, calendar, messages, tasks. Read only.",
            "Show a first version early, then fill it in: what needs attention, what changed, what is next.",
            "Offer a morning cron job that sends the brief. Schedule it only on yes.",
        ),
        output="One designed HTML page in ~/hermes-first-task/, shown with desktop_preview.",
        quality="Every item comes from a connected app and says which one. Nothing invented.",
        done="The brief is shown and the morning job is set up or declined."),
    FirstTask(
        "week", "Recap my week on one page", _connected, ("weekly-review-planning", "claude-design"),
        goal="make one page that recaps their past seven days from the connected apps",
        steps=(
            "Check the connected apps with manage_connections(action=\"status\").",
            "Read the past seven days from them. Read only.",
            "Show a first version early, then fill it in: meetings, threads, work shipped, open loops.",
            "End with a short plan for next week built from the open loops.",
        ),
        output="One designed HTML page in ~/hermes-first-task/, shown with desktop_preview.",
        quality="Every item comes from a connected app and says which one. Nothing invented.",
        done="The page is shown with all five parts filled from real data."),
    FirstTask(
        "mac", "Set up my Mac helpers", _macos, ("apple-reminders", "apple-notes", "hermes-agent"),
        goal="set up their Mac helpers: one real reminder, one real note and a morning routine",
        steps=(
            "Ask in one clarify what the reminder and the note should be, with three concrete suggestions.",
            "Make the reminder in Reminders and the note in Notes with the skills.",
            "Propose a morning routine as a cron job: what it does and when. Schedule it only on yes.",
        ),
        output="The reminder and note in their apps and the cron job, each named in chat.",
        quality="Real items they asked for. No test items.",
        done="The reminder and note exist, and the morning job is set up or declined."),
    FirstTask(
        "notes", "Turn my notes into a wiki", _always, ("obsidian", "llm-wiki"),
        goal="turn their notes into a small linked wiki",
        steps=(
            "Ask which folder holds their notes. Never search the disk for it.",
            "Read the notes there and find the three main topics.",
            "Write an index page and three linked topic pages, each citing the notes it came from.",
            "Show the index page.",
        ),
        output="Four Markdown pages in ~/hermes-first-task/wiki/, unless they ask for their notes folder.",
        quality="Only what their notes say. Every link resolves.",
        done="The four pages exist, the links work and the index is shown."),
    FirstTask(
        "explain", "Explain a topic I care about", _always,
        ("grounded-citations", "claude-design", "p5js", "blocked-page-recovery"),
        goal="explain a topic they care about on one designed page with an interactive figure",
        steps=(
            "If the label they clicked names a topic, use it. Otherwise offer three topic guesses from what you "
            "learned about them in one clarify.",
            "Research it with citations. For a technical topic, also load arxiv.",
            "Show a first version early, then refine the same file.",
            "Add one interactive p5js figure that shows the core idea.",
        ),
        output="One self-contained HTML file in ~/hermes-first-task/, shown with desktop_preview, sources at "
               "the end.",
        quality="Designed with claude-design. Every claim is cited. The figure responds to input.",
        done="The page is shown with the figure working and every source listed."),
    FirstTask(
        "youtube", "Turn a video into a page", _always, ("youtube-content", "claude-design", "p5js"),
        goal="turn one video into a page that lays its ideas out visually",
        steps=(
            "Ask for the video link.",
            "Get the transcript with the skill.",
            "Pull out the main ideas, each with a timestamp and a quote.",
            "Show a first version early, then lay the ideas out visually, with one figure where it helps.",
        ),
        output="One self-contained HTML file in ~/hermes-first-task/, shown with desktop_preview; each "
               "timestamp links to that moment in the video.",
        quality="Designed with claude-design. Quotes are exact words from the transcript.",
        done="The page is shown and every idea has its timestamp and quote."),
    FirstTask(
        "watch", "Watch something for me daily", _always,
        ("competitor-news-monitor|product-price-monitor", "grounded-citations"),
        goal="watch something for them every day and report here",
        steps=(
            "Ask what to watch in one clarify, with three concrete options from what you know about them.",
            "Run the first check now and report it with sources.",
            "Propose a daily cron job: when it runs and what it reports. Schedule it only on yes.",
        ),
        output="The first report in chat, and the daily cron job.",
        quality="Every finding has a source link. Nothing invented.",
        done="The first report is shown and the daily job is set up or declined."),
    FirstTask(
        "site", "Design a page for me", _always, ("claude-design", "popular-web-designs", "design-md", "p5js"),
        goal="design one web page for them",
        steps=(
            "If you do not know what the page is for, ask in one line.",
            "Offer three design directions in one clarify.",
            "Show a first version early with a p5js hero, then refine the same file.",
            "Offer one variation.",
        ),
        output="One self-contained HTML file in ~/hermes-first-task/, shown with desktop_preview.",
        quality="Designed with the skills. Real content they gave you, never filler text.",
        done="The page is shown and a variation is offered."),
    FirstTask(
        "repo", "Map one of my repos", _always, ("codebase-inspection", "architecture-diagram", "github"),
        goal="map one of their code repositories on an architecture page",
        steps=(
            "Ask which repo, as a path or a URL, and ask before reading it.",
            "Show its language breakdown.",
            "Read its entry points and main modules.",
            "Draw the architecture page: the modules, how they connect, where to start reading.",
        ),
        output="One self-contained HTML file in ~/hermes-first-task/, shown with desktop_preview.",
        quality="Only modules that are really in the repo, named by their paths.",
        done="The page is shown and every box on it maps to a real path."),
)
# A task the user names in words: the same prompt shape, without a catalog row.
OWN_TASK = FirstTask(
    OWN_ID, "", lambda facts, picks: False, ("hermes-agent",),
    goal="build the task they named in their own words",
    steps=(
        "Also load the skills that fit what they asked.",
        "If you know what it is, say the first slice in one line and start. If not, ask one short question "
        "(a clarify with three concrete options is fine).",
        "Cut a big ask to a first slice and say so in one line.",
        "Show a first version early, then refine it.",
    ),
    output="Whatever fits the task; a page is one self-contained HTML file shown with desktop_preview.",
    quality="Real data only, designed with the skills when it is a page.",
    done="The first slice is shown and you said where its files are.")
TASK_BY_ID = {task.id: task for task in FIRST_TASKS}
EVERY_TASK_SKILL = "humanizer"


def eligible_tasks(facts: Facts, picks: Mapping[str, Any]) -> tuple[FirstTask, ...]:
    """The rows the model may pick from on this machine and run, in catalog order."""
    return tuple(task for task in FIRST_TASKS if task.id != FIXED_ID and task.gate(facts, picks))


def card_options(chosen: Any) -> list[dict]:
    """The build card: the model's picks as given, then the fixed row."""
    return [*(dict(option) for option in chosen or ()), {"id": FIXED_ID, "label": TASK_BY_ID[FIXED_ID].label}]


def _option_error(option: Any, eligible: set[str], seen: set[str]) -> str | None:
    if not isinstance(option, Mapping) or not isinstance(option.get("id"), str):
        return 'each option is {"id": ..., "label": ...}'
    if option["id"] == FIXED_ID:
        return f"{FIXED_ID} is added last by itself; do not pick it"
    if option["id"] not in eligible:
        return f"{option['id']!r} is not an eligible id"
    if option["id"] in seen:
        return f"{option['id']!r} is picked twice"
    label = option.get("label")
    if not isinstance(label, str) or not label.strip():
        return f"the label for {option['id']} is empty"
    if len(label) > MAX_LABEL:
        return f'"{label}" is {len(label)} characters; the limit is {MAX_LABEL}. Shorten it'
    return None


def pick_error(options: Any, facts: Facts, picks: Mapping[str, Any]) -> str | None:
    """Why the model's build picks are refused, naming the eligible ids; None when they are valid."""
    eligible = [task.id for task in eligible_tasks(facts, picks)]
    problems: list[str] = []
    if not isinstance(options, list) or not options:
        problems.append("options is required")
    elif len(options) > MAX_PICKS:
        problems.append(f"pick at most {MAX_PICKS}")
    else:
        # Every bad option is named at once, so one retry can fix them all.
        seen: set[str] = set()
        for option in options:
            if problem := _option_error(option, set(eligible), seen):
                problems.append(problem)
            elif isinstance(option, Mapping):
                seen.add(option["id"])
    if not problems:
        return None
    problem = "; ".join(problems)
    return (f'{problem}. Call setup_beat(beat="build", options=[{{"id": ..., "label": ...}}]) with up to '
            f"{MAX_PICKS} of these ids: {', '.join(eligible)}.")


def task_skills(option: str, has_skill: Callable[[str], bool]) -> list[str]:
    """The skills a task loads first, humanizer last. A bundled skill missing here is dropped; a plugin
    skill (``plugin:skill``) is kept, since the plugin being installed is the row's gate."""
    names = [*TASK_BY_ID.get(option, OWN_TASK).skills, EVERY_TASK_SKILL]
    kept = []
    for entry in names:
        found = [name for name in entry.split("|") if ":" in name or has_skill(name)]
        if found:
            kept.append(" or ".join(found))
    return kept
