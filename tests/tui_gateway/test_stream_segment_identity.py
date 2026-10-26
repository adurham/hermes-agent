"""A prompt turn's message.* frames carry its wire identity (tui_gateway/stream_segments.py).

The desktop upserts a re-sent frame by this identity instead of painting a second copy (#144609):
every delta names its turn, segment (interim seals before it) and UTF-16 offset inside that segment,
every interim the segment it seals.
"""

import contextlib
from types import SimpleNamespace

import tui_gateway.server as srv


def test_deltas_and_seals_carry_turn_segment_and_utf16_offset(monkeypatch):
    events = []
    monkeypatch.setattr(srv, "_emit", lambda event, _sid, payload=None: events.append((event, payload)))
    monkeypatch.setattr(srv, "_load_interim_assistant_messages", lambda: True)
    monkeypatch.setattr(srv, "_start_usage_ticker", lambda _sid, _agent: (SimpleNamespace(set=lambda: None), SimpleNamespace(join=lambda: None)))

    def run_conversation(_message, **kwargs):
        stream = kwargs["stream_callback"]
        stream("Looking 👀 ")
        stream("now.")
        agent.interim_assistant_callback("Looking 👀 now.", already_streamed=True)
        stream("Done.")
        return {"final_response": "Done."}

    agent = SimpleNamespace(_session_title_hint="Scratch", run_conversation=run_conversation)
    session = {"pending_title": None, "session_key": "k", "history_lock": contextlib.nullcontext(), "agent": agent}
    st = srv._TurnRun(agent=agent, one_turn_restore=None, terminal_callback=None, receipt_committed=True)
    srv._invoke_agent("sid", session, st, "ping", "ping", None, [], None, None)

    stamps = [(e, p.get("segment"), p.get("offset")) for e, p in events if e in {"message.delta", "message.interim"}]
    # The eye emoji is one code point but two UTF-16 units: "Looking 👀 " advances the offset by 11.
    assert stamps == [
        ("message.delta", 0, 0), ("message.delta", 0, 11), ("message.interim", 0, None), ("message.delta", 1, 0)]
    turn_ids = {p["turn_id"] for e, p in events if e in {"message.delta", "message.interim"}}
    assert turn_ids == {st.segments.turn_id}
    assert st.segments.turn_id and srv._TurnRun(
        agent=agent, one_turn_restore=None, terminal_callback=None, receipt_committed=True).segments.turn_id != st.segments.turn_id
