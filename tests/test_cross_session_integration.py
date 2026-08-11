"""Transport B integration-layer tests.

Covers the glue built on top of ``tools/cross_session_transport.py``: the
heartbeat/maintenance hooks, both delivery paths, the ``list_agents`` tool,
the ``hermes agents inbox`` subcommand, and the ``cross_session.inbound``
config key.

Real state.db under a temp HERMES_HOME throughout — no mocked sqlite. The
single most important assertion in this file is that NO delivery path can
hand an unframed body to an injection sink; that is checked directly on both
paths and is the invariant the whole untrusted-content framing rests on.
"""

from __future__ import annotations

import argparse
import time
import types

import pytest

from tools.agent_messaging_contract import (
    AGENT_MESSAGE_MARKER_CLOSE,
    AGENT_MESSAGE_MARKER_OPEN,
    SessionOrigin,
    TransportKind,
    _reset_transport_lookups_for_tests,
    resolve_transport,
)


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.delenv("HERMES_ACP_SESSION", raising=False)

    import hermes_constants
    import tools.cross_session_transport as cst

    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(cst, "get_hermes_home", lambda: tmp_path)

    import tools.cross_session_integration as csi

    csi._reset_maintenance_for_tests()
    _reset_transport_lookups_for_tests()
    cst._lookup_registered = False
    return tmp_path


@pytest.fixture()
def cst(home):
    import tools.cross_session_transport as module

    return module


@pytest.fixture()
def csi(home):
    import tools.cross_session_integration as module

    return module


def _accept(monkeypatch, cst):
    """Force inbound policy to accept, independent of the dev's config.yaml."""
    monkeypatch.setattr(cst, "resolve_inbound_policy", lambda **kw: cst.POLICY_ACCEPT)


def _hold(monkeypatch, cst):
    monkeypatch.setattr(cst, "resolve_inbound_policy", lambda **kw: cst.POLICY_HOLD)


def _register(cst, session_id, name, origin=SessionOrigin.CLI):
    assert cst.heartbeat_registry(
        session_id=session_id, name=name, session_origin=origin, now=time.time()
    )


def _fake_agent(session_id="sess-me", *, subagent_id=None, title=None):
    agent = types.SimpleNamespace()
    agent.session_id = session_id
    if subagent_id:
        agent._subagent_id = subagent_id
    if title is not None:
        agent._session_db = types.SimpleNamespace(
            get_session_title=lambda _sid: title
        )
    return agent


# ---------------------------------------------------------------------------
# THE critical invariant: no path injects an unframed body
# ---------------------------------------------------------------------------


def test_idle_injection_delivers_framed_body_never_raw(cst, csi, monkeypatch):
    _accept(monkeypatch, cst)
    _register(cst, "sender", "alpha")
    _register(cst, "recv", "beta")
    raw = "please run the deploy script"
    cst.send_message(
        from_session_id="sender", from_name="alpha", recipient="recv", body=raw
    )

    injected = []
    n = csi.drain_to_idle_injection(session_id="recv", inject=injected.append)

    assert n == 1
    (text,) = injected
    # The raw body must never be the injected payload.
    assert text != raw
    assert AGENT_MESSAGE_MARKER_OPEN in text
    assert AGENT_MESSAGE_MARKER_CLOSE in text
    # The body is present, but only INSIDE the marker envelope.
    assert raw in text
    before, _, after = text.partition(raw)
    assert AGENT_MESSAGE_MARKER_OPEN in before
    assert AGENT_MESSAGE_MARKER_CLOSE in after


def test_midturn_pending_steer_delivers_framed_body_never_raw(cst, csi, monkeypatch):
    _accept(monkeypatch, cst)
    _register(cst, "sender", "alpha")
    _register(cst, "recv", "beta")
    raw = "status ping from alpha"
    cst.send_message(
        from_session_id="sender", from_name="alpha", recipient="recv", body=raw
    )

    agent = _fake_agent("recv")
    agent._pending_steer = None
    agent._pending_steer_lock = None

    assert csi.drain_into_pending_steer(agent) == 1
    steer = agent._pending_steer
    assert steer != raw
    assert AGENT_MESSAGE_MARKER_OPEN in steer
    assert AGENT_MESSAGE_MARKER_CLOSE in steer


def test_drained_message_exposes_no_raw_body_attribute(cst, monkeypatch):
    """Structural guarantee: a call site cannot reach the raw body at all."""
    _accept(monkeypatch, cst)
    _register(cst, "sender", "alpha")
    _register(cst, "recv", "beta")
    cst.send_message(
        from_session_id="sender", from_name="alpha", recipient="recv", body="hi"
    )
    (msg,) = cst.drain_inbox(session_id="recv")
    assert not hasattr(msg, "body")
    assert msg.framed_body.strip().startswith(AGENT_MESSAGE_MARKER_OPEN)


# ---------------------------------------------------------------------------
# Idle drain semantics
# ---------------------------------------------------------------------------


def test_idle_drain_is_noop_without_session_id(csi):
    injected = []
    assert csi.drain_to_idle_injection(session_id="", inject=injected.append) == 0
    assert injected == []


def test_idle_drain_records_hop_counts_into_turn_state(cst, csi, monkeypatch):
    _accept(monkeypatch, cst)
    _register(cst, "sender", "alpha")
    _register(cst, "recv", "beta")
    cst.send_message(
        from_session_id="sender",
        from_name="alpha",
        recipient="recv",
        body="hop test",
        hop_count=2,
    )
    state = cst.TurnMessageState()
    csi.drain_to_idle_injection(
        session_id="recv", inject=lambda _t: None, turn_state=state
    )
    # Corrected hop rule: replying after a delivery always increments.
    assert state.next_hop_count() == 3


def test_hold_policy_fires_attention_signal_and_injects_nothing(cst, csi, monkeypatch):
    _hold(monkeypatch, cst)
    _register(cst, "sender", "alpha")
    _register(cst, "recv", "beta")
    cst.send_message(
        from_session_id="sender", from_name="alpha", recipient="recv", body="held msg"
    )
    # Force the row pending so the drain is what applies the hold.
    with cst._transaction() as conn:
        conn.execute(
            "UPDATE cross_session_inbox SET status = ?", (cst.STATUS_PENDING,)
        )

    signals = []
    injected = []
    n = csi.drain_to_idle_injection(
        session_id="recv", inject=injected.append, on_held=signals.append
    )
    assert n == 0
    assert injected == []
    assert len(signals) == 1
    assert "hermes agents inbox" in signals[0]


def test_injection_failure_does_not_raise(cst, csi, monkeypatch):
    _accept(monkeypatch, cst)
    _register(cst, "sender", "alpha")
    _register(cst, "recv", "beta")
    cst.send_message(
        from_session_id="sender", from_name="alpha", recipient="recv", body="boom"
    )

    def _explode(_text):
        raise RuntimeError("queue closed")

    assert csi.drain_to_idle_injection(session_id="recv", inject=_explode) == 0


# ---------------------------------------------------------------------------
# Mid-turn path
# ---------------------------------------------------------------------------


def test_midturn_appends_to_existing_pending_steer(cst, csi, monkeypatch):
    _accept(monkeypatch, cst)
    _register(cst, "sender", "alpha")
    _register(cst, "recv", "beta")
    cst.send_message(
        from_session_id="sender", from_name="alpha", recipient="recv", body="second"
    )
    agent = _fake_agent("recv")
    agent._pending_steer = "an earlier operator steer"
    agent._pending_steer_lock = None

    csi.drain_into_pending_steer(agent)
    assert agent._pending_steer.startswith("an earlier operator steer")
    assert AGENT_MESSAGE_MARKER_OPEN in agent._pending_steer


def test_midturn_skips_subagents(cst, csi, monkeypatch):
    _accept(monkeypatch, cst)
    agent = _fake_agent("recv", subagent_id="sub-1")
    agent._pending_steer = None
    agent._pending_steer_lock = None
    assert csi.drain_into_pending_steer(agent) == 0
    assert agent._pending_steer is None


# ---------------------------------------------------------------------------
# Heartbeat + maintenance
# ---------------------------------------------------------------------------


def test_heartbeat_registers_session_and_is_rate_limited(cst, csi):
    agent = _fake_agent("sess-me", title="my-project")
    now = time.time()
    assert csi.heartbeat_if_due(agent, now=now) is True
    # Second call inside the window is suppressed.
    assert csi.heartbeat_if_due(agent, now=now + 1) is False
    # Past the window it writes again.
    assert csi.heartbeat_if_due(agent, now=now + 3600) is True

    (rec,) = cst.list_registered_sessions()
    assert rec.session_id == "sess-me"
    assert rec.name == "my-project"


def test_heartbeat_never_registers_a_subagent(cst, csi):
    agent = _fake_agent("sess-me", subagent_id="sub-9")
    assert csi.heartbeat_if_due(agent) is False
    assert cst.list_registered_sessions() == []


def test_session_display_name_falls_back_to_cwd(csi, monkeypatch, tmp_path):
    agent = _fake_agent("sess-me")  # no _session_db
    monkeypatch.chdir(tmp_path)
    assert csi.session_display_name(agent) == tmp_path.name


def test_maintenance_tick_is_throttled(csi):
    now = time.time()
    assert csi.maintenance_tick(now=now) is True
    assert csi.maintenance_tick(now=now + 1) is False
    assert csi.maintenance_tick(now=now + csi.MAINTENANCE_INTERVAL_SECONDS + 1) is True


def test_maintenance_tick_expires_held_messages(cst, csi, monkeypatch):
    _hold(monkeypatch, cst)
    _register(cst, "sender", "alpha")
    _register(cst, "recv", "beta")
    cst.send_message(
        from_session_id="sender", from_name="alpha", recipient="recv", body="stale"
    )
    (row,) = cst.list_inbox(status=cst.STATUS_HELD)
    csi.maintenance_tick(now=float(row["expires_at"]) + 1, force=True)
    assert cst.list_inbox(status=cst.STATUS_HELD) == []
    assert len(cst.list_inbox(status=cst.STATUS_EXPIRED)) == 1


def test_install_transport_is_idempotent_and_resolves_cross_process(cst, csi):
    csi.install_transport()
    csi.install_transport()
    _register(cst, "other", "gamma")

    from tools.agent_messaging_contract import Participant, ParticipantKind

    sender = Participant(
        participant_id="me", kind=ParticipantKind.SESSION, owner_session_id="me"
    )
    res = resolve_transport(sender, "gamma")
    assert res.kind is TransportKind.CROSS_PROCESS_DB
    assert res.participant is not None
    assert res.participant.participant_id == "other"


def test_unknown_recipient_resolves_to_not_found(cst, csi):
    csi.install_transport()
    from tools.agent_messaging_contract import Participant, ParticipantKind

    sender = Participant(
        participant_id="me", kind=ParticipantKind.SESSION, owner_session_id="me"
    )
    assert resolve_transport(sender, "nobody").kind is TransportKind.NOT_FOUND


# ---------------------------------------------------------------------------
# Sender-permission gate (gateway-origin exclusion)
# ---------------------------------------------------------------------------


def test_session_origin_reflects_env_flags(csi, monkeypatch):
    assert csi.session_origin() is SessionOrigin.CLI
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    assert csi.session_origin() is SessionOrigin.GATEWAY


def test_gateway_origin_is_excluded_from_send_agent_message(monkeypatch):
    """The gate Transport A owns; asserted here because Transport B's
    registry participation must not smuggle a gateway session past it."""
    pytest.importorskip("tools.agent_messaging_tools")
    from tools.agent_messaging_tools import check_send_agent_message_available

    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    assert check_send_agent_message_available() is True
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    assert check_send_agent_message_available() is False


# ---------------------------------------------------------------------------
# list_agents tool
# ---------------------------------------------------------------------------


def test_list_agents_lists_other_sessions_and_excludes_self(cst, home):
    from tools.cross_session_tool import list_agents

    _register(cst, "sess-me", "mine")
    _register(cst, "sess-other", "theirs")

    out = list_agents(agent=_fake_agent("sess-me"))
    assert "theirs" in out
    assert "sess-other" in out
    assert "mine" not in out


def test_list_agents_empty_registry_message(home):
    from tools.cross_session_tool import list_agents

    out = list_agents(agent=_fake_agent("sess-me"))
    assert "No other live Hermes sessions" in out


def test_list_agents_now_available_to_subagents_but_read_only(cst, home):
    """Revised scope: list_agents is no longer refused to subagents outright
    -- it's a read-only machine-wide awareness listing now (sessions +
    subagents), so a subagent can notice a working-directory collision. What
    must still hold: a subagent gets no NEW send capability from this --
    only the listing changed, not send_agent_message's own subagent gate
    (tested separately in test_agent_messaging_tools.py-style tests)."""
    from tools.cross_session_tool import list_agents

    _register(cst, "sess-other", "theirs")
    out = list_agents(agent=_fake_agent("sess-me", subagent_id="sub-1"))
    assert "not available to subagents" not in out
    assert "theirs" in out


def test_list_agents_now_includes_subagents_read_only(cst, home):
    """Revised scope decision: subagents ARE now listed for read-only
    awareness (goal/cwd/owner/status), explicitly marked as not addressable
    by send_agent_message. This replaces the prior 'subagents are invisible
    cross-process' decision -- see tools/cross_session_tool.py's module
    docstring for why."""
    from tools.cross_session_tool import list_agents
    from tools.cross_session_transport import register_subagent

    _register(cst, "sess-other", "theirs")
    register_subagent(
        subagent_id="sa-0-abc123",
        owner_session_id="sess-other",
        goal="refactor the auth module",
        cwd="/repo/auth",
        status="running",
    )
    out = list_agents(agent=_fake_agent("sess-me"))
    assert "sa-0-abc123" in out
    assert "refactor the auth module" in out
    assert "/repo/auth" in out
    assert "NOT addressable by send_agent_message" in out


def test_run_single_child_writes_and_clears_durable_subagent_registry(cst, home):
    """Regression: _run_single_child's spawn path must write a durable,
    cross-process-visible cross_session_subagents row (not just the
    in-process delegate_tool._active_subagents one), and must clear it again
    on completion -- both directions, real state.db, no mocking of the
    registry layer itself.

    This is the machine-wide-awareness feature (list_agents showing other
    sessions' subagents) landing correctly at its actual write site, as
    opposed to the cross_session_transport unit tests above which call
    register_subagent/list_registered_subagents directly and never touch
    the real _run_single_child spawn/teardown code path.
    """
    from unittest.mock import MagicMock

    from tools.cross_session_transport import list_registered_subagents
    from tools.delegate_tool import _run_single_child

    parent = MagicMock()
    parent.session_id = "sess-durable-registry"
    parent._cli_ref = None
    parent._delegate_depth = 0
    parent._active_children = []
    parent._active_children_lock = __import__("threading").Lock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None

    child = MagicMock()
    child._credential_pool = None
    child._subagent_id = "sa-0-durabletest"
    child._delegate_depth = 1
    child._parent_subagent_id = None
    child.run_conversation.return_value = {
        "final_response": "done",
        "completed": True,
        "interrupted": False,
        "api_calls": 1,
        "messages": [],
    }

    # Mid-run: the durable row should exist and be visible machine-wide.
    seen_during_run = {}

    def _capture_mid_run(**kwargs):
        seen_during_run["rows"] = list_registered_subagents()
        return {
            "final_response": "done",
            "completed": True,
            "interrupted": False,
            "api_calls": 1,
            "messages": [],
        }

    child.run_conversation.side_effect = _capture_mid_run

    _run_single_child(
        task_index=0,
        goal="durable registry regression test goal",
        child=child,
        parent_agent=parent,
    )

    mid_run_rows = seen_during_run.get("rows", [])
    assert any(r.subagent_id == "sa-0-durabletest" for r in mid_run_rows), (
        "durable cross_session_subagents row was not written before the "
        "child started running"
    )
    matching = [r for r in mid_run_rows if r.subagent_id == "sa-0-durabletest"][0]
    assert matching.owner_session_id == "sess-durable-registry"
    assert matching.goal == "durable registry regression test goal"

    # After completion: the row must be cleared, not left dangling.
    after_rows = list_registered_subagents()
    assert not any(r.subagent_id == "sa-0-durabletest" for r in after_rows), (
        "durable cross_session_subagents row was not cleaned up on completion"
    )


def test_delegate_task_spawn_warns_on_real_cwd_collision(cst, home, monkeypatch):
    """Regression, real temp state.db (not mocked): when another live
    subagent is already registered at the same cwd, a newly spawned child
    must (a) get the warning injected into its own system prompt, and (b)
    carry it on _delegate_cwd_collision_warning so the parent's dispatch
    payload can surface it too. This is the proactive half of the 2026-08-11
    follow-up (Fable review: passive list_agents visibility alone doesn't
    satisfy "aware of each other" for a stomping-prevention goal -- a
    subagent has to actually be warned before it edits, not just be ABLE
    to check).
    """
    from unittest.mock import MagicMock, patch

    from tools.cross_session_transport import register_subagent
    from tools.delegate_tool import _build_child_agent

    collision_cwd = str(home / "shared-repo")

    # Another live subagent (different owner, different process in reality)
    # is already registered working in this exact directory.
    register_subagent(
        subagent_id="sa-0-existing",
        owner_session_id="sess-other-owner",
        goal="already editing this repo",
        cwd=collision_cwd,
        status="running",
    )

    parent = MagicMock()
    parent.session_id = "sess-new-spawn"
    parent._delegate_depth = 0
    parent.provider = None
    parent.base_url = None
    parent.api_key = None
    parent.model = "test-model"

    with patch(
        "tools.delegate_tool._resolve_workspace_hint", return_value=collision_cwd
    ), patch("run_agent.AIAgent") as MockAgent:
        mock_child = MagicMock()
        MockAgent.return_value = mock_child

        child = _build_child_agent(
            task_index=0,
            goal="edit something in the shared repo",
            context=None,
            toolsets=None,
            model=None,
            max_iterations=10,
            parent_agent=parent,
            task_count=1,
        )

    # (a) the warning reached the child's own system prompt
    _, kwargs = MockAgent.call_args
    prompt = kwargs.get("ephemeral_system_prompt") or ""
    assert "sa-0-existing" in prompt
    assert "WARNING" in prompt
    assert collision_cwd in prompt

    # (b) it's also stashed for the parent's dispatch payload to surface
    assert "sa-0-existing" in (child._delegate_cwd_collision_warning or "")


def test_delegate_task_spawn_no_warning_when_no_collision(cst, home, monkeypatch):
    """Symmetric negative case, same real-DB setup: an unrelated cwd must
    produce no warning at all, confirming the check doesn't false-positive."""
    from unittest.mock import MagicMock, patch

    from tools.cross_session_transport import register_subagent
    from tools.delegate_tool import _build_child_agent

    register_subagent(
        subagent_id="sa-0-elsewhere",
        owner_session_id="sess-other-owner",
        goal="working on an unrelated repo",
        cwd=str(home / "totally-different-repo"),
        status="running",
    )

    parent = MagicMock()
    parent.session_id = "sess-new-spawn-2"
    parent._delegate_depth = 0
    parent.provider = None
    parent.base_url = None
    parent.api_key = None
    parent.model = "test-model"

    with patch(
        "tools.delegate_tool._resolve_workspace_hint",
        return_value=str(home / "my-own-repo"),
    ), patch("run_agent.AIAgent") as MockAgent:
        mock_child = MagicMock()
        MockAgent.return_value = mock_child

        child = _build_child_agent(
            task_index=0,
            goal="edit something unrelated",
            context=None,
            toolsets=None,
            model=None,
            max_iterations=10,
            parent_agent=parent,
            task_count=1,
        )

    _, kwargs = MockAgent.call_args
    prompt = kwargs.get("ephemeral_system_prompt") or ""
    assert "WARNING" not in prompt
    assert child._delegate_cwd_collision_warning is None


# ---------------------------------------------------------------------------
# hermes agents inbox
# ---------------------------------------------------------------------------


def _inbox_args(**kw):
    defaults = dict(
        agents_command="inbox",
        approve_id=None,
        deny_id=None,
        session_id=None,
        all=False,
        json=False,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def _hold_one(cst, monkeypatch, body="needs approval"):
    _hold(monkeypatch, cst)
    _register(cst, "sender", "alpha")
    _register(cst, "recv", "beta")
    cst.send_message(
        from_session_id="sender", from_name="alpha", recipient="recv", body=body
    )
    (row,) = cst.list_inbox(status=cst.STATUS_HELD)
    return int(row["id"])


def test_inbox_lists_held_messages(cst, monkeypatch, capsys):
    from hermes_cli.agents_inbox import agents_command

    row_id = _hold_one(cst, monkeypatch)
    assert agents_command(_inbox_args()) == 0
    out = capsys.readouterr().out
    assert f"[{row_id}]" in out
    assert "alpha" in out
    assert "needs approval" in out


def test_inbox_empty_listing(home, capsys):
    from hermes_cli.agents_inbox import agents_command

    assert agents_command(_inbox_args()) == 0
    assert "No cross-session messages held" in capsys.readouterr().out


def test_inbox_approve_returns_message_to_pending(cst, monkeypatch, capsys):
    from hermes_cli.agents_inbox import agents_command

    row_id = _hold_one(cst, monkeypatch)
    assert agents_command(_inbox_args(approve_id=row_id)) == 0
    out = capsys.readouterr().out
    assert "returned to the pending queue" in out
    # held -> pending, NOT held -> delivered.
    (row,) = cst.list_inbox(status=cst.STATUS_PENDING)
    assert int(row["id"]) == row_id


def test_inbox_approve_then_recipient_drain_delivers_framed(cst, csi, monkeypatch):
    """End-to-end: hold -> approve -> the recipient's own drain delivers it."""
    from hermes_cli.agents_inbox import agents_command

    row_id = _hold_one(cst, monkeypatch, body="approved content")
    agents_command(_inbox_args(approve_id=row_id))

    # Recipient's policy is re-evaluated at drain time; now it accepts.
    _accept(monkeypatch, cst)
    injected = []
    assert csi.drain_to_idle_injection(session_id="recv", inject=injected.append) == 1
    assert AGENT_MESSAGE_MARKER_OPEN in injected[0]
    assert "approved content" in injected[0]
    assert injected[0] != "approved content"


def test_inbox_deny_blocks_delivery(cst, csi, monkeypatch, capsys):
    from hermes_cli.agents_inbox import agents_command

    row_id = _hold_one(cst, monkeypatch)
    assert agents_command(_inbox_args(deny_id=row_id)) == 0
    assert "denied" in capsys.readouterr().out

    _accept(monkeypatch, cst)
    injected = []
    assert csi.drain_to_idle_injection(session_id="recv", inject=injected.append) == 0
    assert injected == []
    (row,) = cst.list_inbox(status=cst.STATUS_DENIED)
    assert int(row["id"]) == row_id


def test_inbox_approve_unknown_id_reports_failure(home, capsys):
    from hermes_cli.agents_inbox import agents_command

    assert agents_command(_inbox_args(approve_id=999)) == 1
    assert "was not approved" in capsys.readouterr().out


def test_inbox_approve_and_deny_are_mutually_exclusive(home, capsys):
    from hermes_cli.agents_inbox import agents_command

    assert agents_command(_inbox_args(approve_id=1, deny_id=2)) == 2
    assert "mutually exclusive" in capsys.readouterr().out


def test_inbox_json_output(cst, monkeypatch, capsys):
    import json

    from hermes_cli.agents_inbox import agents_command

    row_id = _hold_one(cst, monkeypatch)
    assert agents_command(_inbox_args(json=True)) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [int(r["id"]) for r in rows] == [row_id]


def test_inbox_all_flag_shows_every_status(cst, monkeypatch, capsys):
    from hermes_cli.agents_inbox import agents_command

    row_id = _hold_one(cst, monkeypatch)
    agents_command(_inbox_args(deny_id=row_id))
    capsys.readouterr()

    agents_command(_inbox_args())
    assert "No cross-session messages held" in capsys.readouterr().out

    agents_command(_inbox_args(all=True))
    assert f"[{row_id}]" in capsys.readouterr().out


def test_inbox_unknown_subcommand(home, capsys):
    from hermes_cli.agents_inbox import agents_command

    assert agents_command(_inbox_args(agents_command="bogus")) == 2
    assert "unknown agents subcommand" in capsys.readouterr().out


def test_agents_parser_wires_inbox_flags():
    from hermes_cli.subcommands.agents import build_agents_parser

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    sentinel = object()
    build_agents_parser(subparsers, cmd_agents=lambda _a: sentinel)

    args = parser.parse_args(["agents", "inbox", "--approve", "7"])
    assert args.agents_command == "inbox"
    assert args.approve_id == 7
    assert args.func(args) is sentinel

    args = parser.parse_args(["agents", "inbox", "--deny", "9", "--json"])
    assert args.deny_id == 9
    assert args.json is True


def test_agents_is_a_registered_builtin_subcommand():
    from hermes_cli.main import _BUILTIN_SUBCOMMANDS

    assert "agents" in _BUILTIN_SUBCOMMANDS


# ---------------------------------------------------------------------------
# config.yaml: cross_session.inbound
# ---------------------------------------------------------------------------


def test_inbound_key_is_a_documented_default():
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert "cross_session" in DEFAULT_CONFIG
    assert DEFAULT_CONFIG["cross_session"]["inbound"] == ""


def test_cross_session_is_a_known_root_key():
    """An unrecognized root key would be reported as unknown config."""
    from hermes_cli.config import _KNOWN_ROOT_KEYS

    assert "cross_session" in _KNOWN_ROOT_KEYS


def test_inbound_default_falls_back_to_per_origin_policy(cst, monkeypatch):
    """Empty/unset config keeps the conservative per-origin defaults."""
    monkeypatch.setattr(
        "hermes_cli.config.load_config", lambda *a, **k: {"cross_session": {}}
    )
    assert cst.resolve_inbound_policy(session_origin=SessionOrigin.CLI) == cst.POLICY_HOLD
    assert (
        cst.resolve_inbound_policy(session_origin=SessionOrigin.GATEWAY)
        == cst.POLICY_REFUSE
    )


def test_inbound_config_override_wins_for_every_origin(cst, monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda *a, **k: {"cross_session": {"inbound": "accept"}},
    )
    assert (
        cst.resolve_inbound_policy(session_origin=SessionOrigin.CLI)
        == cst.POLICY_ACCEPT
    )
    assert (
        cst.resolve_inbound_policy(session_origin=SessionOrigin.GATEWAY)
        == cst.POLICY_ACCEPT
    )


def test_invalid_inbound_value_falls_back_to_default(cst, monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda *a, **k: {"cross_session": {"inbound": "sure-why-not"}},
    )
    assert cst.resolve_inbound_policy(session_origin=SessionOrigin.CLI) == cst.POLICY_HOLD


# ---------------------------------------------------------------------------
# cli.py wiring
# ---------------------------------------------------------------------------


def test_cli_idle_hook_injects_framed_body_into_pending_input(cst, csi, monkeypatch):
    """Exercise cli.py's actual method body against a stand-in CLI object."""
    import queue

    import cli as cli_module

    _accept(monkeypatch, cst)
    _register(cst, "sender", "alpha")
    _register(cst, "recv", "beta")
    raw = "cli wiring check"
    cst.send_message(
        from_session_id="sender", from_name="alpha", recipient="recv", body=raw
    )

    fake = types.SimpleNamespace()
    fake.session_id = "recv"
    fake._pending_input = queue.Queue()
    fake._fire_attention_signals = lambda _s: None

    cli_module.HermesCLI._drain_cross_session_inbox(fake)

    payload = fake._pending_input.get_nowait()
    assert payload != raw
    assert AGENT_MESSAGE_MARKER_OPEN in payload
    assert raw in payload


def test_cli_idle_hook_noop_without_session_id():
    import queue

    import cli as cli_module

    fake = types.SimpleNamespace()
    fake.session_id = ""
    fake._pending_input = queue.Queue()
    fake._fire_attention_signals = lambda _s: None

    cli_module.HermesCLI._drain_cross_session_inbox(fake)
    assert fake._pending_input.empty()


def test_cli_idle_hook_passes_attention_signal_for_holds(cst, monkeypatch):
    import queue

    import cli as cli_module

    _hold(monkeypatch, cst)
    _register(cst, "sender", "alpha")
    _register(cst, "recv", "beta")
    cst.send_message(
        from_session_id="sender", from_name="alpha", recipient="recv", body="hold me"
    )
    with cst._transaction() as conn:
        conn.execute("UPDATE cross_session_inbox SET status = ?", (cst.STATUS_PENDING,))

    signals = []
    fake = types.SimpleNamespace()
    fake.session_id = "recv"
    fake._pending_input = queue.Queue()
    fake._fire_attention_signals = signals.append

    cli_module.HermesCLI._drain_cross_session_inbox(fake)

    assert fake._pending_input.empty()
    assert len(signals) == 1


def test_process_loop_calls_the_idle_drain_hook():
    """The hook must actually be wired into the 0.1s idle tick."""
    import inspect

    import cli as cli_module

    src = inspect.getsource(cli_module.HermesCLI.run)
    assert "_drain_cross_session_inbox()" in src
    assert '_drain_process_notifications("cli-idle")' in src


def test_conversation_loop_calls_the_midturn_hook():
    import inspect

    import agent.conversation_loop as loop

    src = inspect.getsource(loop)
    assert "drain_into_pending_steer(agent)" in src
    # Must feed the steer machinery BEFORE it drains, or the message waits a
    # whole extra tool batch.
    assert src.index("drain_into_pending_steer(agent)") < src.index(
        "_pre_api_steer = agent._drain_pending_steer()"
    )


def test_run_agent_heartbeats_on_the_activity_hook():
    import inspect

    import run_agent

    src = inspect.getsource(run_agent.AIAgent._touch_activity)
    assert "_heartbeat_cross_session_registry" in src
