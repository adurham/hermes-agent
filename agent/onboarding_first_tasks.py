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

# The one fixed row: always last on the card, never one of the model's picks.
FIXED_ID = "hermes"


@dataclass(frozen=True)
class FirstTask:
    """``skills`` load first, before the work; an entry ``a|b`` means one of them, whichever fits.
    Plugin skills use the ``<plugin>:<skill>`` form skill_view resolves."""

    id: str
    label: str
    gate: Callable[[Facts, Mapping[str, Any]], bool]
    skills: tuple[str, ...]
    skeleton: str


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
    FirstTask("hermes", "How Hermes works", lambda facts, picks: False, ("hermes-agent", "claude-design", "p5js"),
              "Make a designed HTML page from real facts about this install: the skill learning loop (and the skills "
              "used in this chat), automations (cron, webhooks), and what Hermes can do. Draw the loop as a small "
              "p5js animation. Save it under ~/hermes-first-task/ and <open>."),
    FirstTask("games", "Tune my games", _plugin("nvidia-app"), ("nvidia-app:nvidia-app",),
              "Check NVIDIA App is running. Read the installed games and their settings, optimise them, then show "
              "what changed."),
    FirstTask("broadcast", "Set up Broadcast", _plugin("nvidia-broadcast"), ("nvidia-broadcast:nvidia-broadcast",),
              "Check NVIDIA Broadcast is running. Set up camera and mic effects for a call, then show the before and "
              "after."),
    FirstTask("blender", "Make something in Blender", _plugin("blender"), ("blender:blender",),
              "Check Blender is running. Make one small scene from a short script, render it and show it."),
    FirstTask("brief", "Today's brief", _connected, ("claude-design",),
              "Write today's brief from the connected apps: what needs attention, what changed, what is next. "
              "Then offer a morning cron job that sends it."),
    FirstTask("week", "My week in one page", _connected, ("weekly-review-planning", "claude-design"),
              "Pull the past seven days from the connected apps and make one page: meetings, threads, work shipped, "
              "open loops, and a plan for next week."),
    FirstTask("mac", "Mac helpers", _macos, ("apple-reminders", "apple-notes", "hermes-agent"),
              "Make one real reminder and one real note with them, then set up a morning routine as a cron job."),
    FirstTask("notes", "Turn my notes into a wiki", _always, ("obsidian", "llm-wiki"),
              "Ask which folder holds their notes, then make an index page and three linked topic pages."),
    FirstTask("explain", "Explain a topic", _always,
              ("grounded-citations", "claude-design", "p5js", "blocked-page-recovery"),
              "Offer three topic guesses in one clarify. Research it with citations (for a technical topic also "
              "load arxiv), then make one designed page with one interactive p5js figure."),
    FirstTask("youtube", "A video as one page", _always, ("youtube-content", "claude-design", "p5js"),
              "Ask for a video link, get the transcript, then make one HTML page that lays the ideas out visually "
              "with timestamps and quotes."),
    FirstTask("watch", "Watch something daily", _always,
              ("competitor-news-monitor|product-price-monitor", "grounded-citations"),
              "Ask what to watch. Run the first check now, then set up a daily cron job that reports here."),
    FirstTask("site", "Design a page", _always, ("claude-design", "popular-web-designs", "design-md", "p5js"),
              "Offer three design directions in one clarify, then make one designed page with a p5js hero and offer "
              "a variation."),
    FirstTask("repo", "Map a repo", _always, ("codebase-inspection", "architecture-diagram", "github"),
              "Ask which repo and ask before reading it. Show its language breakdown, then make an architecture page."),
)
TASK_BY_ID = {task.id: task for task in FIRST_TASKS}
EVERY_TASK_SKILL = "humanizer"


def eligible_tasks(facts: Facts, picks: Mapping[str, Any]) -> tuple[FirstTask, ...]:
    """The rows the model may pick from on this machine and run, in catalog order."""
    return tuple(task for task in FIRST_TASKS if task.id != FIXED_ID and task.gate(facts, picks))
