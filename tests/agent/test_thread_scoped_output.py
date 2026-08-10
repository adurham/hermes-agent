"""Tests for agent.thread_scoped_output.thread_scoped_silence.

Behaviour contract: a thread inside ``thread_scoped_silence()`` has its
stdout/stderr routed to devnull, while every OTHER thread keeps writing to the
real stream — even concurrently, while the first thread is still inside the
context.  This is the property the old process-global
``contextlib.redirect_stdout(devnull)`` violated (issue #55769 / #55925).
"""

import io
import sys
import threading
import time

from agent.thread_scoped_output import thread_scoped_silence


def _run_with_real_stream(fn):
    """Bind a StringIO as the real stdout, run fn, return what reached it."""
    real_out = io.StringIO()
    orig = sys.stdout
    sys.stdout = real_out
    try:
        fn()
    finally:
        sys.stdout = orig
    return real_out.getvalue()






def test_stderr_is_also_routed_per_thread():
    real_err = io.StringIO()
    orig = sys.stderr
    sys.stderr = real_err
    try:
        with thread_scoped_silence():
            sys.stderr.write("err-dropped\n")
        sys.stderr.write("err-kept\n")
    finally:
        sys.stderr = orig
    out = real_err.getvalue()
    assert "err-dropped" not in out
    assert "err-kept" in out






def test_overlapping_silence_windows_do_not_close_shared_sink():
    """Regression: two overlapping thread_scoped_silence() windows must not
    let the first window's exit close a sink the second window still uses.

    Real incident: a delegate_task background batch ran two subagent worker
    threads under overlapping thread_scoped_silence() windows (via
    background_review-style teardown). When the first subagent's window
    exited before the second's, the old implementation (fresh devnull sink
    per call, closed on exit) closed a sink object shared by BOTH windows
    (_ensure_installed's "already installed" fast path reuses the existing
    proxy — and therefore its existing sink — for any caller after the
    first). The second thread's next write then raised
    ValueError("I/O operation on closed file"), uncaught, on whatever thread
    happened to own sys.stdout at that point (the CLI's process_loop daemon
    thread in the field report) — freezing the interactive session.
    """
    from agent.thread_scoped_output import thread_scoped_silence

    entered_second = threading.Event()
    exit_first = threading.Event()
    second_write_ok: dict = {"value": None, "exc": None}

    def first():
        with thread_scoped_silence():
            entered_second.wait(timeout=2.0)
            # First window exits (and, pre-fix, closed the shared sink)
            # while the second window below is still silenced.

    def second():
        with thread_scoped_silence():
            entered_second.set()
            exit_first.wait(timeout=2.0)
            try:
                print("still writing after first window closed")
                second_write_ok["value"] = True
            except Exception as e:  # pragma: no cover - the bug this pins
                second_write_ok["exc"] = e

    t1 = threading.Thread(target=first)
    t2 = threading.Thread(target=second)
    t1.start()
    t2.start()
    t1.join(timeout=5.0)
    exit_first.set()
    t2.join(timeout=5.0)

    assert not t1.is_alive() and not t2.is_alive()
    assert second_write_ok["exc"] is None, (
        f"write from overlapping silence window raised: {second_write_ok['exc']!r}"
    )
    assert second_write_ok["value"] is True


def test_many_concurrent_silenced_and_loud_threads():
    """Stress: interleaved silenced/loud threads keep their respective fates."""
    start = threading.Event()
    results_lock = threading.Lock()

    def silenced(i):
        start.wait(timeout=2.0)
        with thread_scoped_silence():
            print(f"S{i}")
            time.sleep(0.05)

    def loud(i):
        start.wait(timeout=2.0)
        time.sleep(0.02)
        print(f"L{i}")

    def body():
        threads = []
        for i in range(5):
            threads.append(threading.Thread(target=silenced, args=(i,)))
            threads.append(threading.Thread(target=loud, args=(i,)))
        for t in threads:
            t.start()
        start.set()
        for t in threads:
            t.join(timeout=15.0)
        assert not any(t.is_alive() for t in threads), "straggler thread would truncate captured output"

    captured = _run_with_real_stream(body)
    for i in range(5):
        assert f"S{i}" not in captured, f"silenced S{i} leaked"
        assert f"L{i}" in captured, f"loud L{i} swallowed"
