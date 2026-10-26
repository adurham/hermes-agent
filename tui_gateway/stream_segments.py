"""Wire identity for one turn's streamed reply.

A turn's frames reach a client more than once on some roads: a reconnect replay that re-stamps
seqs, a superseded attempt that re-streams the segment it already sent, a second socket. Without an
identity the client can only append, so the copy paints beside the original (#144609, #128158).
Every ``message.start`` / ``message.delta`` / ``message.interim`` of a turn therefore carries the
turn's ``turn_id``; a delta adds its ``segment`` (how many interim seals came before it) and its
``offset`` inside that segment, an interim the ``segment`` it seals. A client upserts by that
identity and drops what it has already applied.

``offset`` counts UTF-16 code units, the unit a JavaScript string indexes in, so an emoji or other
astral character advances it by the same amount on both sides.
"""

from __future__ import annotations

import uuid
from typing import Any


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2


class TurnSegments:
    """The segment cursor of one turn: created at ``message.start``, advanced by every delta and seal."""

    def __init__(self) -> None:
        self.turn_id = uuid.uuid4().hex
        self.segment = 0
        self.offset = 0

    def start(self) -> dict[str, Any]:
        return {"turn_id": self.turn_id}

    def delta(self, text: Any) -> dict[str, Any]:
        stamp = {"turn_id": self.turn_id, "segment": self.segment, "offset": self.offset}
        if isinstance(text, str):
            self.offset += _utf16_len(text)
        return stamp

    def seal(self) -> dict[str, Any]:
        stamp = {"turn_id": self.turn_id, "segment": self.segment}
        self.segment += 1
        self.offset = 0
        return stamp
