"""Tests for the fork's silence guard (``agent.silence_guard`` / agent/fork/silence_guard.py).

Covers:

1. Normalization of the config block (``resolve_silence_guard_settings``): defaults on
   missing/partial/invalid input, ``enabled: false`` disables, per-key fallback.
2. Round/streak tracking (``note_assistant_round``): silent tool-call-only rounds extend the
   streak; any visible-text round resets it and re-arms the progress nudge.
3. One-time-ness of the two nudges (``maybe_inject_silence_guard``): the progress nudge fires
   once per silent streak at its threshold and not before; the convergence nudge fires once
   per turn at the round threshold; both append to the newest tool result (cache-safe), never
   a synthetic user/system row.
4. ``enabled: false`` disables everything (nudges AND the end-of-turn warning).
5. Per-turn reset across two turns (``turn_context._reset_per_turn_agent_state``).
6. Coexistence with ``_maybe_inject_iteration_budget_warning`` on the same tool row.
7. The silent-long-turn WARNING at turn end (``maybe_log_silent_long_turn``).
8. The wiring seam: ``prepare_iteration`` actually calls the guard.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest


# ── helpers ────────────────────────────────────────────────────────────────

class _StubAgent:
    """The subset of agent state the silence guard reads/writes."""

    def __init__(
        self, *, enabled=True, status_after=2, converge_after=3, session_id="sess-1",
    ):
        self._silence_guard_enabled = enabled
        self._silence_guard_status_after_rounds = status_after
        self._silence_guard_converge_after_rounds = converge_after
        self._silence_guard_rounds = 0
        self._silence_guard_silent_streak = 0
        self._silence_guard_status_injected = False
        self._silence_guard_converge_injected = False
        self.session_id = session_id


def _assistant(content, tool_calls=True):
    return SimpleNamespace(content=content, tool_calls=[{"id": "t"}] if tool_calls else [])


def _simulate_round(agent, messages, content):
    """One assistant API round: record it, then run the guard's pre-request injection."""
    from agent.fork.silence_guard import maybe_inject_silence_guard, note_assistant_round

    note_assistant_round(agent, _assistant(content))
    rid = f"t{agent._silence_guard_rounds}"
    messages.append({"role": "assistant", "content": content or None,
                     "tool_calls": [{"id": rid, "type": "function",
                                     "function": {"name": "read_file", "arguments": "{}"}}]})
    messages.append({"role": "tool", "tool_call_id": rid, "content": "result"})
    return maybe_inject_silence_guard(agent, messages)


def _count_marker(messages, marker):
    return sum(marker in str(m.get("content")) for m in messages)


def _tool_tail():
    return [
        {"role": "user", "content": "do the task"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": "result"},
    ]


def _make_agent(tmp_path, monkeypatch, config_body: str = ""):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("", encoding="utf-8")
    (tmp_path / "config.yaml").write_text(config_body or "{}\n", encoding="utf-8")
    from run_agent import AIAgent

    return AIAgent(
        model="gpt-5.5", provider="openai", api_key="sk-dummy",
        base_url="https://api.openai.com/v1", quiet_mode=True,
        skip_context_files=True, skip_memory=True, platform="cli",
    )


# ── normalization ──────────────────────────────────────────────────────────

def test_normalize_defaults_on_missing_and_invalid():
    from agent.fork.silence_guard import (
        DEFAULT_CONVERGE_AFTER_ROUNDS,
        DEFAULT_ENABLED,
        DEFAULT_STATUS_AFTER_ROUNDS,
        resolve_silence_guard_settings,
    )

    default = (DEFAULT_ENABLED, DEFAULT_STATUS_AFTER_ROUNDS, DEFAULT_CONVERGE_AFTER_ROUNDS)
    assert default == (True, 20, 50)
    for raw in (None, {}, "garbage", 5, [], {"enabled": "yes"}, {"enabled": 1},
                {"status_after_rounds": -3}, {"status_after_rounds": True},
                {"converge_after_rounds": "abc"}, {"converge_after_rounds": 0}):
        assert resolve_silence_guard_settings(raw) == default, raw


def test_normalize_partial_block_keeps_other_defaults():
    from agent.fork.silence_guard import resolve_silence_guard_settings

    assert resolve_silence_guard_settings({"converge_after_rounds": 30}) == (True, 20, 30)
    assert resolve_silence_guard_settings({"status_after_rounds": "5"}) == (True, 5, 50)
    assert resolve_silence_guard_settings({"enabled": False}) == (False, 20, 50)


def test_default_config_block_matches_module_defaults():
    """DEFAULT_CONFIG and the module defaults are one contract, not two constants."""
    from agent.fork.silence_guard import (
        DEFAULT_CONVERGE_AFTER_ROUNDS,
        DEFAULT_ENABLED,
        DEFAULT_STATUS_AFTER_ROUNDS,
        resolve_silence_guard_settings,
    )
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    block = DEFAULT_CONFIG["agent"]["silence_guard"]
    assert resolve_silence_guard_settings(block) == (
        DEFAULT_ENABLED, DEFAULT_STATUS_AFTER_ROUNDS, DEFAULT_CONVERGE_AFTER_ROUNDS,
    )


# ── streak tracking ─────────────────────────────────────────────────────────

def test_silent_rounds_extend_streak_and_visible_text_resets_it():
    from agent.fork.silence_guard import note_assistant_round

    agent = _StubAgent()
    for _ in range(3):
        note_assistant_round(agent, _assistant(None))
    assert agent._silence_guard_rounds == 3
    assert agent._silence_guard_silent_streak == 3
    note_assistant_round(agent, _assistant("working on it"))
    assert agent._silence_guard_rounds == 4
    assert agent._silence_guard_silent_streak == 0
    # An empty-string / whitespace-only message is silent too.
    note_assistant_round(agent, _assistant("   \n "))
    assert agent._silence_guard_silent_streak == 1


# ── progress nudge (Nudge 1) ────────────────────────────────────────────────

def test_status_nudge_fires_exactly_once_at_threshold_and_not_before():
    from agent.fork.silence_guard import STATUS_NUDGE_MARKER

    agent = _StubAgent(status_after=3, converge_after=100)
    messages = [{"role": "user", "content": "go"}]
    assert _simulate_round(agent, messages, None) is False   # streak 1
    assert _simulate_round(agent, messages, None) is False   # streak 2 — not yet
    assert _count_marker(messages, STATUS_NUDGE_MARKER) == 0

    assert _simulate_round(agent, messages, None) is True    # streak 3 — fires
    assert messages[-1]["content"].startswith("result")      # appended, not clobbered
    assert STATUS_NUDGE_MARKER in messages[-1]["content"]
    assert _count_marker(messages, STATUS_NUDGE_MARKER) == 1

    # Latched for the streak: a longer streak does not re-inject.
    assert _simulate_round(agent, messages, None) is False
    assert _simulate_round(agent, messages, None) is False
    assert _count_marker(messages, STATUS_NUDGE_MARKER) == 1


def test_status_nudge_rearms_after_streak_resets():
    from agent.fork.silence_guard import STATUS_NUDGE_MARKER

    agent = _StubAgent(status_after=2, converge_after=100)
    messages = [{"role": "user", "content": "go"}]
    assert _simulate_round(agent, messages, None) is False
    assert _simulate_round(agent, messages, None) is True    # streak 2 -> fires
    assert agent._silence_guard_status_injected is True

    # Visible text resets the streak and re-arms the latch.
    assert _simulate_round(agent, messages, "progress: done X, doing Y") is False
    assert agent._silence_guard_silent_streak == 0
    assert agent._silence_guard_status_injected is False

    assert _simulate_round(agent, messages, None) is False
    assert _simulate_round(agent, messages, None) is True    # a later streak fires again
    assert _count_marker(messages, STATUS_NUDGE_MARKER) == 2


def test_status_nudge_reads_as_a_system_notice_not_a_user_message():
    from agent.fork.silence_guard import STATUS_NUDGE_MARKER

    agent = _StubAgent(status_after=1, converge_after=100)
    messages = _tool_tail()
    assert _simulate_round(agent, messages, None) is True
    assert STATUS_NUDGE_MARKER.startswith("[SYSTEM NOTICE")
    # No synthetic user/system row was inserted — the tool row just grew.
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "assistant", "tool"]


# ── convergence nudge (Nudge 2) ─────────────────────────────────────────────

def test_converge_nudge_fires_once_per_turn_at_round_threshold():
    from agent.fork.silence_guard import CONVERGE_NUDGE_MARKER

    agent = _StubAgent(status_after=100, converge_after=4)
    messages = [{"role": "user", "content": "go"}]
    for _ in range(3):
        assert _simulate_round(agent, messages, None) is False  # rounds 1..3
    assert _count_marker(messages, CONVERGE_NUDGE_MARKER) == 0

    assert _simulate_round(agent, messages, None) is True       # round 4 -> fires
    assert CONVERGE_NUDGE_MARKER in messages[-1]["content"]
    assert agent._silence_guard_converge_injected is True

    # Once per turn — no second injection as the turn runs on.
    assert _simulate_round(agent, messages, None) is False
    assert _count_marker(messages, CONVERGE_NUDGE_MARKER) == 1


def test_converge_gate_cannot_be_unset_by_visible_text():
    """Unlike the progress nudge, visible text does not re-arm the per-turn convergence nudge."""
    from agent.fork.silence_guard import CONVERGE_NUDGE_MARKER

    agent = _StubAgent(status_after=100, converge_after=2)
    messages = [{"role": "user", "content": "go"}]
    assert _simulate_round(agent, messages, None) is False
    assert _simulate_round(agent, messages, None) is True    # round 2 -> fires
    assert _simulate_round(agent, messages, "a real answer in progress") is False
    assert _count_marker(messages, CONVERGE_NUDGE_MARKER) == 1


# ── enabled: false ──────────────────────────────────────────────────────────

def test_disabled_disables_nudges_and_warning(caplog):
    from agent.fork.silence_guard import maybe_log_silent_long_turn

    agent = _StubAgent(enabled=False, status_after=1, converge_after=1)
    messages = [{"role": "user", "content": "go"}]
    for _ in range(4):
        assert _simulate_round(agent, messages, None) is False
    assert agent._silence_guard_rounds == 0      # tracking itself is inert
    assert agent._silence_guard_silent_streak == 0
    joined = "".join(str(m.get("content")) for m in messages)
    assert "SYSTEM NOTICE" not in joined

    agent._silence_guard_rounds = 40
    agent._silence_guard_silent_streak = 40
    with caplog.at_level(logging.WARNING, logger="agent.conversation_loop"):
        maybe_log_silent_long_turn(agent)
    assert "silent long turn" not in caplog.text


# ── multimodal content + cache contract ─────────────────────────────────────

def test_injection_appends_a_text_block_to_multimodal_tool_content():
    from agent.fork.silence_guard import STATUS_NUDGE_MARKER

    agent = _StubAgent(status_after=1, converge_after=100)
    messages = [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": [{"type": "text", "text": "block"}]},
    ]
    from agent.fork.silence_guard import note_assistant_round, maybe_inject_silence_guard

    note_assistant_round(agent, _assistant(None))
    assert maybe_inject_silence_guard(agent, messages) is True
    blocks = messages[-1]["content"]
    assert blocks[0] == {"type": "text", "text": "block"}
    assert blocks[-1]["type"] == "text" and STATUS_NUDGE_MARKER in blocks[-1]["text"]


def test_injection_skips_already_persisted_tool_row():
    """A persisted tool row is append-only; mutating it would diverge replay from the wire."""
    from agent.context_compressor import _DB_PERSISTED_MARKER
    from agent.fork.silence_guard import maybe_inject_silence_guard, note_assistant_round

    agent = _StubAgent(status_after=1, converge_after=100)
    messages = _tool_tail()
    messages[-1][_DB_PERSISTED_MARKER] = True
    snapshot = [dict(m) for m in messages]
    note_assistant_round(agent, _assistant(None))

    assert maybe_inject_silence_guard(agent, messages) is False
    assert agent._silence_guard_status_injected is False      # latch stays open
    assert messages == snapshot


def test_injection_without_a_tool_message_stays_unlatched():
    from agent.fork.silence_guard import maybe_inject_silence_guard, note_assistant_round

    agent = _StubAgent(status_after=1, converge_after=100)
    messages = [{"role": "user", "content": "just started"}]
    note_assistant_round(agent, _assistant(None))
    assert maybe_inject_silence_guard(agent, messages) is False
    assert agent._silence_guard_status_injected is False      # retried next iteration


# ── coexistence ─────────────────────────────────────────────────────────────

def test_coexists_with_iteration_budget_warning(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from agent.fork.silence_guard import (
        STATUS_NUDGE_MARKER, maybe_inject_silence_guard, note_assistant_round,
    )
    from agent.turn_iteration_prep import _maybe_inject_iteration_budget_warning

    agent = _StubAgent(status_after=1, converge_after=100)
    agent._interrupt_requested = False
    agent._iteration_budget_warning_injected = False
    agent.budget_warning_ratio = 0.5
    agent.valid_tool_names = ("read_file",)
    agent.iteration_budget = SimpleNamespace(used=3, max_total=4)  # 75% >= 50% -> fires

    messages = _tool_tail()
    note_assistant_round(agent, _assistant(None))

    assert _maybe_inject_iteration_budget_warning(agent, messages) is True
    assert maybe_inject_silence_guard(agent, messages) is True

    tail = messages[-1]["content"]
    assert "iteration budget checkpoint" in tail
    assert STATUS_NUDGE_MARKER in tail
    assert agent._iteration_budget_warning_injected is True
    assert agent._silence_guard_status_injected is True
    # The iteration-budget notice still reads as before; the guard appended after it.
    assert tail.startswith("result")


# ── turn end: silent long turn warning ───────────────────────────────────────

def test_silent_long_turn_warns_once_at_turn_end(caplog):
    from agent.fork.silence_guard import maybe_log_silent_long_turn

    agent = _StubAgent(session_id="sess-42")
    agent._silence_guard_rounds = 25
    agent._silence_guard_silent_streak = 25
    with caplog.at_level(logging.WARNING, logger="agent.conversation_loop"):
        maybe_log_silent_long_turn(agent)
    assert "silent long turn: rounds=25 no user-visible text" in caplog.text
    assert "sess-42" in caplog.text
    assert sum(r.levelno == logging.WARNING for r in caplog.records) == 1


@pytest.mark.parametrize("rounds,streak", [(24, 24), (40, 39), (10, 10)])
def test_no_warning_when_short_or_a_round_had_text(caplog, rounds, streak):
    from agent.fork.silence_guard import maybe_log_silent_long_turn

    agent = _StubAgent()
    agent._silence_guard_rounds = rounds
    agent._silence_guard_silent_streak = streak  # streak < rounds => some round had text
    with caplog.at_level(logging.WARNING, logger="agent.conversation_loop"):
        maybe_log_silent_long_turn(agent)
    assert "silent long turn" not in caplog.text


# ── per-turn reset (integration) ────────────────────────────────────────────

def test_per_turn_reset_across_two_turns(monkeypatch, tmp_path):
    from agent.fork.silence_guard import (
        STATUS_NUDGE_MARKER, maybe_inject_silence_guard, note_assistant_round,
    )
    from agent.turn_context import _reset_per_turn_agent_state

    agent = _make_agent(
        tmp_path, monkeypatch,
        "agent:\n  silence_guard:\n    enabled: true\n    status_after_rounds: 2\n"
        "    converge_after_rounds: 9\n",
    )
    # Config reached the agent.
    assert agent._silence_guard_enabled is True
    assert agent._silence_guard_status_after_rounds == 2
    assert agent._silence_guard_converge_after_rounds == 9

    # Turn 1: reaches the threshold, fires, latches.
    for _ in range(2):
        note_assistant_round(agent, _assistant(None))
    assert maybe_inject_silence_guard(agent, _tool_tail()) is True
    assert agent._silence_guard_status_injected is True
    assert agent._silence_guard_rounds == 2

    # Turn boundary resets every counter/latch.
    _reset_per_turn_agent_state(agent)
    assert agent._silence_guard_rounds == 0
    assert agent._silence_guard_silent_streak == 0
    assert agent._silence_guard_status_injected is False
    assert agent._silence_guard_converge_injected is False

    # Turn 2: the latch is re-armed and can fire again.
    for _ in range(2):
        note_assistant_round(agent, _assistant(None))
    turn2 = _tool_tail()
    assert maybe_inject_silence_guard(agent, turn2) is True
    assert STATUS_NUDGE_MARKER in turn2[-1]["content"]


# ── wiring seam ─────────────────────────────────────────────────────────────

def test_prepare_iteration_injects_the_silence_nudge(monkeypatch, tmp_path):
    """The guard is actually called from the pre-request seam (not just unit-testable)."""
    from agent.fork.silence_guard import STATUS_NUDGE_MARKER, note_assistant_round
    from agent.turn_iteration_prep import prepare_iteration

    agent = _make_agent(
        tmp_path, monkeypatch,
        "agent:\n  silence_guard:\n    enabled: true\n    status_after_rounds: 1\n"
        "    converge_after_rounds: 50\n",
    )
    note_assistant_round(agent, _assistant(None))
    messages = _tool_tail()
    prepare_iteration(agent, messages=messages, api_call_count=1)
    assert STATUS_NUDGE_MARKER in messages[-1]["content"]
