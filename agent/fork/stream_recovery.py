"""Streaming-recovery helpers (fork-only).

The fork adds a cold-start grace period to the stream stale-timeout: before the
first event arrives, a slow provider (large prompt, queue stall, cold prefill)
shouldn't be killed at the normal mid-stream threshold. Upstream uses a flat
``_stream_stale_timeout`` throughout; this longer cold-start window is a
fork-only behavior that, when it lived inline in ``chat_completion_helpers``,
conflicted with upstream's edits to the stream-watchdog block.

Only the timeout *computation* lives here (a pure function). The stale-kill
loop control — counters, ``break``, the daemon-thread teardown — stays inline
in ``chat_completion_helpers`` because it's tightly coupled to that function's
local streaming state.
"""
from __future__ import annotations

import os


def effective_stale_timeout(first_event_seen: bool, stream_stale_timeout: float) -> float:
    """Return the stale-timeout to enforce given whether streaming has started.

    * After the first event (``first_event_seen``): the normal, shorter
      ``stream_stale_timeout`` — any silence now is a real stall.
    * Before the first event, when the base timeout is infinite: stay infinite
      (caller disabled the watchdog).
    * Before the first event otherwise: a generous cold-start window —
      ``max(3x the base, HERMES_STREAM_COLD_START_TIMEOUT (default 600s))`` —
      so a slow cold prefill / queue wait isn't killed prematurely.
    """
    if first_event_seen:
        return stream_stale_timeout
    if stream_stale_timeout == float("inf"):
        return float("inf")
    return max(
        stream_stale_timeout * 3.0,
        float(os.getenv("HERMES_STREAM_COLD_START_TIMEOUT", 600.0)),
    )
