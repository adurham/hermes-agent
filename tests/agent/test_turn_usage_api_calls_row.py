"""FORK: the ``api_calls`` per-call telemetry sink must actually be written.

``hermes_state``'s FORK_SCHEMA_SQL creates ``api_calls`` (and FK-heals it) on
every store open, but the v2026.9.14 merge dropped its only writer: upstream
moved usage handling out of ``agent/conversation_loop.py`` into the new
``agent/turn_usage.py`` and the fork's ``record_api_call`` call site went with
it. The table was created-but-never-written -- session totals still landed, so
nothing looked broken, but the per-call cache split / latency / request id that
answers "was THIS turn a cold prefill?" was silently gone.

These tests exercise the real ``record_response_usage`` against a real
``SessionDB`` and read the real rows back, rather than asserting a mock was
called -- a mock would have passed throughout the regression.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

import hermes_state
from agent import turn_usage


class _Compressor:
    threshold_tokens = 100_000
    context_length = 200_000
    _verify_compaction_cleared_threshold = False
    _context_probed = False
    awaiting_real_usage_after_compression = False

    def update_from_response(self, _usage_dict) -> None:
        pass


@pytest.fixture
def agent_with_store(tmp_path, monkeypatch):
    """A real SessionDB plus the minimal agent surface record_response_usage reads."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = hermes_state.SessionDB(db_path=tmp_path / "state.db")
    session_id = "s_api_calls_probe"
    db.create_session(session_id, source="cli", model="claude-opus-5")
    agent = SimpleNamespace(
        _session_db=db, session_id=session_id, _session_db_created=True,
        model="claude-opus-5", provider="anthropic", base_url="https://api.anthropic.com",
        api_mode="anthropic_messages", api_key="", session_api_calls=0,
        context_compressor=_Compressor(), quiet_mode=True, verbose_logging=False,
        session_prompt_tokens=0, session_completion_tokens=0, session_total_tokens=0,
        session_input_tokens=0, session_output_tokens=0, session_cache_read_tokens=0,
        session_cache_write_tokens=0, session_reasoning_tokens=0,
        session_estimated_cost_usd=0.0, session_cost_status="unknown",
        session_cost_source="none", _api_latency_history=None, _api_output_history=None,
        client=None, _last_turn_usage=None, _last_prompt_size_tokens=0,
        _ensure_db_session=lambda: None, _vprint=lambda *a, **k: None,
        _safe_print=lambda *a, **k: None, log_prefix="",
        _current_streamed_assistant_text="",
    )
    yield agent, db, tmp_path / "state.db"
    db.close()


def _anthropic_response(**overrides):
    usage = SimpleNamespace(
        input_tokens=1200, output_tokens=340, cache_read_input_tokens=48_000,
        cache_creation_input_tokens=2100, server_tool_use=None,
    )
    base = dict(usage=usage, id="msg_01ProbeRealRequestId", stop_reason="end_turn",
                provider="anthropic-direct", content=[])
    base.update(overrides)
    return SimpleNamespace(**base)


def _api_calls(db_path) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM api_calls ORDER BY call_seq")]
    finally:
        conn.close()


def _record(agent, response, *, api_duration=3.25, api_call_count=1):
    return turn_usage.record_response_usage(
        agent, response, messages=[{"role": "user", "content": "hi"}],
        api_call_count=api_call_count, api_duration=api_duration,
        compression_attempts=0, max_compression_attempts=3,
    )


def test_a_real_turn_writes_one_api_calls_row(agent_with_store) -> None:
    """The regression itself: this table was write-never after the merge."""
    agent, db, db_path = agent_with_store
    _record(agent, _anthropic_response())
    db.close()

    rows = _api_calls(db_path)
    assert len(rows) == 1, "record_response_usage wrote no api_calls row"
    row = rows[0]
    assert row["session_id"] == "s_api_calls_probe"
    assert row["call_seq"] == 1
    assert row["call_type"] == "main"
    assert row["model"] == "claude-opus-5"
    assert row["provider"] == "anthropic"


def test_the_row_carries_the_per_call_cache_split(agent_with_store) -> None:
    """The split is the whole point: cumulative session totals cannot distinguish a
    cold prefill from a warm one, only cache_read vs cache_write per call can."""
    agent, db, db_path = agent_with_store
    _record(agent, _anthropic_response())
    db.close()

    row = _api_calls(db_path)[0]
    assert row["input_tokens"] == 1200
    assert row["cache_read_tokens"] == 48_000
    assert row["cache_write_tokens"] == 2100
    assert row["output_tokens"] == 340
    # Non-cached + read + write is the real prompt size.
    assert row["prompt_tokens_total"] == 1200 + 48_000 + 2100


def test_latency_matches_the_duration_the_loop_measured(agent_with_store) -> None:
    """started_at is derived from api_duration, so latency_seconds must be exact."""
    agent, db, db_path = agent_with_store
    _record(agent, _anthropic_response(), api_duration=7.5)
    db.close()

    row = _api_calls(db_path)[0]
    assert row["latency_seconds"] == pytest.approx(7.5, abs=1e-6)
    assert row["ended_at"] > row["started_at"]
    assert row["ended_at"] - row["started_at"] == pytest.approx(7.5, abs=1e-6)


def test_request_id_is_captured_from_the_response(agent_with_store) -> None:
    """The fork's ``_hermes_request_id`` stash is gone upstream; ``response.id``
    (what upstream's own log line reports as ``id=``) is the replacement."""
    agent, db, db_path = agent_with_store
    _record(agent, _anthropic_response())
    db.close()

    row = _api_calls(db_path)[0]
    assert row["request_id"] == "msg_01ProbeRealRequestId"
    assert row["stop_reason"] == "end_turn"
    assert json.loads(row["extra"])["routing"] == {"upstream": "anthropic-direct"}


def test_request_id_falls_back_to_the_sdk_attribute_then_headers() -> None:
    """Non-streaming SDK responses carry the header on ``_request_id``; some
    transports hand the raw headers back instead."""
    assert turn_usage._response_request_id(
        SimpleNamespace(_request_id="req_sdk_123")
    ) == "req_sdk_123"
    assert turn_usage._response_request_id(
        SimpleNamespace(headers={"request-id": "req_hdr_456"})
    ) == "req_hdr_456"
    assert turn_usage._response_request_id(
        SimpleNamespace(headers={"x-request-id": "req_hdr_789"})
    ) == "req_hdr_789"
    # A response with nothing usable degrades the row; it must not raise.
    assert turn_usage._response_request_id(SimpleNamespace()) is None


def test_each_call_gets_its_own_row_and_seq(agent_with_store) -> None:
    """call_seq comes from agent.session_api_calls, which upstream bumps per call."""
    agent, db, db_path = agent_with_store
    _record(agent, _anthropic_response(), api_call_count=1)
    _record(agent, _anthropic_response(id="msg_02"), api_call_count=2)
    db.close()

    rows = _api_calls(db_path)
    assert [row["call_seq"] for row in rows] == [1, 2]
    assert [row["request_id"] for row in rows] == ["msg_01ProbeRealRequestId", "msg_02"]


def test_a_usage_less_response_writes_no_row(agent_with_store) -> None:
    """Providers that omit usage still count as an attempt (session_api_calls
    bumps) but there is no per-call telemetry to record -- a row of zeroes would
    corrupt any cache-hit-ratio query over this table."""
    agent, db, db_path = agent_with_store
    _record(agent, SimpleNamespace(usage=None, id="msg_nousage"))
    assert agent.session_api_calls == 1, "the attempt must still be observable"
    db.close()

    assert _api_calls(db_path) == []


def test_telemetry_failure_never_breaks_the_turn(agent_with_store) -> None:
    """Per-call telemetry is best-effort: a broken sink must not propagate."""
    agent, db, db_path = agent_with_store

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated telemetry sink failure")

    agent._session_db = SimpleNamespace(
        record_api_call=_boom, queue_token_counts=lambda *a, **k: None,
    )
    outcome = _record(agent, _anthropic_response())
    assert outcome is not None  # the turn completed regardless
