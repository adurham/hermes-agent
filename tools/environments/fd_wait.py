"""POSIX fd-readiness helper shared by the stdout drains and the PTY bridge.

``select.select()`` raises ``ValueError: filedescriptor out of range in select()``
once an fd reaches FD_SETSIZE (1024). A long-lived gateway/desktop process holds
that many open fds, and every drain swallowed that ValueError as "fd already
closed", silently discarding output (issue #94928).

``selectors.DefaultSelector`` (epoll on Linux, kqueue on macOS) has no FD_SETSIZE
ceiling. ``poll()`` is not a substitute: macOS ``poll()`` does not support device
fds and reports a PTY master as POLLNVAL, i.e. "ready" at once, so a PTY read loop
would spin a core.
"""

import selectors


class FdWaiter:
    """Readability wait on one fd. Owns a selector (itself one fd): create it once
    per reader lifetime and ``close()`` it when the reader is done."""

    def __init__(self, fd: int):
        self._selector = selectors.DefaultSelector()
        try:
            self._selector.register(fd, selectors.EVENT_READ)
        except BaseException:
            self._selector.close()
            raise

    def wait(self, timeout_s: float) -> bool:
        """True as soon as the fd is readable (EOF and hangup included), False when
        *timeout_s* elapses idle."""
        return bool(self._selector.select(max(timeout_s, 0)))

    def close(self) -> None:
        self._selector.close()


def open_fd_waiter(fd: int):
    """Return an :class:`FdWaiter` for *fd*, or None when *fd* cannot be watched
    (already closed / invalid) and there is nothing to drain."""
    try:
        return FdWaiter(fd)
    except (ValueError, OSError):
        return None
