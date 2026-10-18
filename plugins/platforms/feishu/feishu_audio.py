"""Audio metadata for Feishu uploads (the ``duration`` field opus files carry)."""

from __future__ import annotations

import logging
import struct

logger = logging.getLogger(__name__)


def ogg_duration_ms(file_path: str) -> int:
    """OGG/Opus duration in ms (pure Python): last granule position / 48000 Hz; 0 on non-OGG or error."""
    try:
        with open(file_path, "rb") as f:
            data = f.read()
        pos = last_granule = 0
        while pos < len(data) - 27:
            pos = data.find(b"OggS", pos)
            if pos == -1 or pos + 27 > len(data):
                break
            granule = struct.unpack_from("<q", data, pos + 6)[0]
            num_segments = data[pos + 26]
            last_granule = granule if granule > 0 else last_granule
            if pos + 27 + num_segments > len(data):
                break
            pos += num_segments + sum(data[pos + 27 : pos + 27 + num_segments])
        return int(last_granule / 48000 * 1000) if last_granule > 0 else 0
    except (OSError, ValueError, struct.error):
        logger.debug("[Feishu] Could not read OGG duration from %s", file_path, exc_info=True)
        return 0
