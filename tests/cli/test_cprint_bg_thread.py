"""Tests for cli._cprint's bg-thread cooperation with prompt_toolkit.

Background: when a prompt_toolkit Application is running, a bg thread that
calls ``_pt_print`` directly can race with the input-area redraw and the
printed line can end up visually buried behind the prompt.  ``_cprint`` now
routes cross-thread prints through ``run_in_terminal`` via
``loop.call_soon_threadsafe`` so the self-improvement background review's
``💾 Self-improvement review: …`` summary actually surfaces to the user.

These tests verify the routing logic without spinning up a real PT app.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

import cli


@pytest.fixture(autouse=True)
def reset_output_history():
    cli._configure_output_history(False, 200)
    # Reset the cross-thread coalescer between tests — it's module-global
    # state (buffer + pending flag) and a test that schedules a drain but
    # never runs it would otherwise leak a stale pending flag into the next.
    with cli._CPRINT_COALESCE_LOCK:
        cli._CPRINT_COALESCE_BUF.clear()
        cli._CPRINT_COALESCE_PENDING = False
    yield
    with cli._CPRINT_COALESCE_LOCK:
        cli._CPRINT_COALESCE_BUF.clear()
        cli._CPRINT_COALESCE_PENDING = False
    cli._configure_output_history(True, 200)


def test_cprint_no_app_direct_print(monkeypatch):
    """No active app → direct _pt_print, no run_in_terminal involvement."""
    calls = []
    monkeypatch.setattr(cli, "_pt_print", lambda x: calls.append(("pt_print", x)))
    monkeypatch.setattr(cli, "_PT_ANSI", lambda t: ("ANSI", t))

    # Patch the prompt_toolkit import the function performs internally.
    fake_pt_app = types.ModuleType("prompt_toolkit.application")
    fake_pt_app.get_app_or_none = lambda: None
    fake_pt_app.run_in_terminal = lambda *a, **kw: calls.append(("run_in_terminal",))
    monkeypatch.setitem(sys.modules, "prompt_toolkit.application", fake_pt_app)

    cli._cprint("hello")

    assert calls == [("pt_print", ("ANSI", "hello"))]


def test_cprint_app_not_running_direct_print(monkeypatch):
    """App exists but not running (e.g. teardown) → direct print."""
    calls = []
    monkeypatch.setattr(cli, "_pt_print", lambda x: calls.append(("pt_print", x)))
    monkeypatch.setattr(cli, "_PT_ANSI", lambda t: t)

    fake_app = SimpleNamespace(_is_running=False, loop=None)
    fake_pt_app = types.ModuleType("prompt_toolkit.application")
    fake_pt_app.get_app_or_none = lambda: fake_app
    fake_pt_app.run_in_terminal = lambda *a, **kw: calls.append(("run_in_terminal",))
    monkeypatch.setitem(sys.modules, "prompt_toolkit.application", fake_pt_app)

    cli._cprint("x")

    assert calls == [("pt_print", "x")]


def test_cprint_bg_thread_schedules_on_app_loop(monkeypatch):
    """App running + different thread → schedules via call_soon_threadsafe."""
    scheduled = []
    direct_prints = []

    monkeypatch.setattr(cli, "_pt_print", lambda x: direct_prints.append(x))
    monkeypatch.setattr(cli, "_PT_ANSI", lambda t: t)

    class FakeLoop:
        def is_running(self):
            return True

        def call_soon_threadsafe(self, cb, *args):
            scheduled.append(cb)

    fake_loop = FakeLoop()

    # Install a fake "current loop" that is NOT the app's loop, so the
    # cross-thread branch is taken.
    fake_current_loop = SimpleNamespace(is_running=lambda: True)
    fake_asyncio = types.ModuleType("asyncio")

    class _Policy:
        def get_event_loop(self):
            return fake_current_loop

    fake_asyncio.get_event_loop_policy = lambda: _Policy()
    monkeypatch.setitem(sys.modules, "asyncio", fake_asyncio)

    fake_app = SimpleNamespace(_is_running=True, loop=fake_loop)
    fake_pt_app = types.ModuleType("prompt_toolkit.application")
    fake_pt_app.get_app_or_none = lambda: fake_app

    run_in_terminal_calls = []

    def _fake_run_in_terminal(func, **kw):
        run_in_terminal_calls.append(func)
        # Simulate run_in_terminal actually calling func (as the real PT
        # impl would once the app loop tick picks it up).
        func()
        return None

    fake_pt_app.run_in_terminal = _fake_run_in_terminal
    monkeypatch.setitem(sys.modules, "prompt_toolkit.application", fake_pt_app)

    cli._cprint("💾 Self-improvement review: Skill updated")

    # call_soon_threadsafe must have been called with a scheduling cb.
    assert len(scheduled) == 1

    # Invoking the scheduled callback should hit run_in_terminal.
    scheduled[0]()
    assert len(run_in_terminal_calls) == 1

    # And run_in_terminal's inner func should have emitted a pt_print.
    assert direct_prints == ["💾 Self-improvement review: Skill updated"]


def test_cprint_bg_thread_coalesces_burst_into_single_drain(monkeypatch):
    """A burst of cross-thread lines before the drain runs → ONE drain
    scheduled, ONE run_in_terminal, lines joined into one payload.

    This is the streaming-stall fix: each line must NOT pay its own
    serialized run_in_terminal (cursor-position round-trip + erase/redraw).
    """
    scheduled = []
    direct_prints = []

    monkeypatch.setattr(cli, "_pt_print", lambda x: direct_prints.append(x))
    monkeypatch.setattr(cli, "_PT_ANSI", lambda t: t)

    class FakeLoop:
        def is_running(self):
            return True

        def call_soon_threadsafe(self, cb, *args):
            scheduled.append(cb)

    fake_loop = FakeLoop()
    fake_current_loop = SimpleNamespace(is_running=lambda: True)
    fake_asyncio = types.ModuleType("asyncio")

    class _Policy:
        def get_event_loop(self):
            return fake_current_loop  # NOT the app loop → cross-thread

    fake_asyncio.get_event_loop_policy = lambda: _Policy()
    monkeypatch.setitem(sys.modules, "asyncio", fake_asyncio)

    fake_app = SimpleNamespace(_is_running=True, loop=fake_loop)
    fake_pt_app = types.ModuleType("prompt_toolkit.application")
    fake_pt_app.get_app_or_none = lambda: fake_app

    run_in_terminal_calls = []

    def _fake_run_in_terminal(func, **kw):
        run_in_terminal_calls.append(func)
        func()
        return None

    fake_pt_app.run_in_terminal = _fake_run_in_terminal
    monkeypatch.setitem(sys.modules, "prompt_toolkit.application", fake_pt_app)

    # Three lines arrive back-to-back BEFORE the loop services the drain.
    cli._cprint("line-1")
    cli._cprint("line-2")
    cli._cprint("line-3")

    # Only the first call arms a drain; the other two ride its batch.
    assert len(scheduled) == 1
    assert run_in_terminal_calls == []  # not drained until the loop ticks

    # Loop services the single scheduled drain.
    scheduled[0]()

    # ONE run_in_terminal, ONE joined payload — not three.
    assert len(run_in_terminal_calls) == 1
    assert direct_prints == ["line-1\nline-2\nline-3"]


def test_cprint_bg_thread_frame_paces_flood(monkeypatch):
    """A flood larger than the per-frame cap drains in bounded batches,
    re-arming via call_later — not one giant paint.  This is the smoothness
    layer: floods scroll out at ~frame rate instead of clumping.
    """
    scheduled = []        # call_soon_threadsafe callbacks
    deferred = []         # (delay, cb) from call_later
    direct_prints = []

    monkeypatch.setattr(cli, "_pt_print", lambda x: direct_prints.append(x))
    monkeypatch.setattr(cli, "_PT_ANSI", lambda t: t)
    # Keep the cap deterministic regardless of future tuning.
    monkeypatch.setattr(cli, "_CPRINT_MAX_LINES_PER_FRAME", 3)

    class FakeLoop:
        def is_running(self):
            return True

        def call_soon_threadsafe(self, cb, *args):
            scheduled.append(cb)

        def call_later(self, delay, cb, *args):
            deferred.append((delay, cb))

    fake_loop = FakeLoop()
    fake_current_loop = SimpleNamespace(is_running=lambda: True)
    fake_asyncio = types.ModuleType("asyncio")

    class _Policy:
        def get_event_loop(self):
            return fake_current_loop

    fake_asyncio.get_event_loop_policy = lambda: _Policy()
    monkeypatch.setitem(sys.modules, "asyncio", fake_asyncio)

    fake_app = SimpleNamespace(_is_running=True, loop=fake_loop)
    fake_pt_app = types.ModuleType("prompt_toolkit.application")
    fake_pt_app.get_app_or_none = lambda: fake_app
    fake_pt_app.run_in_terminal = lambda func, **kw: (func(), None)[1]
    monkeypatch.setitem(sys.modules, "prompt_toolkit.application", fake_pt_app)

    # 7 lines, cap 3 → frame 1 emits 3, then re-arms; frame 2 emits 3,
    # re-arms; frame 3 emits 1, done.
    for i in range(7):
        cli._cprint(f"line-{i}")

    assert len(scheduled) == 1  # only one initial drain armed
    scheduled[0]()              # frame 1
    assert direct_prints == ["line-0\nline-1\nline-2"]
    assert len(deferred) == 1   # backlog → one re-arm

    deferred[0][1]()            # frame 2
    assert direct_prints[-1] == "line-3\nline-4\nline-5"
    assert len(deferred) == 2

    deferred[1][1]()            # frame 3 (remainder)
    assert direct_prints[-1] == "line-6"
    # No further re-arm once the buffer is empty.
    assert len(deferred) == 2
    assert cli._CPRINT_COALESCE_PENDING is False


def test_cprint_bg_thread_frame_pacing_fallback_when_call_later_unavailable(monkeypatch):
    """If call_later raises (loop can't pace), the remaining backlog is
    flushed immediately so nothing is stranded and the flag is cleared."""
    scheduled = []
    direct_prints = []

    monkeypatch.setattr(cli, "_pt_print", lambda x: direct_prints.append(x))
    monkeypatch.setattr(cli, "_PT_ANSI", lambda t: t)
    monkeypatch.setattr(cli, "_CPRINT_MAX_LINES_PER_FRAME", 2)

    class FakeLoop:
        def is_running(self):
            return True

        def call_soon_threadsafe(self, cb, *args):
            scheduled.append(cb)

        def call_later(self, delay, cb, *args):
            raise RuntimeError("no pacing")

    fake_loop = FakeLoop()
    fake_current_loop = SimpleNamespace(is_running=lambda: True)
    fake_asyncio = types.ModuleType("asyncio")

    class _Policy:
        def get_event_loop(self):
            return fake_current_loop

    fake_asyncio.get_event_loop_policy = lambda: _Policy()
    monkeypatch.setitem(sys.modules, "asyncio", fake_asyncio)

    fake_app = SimpleNamespace(_is_running=True, loop=fake_loop)
    fake_pt_app = types.ModuleType("prompt_toolkit.application")
    fake_pt_app.get_app_or_none = lambda: fake_app
    fake_pt_app.run_in_terminal = lambda func, **kw: (func(), None)[1]
    monkeypatch.setitem(sys.modules, "prompt_toolkit.application", fake_pt_app)

    for i in range(5):  # cap 2 → first frame emits 2, backlog 3
        cli._cprint(f"line-{i}")

    scheduled[0]()  # frame 1 emits 2, then call_later raises → flush remainder

    # First paint = capped batch; second paint = the whole remainder.
    assert direct_prints == ["line-0\nline-1", "line-2\nline-3\nline-4"]
    assert cli._CPRINT_COALESCE_PENDING is False


def test_cprint_bg_thread_rearms_after_drain(monkeypatch):
    """After a drain completes, a later line arms a fresh drain (the
    pending flag is cleared so streaming keeps flowing turn after turn)."""
    scheduled = []
    direct_prints = []

    monkeypatch.setattr(cli, "_pt_print", lambda x: direct_prints.append(x))
    monkeypatch.setattr(cli, "_PT_ANSI", lambda t: t)

    class FakeLoop:
        def is_running(self):
            return True

        def call_soon_threadsafe(self, cb, *args):
            scheduled.append(cb)

    fake_loop = FakeLoop()
    fake_current_loop = SimpleNamespace(is_running=lambda: True)
    fake_asyncio = types.ModuleType("asyncio")

    class _Policy:
        def get_event_loop(self):
            return fake_current_loop

    fake_asyncio.get_event_loop_policy = lambda: _Policy()
    monkeypatch.setitem(sys.modules, "asyncio", fake_asyncio)

    fake_app = SimpleNamespace(_is_running=True, loop=fake_loop)
    fake_pt_app = types.ModuleType("prompt_toolkit.application")
    fake_pt_app.get_app_or_none = lambda: fake_app
    fake_pt_app.run_in_terminal = lambda func, **kw: (func(), None)[1]
    monkeypatch.setitem(sys.modules, "prompt_toolkit.application", fake_pt_app)

    cli._cprint("a")
    scheduled[0]()  # drain batch 1
    cli._cprint("b")  # should re-arm a NEW drain
    assert len(scheduled) == 2
    scheduled[1]()  # drain batch 2

    assert direct_prints == ["a", "b"]


def test_cprint_bg_thread_loop_unreachable_flushes_buffer(monkeypatch):
    """call_soon_threadsafe raising → buffered lines are flushed directly
    so nothing is lost, and the pending flag is cleared."""
    direct_prints = []

    monkeypatch.setattr(cli, "_pt_print", lambda x: direct_prints.append(x))
    monkeypatch.setattr(cli, "_PT_ANSI", lambda t: t)

    class FakeLoop:
        def is_running(self):
            return True

        def call_soon_threadsafe(self, cb, *args):
            raise RuntimeError("loop gone")

    fake_loop = FakeLoop()
    fake_current_loop = SimpleNamespace(is_running=lambda: True)
    fake_asyncio = types.ModuleType("asyncio")

    class _Policy:
        def get_event_loop(self):
            return fake_current_loop

    fake_asyncio.get_event_loop_policy = lambda: _Policy()
    monkeypatch.setitem(sys.modules, "asyncio", fake_asyncio)

    fake_app = SimpleNamespace(_is_running=True, loop=fake_loop)
    fake_pt_app = types.ModuleType("prompt_toolkit.application")
    fake_pt_app.get_app_or_none = lambda: fake_app
    fake_pt_app.run_in_terminal = lambda *a, **kw: None
    monkeypatch.setitem(sys.modules, "prompt_toolkit.application", fake_pt_app)

    cli._cprint("dont-lose-me")

    assert direct_prints == ["dont-lose-me"]
    assert cli._CPRINT_COALESCE_PENDING is False


def test_cprint_same_thread_as_app_loop_direct_print(monkeypatch):
    """App running on same thread → direct print (no scheduling)."""
    direct_prints = []
    monkeypatch.setattr(cli, "_pt_print", lambda x: direct_prints.append(x))
    monkeypatch.setattr(cli, "_PT_ANSI", lambda t: t)

    class FakeLoop:
        def is_running(self):
            return True

        def call_soon_threadsafe(self, cb, *args):
            raise AssertionError(
                "call_soon_threadsafe must not be used on the app's own thread"
            )

    fake_loop = FakeLoop()
    fake_asyncio = types.ModuleType("asyncio")

    class _Policy:
        def get_event_loop(self):
            return fake_loop  # same as app loop

    fake_asyncio.get_event_loop_policy = lambda: _Policy()
    monkeypatch.setitem(sys.modules, "asyncio", fake_asyncio)

    fake_app = SimpleNamespace(_is_running=True, loop=fake_loop)
    fake_pt_app = types.ModuleType("prompt_toolkit.application")
    fake_pt_app.get_app_or_none = lambda: fake_app
    fake_pt_app.run_in_terminal = lambda *a, **kw: None
    monkeypatch.setitem(sys.modules, "prompt_toolkit.application", fake_pt_app)

    cli._cprint("x")

    assert direct_prints == ["x"]


def test_cprint_swallows_app_loop_attr_error(monkeypatch):
    """Loop missing on app → fall back to direct print, no crash."""
    direct_prints = []
    monkeypatch.setattr(cli, "_pt_print", lambda x: direct_prints.append(x))
    monkeypatch.setattr(cli, "_PT_ANSI", lambda t: t)

    class WeirdApp:
        _is_running = True

        @property
        def loop(self):
            raise RuntimeError("no loop for you")

    fake_pt_app = types.ModuleType("prompt_toolkit.application")
    fake_pt_app.get_app_or_none = lambda: WeirdApp()
    fake_pt_app.run_in_terminal = lambda *a, **kw: None
    monkeypatch.setitem(sys.modules, "prompt_toolkit.application", fake_pt_app)

    cli._cprint("fallback")

    assert direct_prints == ["fallback"]


def test_cprint_swallows_prompt_toolkit_import_error(monkeypatch):
    """If prompt_toolkit.application itself fails to import, fall back."""
    direct_prints = []
    monkeypatch.setattr(cli, "_pt_print", lambda x: direct_prints.append(x))
    monkeypatch.setattr(cli, "_PT_ANSI", lambda t: t)

    # Drop cached prompt_toolkit.application AND install a meta-path finder
    # that raises ImportError on re-import.
    monkeypatch.delitem(sys.modules, "prompt_toolkit.application", raising=False)

    class _BlockFinder:
        def find_module(self, name, path=None):
            if name == "prompt_toolkit.application":
                return self
            return None

        def load_module(self, name):
            raise ImportError("blocked for test")

        def find_spec(self, name, path=None, target=None):
            if name == "prompt_toolkit.application":
                # Returning a bogus spec that will fail on load works too,
                # but raising here keeps the test simple.
                raise ImportError("blocked for test")
            return None

    blocker = _BlockFinder()
    sys.meta_path.insert(0, blocker)
    try:
        cli._cprint("fallback2")
    finally:
        sys.meta_path.remove(blocker)

    assert direct_prints == ["fallback2"]


def test_output_history_preserves_ansi_and_keeps_recent_lines():
    cli._configure_output_history(True, 10)

    for idx in range(12):
        cli._record_output_history(f"\x1b[31mline-{idx}\x1b[0m")

    assert list(cli._OUTPUT_HISTORY) == [
        f"\x1b[31mline-{idx}\x1b[0m" for idx in range(2, 12)
    ]


def test_replay_output_history_does_not_record_replayed_lines(monkeypatch):
    cli._configure_output_history(True, 10)
    cli._record_output_history("visible output")
    printed = []

    def _fake_print(value):
        printed.append(value)
        cli._record_output_history("duplicated replay")

    monkeypatch.setattr(cli, "_pt_print", _fake_print)
    monkeypatch.setattr(cli, "_PT_ANSI", lambda text: text)

    cli._replay_output_history()

    assert printed == ["visible output"]
    assert list(cli._OUTPUT_HISTORY) == ["visible output"]


def test_replay_output_history_rerenders_callable_entries(monkeypatch):
    cli._configure_output_history(True, 10)
    widths_seen = []
    printed = []

    def _render_current_width():
        widths_seen.append("called")
        return ["top border", "body"]

    cli._record_output_history_entry(_render_current_width)
    monkeypatch.setattr(cli, "_pt_print", lambda value: printed.append(value))
    monkeypatch.setattr(cli, "_PT_ANSI", lambda text: text)

    cli._replay_output_history()

    assert widths_seen == ["called"]
    assert printed == ["top border\nbody"]
    assert list(cli._OUTPUT_HISTORY) == [_render_current_width]


def test_replay_output_history_batches_rendered_lines_into_one_print(monkeypatch):
    cli._configure_output_history(True, 10)
    cli._record_output_history("first line")
    cli._record_output_history("second line")
    cli._record_output_history_entry(lambda: ["third line", "fourth line"])
    printed = []

    monkeypatch.setattr(cli, "_pt_print", lambda value: printed.append(value))
    monkeypatch.setattr(cli, "_PT_ANSI", lambda text: text)

    cli._replay_output_history()

    assert printed == ["first line\nsecond line\nthird line\nfourth line"]


def test_chat_console_records_rich_ansi_for_resize_replay(monkeypatch):
    cli._configure_output_history(True, 10)
    monkeypatch.setattr(cli, "_pt_print", lambda *_args, **_kwargs: None)

    cli.ChatConsole().print("[bold red]Hello[/]")

    assert cli._OUTPUT_HISTORY
    assert any("\x1b[" in line for line in cli._OUTPUT_HISTORY)


def test_suspend_output_history_blocks_recording():
    cli._configure_output_history(True, 10)

    with cli._suspend_output_history():
        cli._record_output_history("hidden")
        cli._record_output_history_entry("also hidden")

    assert list(cli._OUTPUT_HISTORY) == []


def test_clear_output_history_removes_replayable_lines():
    cli._configure_output_history(True, 10)
    cli._record_output_history("before clear")

    cli._clear_output_history()

    assert list(cli._OUTPUT_HISTORY) == []
