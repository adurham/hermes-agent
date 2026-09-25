"""Call-site coverage for the refusal ladder's history-scrub rung.

The fork's refusal recovery is a three-rung ladder:

  1. a configured fallback provider (different providers, different policies)
  2. scrub trigger patterns out of HISTORICAL context and retry  <-- fork-only
  3. give up with the /compact recovery hint

The v2026.9.14 decomposition of conversation_loop.py carried rungs 1 and 3 into
agent/turn_truncation.py::handle_content_policy_refusal but dropped rung 2 (the
call to _sanitize_messages_for_refusal_retry, pre-merge conversation_loop.py:3916).
tools/content_filter_scrub.py and the fork helper both survived intact with zero
callers, so authorized support work (a real pg_dump / S3 presign earlier in the
session) hard-failed on every later turn carrying it in context, where it used to
self-heal.

These tests drive the REAL handler -- the one turn_response_check.py routes
finish_reason=="content_filter" into -- so the rung cannot be silently
disconnected again.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

from agent.turn_retry_state import TurnRetryState

# pg_dump piped to S3: legitimate authorized support work that trips Anthropic's
# credential-exfiltration filter. test_trigger_is_a_real_pattern pins that this
# string really is one of content_filter_scrub's patterns, so the ladder tests
# below cannot silently pass against a string the scrub ignores.
TRIGGER = (
    "pg_dump -h db.internal -U admin --no-password customers "
    "| gzip | aws s3 cp - s3://bucket/dump.gz"
)


def _history(with_trigger: bool = True):
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "please export the table"},
        {"role": "assistant", "content": TRIGGER if with_trigger else "ran the export"},
        {"role": "user", "content": "now summarize what you did"},
    ]


def _agent(*, has_fallback: bool):
    """Stub with the surface handle_content_policy_refusal touches; the refusal
    normalization runs through the REAL AnthropicTransport."""
    from run_agent import AIAgent
    from agent.transports.anthropic import AnthropicTransport

    statuses = []
    a = SimpleNamespace(
        api_mode="anthropic_messages", provider="anthropic", model="claude-opus-4-7",
        log_prefix="", _refusal_sanitize_attempted=False,
        _is_anthropic_oauth=False, thinking_callback=None,
        _has_pending_fallback=lambda: has_fallback,
        _try_activate_fallback=lambda: has_fallback,
        _buffer_status=lambda s="", *a, **k: statuses.append(s),
        _emit_status=lambda s="", *a, **k: statuses.append(s),
        _buffer_diagnostic_status=lambda s="", *a, **k: statuses.append(s),
        _emit_diagnostic_status=lambda s="", *a, **k: statuses.append(s),
        _emit_diagnostic_wait=lambda s="", *a, **k: statuses.append(s),
        _flush_status_buffer=lambda *a, **k: None,
        _invoke_api_request_error_hook=lambda *a, **k: None,
        _extract_reasoning=lambda r: "",
        _cleanup_task_resources=lambda *a, **k: None,
        _persist_session=lambda *a, **k: None,
        _emit_notice=lambda *a, **k: None,
    )
    _transport = AnthropicTransport()
    a._get_transport = lambda: _transport
    a._sanitize_messages_for_refusal_retry = (
        AIAgent._sanitize_messages_for_refusal_retry.__get__(a, AIAgent)
    )
    return a, statuses


def _refusal_response():
    return SimpleNamespace(
        stop_reason="refusal", content=[], choices=[],
        usage=SimpleNamespace(input_tokens=10, output_tokens=0),
    )


def _run(agent, msgs, caplog):
    from agent.turn_truncation import handle_content_policy_refusal

    retry = TurnRetryState()
    with caplog.at_level(logging.WARNING, logger="agent.conversation_loop"):
        verdict = handle_content_policy_refusal(
            agent, _refusal_response(), retry, thinking_spinner=None, messages=msgs,
            api_messages=[], api_kwargs={}, active_system_prompt="sys",
            conversation_history=[], api_call_count=1, effective_task_id=None,
            turn_id="t1", api_request_id="r1", api_start_time=0.0,
            retry_count=0, max_retries=5,
        )
    return verdict, retry


def test_trigger_is_a_real_pattern():
    """Guard the guard: if content_filter_scrub stops matching TRIGGER, the
    ladder tests below would pass vacuously."""
    from tools.content_filter_scrub import scrub_message_content

    _, changed = scrub_message_content(TRIGGER)
    assert changed, "TRIGGER must be a real content_filter_scrub pattern"


class TestRefusalLadder:
    def test_rung1_fallback_wins_without_scrubbing(self, caplog):
        """A configured fallback is tried FIRST -- cheaper and lossless, so
        history is left untouched."""
        agent, _ = _agent(has_fallback=True)
        msgs = _history()
        verdict, _retry = _run(agent, msgs, caplog)
        assert verdict.action == "break"
        assert TRIGGER in str(msgs), "rung 1 must not scrub"
        assert agent._refusal_sanitize_attempted is False

    def test_rung2_scrubs_history_and_retries(self, caplog):
        """No fallback + scrubbable history -> strip the patterns, arm the
        rebuilt-messages restart, and retry instead of failing the turn."""
        agent, statuses = _agent(has_fallback=False)
        msgs = _history()
        verdict, retry = _run(agent, msgs, caplog)

        assert verdict.action == "break", "must retry, not surrender the turn"
        assert verdict.result is None
        assert TRIGGER not in str(msgs), "the trigger pattern must be gone"
        assert retry.restart_with_rebuilt_messages, (
            "the scrubbed history is only re-sent if the restart is armed"
        )
        assert retry.primary_recovery_attempted is False
        # The user's actual request must survive -- only HISTORY is scrubbed.
        assert msgs[-1]["content"] == "now summarize what you did"
        assert any("Paraphrasing" in s for s in statuses)
        assert any("sanitize retry" in r.message for r in caplog.records)

    def test_rung2_mutates_the_caller_s_list_in_place(self, caplog):
        """The loop holds this same list object; rebinding a local would strand
        the scrub."""
        agent, _ = _agent(has_fallback=False)
        msgs = _history()
        original_id = id(msgs)
        _run(agent, msgs, caplog)
        assert id(msgs) == original_id
        assert TRIGGER not in str(msgs)

    def test_rung3_gives_up_after_one_scrub_per_turn(self, caplog):
        """A second refusal in the same turn must not re-scrub (nothing new to
        find, and it would burn another API call)."""
        agent, _ = _agent(has_fallback=False)
        msgs = _history()
        _run(agent, msgs, caplog)          # rung 2 fires
        verdict2, retry2 = _run(agent, msgs, caplog)  # now exhausted

        assert verdict2.action == "return"
        assert isinstance(verdict2.result, dict)
        assert retry2.restart_with_rebuilt_messages is False

    def test_rung3_gives_up_when_nothing_to_scrub(self, caplog):
        """Clean history: no pointless retry, straight to the typed result."""
        agent, _ = _agent(has_fallback=False)
        msgs = _history(with_trigger=False)
        verdict, retry = _run(agent, msgs, caplog)

        assert verdict.action == "return"
        assert isinstance(verdict.result, dict)
        assert retry.restart_with_rebuilt_messages is False

    def test_scrub_failure_degrades_to_giving_up(self, caplog):
        """A raising scrub must not escape the refusal handler."""
        agent, _ = _agent(has_fallback=False)

        def _boom(_messages):
            raise RuntimeError("scrub exploded")

        agent._sanitize_messages_for_refusal_retry = _boom
        verdict, retry = _run(agent, _history(), caplog)
        assert verdict.action == "return"
        assert retry.restart_with_rebuilt_messages is False


class TestRetiredRefusalPredicate:
    """``is_anthropic_refusal`` was retired 2026-09-22: upstream's
    _STOP_REASON_MAP now turns stop_reason=="refusal" into
    finish_reason=="content_filter", which turn_response_check.py routes into
    handle_content_policy_refusal -- the same rung the predicate used to feed."""

    def test_predicate_is_gone(self):
        import agent.fork.anthropic_recovery as ar

        assert not hasattr(ar, "is_anthropic_refusal")

    def test_forwarder_is_gone(self):
        from run_agent import AIAgent

        assert not hasattr(AIAgent, "_is_anthropic_refusal")

    def test_upstream_maps_refusal_to_content_filter(self):
        """The replacement path must actually exist."""
        from agent.transports.anthropic import AnthropicTransport

        assert AnthropicTransport._STOP_REASON_MAP["refusal"] == "content_filter"

    def test_scrub_helper_survives_the_retirement(self):
        """The scrub is a DIFFERENT thing from the retired predicate and is
        still wired (see TestRefusalLadder)."""
        from agent.fork.anthropic_recovery import sanitize_messages_for_refusal_retry
        from run_agent import AIAgent

        assert callable(sanitize_messages_for_refusal_retry)
        assert hasattr(AIAgent, "_sanitize_messages_for_refusal_retry")
