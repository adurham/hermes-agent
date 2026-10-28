"""process_manage write/submit on a background process without stdin names the fix that works.

A local ``terminal(background=true)`` spawn without ``pty=true`` connects stdin to /dev/null by
design; the old error blamed a "non-local backend or stdin closed", so agents retried blind.
"""
import pytest

from tools.process_registry import ProcessRegistry


@pytest.fixture()
def registry():
    reg = ProcessRegistry()
    yield reg
    for sid in list(reg._running):
        reg.kill_process(sid)


def _spawn_reader(registry):
    # Alive and waiting; a bare ``read`` would hit EOF on /dev/null and exit before the write.
    return registry.spawn_local("sleep 30", cwd=None, task_id="t-stdin")


@pytest.mark.platforms("posix")
def test_pipe_mode_write_says_to_relaunch_with_pty(registry):
    session = _spawn_reader(registry)
    result = registry.write_stdin(session.id, "hello\n")
    assert result["status"] == "error"
    assert "pty=true" in result["error"] and "non-local" not in result["error"]


@pytest.mark.platforms("posix")
def test_no_pty_advice_where_a_pty_cannot_be_had(registry, monkeypatch):
    import tools.process_registry_stdin as stdin_mod

    session = _spawn_reader(registry)
    monkeypatch.setattr(stdin_mod, "_pty_available", lambda: False)
    error = stdin_mod.stdin_unavailable_error(session)
    assert "pty=true would not help" in error
    session.output_log = "worker.log"  # an isolated worker's log-backed spawn
    monkeypatch.setattr(stdin_mod, "_pty_available", lambda: True)
    assert "pty=true would not help" in stdin_mod.stdin_unavailable_error(session)


def test_non_local_session_keeps_the_backend_explanation():
    from types import SimpleNamespace

    from tools.process_registry_stdin import stdin_unavailable_error

    error = stdin_unavailable_error(SimpleNamespace(process=None, output_log=""))
    assert "non-local terminal backend" in error and "pty=true" not in error
