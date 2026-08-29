"""Tests for process wait timeout-result clarity (not-an-error semantics)."""

import pytest

from tools.process_registry import ProcessRegistry


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    return ProcessRegistry()


def _spawn_sleeper(registry, notify=False):
    session = registry.spawn_local("sleep 30", cwd="/tmp", task_id="t-waitclar")
    session.notify_on_complete = notify
    return session.id


class TestWaitTimeoutClarity:
    def test_wait_timeout_marks_process_running(self, registry):
        sid = _spawn_sleeper(registry)
        try:
            r = registry.wait(sid, timeout=1)
            assert r["status"] == "timeout"
            assert r["process_running"] is True
            assert "not an error" in r["timeout_note"]
            assert "Uptime" in r["timeout_note"]
        finally:
            registry.kill_process(sid)

    def test_wait_timeout_suggests_notify_when_unset(self, registry):
        sid = _spawn_sleeper(registry, notify=False)
        try:
            r = registry.wait(sid, timeout=1)
            assert "notify_on_complete=true" in r["timeout_note"]
        finally:
            registry.kill_process(sid)

    def test_wait_timeout_defers_to_notify_when_set(self, registry):
        sid = _spawn_sleeper(registry, notify=True)
        try:
            r = registry.wait(sid, timeout=1)
            assert "you will be notified on exit" in r["timeout_note"]
        finally:
            registry.kill_process(sid)

    def test_clamped_wait_keeps_clamp_note_and_running_semantics(self, registry, monkeypatch):
        # The clamp ceiling is TERMINAL_WAIT_MAX_TIMEOUT, a DISTINCT knob
        # from TERMINAL_TIMEOUT (which only controls the no-argument
        # default). Setting only TERMINAL_TIMEOUT must not clamp an
        # explicit long wait -- see
        # test_long_explicit_wait_not_clamped_by_default_timeout below.
        monkeypatch.setenv("TERMINAL_WAIT_MAX_TIMEOUT", "1")
        sid = _spawn_sleeper(registry)
        try:
            r = registry.wait(sid, timeout=600)
            assert r["status"] == "timeout"
            assert "clamped" in r["timeout_note"]
            assert "not an error" in r["timeout_note"]
            assert r["process_running"] is True
        finally:
            registry.kill_process(sid)

    def test_exited_process_unaffected(self, registry):
        session = registry.spawn_local("true", cwd="/tmp", task_id="t-waitclar")
        r = registry.wait(session.id, timeout=10)
        assert r["status"] == "exited"
        assert "process_running" not in r


class TestWaitMaxTimeoutDecoupledFromDefault:
    """terminal.wait_max_timeout (TERMINAL_WAIT_MAX_TIMEOUT) is the ceiling
    an explicit ``wait(timeout=N)`` gets clamped to; terminal.timeout
    (TERMINAL_TIMEOUT) only supplies the no-argument default. Regression
    coverage for the bug where they were the same variable, forcing any
    caller waiting on a long-running background job (a 20min-2hr benchmark
    stage) into a repeated wait(timeout=300)-style poll loop -- each call of
    which resends the full cached conversation prefix in an LLM tool loop.
    """

    def test_long_explicit_wait_not_clamped_by_default_timeout(self, registry, monkeypatch):
        """A short TERMINAL_TIMEOUT must not clamp a long explicit wait when
        TERMINAL_WAIT_MAX_TIMEOUT (default 3600s) permits it."""
        monkeypatch.setenv("TERMINAL_TIMEOUT", "1")
        monkeypatch.delenv("TERMINAL_WAIT_MAX_TIMEOUT", raising=False)
        sid = _spawn_sleeper(registry)
        try:
            r = registry.wait(sid, timeout=5)
            assert r["status"] == "timeout"
            assert "timeout_note" not in r or "clamped" not in r["timeout_note"]
            assert r["process_running"] is True
        finally:
            registry.kill_process(sid)

    def test_wait_max_timeout_ceiling_never_below_default(self, registry, monkeypatch):
        """The ceiling can never be tighter than the plain no-argument
        default -- a caller passing no explicit timeout must never be
        clamped below what they already get for free."""
        monkeypatch.setenv("TERMINAL_TIMEOUT", "5")
        monkeypatch.setenv("TERMINAL_WAIT_MAX_TIMEOUT", "1")
        sid = _spawn_sleeper(registry)
        try:
            r = registry.wait(sid, timeout=None)
            assert r["status"] == "timeout"
            assert "timeout_note" not in r or "clamped" not in r["timeout_note"]
        finally:
            registry.kill_process(sid)

    def test_configurable_ceiling_permits_hour_long_wait_request(self, registry, monkeypatch):
        """A caller may explicitly request up to the configured ceiling
        (default 3600s) without being clamped -- the whole point of the fix."""
        monkeypatch.delenv("TERMINAL_TIMEOUT", raising=False)
        monkeypatch.delenv("TERMINAL_WAIT_MAX_TIMEOUT", raising=False)
        sid = _spawn_sleeper(registry)
        try:
            r = registry.wait(sid, timeout=1)  # short wait, but exercises the same path
            assert r["status"] == "timeout"
            assert "timeout_note" not in r or "clamped" not in r["timeout_note"]
        finally:
            registry.kill_process(sid)


class TestWaitOutputUnchangedOptimization:
    """A second consecutive wait() timeout on a session whose output tail
    hasn't changed reports it cheaply instead of re-sending the same bytes.

    This is the second cost lever alongside the configurable
    wait_max_timeout ceiling: a caller re-polling a still-running,
    still-silent process shouldn't pay to resend identical output on every
    wake in an LLM tool loop.
    """

    def _spawn_quiet_sleeper(self, registry, notify=False):
        # A quiet process (no output at all) makes consecutive wait()
        # timeouts trivially produce the same empty tail.
        session = registry.spawn_local("sleep 30", cwd="/tmp", task_id="t-waitunchanged")
        session.notify_on_complete = notify
        return session.id

    def test_first_timeout_has_no_output_unchanged_marker(self, registry):
        sid = self._spawn_quiet_sleeper(registry)
        try:
            r = registry.wait(sid, timeout=1)
            assert r["status"] == "timeout"
            assert "output_unchanged" not in r
        finally:
            registry.kill_process(sid)

    def test_second_timeout_with_identical_output_is_marked_unchanged(self, registry):
        sid = self._spawn_quiet_sleeper(registry)
        try:
            r1 = registry.wait(sid, timeout=1)
            assert "output_unchanged" not in r1
            r2 = registry.wait(sid, timeout=1)
            assert r2["status"] == "timeout"
            assert r2["output_unchanged"] is True
            assert r2["output"] == ""
            assert "unchanged" in r2["timeout_note"]
        finally:
            registry.kill_process(sid)

    def test_output_change_between_waits_clears_unchanged_marker(self, registry):
        sid = self._spawn_quiet_sleeper(registry)
        try:
            r1 = registry.wait(sid, timeout=1)
            assert "output_unchanged" not in r1
            session = registry.get(sid)
            with session._lock:
                session.output_buffer += "new output line\n"
            r2 = registry.wait(sid, timeout=1)
            assert "output_unchanged" not in r2
            assert "new output line" in r2["output"]
        finally:
            registry.kill_process(sid)

    def test_exited_process_wait_is_unaffected_by_unchanged_tracking(self, registry):
        session = registry.spawn_local("true", cwd="/tmp", task_id="t-waitunchanged")
        r = registry.wait(session.id, timeout=10)
        assert r["status"] == "exited"
        assert "output_unchanged" not in r
