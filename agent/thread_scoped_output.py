"""Thread-scoped stdout/stderr silencing for background worker threads.

``contextlib.redirect_stdout``/``redirect_stderr`` reassign the *process-global*
``sys.stdout``/``sys.stderr``.  When a daemon worker thread (e.g. the background
memory/skill review) wraps its whole body in those context managers, every other
thread in the process — including a gateway's asyncio event-loop thread driving a
Telegram long-poll — sees ``sys.stdout``/``sys.stderr`` pointing at ``devnull``
for the full duration.  Any bare ``print`` / ``sys.stderr.write`` from those other
threads is silently lost during that window (see issue #55769 / #55925).

This module installs a thin proxy as ``sys.stdout``/``sys.stderr`` that routes
writes per-thread: threads registered as "silenced" go to a sink; every other
thread passes through to the *original* stream.  The proxy is installed once,
idempotently, and is never uninstalled (uninstalling would race other threads
mid-write), so the only observable effect for unregistered threads is one extra
attribute lookup per write.
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
from typing import Iterator, TextIO

__all__ = ["thread_scoped_silence"]

_install_lock = threading.Lock()
# Maps the proxy we installed for a given attribute ("stdout"/"stderr") so we
# never double-wrap and so we can recover the original stream.
_installed: dict[str, "_ThreadRoutingStream"] = {}
# One process-lifetime sink per stream. Temporary process-global redirects can
# displace and later restore a routing proxy; they must not allocate another
# permanent /dev/null descriptor every time that happens.
_sinks: dict[str, TextIO] = {}
_routing_states: dict[str, "_RoutingState"] = {}


class _RoutingState:
    """Silencing registry shared by every proxy generation for one stream."""

    def __init__(self, sink: TextIO) -> None:
        self.sink = sink
        self.silenced: dict[int, int] = {}
        self.lock = threading.Lock()


class _ThreadRoutingStream:
    """A ``sys.stdout``/``sys.stderr`` stand-in that routes writes per-thread.

    Threads whose ident is in ``_silenced`` write to ``_sink``; all other
    threads write to ``_passthrough`` (the original stream captured at install
    time).  Attribute access for anything other than the methods we override
    is delegated to the *current* target so things like ``.encoding`` /
    ``.fileno()`` behave like the underlying stream for the calling thread.
    """

    def __init__(self, passthrough: TextIO, state: _RoutingState) -> None:
        self._passthrough = passthrough
        self._state = state

    def _target(self) -> TextIO:
        if self._state.silenced.get(threading.get_ident(), 0) > 0:
            return self._state.sink
        return self._passthrough

    # --- registration -----------------------------------------------------
    def silence(self, ident: int) -> None:
        with self._state.lock:
            self._state.silenced[ident] = self._state.silenced.get(ident, 0) + 1

    def unsilence(self, ident: int) -> None:
        with self._state.lock:
            depth = self._state.silenced.get(ident, 0) - 1
            if depth > 0:
                self._state.silenced[ident] = depth
            else:
                self._state.silenced.pop(ident, None)

    # --- file-like surface ------------------------------------------------
    def write(self, data):  # type: ignore[no-untyped-def]
        try:
            return self._target().write(data)
        except Exception:
            return len(data) if isinstance(data, str) else 0

    def flush(self):  # type: ignore[no-untyped-def]
        try:
            return self._target().flush()
        except Exception:
            return None

    def writelines(self, lines):  # type: ignore[no-untyped-def]
        target = self._target()
        try:
            return target.writelines(lines)
        except Exception:
            return None

    def isatty(self) -> bool:
        try:
            return bool(self._target().isatty())
        except Exception:
            return False

    def fileno(self):  # type: ignore[no-untyped-def]
        # Same resilience contract as write()/flush(): a silenced thread's
        # target is our shared devnull sink, which lives for the process
        # lifetime (see module docstring + _SHARED_SINKS) and should never
        # be closed out from under us. But defense-in-depth matters here —
        # any stale/closed target (e.g. a passthrough stream torn down by
        # unrelated cleanup) must not raise an uncaught ValueError/OSError
        # into a caller (prompt_toolkit's renderer, in the CLI, probes
        # .fileno() on sys.stdout on effectively every redraw). Fall back to
        # the real stdio fd rather than propagating.
        try:
            return self._target().fileno()
        except (ValueError, OSError):
            return sys.__stdout__.fileno() if sys.__stdout__ else 1

    def __getattr__(self, name):  # type: ignore[no-untyped-def]
        # Delegate everything we don't override (encoding, buffer, mode, ...)
        # to the calling thread's current target. Mirror _SafeWriter's
        # guarantee (agent/process_bootstrap.py) that a closed/broken
        # underlying stream can never crash the caller — this proxy sits in
        # front of sys.stdout/sys.stderr for EVERY thread in the process, not
        # just the silenced ones, so an uncaught exception here is a global
        # freeze risk (e.g. process_loop's daemon thread; #14726-adjacent).
        try:
            return getattr(self._target(), name)
        except (ValueError, OSError):
            return getattr(self._passthrough, name)


# One devnull sink per attr ("stdout"/"stderr"), opened once and held open
# for the life of the process — never closed. This mirrors the proxy's own
# "installed once, idempotently, never uninstalled" contract (see module
# docstring). Earlier code opened a fresh sink per thread_scoped_silence()
# call and closed it on that call's exit; when two windows overlapped,
# _ensure_installed's "already installed" fast path made the SECOND caller
# silently reuse the FIRST caller's sink object, so the first caller's exit
# closed a sink the second caller was still writing through. Any write
# after that raised ValueError("I/O operation on closed file") — uncaught,
# because it happened on a totally unrelated thread (e.g. the CLI's
# process_loop) whose sys.stdout is this same shared proxy. A single
# perpetually-open sink removes the race entirely: nothing ever closes it.
_SHARED_SINKS: dict[str, TextIO] = {}


def _get_shared_sink() -> TextIO:
    with _install_lock:
        sink = _SHARED_SINKS.get("devnull")
        if sink is None or sink.closed:
            sink = open(os.devnull, "w", encoding="utf-8")
            _SHARED_SINKS["devnull"] = sink
        return sink


def _ensure_installed(attr: str, passthrough: TextIO) -> "_ThreadRoutingStream":
    """Install (idempotently) a routing proxy as ``sys.<attr>`` and return it."""
    with _install_lock:
        proxy = _installed.get(attr)
        current = getattr(sys, attr, None)
        if isinstance(current, _ThreadRoutingStream):
            # A redirect context can restore an older routing proxy after a
            # temporary replacement. Adopt it instead of wrapping it and
            # growing an unbounded proxy chain.
            _installed[attr] = current
            _routing_states[attr] = current._state
            return current
        if proxy is not None and current is proxy:
            return proxy
        # Capture whatever is currently bound as the passthrough. If a prior
        # global redirect_stdout is active, route non-silenced threads to that
        # stream to preserve the old behavior.
        passthrough = current if current is not None else passthrough
        sink = _sinks.get(attr)
        if sink is None or sink.closed:
            sink = open(os.devnull, "w", encoding="utf-8")
            _sinks[attr] = sink
        state = _routing_states.get(attr)
        if state is None or state.sink is not sink:
            state = _RoutingState(sink)
            _routing_states[attr] = state
        proxy = _ThreadRoutingStream(passthrough, state)
        setattr(sys, attr, proxy)
        _installed[attr] = proxy
        return proxy


@contextlib.contextmanager
def thread_scoped_silence() -> Iterator[None]:
    """Silence ``stdout``/``stderr`` for the *current thread only*.

    Other threads keep writing to the real streams.  Use this around a worker
    thread's body instead of ``contextlib.redirect_stdout(devnull)`` when the
    process is multi-threaded and another thread must keep its console output.
    """
    sink = _get_shared_sink()
    ident = threading.get_ident()
    out_proxy = _ensure_installed("stdout", sys.__stdout__ or sys.stdout)
    err_proxy = _ensure_installed("stderr", sys.__stderr__ or sys.stderr)
    out_proxy.silence(ident)
    err_proxy.silence(ident)
    try:
        yield
    finally:
        out_proxy.unsilence(ident)
        err_proxy.unsilence(ident)
        # The sink is shared and process-lifetime — never close it here.
        # See _SHARED_SINKS for why closing per-call was the original bug.
