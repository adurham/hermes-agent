"""Smoke tests for tools/agent_messaging_contract.py — the shared interface
seam for local agent messaging (design: docs/design/local-agent-messaging.md).

This tests the contract module in isolation, before either transport is
implemented against it. Transport-specific behavior belongs in each
transport's own test file, not here.
"""

from __future__ import annotations

import pytest

from tools.agent_messaging_contract import (
    AGENT_MESSAGE_MARKER_CLOSE,
    AGENT_MESSAGE_MARKER_OPEN,
    COALESCED_PENDING_CAP_BYTES,
    PER_MESSAGE_CAP_BYTES,
    TOOL_NAME_LIST_AGENTS,
    TOOL_NAME_SEND_AGENT_MESSAGE,
    TOOL_NAME_SEND_TO_PARENT,
    TOOLSET_NAME,
    DeliveryOutcome,
    MessageTooLargeError,
    Participant,
    ParticipantKind,
    SendResult,
    SessionOrigin,
    TransportKind,
    TransportResolution,
    _reset_transport_lookups_for_tests,
    build_agent_message_marker,
    check_message_size,
    register_transport_lookup,
    resolve_transport,
)


@pytest.fixture(autouse=True)
def _clean_lookups():
    _reset_transport_lookups_for_tests()
    yield
    _reset_transport_lookups_for_tests()


def _sender() -> Participant:
    return Participant(
        participant_id="sess-parent",
        kind=ParticipantKind.SESSION,
        owner_session_id="sess-parent",
        session_origin=SessionOrigin.CLI,
    )


class TestParticipantModel:
    def test_session_participant_has_no_parent(self):
        p = _sender()
        assert p.kind == ParticipantKind.SESSION
        assert p.parent_participant_id is None

    def test_subagent_participant_shape(self):
        p = Participant(
            participant_id="sa-0-abcd1234",
            kind=ParticipantKind.SUBAGENT,
            owner_session_id="sess-parent",
            parent_participant_id="sess-parent",
        )
        assert p.kind == ParticipantKind.SUBAGENT
        assert p.parent_participant_id == "sess-parent"
        # Subagents never carry a session_origin — that's a session-only field.
        assert p.session_origin is None


class TestResolveTransport:
    def test_no_lookups_registered_returns_not_found(self):
        result = resolve_transport(_sender(), "anything")
        assert result.kind == TransportKind.NOT_FOUND
        assert result.participant is None

    def test_first_matching_lookup_wins(self):
        target = Participant(
            participant_id="sa-0-abcd1234",
            kind=ParticipantKind.SUBAGENT,
            owner_session_id="sess-parent",
        )
        register_transport_lookup(
            TransportKind.IN_PROCESS, lambda sender, rid: target if rid == "sa-0-abcd1234" else None
        )
        register_transport_lookup(TransportKind.CROSS_PROCESS_DB, lambda sender, rid: None)

        result = resolve_transport(_sender(), "sa-0-abcd1234")
        assert result.kind == TransportKind.IN_PROCESS
        assert result.participant is target

    def test_falls_through_to_second_lookup(self):
        target = Participant(
            participant_id="sess-other",
            kind=ParticipantKind.SESSION,
            owner_session_id="sess-other",
            session_origin=SessionOrigin.GATEWAY,
        )
        register_transport_lookup(TransportKind.IN_PROCESS, lambda sender, rid: None)
        register_transport_lookup(
            TransportKind.CROSS_PROCESS_DB, lambda sender, rid: target if rid == "sess-other" else None
        )

        result = resolve_transport(_sender(), "sess-other")
        assert result.kind == TransportKind.CROSS_PROCESS_DB
        assert result.participant is target

    def test_unknown_id_never_raises(self):
        """Finding 6: unknown/hallucinated IDs get a typed NOT_FOUND result,
        never an unhandled exception."""
        register_transport_lookup(TransportKind.IN_PROCESS, lambda sender, rid: None)
        register_transport_lookup(TransportKind.CROSS_PROCESS_DB, lambda sender, rid: None)

        result = resolve_transport(_sender(), "totally-hallucinated-id")
        assert result.kind == TransportKind.NOT_FOUND

    def test_lookup_exception_does_not_propagate_as_a_bug_masking_not_found(self):
        """A lookup that raises is a bug in that transport's code, but this
        test documents the current (intentionally strict) behavior: we do
        NOT swallow exceptions here, because a raising lookup is a real
        programming error that should surface loudly, not be silently
        treated as NOT_FOUND. If a transport's lookup can legitimately fail
        without meaning "not found", it must catch its own exceptions and
        return None."""
        def _raises(sender, rid):
            raise RuntimeError("simulated transport bug")

        register_transport_lookup(TransportKind.IN_PROCESS, _raises)

        with pytest.raises(RuntimeError):
            resolve_transport(_sender(), "anything")


class TestMessageMarker:
    def test_marker_wraps_body_with_open_and_close(self):
        marker = build_agent_message_marker(
            sender_participant_id="sa-1-deadbeef",
            sender_kind=ParticipantKind.SUBAGENT,
            sender_origin=None,
            body="hello from a subagent",
        )
        assert AGENT_MESSAGE_MARKER_OPEN in marker
        assert AGENT_MESSAGE_MARKER_CLOSE in marker
        assert "hello from a subagent" in marker
        assert "sa-1-deadbeef" in marker
        assert "subagent" in marker

    def test_marker_includes_session_origin_when_present(self):
        marker = build_agent_message_marker(
            sender_participant_id="sess-gw-1",
            sender_kind=ParticipantKind.SESSION,
            sender_origin=SessionOrigin.GATEWAY,
            body="ping",
        )
        assert "gateway" in marker

    def test_marker_labels_missing_origin_as_n_slash_a(self):
        marker = build_agent_message_marker(
            sender_participant_id="sa-0-x",
            sender_kind=ParticipantKind.SUBAGENT,
            sender_origin=None,
            body="ping",
        )
        assert "n/a" in marker

    def test_marker_never_returns_bare_body_without_wrapping(self):
        """Regression guard for the final-sign-off-pass finding: the idle
        delivery path must never inject a bare message body. This test
        can't verify the CALL SITES (that's Transport A's job), but it
        pins the marker function itself to always wrap, so a future edit
        that accidentally short-circuits wrapping is caught here first."""
        marker = build_agent_message_marker(
            sender_participant_id="x",
            sender_kind=ParticipantKind.SESSION,
            sender_origin=SessionOrigin.CLI,
            body="just the body",
        )
        assert marker.strip() != "just the body"
        assert marker.startswith("\n\n" + AGENT_MESSAGE_MARKER_OPEN)
        assert marker.endswith(AGENT_MESSAGE_MARKER_CLOSE)


class TestSizeCaps:
    def test_message_under_cap_passes(self):
        check_message_size("x" * 100)  # should not raise

    def test_message_at_exact_cap_passes(self):
        check_message_size("x" * PER_MESSAGE_CAP_BYTES)  # should not raise

    def test_message_over_cap_raises(self):
        with pytest.raises(MessageTooLargeError):
            check_message_size("x" * (PER_MESSAGE_CAP_BYTES + 1))

    def test_error_message_mentions_file_path_alternative(self):
        with pytest.raises(MessageTooLargeError, match="file"):
            check_message_size("x" * (PER_MESSAGE_CAP_BYTES + 1))

    def test_multibyte_body_counts_utf8_bytes_not_chars(self):
        # Each "é" is 2 bytes in UTF-8 — a naive len(str) check would
        # undercount and let an oversized payload through.
        body = "é" * (PER_MESSAGE_CAP_BYTES // 2 + 10)
        with pytest.raises(MessageTooLargeError):
            check_message_size(body)

    def test_coalesced_cap_is_larger_than_per_message_cap(self):
        # Sanity: the queue cap must be able to hold more than one message,
        # or every second message to the same recipient would always reject.
        assert COALESCED_PENDING_CAP_BYTES > PER_MESSAGE_CAP_BYTES


class TestSendResult:
    def test_queued_ephemeral_is_not_a_delivery_guarantee(self):
        result = SendResult(outcome=DeliveryOutcome.QUEUED_EPHEMERAL)
        assert result.outcome == DeliveryOutcome.QUEUED_EPHEMERAL
        # This is a data/documentation test: QUEUED_EPHEMERAL must exist
        # as a distinct outcome from any terminal "delivered" state, since
        # Transport A never confirms actual delivery synchronously.
        assert result.outcome != DeliveryOutcome.HELD

    def test_recipient_not_found_is_distinct_outcome(self):
        result = SendResult(
            outcome=DeliveryOutcome.RECIPIENT_NOT_FOUND,
            detail="no active session or subagent matches 'sa-9-ffff'",
        )
        assert result.outcome == DeliveryOutcome.RECIPIENT_NOT_FOUND
        assert "sa-9-ffff" in result.detail


class TestToolNaming:
    def test_three_distinct_tool_names(self):
        """Decision: two distinctly-named tools (send_agent_message,
        send_to_parent), plus list_agents (session-only). All three must
        be distinct strings — a collision here would silently merge two
        different schemas."""
        names = {TOOL_NAME_SEND_AGENT_MESSAGE, TOOL_NAME_SEND_TO_PARENT, TOOL_NAME_LIST_AGENTS}
        assert len(names) == 3

    def test_toolset_name_matches_design_doc(self):
        assert TOOLSET_NAME == "cross_session"
