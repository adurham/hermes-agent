"""exo generation-stats capture (fork-only): persist the engine's per-response ``: generation_stats``.

Why (forensics data gap): exo's dsv41/MTP runner emits per-response generation statistics as an
SSE **comment** frame on the chat_completions wire::

    : generation_stats {"generation_tokens":749,"prefix_cache_hit":"partial",
                        "mtp_cycles_cumulative":46284,"mtp_accepted_drafts_cumulative":32130, …}

``mtp_cycles_cumulative`` is the cumulative count of speculative rounds (sum of gamma) and
``mtp_accepted_drafts_cumulative`` the accepted draft tokens, so the two give the speculative-decode
acceptance/exposure for a response; ``prefix_cache_hit`` tells whether the prefill hit the prefix
cache. Three separate degeneration investigations were BLIND to this because the OpenAI SDK DROPS SSE
comment lines before they reach any consumer — ``openai/_streaming.py::SSEDecoder.decode`` returns
``None`` for any line starting with ``:`` (SSE spec: comment), and ``Stream.__stream__`` skips the
result. The only surviving copies lived in raw ``.sse`` captures. This module taps the decoder so the
data is captured and persisted per API call.

Hook design (minimal + safe): a **per-stream** decoder shim, installed on the stream instance the SDK
returns from ``chat.completions.create(stream=True)`` — NOT a process-wide patch of the SDK class. It
wraps ``stream._decoder.decode`` (the per-line entry point inside ``SSEDecoder.iter_bytes``) so each
raw, already-decoded SSE line is offered to the tap before the SDK drops it. Scoped to exactly this
stream, so concurrent streams never interfere and no global state is mutated; if the object does not
look like an SDK stream (facades, test stand-ins, a future SDK refactor) the attach returns ``None``
and nothing changes.

Every function here is best-effort: a parse failure or a missing attribute must never break the
stream, and an endpoint that never sends the frame (any non-exo provider) simply yields no stats.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger("agent.conversation_loop")

#: The SSE comment field name the engine stamps ahead of its JSON payload.
COMMENT_FIELD = "generation_stats"

#: Engine keys kept verbatim in the persisted record — the engine's own names are already
#: self-describing, and keeping them lets a stored row cross-reference the engine's own logs.
_INT_KEYS = (
    "mtp_cycles_cumulative",           # total speculative rounds (= sum of gamma) for this response
    "mtp_accepted_drafts_cumulative",  # accepted draft tokens (numerator for the acceptance ratio)
    "prompt_tokens",
    "generation_tokens",
)
_FLOAT_KEYS = ("prompt_tps", "generation_tps")
_STR_KEYS = ("prefix_cache_hit",)


def parse_generation_stats_comment(line: Any) -> Optional[dict]:
    """Parse ONE raw SSE line; return a normalized stats dict for a ``generation_stats`` comment
    frame, or ``None`` for anything else (data frames, ``: keep-alive``, malformed payloads).

    Never raises. The common case — a ``data:`` frame — is rejected with an O(1) first-character
    check so the per-line hot path stays cheap.
    """
    # SSE comment lines start with ':' in column 0 (spec); everything else is not ours.
    if not isinstance(line, str) or not line or line[0] != ":":
        return None
    payload_text = line[1:].strip()
    if not payload_text.startswith(COMMENT_FIELD):
        return None
    rest = payload_text[len(COMMENT_FIELD):].strip()
    if not rest.startswith("{"):
        return None
    try:
        raw = json.loads(rest)
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None

    stats: dict[str, Any] = {}
    for key in _INT_KEYS:
        value = raw.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            stats[key] = value
        elif isinstance(value, float) and value.is_integer():
            stats[key] = int(value)
    for key in _FLOAT_KEYS:
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            stats[key] = float(value)
    for key in _STR_KEYS:
        value = raw.get(key)
        if isinstance(value, str):
            stats[key] = value
    peak = raw.get("peak_memory_usage")
    if isinstance(peak, dict) and isinstance(peak.get("in_bytes"), int):
        stats["peak_memory_bytes"] = peak["in_bytes"]
    return stats or None


class GenerationStatsTap:
    """Accumulates generation-stats frames seen on one stream.

    The engine sends CUMULATIVE counters, several frames per response; the last frame is the
    response's final figure, so the tap keeps the most recent and counts how many it saw.
    """

    __slots__ = ("_latest", "errors", "frames")

    def __init__(self) -> None:
        self._latest: Optional[dict] = None
        self.frames = 0
        self.errors = 0

    def observe_line(self, line: Any) -> None:
        """Offer one raw SSE line; parses and keeps it if it is a generation-stats frame."""
        try:
            stats = parse_generation_stats_comment(line)
        except Exception:
            self.errors += 1
            return
        if stats:
            self.frames += 1
            self._latest = stats

    def snapshot(self) -> Optional[dict]:
        """The last frame's stats plus a ``frames`` count, or ``None`` when none was seen."""
        if not self._latest:
            return None
        return {**self._latest, "frames": self.frames}


def attach_generation_stats_tap(stream: Any) -> Optional[GenerationStatsTap]:
    """Install a per-stream decoder shim that observes SSE comment frames.

    Returns the :class:`GenerationStatsTap` on success, or ``None`` when the object does not expose
    an SDK-style decoder (facades, non-SDK clients, a future SDK refactor) — in which case the stream
    behaves exactly as before. Idempotent in effect: re-attaching replaces the previous shim.
    """
    if stream is None:
        return None
    decoder = getattr(stream, "_decoder", None)
    original_decode = getattr(decoder, "decode", None)
    if decoder is None or not callable(original_decode):
        return None
    tap = GenerationStatsTap()

    def _decode(line, _original=original_decode, _tap=tap):
        _tap.observe_line(line)
        return _original(line)

    try:
        decoder.decode = _decode
    except Exception as exc:  # pragma: no cover - a decoder that refuses attribute writes
        logger.debug("generation_stats tap not installed (%s)", exc, exc_info=True)
        return None
    return tap


__all__ = ["GenerationStatsTap", "attach_generation_stats_tap", "parse_generation_stats_comment"]
