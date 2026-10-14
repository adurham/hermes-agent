"""exo ``: generation_stats`` capture + persistence (``agent/fork/generation_stats.py``).

exo's dsv41/MTP runner stamps its per-response generation statistics on the chat_completions wire as
an SSE COMMENT frame:

    : generation_stats {"…","prefix_cache_hit":"partial",
                        "mtp_cycles_cumulative":67554,"mtp_accepted_drafts_cumulative":46018, …}

The OpenAI SDK drops every comment line before any consumer sees it
(``openai/_streaming.py::SSEDecoder.decode`` → ``None`` for a line starting with ``:``; ``__stream__``
skips the ``None``). The fork therefore never parsed or persisted these — which left three
degeneration investigations blind to speculative-decode acceptance/exposure. This module's tests pin:

1. ``parse_generation_stats_comment`` accepts a REAL capture frame and rejects everything else.
2. The per-stream decoder shim captures frames the SDK itself drops (the drop is asserted directly).
3. Absence (no frame at all) and malformed frames are clean no-ops — the stream is never broken.
4. ``turn_usage._record_api_call_row`` persists the captured stats into ``api_calls.extra``, and
   ``chat_completion_helpers`` stamps them onto the response so it can.

Fixture provenance: ``tests/fixtures/generation_stats_stream.sse`` — the two ``: generation_stats``
comment lines are copied VERBATIM from the 2026-10-14 live exo captures
(``~/.hermes/cache/scratch/replay-loop/live-probe/caps/cap_p8_k1_20261014-161826.sse`` and
``…/cap_p40d_k3_20261014-165725.sse``); the ``data:`` frames are minimal SYNTHETIC stand-ins so no
captured prompt/completion text is committed.
"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "generation_stats_stream.sse"

#: The exact figure in the fixture's SECOND (last) generation-stats frame — the one the tap keeps.
LAST_FRAME = {
    "prompt_tps": 0.0,
    "generation_tps": 0.0,
    "prompt_tokens": 78375,
    "generation_tokens": 1295,
    "mtp_cycles_cumulative": 67554,
    "mtp_accepted_drafts_cumulative": 46018,
    "prefix_cache_hit": "partial",
    "peak_memory_bytes": 123996778246,
}
FIRST_FRAME = {
    "prompt_tps": 0.0,
    "generation_tps": 0.0,
    "prompt_tokens": 41864,
    "generation_tokens": 408,
    "mtp_cycles_cumulative": 43644,
    "mtp_accepted_drafts_cumulative": 30216,
    "prefix_cache_hit": "partial",
    "peak_memory_bytes": 123782766245,
}


def _fixture_text() -> str:
    return FIXTURE.read_text()


def _fixture_bytes() -> bytes:
    return FIXTURE.read_bytes()


def _stats_frames() -> list[str]:
    return [line for line in _fixture_text().splitlines() if line.startswith(": generation_stats")]


def _sse_decoder():
    from openai._streaming import SSEDecoder

    return SSEDecoder()


def _tapped_decoder():
    """A REAL SDK ``SSEDecoder`` with the fork's per-stream shim installed on a fake stream object."""
    from agent.fork.generation_stats import attach_generation_stats_tap

    decoder = _sse_decoder()
    tap = attach_generation_stats_tap(SimpleNamespace(_decoder=decoder))
    assert tap is not None
    return decoder, tap


# ── 1. the parser ──────────────────────────────────────────────────────────

def test_parse_accepts_a_real_capture_frame():
    from agent.fork.generation_stats import parse_generation_stats_comment

    line = _stats_frames()[0]
    assert parse_generation_stats_comment(line) == FIRST_FRAME


@pytest.mark.parametrize("line", [
    "",
    "   ",
    ": keep-alive",
    ": ping",
    'data: {"choices":[]}',
    'data: {"id":"x","choices":[{"delta":{"content":"hi"}}]}',
    ": generation_stats",                       # no payload
    ": generation_stats not-json",
    ": generation_stats [1, 2]",                # JSON but not an object
    ": generation_stats {}",                    # object but no known fields
    "generation_stats {\"mtp_cycles_cumulative\": 1}",  # missing the leading ':'
])
def test_parse_rejects_everything_that_is_not_a_frame(line):
    from agent.fork.generation_stats import parse_generation_stats_comment

    assert parse_generation_stats_comment(line) is None


def test_parse_rejects_non_strings_without_raising():
    from agent.fork.generation_stats import parse_generation_stats_comment

    assert parse_generation_stats_comment(None) is None
    assert parse_generation_stats_comment(b": generation_stats {}") is None
    assert parse_generation_stats_comment(123) is None


# ── 2. the decoder shim captures what the SDK drops ────────────────────────

def test_shim_captures_frames_the_sdk_itself_discards():
    """The comment frame never becomes an SDK event, yet the tap sees both frames; last wins."""
    from agent.fork.generation_stats import attach_generation_stats_tap

    # Control: with NO shim, the SDK yields only the data frames and no stats.
    plain = list(_sse_decoder().iter_bytes(iter([_fixture_bytes()])))
    assert plain, "the fixture must yield SDK events"
    assert not any("generation_stats" in str(event.json()) for event in plain)

    decoder, tap = _tapped_decoder()
    events = list(decoder.iter_bytes(iter([_fixture_bytes()])))
    assert [e.json() for e in events] == [e.json() for e in plain]  # unchanged consumer behavior
    assert tap.frames == 2, "both comment frames must be observed"
    assert tap.snapshot() == {**LAST_FRAME, "frames": 2}


def test_shim_keeps_only_cumulative_last_frame_across_chunk_boundaries():
    """A frame split across two byte chunks is still parsed (the SDK buffers lines)."""
    from agent.fork.generation_stats import attach_generation_stats_tap

    body = _fixture_bytes()
    cut = body.index(b": generation_stats") + 20
    decoder = _sse_decoder()
    tap = attach_generation_stats_tap(SimpleNamespace(_decoder=decoder))
    list(decoder.iter_bytes(iter([body[:cut], body[cut:]])))
    assert tap.frames == 2
    assert tap.snapshot()["mtp_cycles_cumulative"] == LAST_FRAME["mtp_cycles_cumulative"]


def test_tap_observes_directly_without_a_stream():
    from agent.fork.generation_stats import GenerationStatsTap

    tap = GenerationStatsTap()
    tap.observe_line('data: {"choices":[]}')
    assert tap.snapshot() is None
    tap.observe_line(_stats_frames()[0])
    assert tap.snapshot() == {**FIRST_FRAME, "frames": 1}


# ── 3. absence + malformed robustness ──────────────────────────────────────

def test_absence_is_a_clean_noop():
    from agent.fork.generation_stats import attach_generation_stats_tap

    body = "\n".join(
        line for line in _fixture_text().splitlines() if not line.startswith(": generation_stats")
    ) + "\n"
    decoder = _sse_decoder()
    tap = attach_generation_stats_tap(SimpleNamespace(_decoder=decoder))
    events = list(decoder.iter_bytes(iter([body.encode()])))
    assert events, "data frames must still flow"
    assert tap.frames == 0
    assert tap.snapshot() is None


def test_malformed_frames_never_break_the_stream():
    body = (
        ": generation_stats {not json}\n\n"
        ": generation_stats [1, 2]\n\n"
        ": generation_stats\n\n"
        ": generation_stats {}\n\n"
        'data: {"choices":[]}\n\n'
    )
    decoder, tap = _tapped_decoder()
    events = list(decoder.iter_bytes(iter([body.encode()])))
    assert tap.snapshot() is None
    assert len(events) == 1  # only the one well-formed data frame


@pytest.mark.parametrize("target", [
    None,
    SimpleNamespace(),                       # no _decoder
    SimpleNamespace(_decoder=None),
    SimpleNamespace(_decoder=SimpleNamespace()),   # decoder without decode()
    SimpleNamespace(_decoder=object()),
])
def test_attach_returns_none_for_objects_that_are_not_sdk_streams(target):
    from agent.fork.generation_stats import attach_generation_stats_tap

    assert attach_generation_stats_tap(target) is None


# ── 4. persistence wiring ──────────────────────────────────────────────────

class _RecordingSessionDB:
    def __init__(self):
        self.recorded: list[tuple[str, dict]] = []

    def record_api_call(self, session_id, **kwargs):
        self.recorded.append((session_id, kwargs))


class _StubAgent:
    def __init__(self, db):
        self._session_db = db
        self.session_id = "sess-genstats"
        self.session_api_calls = 3
        self.model = "dealignai/DeepSeek-V4.1-Flash-UNCENSORED-EXL3-2.9bpw"
        self.provider = "custom"


def _canonical_usage():
    return SimpleNamespace(
        input_tokens=10, cache_read_tokens=20, cache_write_tokens=0, output_tokens=5,
        reasoning_tokens=0, raw_usage={"prompt_tokens": 30})


def test_record_api_call_row_persists_generation_stats_into_extra():
    from agent.turn_usage import _record_api_call_row

    db = _RecordingSessionDB()
    agent = _StubAgent(db)
    stats = {**LAST_FRAME, "frames": 2}
    response = SimpleNamespace(provider=None, _generation_stats=stats)

    _record_api_call_row(agent, response, _canonical_usage(), api_duration=1.25)

    assert len(db.recorded) == 1
    session_id, kwargs = db.recorded[0]
    assert session_id == "sess-genstats"
    assert kwargs["extra"]["generation_stats"] == stats
    assert kwargs["extra"]["raw_usage"] == {"prompt_tokens": 30}  # existing fields preserved
    assert kwargs["call_seq"] == 3


def test_record_api_call_row_without_stats_keeps_extra_clean():
    from agent.turn_usage import _record_api_call_row

    db = _RecordingSessionDB()
    agent = _StubAgent(db)
    response = SimpleNamespace(provider=None)  # e.g. a non-exo provider: no captured frame

    _record_api_call_row(agent, response, _canonical_usage(), api_duration=0.5)

    extra = db.recorded[0][1]["extra"]
    assert "generation_stats" not in extra
    assert set(extra) == {"raw_usage", "routing"}


def test_record_api_call_row_ignores_a_non_dict_stats_attribute():
    from agent.turn_usage import _record_api_call_row

    db = _RecordingSessionDB()
    agent = _StubAgent(db)
    response = SimpleNamespace(provider=None, _generation_stats="not-a-dict")

    _record_api_call_row(agent, response, _canonical_usage(), api_duration=0.5)
    assert "generation_stats" not in db.recorded[0][1]["extra"]


# ── 5. the stream-helper seams ─────────────────────────────────────────────

def _bare_streaming_call():
    from agent.chat_completion_helpers import _StreamingCall

    return object.__new__(_StreamingCall)


def test_attach_generation_stats_stamps_the_response():
    from agent.fork.generation_stats import GenerationStatsTap

    call = _bare_streaming_call()
    call.agent = SimpleNamespace(log_prefix="")
    call._generation_stats_tap = GenerationStatsTap()
    call._generation_stats_tap.observe_line(_stats_frames()[1])

    response = SimpleNamespace()
    call._attach_generation_stats(response)

    assert response._generation_stats == {**LAST_FRAME, "frames": 1}


def test_attach_generation_stats_is_a_noop_without_a_tap():
    call = _bare_streaming_call()
    call.agent = SimpleNamespace(log_prefix="")
    call._generation_stats_tap = None

    response = SimpleNamespace()
    call._attach_generation_stats(response)

    assert not hasattr(response, "_generation_stats")


def test_attach_generation_stats_survives_a_frozen_response():
    class _Frozen:
        __slots__ = ()

        def __setattr__(self, name, value):
            raise RuntimeError("cannot set attribute")

    call = _bare_streaming_call()
    call.agent = SimpleNamespace(log_prefix="")
    from agent.fork.generation_stats import GenerationStatsTap

    call._generation_stats_tap = GenerationStatsTap()
    call._generation_stats_tap.observe_line(_stats_frames()[0])

    call._attach_generation_stats(_Frozen())  # must not raise


class _NoopAgent:
    """Only the agent surface ``_chat_stream_created`` touches."""

    log_prefix = ""

    def _capture_rate_limits(self, response): pass

    def _capture_credits(self, response): pass

    def _capture_nous_model_switch(self, response): pass

    def _stream_diag_capture_response(self, diag, response): pass

    def _check_openrouter_cache_status(self, response): pass


def test_chat_stream_created_installs_the_decoder_tap():
    """Wiring seam: the streaming helper taps the raw SDK stream it was handed."""
    call = _bare_streaming_call()
    call.agent = _NoopAgent()
    call.clients = MagicMock(diag={})
    call.stream_attempt_lock = threading.Lock()
    call.stream_attempt_state = {"current": 0, "cancelled": set()}
    call._request_cancelled = {"value": False}

    raw_stream = SimpleNamespace(_decoder=_sse_decoder(), response=None)
    call._chat_stream_created(raw_stream)

    assert call._generation_stats_tap is not None
    assert call._attempt_stream_response is None  # pre-existing behavior preserved

    # the installed tap is live: consuming the stream populates it end-to-end.
    list(raw_stream._decoder.iter_bytes(iter([_fixture_bytes()])))
    assert call._generation_stats_tap.frames == 2


def test_chat_stream_created_leaves_the_tap_absent_for_a_non_sdk_stream():
    call = _bare_streaming_call()
    call.agent = _NoopAgent()
    call.clients = MagicMock(diag={})
    call.stream_attempt_lock = threading.Lock()
    call.stream_attempt_state = {"current": 0, "cancelled": set()}
    call._request_cancelled = {"value": False}

    call._chat_stream_created(SimpleNamespace(response=None))

    assert call._generation_stats_tap is None
