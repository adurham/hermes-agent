"""Integration tests for the send DISPATCH seam — the wiring between the
tool layer and each transport's actual send path.

This file exists because of a specific shipped bug: ``send_agent_message``
resolved a cross-process recipient correctly, then dead-ended in
``tools/agent_messaging_tools.py`` with a bare "not reachable in-process"
error instead of calling ``cross_session_transport.send_message()``. Neither
transport's own test suite could catch it — neither imports the other, and
the seam between them was owned by neither.

So the load-bearing assertion here is deliberately end-to-end and
un-mockable: resolve to CROSS_PROCESS_DB via Transport B's real registration
path, call the real tool function, and then assert a row actually landed in
``cross_session_inbox``. Anything less (asserting on the return string,
mocking send_message) would have passed against the buggy code too.
"""

from __future__ import annotations

import pytest

from tools.agent_messaging_contract import (
    DeliveryOutcome,
    Participant,
    ParticipantKind,
    SendResult,
    SessionOrigin,
    TransportKind,
    _reset_transport_lookups_for_tests,
    get_transport_send,
    resolve_transport,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME so each test gets its own real state.db.

    Mirrors tests/test_cross_session_transport.py's fixture: a real sqlite
    file, never a mock, because durable insertion is the exact thing under
    test.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    import hermes_constants

    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)

    import tools.cross_session_transport as cst

    monkeypatch.setattr(cst, "get_hermes_home", lambda: tmp_path)
    return tmp_path


@pytest.fixture()
def cst(home):
    import tools.cross_session_transport as module

    return module


@pytest.fixture(autouse=True)
def _clean_registry():
    """Transport registrations are process-global; isolate each test.

    Transport B's ``register_lookup()`` is idempotent via its own
    ``_lookup_registered`` module flag, so clearing the contract's registry
    alone is not enough — the flag has to be reset too, or every test after
    the first silently gets an empty registry.
    """
    import tools.cross_session_transport as cst_mod

    _reset_transport_lookups_for_tests()
    cst_mod._lookup_registered = False
    yield
    _reset_transport_lookups_for_tests()
    cst_mod._lookup_registered = False


class _FakeAgent:
    """Minimal stand-in for a top-level session AIAgent.

    ``_caller_participant`` only reads ``session_id`` and the absence of
    ``_subagent_id``, so this is the whole surface the tool layer touches.
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id


def _register_session(cst, session_id: str, name: str, origin=SessionOrigin.CLI):
    assert cst.heartbeat_registry(
        session_id=session_id, name=name, session_origin=origin
    )


def _force_accept_policy(cst, monkeypatch):
    """Pin the recipient's inbound policy to ``accept`` so a successful send
    lands as a PENDING row rather than HELD. The CLI default is ``hold``,
    which is exercised separately below.
    """
    monkeypatch.setattr(
        cst, "resolve_inbound_policy", lambda *, session_origin=None: cst.POLICY_ACCEPT
    )


# ---------------------------------------------------------------------------
# The regression test: CROSS_PROCESS_DB send actually reaches the DB
# ---------------------------------------------------------------------------


def test_cross_process_send_inserts_inbox_row(cst, monkeypatch):
    """send_agent_message -> CROSS_PROCESS_DB -> real row in cross_session_inbox.

    This is the test whose absence let the dispatch bug ship.
    """
    import tools.agent_messaging_tools as amt

    cst.register_lookup()
    _force_accept_policy(cst, monkeypatch)

    _register_session(cst, "sess-sender", "sender-box")
    _register_session(cst, "sess-recipient", "recipient-box")

    out = amt.send_agent_message(
        recipient="recipient-box",
        body="ping from the dispatch test",
        agent=_FakeAgent("sess-sender"),
    )

    # (a) the call succeeded — specifically NOT the old dead-end error.
    assert "not reachable in-process" not in out
    assert not out.lower().startswith("error")
    assert DeliveryOutcome.QUEUED_DURABLE.value in out

    # (b) a row actually exists in cross_session_inbox, with correct
    #     body/recipient/sender. This is the assertion that fails against
    #     the buggy code.
    rows = cst.list_inbox(session_id="sess-recipient", status=cst.STATUS_PENDING)
    assert len(rows) == 1
    row = rows[0]
    assert row["body"] == "ping from the dispatch test"
    assert row["to_session_id"] == "sess-recipient"
    assert row["from_session_id"] == "sess-sender"
    # from_name resolved from the sender's OWN registry row, not the raw id.
    assert row["from_name"] == "sender-box"


def test_cross_process_send_resolves_by_session_id_too(cst, monkeypatch):
    """Addressing by raw session_id works identically to addressing by name."""
    import tools.agent_messaging_tools as amt

    cst.register_lookup()
    _force_accept_policy(cst, monkeypatch)
    _register_session(cst, "sess-sender", "sender-box")
    _register_session(cst, "sess-recipient", "recipient-box")

    amt.send_agent_message(
        recipient="sess-recipient",
        body="addressed by id",
        agent=_FakeAgent("sess-sender"),
    )

    rows = cst.list_inbox(session_id="sess-recipient", status=cst.STATUS_PENDING)
    assert [r["body"] for r in rows] == ["addressed by id"]


def test_cross_process_send_under_hold_policy_lands_held(cst, monkeypatch):
    """A hold-policy recipient gets a HELD row, and the caller is told so."""
    import tools.agent_messaging_tools as amt

    cst.register_lookup()
    monkeypatch.setattr(
        cst, "resolve_inbound_policy", lambda *, session_origin=None: cst.POLICY_HOLD
    )
    _register_session(cst, "sess-sender", "sender-box")
    _register_session(cst, "sess-recipient", "recipient-box")

    out = amt.send_agent_message(
        recipient="recipient-box",
        body="held message",
        agent=_FakeAgent("sess-sender"),
    )

    assert DeliveryOutcome.HELD.value in out
    rows = cst.list_inbox(session_id="sess-recipient", status=cst.STATUS_HELD)
    assert len(rows) == 1
    assert rows[0]["body"] == "held message"


def test_sender_name_falls_back_to_session_id_when_unregistered(cst, monkeypatch):
    """A sender with no registry row of its own still sends; from_name degrades
    to the raw session_id rather than blocking delivery.
    """
    import tools.agent_messaging_tools as amt

    cst.register_lookup()
    _force_accept_policy(cst, monkeypatch)
    # Note: sender is deliberately NOT registered.
    _register_session(cst, "sess-recipient", "recipient-box")

    amt.send_agent_message(
        recipient="recipient-box",
        body="from an unregistered sender",
        agent=_FakeAgent("sess-ghost"),
    )

    rows = cst.list_inbox(session_id="sess-recipient", status=cst.STATUS_PENDING)
    assert len(rows) == 1
    assert rows[0]["from_name"] == "sess-ghost"
    assert rows[0]["from_session_id"] == "sess-ghost"


def test_adapter_passes_resolved_session_id_not_raw_recipient_string(cst, monkeypatch):
    """The adapter must hand send_message() the ALREADY-RESOLVED session_id.

    Guards the TOCTOU decision: re-resolving the caller's free-text string a
    second time inside send_message() could pick a different (or newly
    ambiguous) recipient. Verified by capturing send_message's kwargs.
    """
    import tools.agent_messaging_tools as amt

    cst.register_lookup()
    _force_accept_policy(cst, monkeypatch)
    _register_session(cst, "sess-sender", "sender-box")
    _register_session(cst, "sess-recipient", "recipient-box")

    captured = {}
    real_send = cst.send_message

    def _spy(**kwargs):
        captured.update(kwargs)
        return real_send(**kwargs)

    monkeypatch.setattr(cst, "send_message", _spy)

    # Address by NAME; the adapter should translate to the session_id.
    amt.send_agent_message(
        recipient="recipient-box", body="x", agent=_FakeAgent("sess-sender")
    )

    assert captured["recipient"] == "sess-recipient"
    assert captured["hop_count"] == 0  # documented gap: no turn state at the tool


# ---------------------------------------------------------------------------
# Regression guard: the working transport still works after the refactor
# ---------------------------------------------------------------------------


def test_in_process_send_still_dispatches_through_the_new_seam(monkeypatch):
    """An IN_PROCESS resolution reaches send_in_process via resolution.send.

    The dispatch fix replaced a hardcoded ``send_in_process(...)`` call with
    a registry lookup; this makes sure Transport A did not quietly break in
    the process.
    """
    import tools.agent_messaging_tools as amt
    import tools.agent_messaging_transport_a as ta

    ta._reset_for_tests()
    # Re-register: the autouse fixture cleared the process-global registry.
    from tools.agent_messaging_contract import register_transport

    register_transport(TransportKind.IN_PROCESS, ta.in_process_lookup, ta._in_process_send)

    calls = []

    def _spy(*, sender, recipient, body):
        calls.append((sender.participant_id, recipient.participant_id, body))
        return SendResult(
            outcome=DeliveryOutcome.QUEUED_EPHEMERAL, detail="queued for test"
        )

    monkeypatch.setattr(ta, "send_in_process", _spy)

    recipient = Participant(
        participant_id="sess-peer",
        kind=ParticipantKind.SESSION,
        owner_session_id="sess-peer",
        session_origin=SessionOrigin.CLI,
    )
    ta.register_session_participant(recipient, agent=object(), cli=None)

    # The in-process lookup requires a shared owner_session_id.
    out = amt._send(
        Participant(
            participant_id="sess-peer",
            kind=ParticipantKind.SESSION,
            owner_session_id="sess-peer",
            session_origin=SessionOrigin.CLI,
        ),
        "sess-peer",
        "hello in-process",
    )

    assert calls == [("sess-peer", "sess-peer", "hello in-process")]
    assert DeliveryOutcome.QUEUED_EPHEMERAL.value in out

    ta._reset_for_tests()


def test_both_transports_register_a_send_callable():
    """Structural guard: a lookup without a paired send is the original bug."""
    import tools.agent_messaging_transport_a as ta
    import tools.cross_session_transport as cst_mod
    from tools.agent_messaging_contract import register_transport

    register_transport(
        TransportKind.IN_PROCESS, ta.in_process_lookup, ta._in_process_send
    )
    register_transport(
        TransportKind.CROSS_PROCESS_DB,
        cst_mod._cross_process_lookup,
        cst_mod._cross_process_send,
    )

    assert get_transport_send(TransportKind.IN_PROCESS) is not None
    assert get_transport_send(TransportKind.CROSS_PROCESS_DB) is not None


def test_resolution_with_no_send_callable_errors_instead_of_crashing():
    """Defensive path: lookup registered, send missing -> tool_error, not a crash."""
    import tools.agent_messaging_tools as amt
    from tools.agent_messaging_contract import register_transport_lookup

    ghost = Participant(
        participant_id="ghost",
        kind=ParticipantKind.SESSION,
        owner_session_id="ghost",
    )
    register_transport_lookup(
        TransportKind.CROSS_PROCESS_DB, lambda sender, rid: ghost
    )

    sender = Participant(
        participant_id="me", kind=ParticipantKind.SESSION, owner_session_id="me"
    )
    resolution = resolve_transport(sender, "ghost")
    assert resolution.send is None

    out = amt._send(sender, "ghost", "body")
    assert "no send path" in out


# ---------------------------------------------------------------------------
# _format_result wording branches
# ---------------------------------------------------------------------------


def test_format_result_held_does_not_claim_queued_for_delivery():
    """HELD must not reuse the 'queued means accepted for delivery' note —
    a held message is queued for a HUMAN and may never be delivered.
    """
    import tools.agent_messaging_tools as amt

    out = amt._format_result(
        SendResult(outcome=DeliveryOutcome.HELD, detail="queued for 'peer'")
    )

    assert "human approval" in out
    assert "may never be delivered" in out
    assert "queued means accepted for delivery" not in out


@pytest.mark.parametrize(
    "outcome",
    [DeliveryOutcome.QUEUED_DURABLE, DeliveryOutcome.QUEUED_EPHEMERAL],
)
def test_format_result_queued_keeps_not_delivered_wording(outcome):
    import tools.agent_messaging_tools as amt

    out = amt._format_result(SendResult(outcome=outcome, detail="d"))

    assert "queued means accepted for delivery" in out
    assert "human approval" not in out


def test_format_result_not_found_is_an_error():
    import tools.agent_messaging_tools as amt

    out = amt._format_result(
        SendResult(outcome=DeliveryOutcome.RECIPIENT_NOT_FOUND, detail="gone")
    )

    assert "gone" in out
    assert "Note:" not in out
