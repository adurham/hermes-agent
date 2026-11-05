"""Battery-aware process scheduling on macOS.

A Hermes backend spends most of its life blocked on a model API and then runs
short bursts of tool subprocesses. On Apple Silicon a task carrying the darwin
background policy is scheduled on the E-cluster only, and the same work costs
roughly a third of the energy — measured here as CPU rail 640 mW -> 453 mW on an
idle M4 Max. The catch is throughput: the identical CPU-bound loop ran **6.1x**
slower under the background policy (47.0M -> 7.7M iterations in 2 s), and the
clamp also throttles disk I/O and overrides per-thread QoS, so it applies to
every tool subprocess the backend spawns.

That is a real trade, not a free win, which is why this is **opt-in**: set
``power.battery_qos: true`` in config to enable it. It is macOS-only and a
no-op everywhere else.

Why ``taskpolicy`` rather than ``pthread_set_qos_class_self_np``: the pthread
call only retags the calling thread, leaving pool threads and future children at
full P-core QoS. ``taskpolicy -b`` sets the *task* policy, covering all threads
and everything the backend spawns.
"""

from __future__ import annotations

import logging
import os
import platform
import subprocess
from typing import Callable, Optional

logger = logging.getLogger(__name__)

#: Seconds between power-source checks. A laptop crossing from battery to AC
#: does not need to be noticed faster than this.
CHECK_INTERVAL_SEC = 60.0

#: How many consecutive probes must agree before the policy follows them. A
#: charger that drops for one tick should not re-policy the whole task.
FLIP_CONFIRMATIONS = 2

PMSET = "/usr/bin/pmset"
TASKPOLICY = "/usr/bin/taskpolicy"

Runner = Callable[[list], int]
Probe = Callable[[], Optional[bool]]


def battery_qos_enabled(config: Optional[dict]) -> bool:
    """Whether the caller asked for battery QoS. Pure; defaults to OFF."""
    if not isinstance(config, dict):
        return False
    power = config.get("power")
    if not isinstance(power, dict):
        return False
    return bool(power.get("battery_qos", False))


def parse_on_battery(pmset_output: str) -> Optional[bool]:
    """Tri-state: True on battery, False on AC, None when the payload is not
    one we understand (UPS, missing text, empty output)."""
    if not pmset_output:
        return None
    if "Battery Power" in pmset_output:
        return True
    if "AC Power" in pmset_output:
        return False
    return None


def is_on_battery() -> Optional[bool]:
    try:
        out = subprocess.run(
            [PMSET, "-g", "batt"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_on_battery(out.stdout or "")


def _run_taskpolicy(argv: list) -> int:
    return subprocess.run(argv, capture_output=True, timeout=5).returncode


def taskpolicy_argv(on_battery: bool, pid: Optional[int] = None) -> list:
    """The command that sets (on battery) or clears (AC) the policy."""
    target = os.getpid() if pid is None else pid
    return [TASKPOLICY, "-b" if on_battery else "-B", "-p", str(target)]


def apply(on_battery: bool, runner: Optional[Runner] = None, pid: Optional[int] = None) -> bool:
    """Set the task policy for the current power source. True if it took."""
    run = runner or _run_taskpolicy
    argv = taskpolicy_argv(on_battery, pid)
    try:
        rc = run(argv)
    except (OSError, subprocess.SubprocessError):
        logger.warning("battery qos: taskpolicy unavailable; leaving scheduling alone")
        return False
    if rc != 0:
        logger.warning("battery qos: taskpolicy exited %s for %s", rc, argv)
        return False
    return True


class _PowerWatch:
    """Keeps the task policy in step with the power source.

    A probe that cannot tell (``None``) never moves the policy: failing to read
    the charger must not be read as "on AC" and silently undo the setting.
    """

    def __init__(self, runner: Optional[Runner] = None, probe: Optional[Probe] = None):
        self._runner = runner
        self._probe = probe or is_on_battery
        self._current: Optional[bool] = None
        self._pending: Optional[bool] = None
        self._pending_count = 0

    def tick(self) -> bool:
        on_battery = self._probe()
        if on_battery is None:
            return True
        if on_battery == self._current:
            self._pending = None
            self._pending_count = 0
            return True
        if on_battery != self._pending:
            self._pending = on_battery
            self._pending_count = 1
            return True
        self._pending_count += 1
        if self._pending_count < FLIP_CONFIRMATIONS:
            return True
        if apply(on_battery, self._runner):
            self._current = on_battery
            self._pending = None
            self._pending_count = 0
            logger.info(
                "battery qos: %s",
                "background (E-cores) while on battery" if on_battery else "default (on AC)",
            )
        return True


def maybe_enable(
    config: Optional[dict] = None,
    runner: Optional[Runner] = None,
    probe: Optional[Probe] = None,
) -> bool:
    """Apply the policy now and keep it current. False when not applicable."""
    if platform.system() != "Darwin":
        return False
    if config is None:
        try:
            from hermes_cli.config import load_config

            config = load_config()
        except Exception:
            logger.debug("battery qos: config unavailable", exc_info=True)
            return False
    if not battery_qos_enabled(config):
        return False

    watch = _PowerWatch(runner=runner, probe=probe)
    watch.tick()
    watch.tick()  # the first flip needs two agreeing probes
    try:
        from agent.periodic_scheduler import schedule

        schedule(watch.tick, CHECK_INTERVAL_SEC)
    except Exception:
        logger.debug("battery qos: no scheduler; policy is fixed for this process", exc_info=True)
    return True
