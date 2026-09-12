"""Shims to stop the old updater doing work until relaunch."""

import sys
from typing import NoReturn


def stop_for_relaunch(*, incomplete: bool = False) -> NoReturn:
    """Do not return: old callers would fall back to pip or claim completion."""
    command = "hermes update" if incomplete else "hermes"
    print(
        "You're updating from an older version of Hermes Agent. "
        f"To complete this update, run `{command}` again.",
        file=sys.stderr,
    )
    raise SystemExit(1 if incomplete else 0)
