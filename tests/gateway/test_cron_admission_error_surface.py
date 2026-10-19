"""Cron admission conflicts keep a safe, actionable terminal error."""
from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from hermes_state_runtime import RuntimeStoreError


def _claimed():
    authority = SimpleNamespace(
        runner=SimpleNamespace(),
        pending_results={},
        executing={"session-1": object()},
    )
    live = SimpleNamespace(
        source=SimpleNamespace(),
        event_stream=SimpleNamespace(lock=threading.RLock()),
    )
    ref = SimpleNamespace(session_id="session-1")
    row = {"admission_id": "admission-1", "payload": {"text": "hello"}}
    return authority, live, ref, row


@pytest.mark.asyncio
async def test_cron_admission_conflict_preserves_actionable_result(monkeypatch):
    from gateway import session_finite, session_policy
    from gateway import session_authority
    from gateway.session_authority import SessionAuthority

    async def fail(*_args):
        raise RuntimeStoreError("admission_conflict")

    monkeypatch.setattr(session_finite, "execute_finite_admission", fail)
    monkeypatch.setattr(session_policy, "policy_for_source", lambda *_args: SimpleNamespace(source="cron"))
    monkeypatch.setattr(time, "time", lambda: 123.5)
    authority, live, ref, row = _claimed()

    response, outcome = await SessionAuthority._execute_claimed(authority, ref, live, row)

    assert outcome == "failed"
    assert response == "This session belongs to a scheduled run and cannot accept your message. Start a new chat to send it."
    assert authority.pending_results[row["admission_id"]]["result"] == {
        "final_response": "",
        "messages": [],
        "failed": True,
        "completed": False,
        "error": response,
        "error_surface": {"layer": "gateway", "code": "cron_session_read_only", "retryable": False},
        "failure_at": 123.5,
    }


@pytest.mark.asyncio
async def test_same_message_after_terminal_cron_admission_is_rejected_without_running_job(owner, monkeypatch):
    from gateway import session_authority
    from gateway.config import Platform
    from gateway.session import SessionSource
    from gateway.session_authority import SessionAuthority
    from gateway.session_contract import SessionRef
    from gateway.session_local import LocalSessionAdapter
    from hermes_state_runtime import admit_session_input, claim_session_input, settle_session_input

    session_id = "cron-guard-session"
    owner.db.create_session(session_id, source="local")
    admit_session_input(owner.db, epoch=owner.epoch, principal_id="cron-user", session_id=session_id,
                        request_id="request-1", payload={"text": "expected"})
    original = claim_session_input(owner.db, epoch=owner.epoch, session_id=session_id)
    settle_session_input(owner.db, epoch=owner.epoch, admission_id=original["admission_id"],
                         generation=original["generation"], outcome="completed")
    admit_session_input(owner.db, epoch=owner.epoch, principal_id="cron-user", session_id=session_id,
                        request_id="request-2", payload={"text": "expected"})
    row = claim_session_input(owner.db, epoch=owner.epoch, session_id=session_id)
    source = SessionSource(platform=Platform.LOCAL, chat_id=session_id, user_id="cron-user", profile="default")
    live = SimpleNamespace(source=source, event_stream=SimpleNamespace(lock=threading.RLock()))
    owner.sessions[session_id] = live
    adapter = LocalSessionAdapter(owner)
    adapter.register_source(source)
    owner.runner._adapter_for_source = lambda _source: adapter
    adapter.policies[session_id] = SimpleNamespace(
        source="cron",
        request_json=json.dumps({
            "cron_job": {"id": "job-1"}, "extra_prompt": "expected", "request_id": "request-1",
        }),
    )
    run_calls = []

    def run_job(*args, **kwargs):
        run_calls.append((args, kwargs))
        raise AssertionError("cron run_job must not run after the admission guard rejects the payload")

    from cron import scheduler

    monkeypatch.setattr(scheduler, "run_job", run_job)
    monkeypatch.setattr(time, "time", lambda: 123.5)
    response, outcome = await SessionAuthority._execute_claimed(
        owner, SessionRef("default", session_id), live, row)

    assert (response, outcome) == (
        "This session belongs to a scheduled run and cannot accept your message. Start a new chat to send it.",
        "failed",
    )
    assert run_calls == []
    assert owner.pending_results[row["admission_id"]]["result"]["failure_at"] == 123.5


@pytest.mark.asyncio
async def test_unexpected_admission_failure_stays_generic(monkeypatch):
    from gateway import session_finite, session_policy
    from gateway.session_authority import SessionAuthority

    async def fail(*_args):
        raise RuntimeStoreError("admission_conflict")

    monkeypatch.setattr(session_finite, "execute_finite_admission", fail)
    monkeypatch.setattr(session_policy, "policy_for_source", lambda *_args: SimpleNamespace(source="cli"))
    authority, live, ref, row = _claimed()

    response, outcome = await SessionAuthority._execute_claimed(authority, ref, live, row)

    assert (response, outcome) == ("The admitted turn failed.", "failed")
    assert authority.pending_results == {}


def test_completion_forwards_only_safe_cron_error_metadata():
    from gateway.session_settlement_recovery import completion_payload

    row = {"admission_id": "admission-1", "request_id": "request-1"}
    captured = {
        "result": {
            "error": "This session belongs to a scheduled run and cannot accept your message. Start a new chat to send it.",
            "error_surface": {"layer": "gateway", "code": "cron_session_read_only", "retryable": False},
            "failure_at": 123.5,
        }
    }

    complete = completion_payload(row, {"outcome": "failed"}, captured["result"]["error"], captured)

    assert complete["error"] == captured["result"]["error"]
    assert complete["error_surface"] == captured["result"]["error_surface"]
    assert complete["timestamp"] == 123.5


def test_saved_result_keeps_failure_time_for_reopened_admission(owner):
    from gateway.session_results import admission_result, finish_result
    from gateway.session_settlement_recovery import completion_payload
    from hermes_state_runtime import admit_session_input, claim_session_input

    owner.db.create_session("cron", source="local")
    admit_session_input(owner.db, epoch=owner.epoch, principal_id="cron", session_id="cron",
                        request_id="request-1", payload={"text": "hello"})
    row = claim_session_input(owner.db, epoch=owner.epoch, session_id="cron")
    captured = {
        "result": {
            "final_response": "",
            "messages": [],
            "failed": True,
            "completed": False,
            "error": "This session belongs to a scheduled run and cannot accept your message. Start a new chat to send it.",
            "error_surface": {"layer": "gateway", "code": "cron_session_read_only", "retryable": False},
            "failure_at": 123.5,
        },
        "usage": {},
    }

    finish_result(owner.db, epoch=owner.epoch, row=row, response=captured["result"]["error"],
                  outcome="failed", result=captured)

    reopened = admission_result(owner.db, row["admission_id"])
    assert reopened["result"]["failure_at"] == 123.5
    complete = completion_payload(row, {"outcome": "failed"}, reopened["result"]["error"], reopened)
    assert complete["timestamp"] == 123.5
