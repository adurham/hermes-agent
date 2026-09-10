"""E2E: streamed Anthropic aux calls are bounded by progress, not by the executor.

Regression for the 420.0s consult failures (2026-09-06..09-10). Drives the REAL
public paths -- create_anthropic_message() with a fake SDK client, and the
registry's owns_own_deadline hook -- not internal helpers in isolation.
"""

import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class _FakeStream:
    """Minimal stand-in for anthropic's MessageStreamManager."""

    def __init__(self, events, final="done"):
        self._events = events
        self._final = final
        self.response = None
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def __iter__(self):
        return iter(self._events)

    def get_final_message(self):
        return self._final


def _ping(delay=0.0):
    return ("ping", delay)


def _text(delay=0.0):
    return ("text", delay)


class _FakeMessages:
    def __init__(self, script, final="done"):
        self._script = script
        self._final = final
        self.stream_calls = 0
        self.create_calls = 0

    def stream(self, **kwargs):
        self.stream_calls += 1
        return _FakeStream(list(self._script), final=self._final)

    def create(self, **kwargs):
        self.create_calls += 1
        return "created"


class _FakeClient:
    def __init__(self, script, final="done"):
        self.messages = _FakeMessages(script, final=final)
        self.beta = None


def _events(kinds, clock):
    """Turn a script of (kind, delay) into event objects that advance a fake clock."""
    out = []
    for kind, delay in kinds:
        ev = types.SimpleNamespace(type="ping" if kind == "ping" else "content_block_delta")
        if kind != "ping":
            ev.delta = types.SimpleNamespace(text="hello")
        out.append((ev, delay))
    return out


class _ScriptedStreamClient(_FakeClient):
    """Advances a monotonic fake clock as each event is consumed."""

    def __init__(self, script, clock):
        self._clock = clock
        events = []
        for ev, delay in _events(script, clock):
            events.append((ev, delay))
        self._pairs = events
        super().__init__([p[0] for p in events])
        outer = self

        class _M(_FakeMessages):
            def stream(self, **kwargs):
                self.stream_calls += 1

                def gen():
                    for ev, delay in outer._pairs:
                        outer._clock.advance(delay)
                        yield ev

                s = _FakeStream(gen())
                return s

        self.messages = _M([])


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def advance(self, dt):
        self.t += dt

    def __call__(self):
        return self.t


def _run(script, clock, monkeypatch, **kwargs):
    """Drive create_anthropic_message the way the aux client really does.

    Notably: the aux client always supplies ``is_progress_event``. Without a
    discriminator the adapter cannot tell a keepalive from a token and must
    treat every event as progress, so passing the real one is part of the
    contract under test.
    """
    from agent import anthropic_adapter
    from agent.auxiliary_client import _anthropic_event_has_content

    monkeypatch.setattr(anthropic_adapter.time, "monotonic", clock)
    kwargs.setdefault("is_progress_event", _anthropic_event_has_content)
    client = _ScriptedStreamClient(script, clock)
    return anthropic_adapter.create_anthropic_message(
        client, {"model": "claude-fable-5-1", "messages": []}, **kwargs
    )


def test_long_thinking_silence_before_first_content_is_not_a_stall(monkeypatch):
    """REGRESSION (found live 2026-09-10): pre-content silence must NOT be killed.

    ``thinking.display`` defaults to "omitted", so a model reasoning at max
    effort emits nothing but keepalives for minutes before any content. An
    earlier version of this fix armed the 60s content window from stream open
    and killed a healthy claude-fable-5-1 consult at 253.6s mid-thought.
    """
    clock = _Clock()
    # ~5 minutes of pure thinking keepalives, THEN real content, then done.
    script = [_ping(20.0) for _ in range(15)] + [_text(5.0) for _ in range(4)]
    result = _run(script, clock, monkeypatch, no_progress_timeout=60.0, total_ceiling=2400.0)
    assert result == "done"
    assert clock.t - 1000.0 > 300.0, "test must actually cross a long silent phase"


def test_content_that_starts_then_stalls_is_caught(monkeypatch):
    """Once content flows, a stall IS real and must trip the window."""
    clock = _Clock()
    script = [_text(1.0), _text(1.0)] + [_ping(20.0) for _ in range(40)]
    with pytest.raises(TimeoutError) as exc:
        _run(script, clock, monkeypatch, no_progress_timeout=60.0, total_ceiling=2400.0)
    assert "content stopped" in str(exc.value)
    # Caught on the stall window, not the far-away ceiling.
    assert clock.t - 1000.0 < 200.0


def test_keepalive_only_stream_dies_on_the_total_ceiling(monkeypatch):
    """A ping-only zombie must still terminate — via the ceiling.

    This is the case the httpx read timeout can never catch: every ping is a
    successful read that re-arms it. It cannot be caught by the content window
    (that would be indistinguishable from legitimate thinking silence), so the
    ceiling is what bounds it.
    """
    clock = _Clock()
    script = [_ping(20.0) for _ in range(120)]
    with pytest.raises(TimeoutError) as exc:
        _run(script, clock, monkeypatch, no_progress_timeout=60.0, total_ceiling=600.0)
    assert "total ceiling" in str(exc.value)


def test_slow_but_generating_stream_is_allowed_to_finish(monkeypatch):
    """A live reasoning stream past 420s must COMPLETE, not be amputated.

    Directly encodes the observed incident: fable at max effort needs ~5-7min
    and one such call did finish in 364.8s.
    """
    clock = _Clock()
    # 10 minutes of real content, one substantive delta every 30s.
    script = [_text(30.0) for _ in range(20)]
    result = _run(script, clock, monkeypatch, no_progress_timeout=60.0, total_ceiling=2400.0)
    assert result == "done"
    assert clock.t - 1000.0 > 420.0, "test must actually cross the old 420s bound"


def test_pathological_drip_still_hits_the_total_ceiling(monkeypatch):
    """Content slower than the window but never idle enough must still terminate."""
    clock = _Clock()
    script = [_text(50.0) for _ in range(100)]
    with pytest.raises(TimeoutError) as exc:
        _run(script, clock, monkeypatch, no_progress_timeout=60.0, total_ceiling=600.0)
    assert "total ceiling" in str(exc.value)


def test_unbounded_by_default_preserves_main_turn_loop_behavior(monkeypatch):
    """No bounds passed => historical behavior, no synthetic timeout."""
    clock = _Clock()
    script = [_ping(600.0) for _ in range(10)]
    assert _run(script, clock, monkeypatch) == "done"


def test_without_a_discriminator_every_event_counts_as_progress(monkeypatch):
    """Explicit contract: no ``is_progress_event`` => cannot detect keepalives.

    The adapter must not GUESS which events are substantive. A caller that
    wants keepalive-aware behavior has to supply the discriminator (the aux
    client does); one that doesn't gets re-armed by any event and is bounded
    only by ``total_ceiling``.
    """
    from agent import anthropic_adapter

    clock = _Clock()
    monkeypatch.setattr(anthropic_adapter.time, "monotonic", clock)
    client = _ScriptedStreamClient([_ping(20.0) for _ in range(60)], clock)
    with pytest.raises(TimeoutError) as exc:
        anthropic_adapter.create_anthropic_message(
            client,
            {"model": "m", "messages": []},
            no_progress_timeout=60.0,
            total_ceiling=600.0,
        )
    assert "total ceiling" in str(exc.value)


def test_non_iterable_stream_shim_still_works(monkeypatch):
    """Regression: adding deadlines must not break get_final_message-only shims.

    Anthropic-compatible shims and restricted backends can expose a stream
    object with no ``__iter__``. Per-event enforcement is impossible there by
    construction, but the call must still succeed rather than TypeError on a
    path that worked before deadlines existed.
    """
    from agent import anthropic_adapter

    class _NoIter:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get_final_message(self):
            return "final"

    client = types.SimpleNamespace(
        beta=None,
        messages=types.SimpleNamespace(
            stream=lambda **kw: _NoIter(),
            create=lambda **kw: "created",
        ),
    )
    result = anthropic_adapter.create_anthropic_message(
        client,
        {"model": "m", "messages": []},
        total_ceiling=600.0,
        no_progress_timeout=60.0,
    )
    assert result == "final"


def test_progress_events_are_content_only_not_keepalives():
    """The discriminator the aux client passes must reject pings."""
    from agent.auxiliary_client import _anthropic_event_has_content

    ping = types.SimpleNamespace(type="ping")
    delta = types.SimpleNamespace(
        type="content_block_delta", delta=types.SimpleNamespace(text="hi")
    )
    empty = types.SimpleNamespace(
        type="content_block_delta", delta=types.SimpleNamespace(text="")
    )
    assert _anthropic_event_has_content(delta) is True
    assert _anthropic_event_has_content(ping) is False
    assert _anthropic_event_has_content(empty) is False


def test_consult_bypasses_the_generic_executor_deadline():
    """END-TO-END through the registry hook the executor actually calls.

    The executor asks registry.tool_owns_own_deadline("consult", ...); that must
    be True while consult's in-band ceiling exceeds the generic bound, otherwise
    the 420s pre-emption returns.
    """
    import tools.consult_tool  # noqa: F401  (registers the tool)
    from tools.registry import registry
    from agent.auxiliary_client import _aux_stream_total_ceiling, _get_task_timeout
    from agent.tool_executor import _resolve_sequential_tool_timeout

    generic = _resolve_sequential_tool_timeout()
    ceiling = _aux_stream_total_ceiling(_get_task_timeout("consult"))
    assert ceiling > generic, "precondition: the two layers are inverted"
    assert registry.tool_owns_own_deadline("consult", {"question": "x"}, None) is True


def test_sibling_aux_tools_keep_the_generic_deadline():
    """Bypass is scoped to consult, not blanket-applied to every aux-backed tool."""
    from tools.registry import registry

    for name in ("mcp", "vision_analyze", "web_extract"):
        assert registry.tool_owns_own_deadline(name, {}, None) is False


def test_ownership_check_fails_closed(monkeypatch):
    """If the bound can't be resolved, the generic deadline must stay on."""
    import agent.auxiliary_client as aux

    monkeypatch.setattr(
        aux, "_get_task_timeout", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    assert aux.aux_backed_tool_owns_own_deadline("consult") is False
