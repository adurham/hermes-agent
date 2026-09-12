import threading
import time
from unittest.mock import patch

from hermes_cli import banner, source_check


def test_prefetch_non_blocking():
    """prefetch_update_check() should return immediately without blocking."""
    banner._update_result = None
    banner._update_check_done = threading.Event()

    with patch.object(source_check, "check_for_updates", return_value={"behind": 5}):
        start = time.monotonic()
        banner.prefetch_update_check()
        assert time.monotonic() - start < 1.0
        banner._update_check_done.wait(timeout=5)
        assert banner._update_result == 5
