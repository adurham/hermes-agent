"""Regression: a nested orchestrator's blocking delegate_task must outlive the
generic per-tool executor deadline — and must never fail SILENTLY.

Field signature (2026-08-23 incident, session 20260822_230340_72a02e, msg
286168 -> 286379, delta exactly 420.0s): an orchestrator subagent (depth 1)
dispatched a nested batch via ``delegate_task(tasks=[...])``. The sync/async
logic was correct — the call entered the synchronous blocking path exactly as
documented. What killed it was ``_DEFAULT_CONCURRENT_TOOL_TIMEOUT_S = 420.0``,
a GENERIC per-tool-call deadline in ``agent/tool_executor.py`` applied to a
tool whose legitimate runtime is the runtime of the work it SUPERVISES.

The deadline could not cancel the delegation: children are daemon threads
spawned inside the tool call, and the executor's abandon path is documented as
leaving the worker "running detached". So the deadline only removed the
JOINER. The orchestrator reported "completed" upward with a placeholder while
its grandchildren kept running with nobody left to consume them. The task
chain stalled silently for ~7 hours.

These tests exercise the REAL path end-to-end — real ``AIAgent``, real
``execute_tool_calls_sequential``, real ``delegate_task``, real registry
``owns_own_deadline`` wiring, temp ``HERMES_HOME`` — because a mock at the
delegate boundary is exactly what would hide this bug (the bug lives in the
seam BETWEEN the executor and the tool, not inside either one).

Coverage:
1. The nested blocking call is not killed by the generic deadline, and its
   real child results survive.                       [the incident itself]
2. The exemption is NARROW: sibling tools and a top-level (async) delegation
   keep the deadline.                                [no blanket disable]
3. If the owner DOES walk away, the failure is OBSERVABLE — a distinct
   ``abandoned`` status and torn-down children, never silent orphaning.
"""

from __future__ import annotations

import json
import threading
import time
import traceback
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import tools.delegate_tool as dt
from agent.tool_executor import execute_tool_calls_sequential

# Generic deadline used for every test in this module. Tight enough to keep
# the suite fast, and the children below deliberately outlive it.
_TEST_DEADLINE_S = 1.0
# Child runtime as a multiple of the deadline. The point of the incident is
# that legitimate child work runs PAST the generic bound.
_CHILD_RUNTIME_S = 2.5


@pytest.fixture(autouse=True)
def _generic_deadline(monkeypatch, tmp_path):
    """Real (tiny) generic tool deadline, temp home, nested spawning enabled.

    ``delegation.max_spawn_depth`` defaults to 1 (flat: depth-1 children may
    not spawn), so nested orchestration — the configuration this whole bug
    class lives in — must be explicitly unlocked. Patching the resolver rather
    than writing a config file keeps the real ``_get_max_spawn_depth`` contract
    in play while staying hermetic.
    """
    monkeypatch.setenv("HERMES_CONCURRENT_TOOL_TIMEOUT_S", str(_TEST_DEADLINE_S))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_home"))
    monkeypatch.setattr(dt, "_get_max_spawn_depth", lambda: 2)
    yield


def _mock_response(content="child work done"):
    msg = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=msg, finish_reason="stop")],
        model="test/model",
        usage=None,
    )


def _make_agent(tmp_path: Path, *, depth: int) -> "object":
    """A real AIAgent standing in for the orchestrator subagent (depth > 0).

    ``_delegate_depth`` is the exact signal both the sync/async decision
    (``_model_background_value`` / ``run_agent._dispatch_delegate_task``) and
    the new ``owns_own_deadline`` predicate read, so setting it here drives
    the real production branch rather than a test-only one.
    """
    from run_agent import AIAgent

    with (
        patch(
            "run_agent.get_tool_definitions",
            return_value=[
                {
                    "type": "function",
                    "function": {
                        "name": "delegate_task",
                        "description": "test",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
        ),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("hermes_cli.config.load_config", return_value={}),
        patch("run_agent.OpenAI"),
        patch("run_agent._hermes_home", tmp_path),
        patch("agent.model_metadata.fetch_model_metadata", return_value={}),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            platform="subagent" if depth else "cli",
        )
    agent._delegate_depth = depth
    agent._flush_messages_to_session_db = MagicMock(return_value=True)
    agent._append_guardrail_observation = MagicMock(
        side_effect=lambda _n, _a, result, **_kw: result
    )
    agent._record_file_mutation_result = MagicMock()
    agent._subdirectory_hints.check_tool_call = MagicMock(return_value="")
    agent._tool_result_content_for_active_model = MagicMock(
        side_effect=lambda _n, result: result
    )
    agent._persist_disabled = True
    agent._session_db = None
    agent._session_json_enabled = False
    return agent


def _make_slow_child(tmp_path: Path, index: int, *, runtime: float, started=None):
    """A real AIAgent child whose single LLM turn takes *runtime* seconds.

    This is the crux of the repro: legitimate child work that outlives the
    generic per-tool deadline. The sleep sits inside the mocked provider call
    so the child's own agent loop, thread capture, and result plumbing are all
    the real code paths.
    """
    from run_agent import AIAgent

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("hermes_cli.config.load_config", return_value={}),
        patch("run_agent.OpenAI"),
        patch("run_agent._hermes_home", tmp_path),
        patch("agent.model_metadata.fetch_model_metadata", return_value={}),
    ):
        child = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            max_iterations=4,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            platform="subagent",
        )

    def _slow_create(*_a, **_kw):
        if started is not None:
            started.set()
        time.sleep(runtime)
        return _mock_response(f"child {index} finished real work")

    child.client = MagicMock()
    child.client.chat.completions.create.side_effect = _slow_create
    child._cached_system_prompt = "You are helpful."
    child._use_prompt_caching = False
    child.compression_enabled = False
    child.save_trajectories = False
    child._fallback_chain = []
    child._delegate_depth = 2
    child._delegate_role = "leaf"
    child._subagent_id = f"subagent-nested-{index}"
    child._delegate_saved_tool_names = []
    child._persist_disabled = True
    child._session_db = None
    child._session_json_enabled = False
    return child


def _tool_call(name="delegate_task", arguments=None, call_id="deleg-1"):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(
            name=name,
            arguments=json.dumps(arguments if arguments is not None else {}),
        ),
    )


def _patch_children(monkeypatch, children):
    """Hand ``delegate_task`` our pre-built children, in task order."""
    handed: list = []

    def _build(**_kw):
        child = children[len(handed) % len(children)]
        handed.append(child)
        return child

    monkeypatch.setattr(dt, "_build_child_agent", _build)
    monkeypatch.setattr(
        dt,
        "_resolve_delegation_credentials",
        lambda *a, **k: {
            "model": "m", "provider": None, "base_url": None, "api_key": None,
            "api_mode": None, "command": None, "args": None,
        },
    )
    return handed


# ── 1. The incident ───────────────────────────────────────────────────


def test_nested_batch_outlives_generic_tool_deadline(tmp_path, monkeypatch):
    """A depth>0 orchestrator's blocking batch must NOT be killed at the
    generic deadline, and must return its children's REAL results.

    Red on the pre-fix code: ``_run_sequential_tool_execution_middleware``
    applied the generic deadline to the blocking delegate_task, so at
    ``_TEST_DEADLINE_S`` the executor abandoned the worker and wrote
    "timed out after 1.0s" into the transcript while the children kept
    running detached — the orphaning the incident describes.
    """
    parent = _make_agent(tmp_path, depth=1)
    kids = [
        _make_slow_child(tmp_path, i, runtime=_CHILD_RUNTIME_S) for i in range(2)
    ]
    _patch_children(monkeypatch, kids)

    args = {"tasks": [{"goal": "nested worker A"}, {"goal": "nested worker B"}]}
    messages: list = []
    started = time.monotonic()

    execute_tool_calls_sequential(
        parent,
        SimpleNamespace(tool_calls=[_tool_call(arguments=args)]),
        messages,
        "task",
    )

    elapsed = time.monotonic() - started
    assert len(messages) == 1
    content = messages[0]["content"]

    # (a) NOT killed by the generic deadline.
    assert "timed out after" not in content, (
        "the generic per-tool deadline killed a blocking nested delegation — "
        f"this is the 2026-08-23 orphaning bug. Result: {content[:400]}"
    )

    # (b) It actually BLOCKED for the children's real runtime. A call that
    #     returned early would be the orphaning failure mode even if it
    #     didn't say "timed out".
    assert elapsed >= _CHILD_RUNTIME_S, (
        f"nested delegation returned after {elapsed:.2f}s but its children "
        f"need {_CHILD_RUNTIME_S}s — it did not join them"
    )

    # (c) The children's REAL work came back — not a placeholder.
    payload = json.loads(content)
    results = payload["results"]
    assert len(results) == 2
    assert [r["status"] for r in results] == ["completed", "completed"]
    summaries = " ".join(r["summary"] or "" for r in results)
    assert "finished real work" in summaries, (
        f"child results were not consumed by the orchestrator: {results}"
    )


# ── 2. The exemption must stay narrow ─────────────────────────────────


def test_generic_deadline_still_applies_to_sibling_tools(tmp_path, monkeypatch):
    """Removing the bound for delegate_task must not remove it for anything
    else — a hung ordinary tool must still time out at the generic deadline."""
    agent = _make_agent(tmp_path, depth=1)
    release = threading.Event()
    started = threading.Event()

    def _dispatch(_name, _args, _task_id, *, tool_call_id, **_kw):
        started.set()
        release.wait(timeout=30)
        return "late result"

    messages: list = []
    try:
        with patch("run_agent.handle_function_call", side_effect=_dispatch):
            execute_tool_calls_sequential(
                agent,
                SimpleNamespace(
                    tool_calls=[_tool_call(name="web_extract", call_id="hung")]
                ),
                messages,
                "task",
            )
    finally:
        release.set()

    assert started.is_set()
    assert f"timed out after {_TEST_DEADLINE_S}s" in messages[0]["content"], (
        "the owns_own_deadline exemption leaked onto an ordinary tool — the "
        "generic bound must still protect every non-supervising call"
    )


@pytest.mark.parametrize(
    "depth,args,expect_exempt",
    [
        # The incident's shape: nested batch → blocks → owns its bound.
        (1, {"tasks": [{"goal": "a"}, {"goal": "b"}]}, True),
        # Nested single task → also blocks → also owns its bound.
        (1, {"goal": "a"}, True),
        # Top-level → forced background=True, returns a handle in ms →
        # must stay bounded.
        (0, {"tasks": [{"goal": "a"}]}, False),
        (0, {"goal": "a"}, False),
        # Cheap in-turn control calls return immediately → stay bounded.
        (1, {"action": "list"}, False),
        (1, {"action": "stop", "subagent_id": "s-1"}, False),
        (1, {"cancel": "deleg-123"}, False),
    ],
)
def test_owns_own_deadline_predicate_is_narrow(depth, args, expect_exempt):
    """Only a call that actually BLOCKS on supervised work is exempt.

    Asserts the registry-level contract directly (behavior, not internals):
    the same query the executor makes, over the real registered entry.
    """
    from tools.registry import registry

    agent = SimpleNamespace(_delegate_depth=depth)
    assert (
        registry.tool_owns_own_deadline("delegate_task", args, agent) is expect_exempt
    )


def test_unregistered_and_raising_predicates_fail_closed():
    """Removing a bound must always be an explicit, WORKING opt-in."""
    from tools.registry import registry
    from agent.tool_executor import _resolve_call_tool_timeout

    agent = SimpleNamespace(_delegate_depth=1)
    # A tool with no predicate keeps the deadline.
    assert registry.tool_owns_own_deadline("read_file", {"path": "x"}, agent) is False
    assert _resolve_call_tool_timeout(agent, "read_file", {}, 420.0) == 420.0
    # An unknown tool keeps the deadline.
    assert registry.tool_owns_own_deadline("no_such_tool", {}, agent) is False

    # A predicate that RAISES keeps the deadline (fails closed).
    entry = registry.get_entry("delegate_task")
    original = entry.owns_own_deadline
    try:
        def _boom(_args, _agent):
            raise RuntimeError("predicate blew up")

        entry.owns_own_deadline = _boom
        assert (
            registry.tool_owns_own_deadline(
                "delegate_task", {"tasks": [{"goal": "a"}]}, agent
            )
            is False
        )
        assert (
            _resolve_call_tool_timeout(
                agent, "delegate_task", {"tasks": [{"goal": "a"}]}, 420.0
            )
            == 420.0
        )
    finally:
        entry.owns_own_deadline = original


# ── 3. Abandonment must be OBSERVABLE, never silent orphaning ─────────


def test_abandoned_delegation_is_observable_and_tears_children_down(
    tmp_path, monkeypatch
):
    """If the owner DOES walk away, the failure mode must be visible.

    The incident's real damage was silence: the aggregation kept joining, its
    result went into a Future nobody read, and the orchestrator reported
    "completed". Here the owner's thread interrupt bit is set mid-flight (what
    the executor's abandon path does) and we assert the delegation notices,
    reports a DISTINCT ``abandoned`` status — not ``timeout``, which would
    blame the subagent for being slow — and hard-interrupts its children
    instead of letting them run headless.
    """
    parent = _make_agent(tmp_path, depth=1)
    child_started = threading.Event()
    kids = [
        _make_slow_child(tmp_path, 0, runtime=30.0, started=child_started),
        _make_slow_child(tmp_path, 1, runtime=30.0),
    ]
    _patch_children(monkeypatch, kids)

    torn_down: list = []
    _real_teardown = dt._teardown_abandoned_children

    def _record_teardown(children, reason):
        torn_down.append(reason)
        return _real_teardown(children, reason)

    monkeypatch.setattr(dt, "_teardown_abandoned_children", _record_teardown)

    out: dict = {}

    def _run_delegation():
        # The aggregation reads the interrupt bit of the thread it runs on,
        # which is this one — exactly as it runs on the executor's worker.
        try:
            out["result"] = dt.delegate_task(
                tasks=[
                    {"goal": "worker A: run the long nested job to completion"},
                    {"goal": "worker B: run the long nested job to completion"},
                ],
                background=False,
                parent_agent=parent,
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced as a failure below
            out["exc"] = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )

    worker = threading.Thread(target=_run_delegation, daemon=True)
    worker.start()
    if not child_started.wait(timeout=20):
        worker.join(timeout=5)
        pytest.fail(
            "children never started"
            + (f"\ndelegate_task raised:\n{out['exc']}" if out.get("exc") else "")
            + (f"\ndelegate_task returned early: {out.get('result')}" if out.get("result") else "")
        )

    # Simulate the executor giving up on THIS call: it sets the worker
    # thread's interrupt bit and walks away (shutdown(wait=False)).
    from tools.interrupt import set_interrupt

    set_interrupt(True, worker.ident)
    try:
        worker.join(timeout=45)
        assert not worker.is_alive(), (
            "abandoned delegation never returned — it kept joining children "
            "against a dead consumer (the orphaning failure mode)"
        )

        payload = json.loads(out["result"])
        results = payload["results"]

        # (a) An observable signal exists — the delegation did not quietly
        #     report success for work nobody consumed.
        assert not all(r.get("status") == "completed" for r in results), (
            f"abandoned delegation silently reported success: {results}"
        )

        # (b) Children were deterministically torn down, not orphaned.
        assert torn_down, (
            "abandonment did not tear its children down — they would keep "
            "burning tokens against a dead consumer"
        )
    finally:
        set_interrupt(False, worker.ident)
        for kid in kids:
            try:
                kid._interrupt_requested = True
            except Exception:
                pass


def test_abandoned_status_is_distinct_from_timeout():
    """``abandoned`` must never be reported as ``timeout``.

    A timeout says "the subagent was too slow"; abandonment says "its
    consumer went away while it was still within budget". Conflating them
    sends a false story to the transcript and to the user.
    """
    assert issubclass(dt._DelegationAbandoned, Exception)
    # The distinction the classifier in _run_single_child relies on: an
    # abandonment is NOT a FuturesTimeoutError/TimeoutError subclass, so the
    # `is_timeout` branch cannot swallow it.
    from concurrent.futures import TimeoutError as FuturesTimeoutError

    assert not issubclass(dt._DelegationAbandoned, (FuturesTimeoutError, TimeoutError))


def test_owner_abandoned_reads_both_signals():
    """Both abandonment signals must be polled, and neither for a detached
    background batch (which has a durable consumer of its own)."""
    from tools.interrupt import set_interrupt

    quiet = SimpleNamespace(_interrupt_requested=False)
    interrupted_parent = SimpleNamespace(_interrupt_requested=True)

    # Signal 1: the parent turn was interrupted.
    assert dt._owner_abandoned(interrupted_parent, True) is True
    assert dt._owner_abandoned(quiet, True) is False

    # Signal 2: THIS worker thread's interrupt bit (what the executor sets
    # when it abandons the call) — the signal the incident missed.
    tid = threading.current_thread().ident
    set_interrupt(True, tid)
    try:
        assert dt._owner_abandoned(quiet, True) is True
        # A detached background batch is owned by the async registry and has
        # a durable consumer, so neither signal applies to it.
        assert dt._owner_abandoned(quiet, False) is False
        assert dt._owner_abandoned(interrupted_parent, False) is False
    finally:
        set_interrupt(False, tid)
