"""A background provider call that never returns pins its mem-sync worker thread forever (Python cannot
stop a thread). MemoryManagers are rebuilt per agent, so without a process-wide view every fresh manager
started one more call against the hung backend and pinned one more thread. Calls for a provider are now
skipped once _MAX_STUCK_CALLS earlier calls for it are stuck past the stall threshold."""
import threading

import pytest

import agent.memory_manager as mm
from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider
from tests.waiting import wait_until


class _HangingProvider(MemoryProvider):
    def __init__(self, name: str = "hangs"):
        self._name = name
        self.release = threading.Event()
        self.started = threading.Event()
        self.sync_calls = 0
        self.prefetch_calls = 0
        self.hang_next = True

    @property
    def name(self) -> str:
        return self._name

    def initialize(self, session_id: str = "", **kwargs) -> None:
        pass

    def is_available(self) -> bool:
        return True

    def system_prompt_block(self) -> str:
        return ""

    def prefetch(self, query, *, session_id: str = "") -> str:
        return ""

    def get_tool_schemas(self):
        return []

    def queue_prefetch(self, query, *, session_id: str = "") -> None:
        self.prefetch_calls += 1

    def sync_turn(self, user_content, assistant_content, *, session_id: str = "") -> None:
        self.sync_calls += 1
        if self.hang_next:
            self.hang_next = False
            self.started.set()
            self.release.wait(10)


@pytest.fixture(autouse=True)
def _short_stall(monkeypatch):
    monkeypatch.setattr(mm, "_BACKGROUND_CALL_STALL_S", 0.0)
    MemoryManager._BACKGROUND_CALLS.clear()
    MemoryManager._BACKGROUND_SKIPS.clear()
    yield
    MemoryManager._BACKGROUND_CALLS.clear()
    MemoryManager._BACKGROUND_SKIPS.clear()


def _manager(provider):
    mgr = MemoryManager()
    mgr.add_provider(provider)
    return mgr


def _mem_sync_threads():
    return [t for t in threading.enumerate() if t.name.startswith("mem-sync")]


def _hang_one_call(name="hangs"):
    """A manager whose provider's first sync_turn never returns (until released)."""
    provider = _HangingProvider(name)
    mgr = _manager(provider)
    mgr.sync_all("hello", "hi")
    assert provider.started.wait(5)
    return mgr, provider


def test_sustained_wedge_pins_at_most_max_stuck_threads():
    """A backend that stays wedged: every call hangs. Calls keep being admitted until
    _MAX_STUCK_CALLS are stuck, then later managers are skipped, so pinned threads stay bounded."""
    hung = [_hang_one_call() for _ in range(mm._MAX_STUCK_CALLS)]
    before = len(_mem_sync_threads())

    for _ in range(5):
        fresh = _manager(_HangingProvider())
        fresh.sync_all("again", "reply")
        fresh.queue_prefetch_all("query")
        assert fresh.flush_pending(timeout=5)  # returns: nothing blocked behind the hung calls
        fresh.shutdown_all()

    key = hung[0][0]._registry_key(hung[0][1])
    try:
        assert MemoryManager._BACKGROUND_SKIPS[key] == 10
        assert len(MemoryManager._BACKGROUND_CALLS[key]) == mm._MAX_STUCK_CALLS
        # Idle workers of shut-down executors exit asynchronously.
        wait_until(lambda: len(_mem_sync_threads()) <= before, timeout=5, what="idle mem-sync workers to exit")
    finally:
        for _, provider in hung:
            provider.release.set()
    for mgr, _ in hung:
        assert mgr.flush_pending(timeout=5)
    assert key not in MemoryManager._BACKGROUND_CALLS
    for mgr, _ in hung:
        mgr.shutdown_all()


def test_healthy_replacement_still_writes_behind_an_orphaned_call():
    """One call that never returns must not silence the provider for the process lifetime: a fresh
    manager with a healthy provider instance (the backend recovered) keeps writing."""
    orphan_mgr, orphan = _hang_one_call()
    healthy = _HangingProvider()
    healthy.hang_next = False
    try:
        for _ in range(5):
            fresh = _manager(healthy)
            fresh.sync_all("again", "reply")
            fresh.queue_prefetch_all("query")
            assert fresh.flush_pending(timeout=5)
            fresh.shutdown_all()
        assert (healthy.sync_calls, healthy.prefetch_calls) == (5, 5)
    finally:
        orphan.release.set()
        orphan_mgr.shutdown_all()


def test_new_outage_warns_on_its_first_skip_even_with_an_orphan_alive(caplog):
    """An orphan keeps the key alive after the gate reopens; the next outage must still warn on
    its first skip instead of waiting for the 100th."""
    hung = [_hang_one_call() for _ in range(mm._MAX_STUCK_CALLS)]
    key = hung[0][0]._registry_key(hung[0][1])
    try:
        _manager(_HangingProvider()).sync_all("skipped", "x")
        assert MemoryManager._BACKGROUND_SKIPS[key] == 1
        hung[-1][1].release.set()  # one returns: the gate reopens, three orphans keep the key alive
        assert hung[-1][0].flush_pending(timeout=5)
        reopened, healthy = _hang_one_call()  # admitted, then it wedges too: a new outage
        hung.append((reopened, healthy))
        caplog.clear()
        with caplog.at_level("WARNING", logger="agent.memory_manager"):
            _manager(_HangingProvider()).sync_all("skipped again", "x")
        assert MemoryManager._BACKGROUND_SKIPS[key] == 1
        assert "background calls stuck" in caplog.text
    finally:
        for _, provider in hung:
            provider.release.set()
    for mgr, _ in hung:
        mgr.shutdown_all()


def test_slow_call_below_threshold_is_not_skipped(monkeypatch):
    monkeypatch.setattr(mm, "_BACKGROUND_CALL_STALL_S", 60.0)
    provider = _HangingProvider()
    first = _manager(provider)
    first.sync_all("hello", "hi")
    assert provider.started.wait(5)

    second = _manager(provider)
    try:
        second.sync_all("again", "reply")
        assert second.flush_pending(timeout=5)
        assert provider.sync_calls == 2
    finally:
        provider.release.set()
    for m in (first, second):
        m.shutdown_all()
