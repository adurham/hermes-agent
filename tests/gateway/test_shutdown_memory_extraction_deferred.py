"""Gateway shutdown must never wait on session-end memory-extraction LLM calls.

Seen in production: during a restart, ``_stop_finalize_agents_and_adapters`` finalized each
idle-cached agent through ``_cleanup_agent_resources_off_loop`` ->
``AIAgent.shutdown_memory_provider`` -> ``_fork_memory_session_end`` ->
``memory_extraction.on_session_end``, which makes up to 2 + N network LLM calls (reconcile,
cleanup, per-entry conflict classify). Each blocked in an SSL read for ~25 s, the agents ran one
after another, the 30 s off-loop bound only logged, and the shutdown watchdog force-exited the
process at 110 s (CRITICAL). The fix defers those passes on the shutdown path only; the
per-session buffer stays on disk for the next session end.

These tests drive the real chain (gateway cleanup -> real ``AIAgent.shutdown_memory_provider``
-> real extractor) with only the network call stubbed.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import sys
import threading
import time
import types

import pytest

from tools.memory_extraction import buffer as mex_buffer
from tools.memory_extraction import extractor as mex_extractor


@pytest.fixture(autouse=True)
def _mock_dotenv(monkeypatch):
    fake = types.ModuleType("dotenv")
    fake.load_dotenv = lambda *a, **kw: None
    monkeypatch.setitem(sys.modules, "dotenv", fake)


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import hermes_constants
    if hasattr(hermes_constants, "_HERMES_HOME_CACHE"):
        monkeypatch.setattr(hermes_constants, "_HERMES_HOME_CACHE", None)
    monkeypatch.setattr(mex_extractor, "is_enabled", lambda: True)
    monkeypatch.setattr(mex_extractor, "_get_extraction_config", lambda: {
        "model": None, "provider": None, "timeout": 30,
        "max_tokens_per_turn": 1024, "max_tokens_session_end": 2048,
        "include_pre_compress": True, "auto_commit_session_end": False,
    })
    # No warm store: keeps the cleanup pass from needing a real DB.
    import tools.memory_warm as _mw
    monkeypatch.setattr(_mw, "get_warm_store", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no warm")))
    return tmp_path


def _bare_agent(session_id: str, messages: list):
    """A real AIAgent instance (real shutdown_memory_provider / _fork_memory_session_end) with only
    the attributes that path reads; close() is stubbed because a bare instance has no clients."""
    from run_agent import AIAgent
    agent = object.__new__(AIAgent)
    agent.session_id = session_id
    agent._memory_manager = None
    agent.context_compressor = None
    agent._session_messages = messages
    agent.close = lambda: None
    return agent


def _runner():
    from gateway.run import GatewayRunner
    return object.__new__(GatewayRunner)


def _shutdown_pools(runner):
    from gateway.run import GatewayRunner
    GatewayRunner._shutdown_executor(runner, drain_timeout=0)


@pytest.mark.asyncio
async def test_shutdown_cleanup_does_not_wait_on_hung_extraction_llm(hermes_home, monkeypatch, caplog):
    """A wedged extraction LLM call must not hold up shutdown cleanup of an idle-cached agent, and
    exactly one log line must say the extraction was deferred."""
    sid = "sid-shutdown-hang"
    mex_buffer.append(sid, [{"content": "buffered fact from an earlier turn"}], source="per_turn")

    release = threading.Event()
    llm_calls: list[str] = []

    def hung_llm(*, system, user, max_tokens, timeout=None):
        llm_calls.append(system[:40])
        release.wait(20)  # stands in for the unbounded SSL read in the watchdog dump
        return json.dumps({"entries": []})

    monkeypatch.setattr(mex_extractor, "_call_extraction_llm", hung_llm)
    runner = _runner()
    agent = _bare_agent(sid, [{"role": "user", "content": "hello"}])
    try:
        with caplog.at_level(logging.INFO):
            t0 = time.monotonic()
            await asyncio.wait_for(
                runner._cleanup_agent_resources_off_loop(agent, context="shutdown idle-cache", session_key="k"),
                timeout=5,
            )
            elapsed = time.monotonic() - t0
    finally:
        release.set()
        _shutdown_pools(runner)

    assert elapsed < 2.0, f"shutdown cleanup waited {elapsed:.1f}s on memory extraction"
    assert llm_calls == [], "no extraction LLM call may be issued during shutdown"
    deferred = [r for r in caplog.records if "memory extraction session_end deferred" in r.getMessage()]
    assert len(deferred) == 1, [r.getMessage() for r in caplog.records]
    assert sid in deferred[0].getMessage()
    # Nothing lost: the buffered proposal is still there for the next session end.
    assert [e["content"] for e in mex_buffer.get_session_entries(sid)] == ["buffered fact from an earlier turn"]


@pytest.mark.asyncio
async def test_shutdown_finalize_context_also_defers(hermes_home, monkeypatch):
    """Active agents finalized at shutdown (context="shutdown finalize") take the same path."""
    calls: list[str] = []
    monkeypatch.setattr(mex_extractor, "_call_extraction_llm",
                        lambda **kw: calls.append("llm") or json.dumps({"entries": []}))
    runner = _runner()
    try:
        await runner._cleanup_agent_resources_off_loop(
            _bare_agent("sid-fin", [{"role": "user", "content": "x"}]), context="shutdown finalize")
    finally:
        _shutdown_pools(runner)
    assert calls == []


@pytest.mark.asyncio
async def test_non_shutdown_cleanup_still_runs_extraction(hermes_home, monkeypatch):
    """A normal (non-shutdown) session end through the same cleanup helper still extracts."""
    sid = "sid-normal"
    calls: list[str] = []

    def fake_llm(*, system, user, max_tokens, timeout=None):
        calls.append(system)
        return json.dumps({"entries": [{"content": "reconciled fact at a normal session end"}]})

    monkeypatch.setattr(mex_extractor, "_call_extraction_llm", fake_llm)
    runner = _runner()
    agent = _bare_agent(sid, [{"role": "user", "content": "remember this"}])
    try:
        await runner._cleanup_agent_resources_off_loop(agent, context="session hygiene")
    finally:
        _shutdown_pools(runner)

    assert calls and calls[0] == mex_extractor._prompts.SESSION_END_SYSTEM
    assert not getattr(agent, "_defer_memory_extraction", False)
    # auto_commit off -> the reconciled proposal is stashed for the next interactive session.
    assert [e["content"] for e in mex_buffer.get_session_entries(sid)] == [
        "reconciled fact at a normal session end"]


def test_direct_shutdown_memory_provider_runs_extraction_by_default(hermes_home, monkeypatch):
    """CLI exit / close() call shutdown_memory_provider directly: no flag, so extraction runs and
    on_session_end is called with exactly today's arguments (no defer_llm)."""
    seen: list[dict] = []
    real = mex_extractor.on_session_end

    def spy(session_id, messages, **kw):
        seen.append(kw)
        return real(session_id, messages, **kw)

    import tools.memory_extraction as mex_pkg
    monkeypatch.setattr(mex_pkg, "on_session_end", spy)
    calls: list[str] = []
    monkeypatch.setattr(mex_extractor, "_call_extraction_llm",
                        lambda **kw: calls.append("llm") or json.dumps({"entries": []}))

    _bare_agent("sid-cli", [{"role": "user", "content": "x"}]).shutdown_memory_provider(
        [{"role": "user", "content": "x"}])

    assert seen == [{"interactive": False}]
    assert calls, "a normal session end must still call the extraction LLM"


def test_defer_knob_defaults_preserve_behaviour():
    assert inspect.signature(mex_extractor.on_session_end).parameters["defer_llm"].default is False
    from run_agent import AIAgent
    assert getattr(object.__new__(AIAgent), "_defer_memory_extraction", False) is False


def test_deferred_buffer_is_reconciled_by_the_next_session_end(hermes_home, monkeypatch):
    """Deferral loses nothing: the next (normal) session end for the same id sees the buffer."""
    sid = "sid-resume"
    mex_buffer.append(sid, [{"content": "proposal captured before the restart"}], source="per_turn")
    monkeypatch.setattr(mex_extractor, "_call_extraction_llm",
                        lambda **kw: pytest.fail("LLM called while deferred"))
    summary = mex_extractor.on_session_end(sid, [{"role": "user", "content": "x"}], defer_llm=True)
    assert summary["deferred"] is True and summary["buffered"] == 1 and summary["committed"] == 0

    prompts_seen: list[str] = []

    def fake_llm(*, system, user, max_tokens, timeout=None):
        prompts_seen.append(user)
        return json.dumps({"entries": []})

    monkeypatch.setattr(mex_extractor, "_call_extraction_llm", fake_llm)
    mex_extractor.on_session_end(sid, [{"role": "user", "content": "x"}])
    assert prompts_seen and "proposal captured before the restart" in prompts_seen[0]
