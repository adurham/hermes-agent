"""Data-use requirements of connector results, handed from dispatch to the tool message.

The result string the model reads never carries them: dispatch records the requirement here under the
call's ``(turn_id, tool_call_id)`` and the agent's tool executor takes it when it builds the tool message,
storing it as that message's ``data_use`` metadata (``agent.message_metadata.DATA_USE``).
"""

from __future__ import annotations

import threading
from typing import Iterable, Optional

_LOCK = threading.Lock()
_PENDING: dict[tuple[str, str], frozenset[str]] = {}
# Callers outside the agent's tool executor (execute_code RPC, MCP serve) never take; keep that bounded.
_MAX_PENDING = 512


def record_data_use(turn_id: Optional[str], tool_call_id: Optional[str], requires: Iterable[str]) -> None:
    wanted = frozenset(requires)
    if not tool_call_id or not wanted:
        return
    with _LOCK:
        key = (turn_id or "", tool_call_id)
        _PENDING[key] = _PENDING.pop(key, frozenset()) | wanted
        while len(_PENDING) > _MAX_PENDING:
            _PENDING.pop(next(iter(_PENDING)))


def take_data_use(turn_id: Optional[str], tool_call_id: Optional[str]) -> list[str]:
    """The requirement recorded for this call (sorted), removing it; ``[]`` when none was recorded."""
    if not tool_call_id:
        return []
    with _LOCK:
        return sorted(_PENDING.pop((turn_id or "", tool_call_id), ()))
