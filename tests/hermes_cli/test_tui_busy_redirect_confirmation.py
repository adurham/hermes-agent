"""Regression tests: the CLI's busy-input ``interrupt`` branch must not claim
"Redirected current turn" when the redirect silently degraded to a queued steer.

Background
----------
``AIAgent.redirect()`` returns True for two very different outcomes: a genuine
live-cancel redirect, and the degraded case where a tool call is in flight and
the correction is only queued as a steer (``agent/interrupt_control.py``) — the
steer lands once the running tool finishes, it does not cancel the live model
request. Both return True. The CLI printed the identical
"↪ Redirected current turn" confirmation for either case, telling the user
their correction was live when it was actually parked behind whatever tool was
still running; if the turn then ended on a generic interrupted/retry filler
before the steer's drain point, the correction was orphaned with no visible
trace.

``cli.py``'s pre-mixin-split Enter-key handler read
``agent._last_redirect_degraded_to_steer`` (stamped by every accepted
``redirect()``) and printed
"⏩ Queued (tool still running — applies once it finishes)" for the degraded
case instead. The mixin split dropped that read; these tests pin both branches
plus the defensive ``getattr`` default for agents that never set the flag.
"""

from __future__ import annotations

import queue

import pytest

import cli as cli_module
from cli import HermesCLI


class _FakeAgent:
    """Minimal redirect-capable agent stand-in."""

    _supports_active_turn_redirect = True

    def __init__(self, *, degraded: bool, accept: bool = True):
        self._last_redirect_degraded_to_steer = degraded
        self._accept = accept

    def redirect(self, text) -> bool:
        return self._accept


class _FakeAgentWithoutFlag:
    """Older agent: supports redirect but never stamps the degraded flag."""

    _supports_active_turn_redirect = True

    def redirect(self, text) -> bool:
        return True


def _make_shell(agent) -> HermesCLI:
    shell = HermesCLI.__new__(HermesCLI)
    shell.agent = agent
    shell.busy_input_mode = "interrupt"
    shell._agent_running = True
    shell._pending_input = queue.Queue()
    shell._interrupt_queue = queue.Queue()
    return shell


@pytest.fixture
def printed(monkeypatch):
    """Capture ``cli._cprint`` output (the handler imports it from ``cli`` per call)."""
    lines: list[str] = []
    monkeypatch.setattr(cli_module, "_cprint", lambda text: lines.append(str(text)))
    # The first-touch /busy onboarding tip would otherwise call mark_seen() against the
    # operator's real ~/.hermes/config.yaml; the hint itself is not under test here.
    monkeypatch.setattr("agent.onboarding.is_seen", lambda config, flag: True)
    return lines


def test_degraded_redirect_prints_corrective_queued_message(printed):
    shell = _make_shell(_FakeAgent(degraded=True))

    shell._tui_enter_while_busy("please use the other host", [], ("please use the other host", []))

    assert any("⏩ Queued (tool still running — applies once it finishes): "
               "'please use the other host'" in line for line in printed), printed
    assert not any("Redirected current turn" in line for line in printed), printed


def test_genuine_redirect_keeps_the_redirected_confirmation(printed):
    shell = _make_shell(_FakeAgent(degraded=False))

    shell._tui_enter_while_busy("stop and explain", [], ("stop and explain", []))

    assert any("↪ Redirected current turn: 'stop and explain'" in line for line in printed), printed
    assert not any("Queued (tool still running" in line for line in printed), printed


def test_agent_without_the_flag_defaults_to_the_redirect_confirmation(printed):
    """Defensive getattr: an agent that never stamps the flag must not crash the handler."""
    shell = _make_shell(_FakeAgentWithoutFlag())

    shell._tui_enter_while_busy("hello", [], ("hello", []))

    assert any("↪ Redirected current turn: 'hello'" in line for line in printed), printed


def test_degraded_preview_truncates_at_80_chars(printed):
    shell = _make_shell(_FakeAgent(degraded=True))
    text = "x" * 100

    shell._tui_enter_while_busy(text, [], (text, []))

    assert any(f"⏩ Queued (tool still running — applies once it finishes): '{'x' * 80}...'" in line
               for line in printed), printed


def test_refused_redirect_still_falls_back_to_the_interrupt_queue(printed):
    shell = _make_shell(_FakeAgent(degraded=False, accept=False))

    shell._tui_enter_while_busy("hello", [], ("hello", []))

    assert shell._pending_input.empty()
    assert shell._interrupt_queue.qsize() == 1
    assert not any("Redirected current turn" in line for line in printed), printed
