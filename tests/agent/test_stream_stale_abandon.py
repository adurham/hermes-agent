"""A streaming worker that stale kills cannot unwind must not stall the turn forever.

Incident: the monitor killed the same Anthropic stream attempt every stale window for
hours. The worker never unwound (parked outside the socket read, so the socket
shutdown could not wake it), ``_count_stale_attempt`` counts each attempt once so the
stale streak stayed at 1, ``_check_stale_giveup`` only runs before a call, and only
/stop ended the turn. Now the call abandons the worker after
``HERMES_STREAM_STALE_ABANDON_KILLS`` kills of the same attempt and raises a retryable
``TimeoutError``, the way the /stop path already leaves a worker behind.
"""

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from agent import chat_completion_helpers as helpers

_ABANDONED = "request abandoned"


def _anthropic_agent():
    from run_agent import AIAgent

    agent = AIAgent(api_key="test-key", base_url="https://example.com/v1", model="claude-opus-4-7",
                    quiet_mode=True, skip_context_files=True, skip_memory=True)
    agent.api_mode = "anthropic_messages"
    agent._anthropic_client = MagicMock()
    agent._anthropic_api_key = "test-anthropic-key"
    agent._create_request_anthropic_client = lambda *a, **k: agent._anthropic_client
    agent._consecutive_stale_streams = 0
    return agent


def _stream_of(events_factory):
    def _open(*_args, **_kwargs):
        cm, raw = MagicMock(), MagicMock()
        raw.response = None
        raw.__iter__ = MagicMock(return_value=events_factory())
        cm.__enter__ = MagicMock(return_value=raw)
        cm.__exit__ = MagicMock(return_value=False)
        return cm
    return _open


def _call_in_thread(agent):
    outcome: dict = {}

    def _run():
        try:
            outcome["value"] = agent._interruptible_streaming_api_call({})  # no model: no opus stale floor
        except BaseException as exc:  # recorded for the assertions
            outcome["error"] = exc

    caller = threading.Thread(target=_run, name="stale-abandon-caller", daemon=True)
    caller.start()
    return caller, outcome


def _log_count(caplog, needle):
    return sum(needle in r.getMessage() for r in caplog.records)


@pytest.fixture()
def wedged(monkeypatch):
    """An Anthropic stream whose worker blocks on an Event that neither the client abort
    nor the socket shutdown can set: a worker parked outside the socket read."""
    monkeypatch.setenv("HERMES_STREAM_STALE_TIMEOUT", "0.3")
    monkeypatch.setenv("HERMES_STREAM_STALE_GIVEUP", "5")
    agent = _anthropic_agent()
    agent._abort_request_anthropic_client = lambda client, reason=None: None
    release = threading.Event()

    def _events():
        release.wait(timeout=30.0)
        yield SimpleNamespace(type="message_stop")

    agent._anthropic_client.messages.stream.side_effect = _stream_of(_events)
    yield agent
    release.set()  # let the abandoned daemon worker finish


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_wedged_worker_is_abandoned_with_a_retryable_timeout(wedged, caplog):
    with caplog.at_level("WARNING"):
        caller, outcome = _call_in_thread(wedged)
        caller.join(timeout=10.0)

    assert not caller.is_alive(), "the call never ended: the same wedged attempt was re-killed forever"
    assert isinstance(outcome.get("error"), TimeoutError), outcome
    assert _ABANDONED in str(outcome["error"])
    assert _log_count(caplog, "Stream stale for") == 3  # HERMES_STREAM_STALE_ABANDON_KILLS default
    assert _log_count(caplog, "abandoning the request") == 1
    assert wedged._consecutive_stale_streams == 1, "one wedged attempt must still count once"


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_late_write_from_the_abandoned_worker_does_not_replace_the_error(wedged, monkeypatch):
    real_abandon = helpers._StreamingCall._abandon_wedged_worker
    late = SimpleNamespace(late=True)

    def _abandon_then_late_write(self, *args):
        real_abandon(self, *args)
        self.result["response"] = late  # the daemon worker wakes up and writes after the verdict

    monkeypatch.setattr(helpers._StreamingCall, "_abandon_wedged_worker", _abandon_then_late_write)
    caller, outcome = _call_in_thread(wedged)
    caller.join(timeout=10.0)

    assert not caller.is_alive()
    assert "value" not in outcome, "a late write from the abandoned worker became the call's result"
    assert isinstance(outcome.get("error"), TimeoutError), outcome


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_worker_that_unwinds_after_a_kill_still_retries(monkeypatch, caplog):
    """The common path: the kill unblocks the read, the worker retries with a fresh
    attempt, and no attempt is killed twice, so nothing is abandoned."""
    monkeypatch.setenv("HERMES_STREAM_STALE_TIMEOUT", "0.3")
    monkeypatch.setenv("HERMES_STREAM_STALE_GIVEUP", "5")
    monkeypatch.setenv("HERMES_STREAM_RETRIES", "2")
    agent = _anthropic_agent()
    unblock = threading.Event()
    agent._abort_request_anthropic_client = lambda client, reason=None: unblock.set()
    opened = []

    def _events():
        opened.append(1)
        unblock.wait(timeout=10.0)
        unblock.clear()
        raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")
        yield  # pragma: no cover - generator marker

    agent._anthropic_client.messages.stream.side_effect = _stream_of(_events)
    with caplog.at_level("WARNING"):
        caller, outcome = _call_in_thread(agent)
        caller.join(timeout=15.0)
    unblock.set()

    assert not caller.is_alive(), "the recoverable call never ended"
    assert _ABANDONED not in str(outcome.get("error")), outcome
    assert len(opened) == 3, f"each kill should lead to a fresh attempt (1 + 2 retries), saw {len(opened)}"
    assert _log_count(caplog, "abandoning the request") == 0


# ── turn level: the abandon error is retried and the turn completes ──────────


def _resp(content):
    msg = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")],
                           model="test/model", usage=None)


def _abandon_error():
    """The exact error a real abandonment produces."""
    call = helpers._StreamingCall.__new__(helpers._StreamingCall)
    call.api_kwargs, call._same_attempt_kills = {"model": "m"}, 3
    call._request_cancelled, call._call_done = {"value": False}, threading.Event()
    call._abandon_wedged_worker(720.0, 1)
    assert call._request_cancelled["value"] and call._call_done.is_set()
    return call._abandon_error


def test_turn_retries_an_abandoned_stream_and_completes(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.discover_plugins", lambda: None)
    session_ends = []
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook",
                        lambda name, **kw: session_ends.append(kw) if name == "on_session_end" else [])
    tool = {"type": "function", "function": {"name": "web_search", "description": "web_search",
                                             "parameters": {"type": "object", "properties": {}}}}
    from run_agent import AIAgent

    with (patch("model_tools.get_tool_definitions", return_value=[tool]),
          patch("model_tools.check_toolset_requirements", return_value={}),
          patch("agent.process_bootstrap.OpenAI")):
        agent = AIAgent(api_key="test-key-1234567890", base_url="https://openrouter.ai/api/v1",
                        quiet_mode=True, skip_context_files=True, skip_memory=True)
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent._persist_session = lambda *a, **k: None
    agent._save_trajectory = lambda *a, **k: None
    abandoned = _abandon_error()
    calls = []

    def _api(api_kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise abandoned
        return _resp("done after retry")

    agent._interruptible_api_call = _api
    with patch("run_agent.time.sleep", return_value=None), patch("time.sleep", return_value=None):
        result = agent.run_conversation("hi")

    assert len(calls) == 2
    assert result.get("completed") is True and result.get("final_response") == "done after retry"
    assert len(session_ends) == 1 and session_ends[0]["completed"] is True


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_abandon_limit_of_one_abandons_on_the_first_kill(wedged, monkeypatch, caplog):
    monkeypatch.setenv("HERMES_STREAM_STALE_ABANDON_KILLS", "1")
    with caplog.at_level("WARNING"):
        caller, outcome = _call_in_thread(wedged)
        caller.join(timeout=10.0)

    assert not caller.is_alive()
    assert isinstance(outcome.get("error"), TimeoutError), outcome
    assert _log_count(caplog, "Stream stale for") == 1, "a limit of one must not wait a second window"
    assert _log_count(caplog, "abandoning the request") == 1


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_abandon_after_invisible_deltas_retries_instead_of_an_empty_stub(monkeypatch):
    """Whitespace-only text reached no consumer before the worker wedged: nothing was
    delivered, so the abandon must surface as a retryable error, not an empty partial stub."""
    monkeypatch.setenv("HERMES_STREAM_STALE_TIMEOUT", "0.3")
    monkeypatch.setenv("HERMES_STREAM_STALE_GIVEUP", "5")
    agent = _anthropic_agent()
    agent._abort_request_anthropic_client = lambda client, reason=None: None
    release = threading.Event()

    def _events():
        yield SimpleNamespace(type="content_block_start", index=0,
                              content_block=SimpleNamespace(type="text", text=""))
        yield SimpleNamespace(type="content_block_delta", index=0,
                              delta=SimpleNamespace(type="text_delta", text="  \n"))
        release.wait(timeout=30.0)
        yield SimpleNamespace(type="message_stop")

    agent._anthropic_client.messages.stream.side_effect = _stream_of(_events)
    try:
        caller, outcome = _call_in_thread(agent)
        caller.join(timeout=10.0)
    finally:
        release.set()

    assert not caller.is_alive()
    assert "value" not in outcome, f"an empty partial stub was returned: {outcome.get('value')!r}"
    assert isinstance(outcome.get("error"), TimeoutError), outcome
