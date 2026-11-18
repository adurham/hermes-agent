"""Charge a managed worker — and everything it forks — to its own capped cgroup.

The gateway spawns each turn's worker as a plain child of ``hermes-gateway.service``
(``subprocess.Popen([sys.executable, '-m', 'agent.managed_worker'])``), so every byte the
worker or one of its tool children touches is charged to the *gateway's* ``MemoryMax``.
One runaway allocation then puts the whole unit into memcg OOM: the kernel kills the biggest
process, the group is still over its cap, and the next largest member is the main ``hermes``
process itself. ``KillMode=mixed`` then tears the cgroup down and every concurrent card dies
with it (2026-11-17: three such deaths in seven days, one child at 23 GB).

The worker adopts *itself* into a transient scope instead of being spawned into one by the
gateway, because the gateway's spawn path only changes on restart and a gateway restart is
the outage this module exists to remove. ``StartTransientUnit`` with a ``PIDs`` property
migrates the calling process into a new cgroup under ``hermes-workers.slice`` with its own
``MemoryMax``/``MemoryHigh``; the process keeps its pid, session and stdio pipes, so the
owner's framing protocol and its own group-kill teardown keep working. Children forked after
adoption inherit the scope, and the scope's charge is never counted against the gateway.

Adoption is best-effort and never fails a turn: outside a systemd service (desktop app, CLI,
tests) there is nothing to isolate from, so the worker simply keeps running where it was.
"""
from __future__ import annotations

import os
import secrets
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

#: Transient slice every adopted worker lands in. Defined by a user unit of the same name
#: (``~/.config/systemd/user/hermes-workers.slice``) so the *aggregate* is bounded too, not
#: just each worker: with ``kanban.max_in_progress`` workers at 6 GiB apiece, one shared
#: MemoryMax is what keeps a burst of cards from exhausting host RAM.
DEFAULT_SLICE = "hermes-workers.slice"
#: Hard per-worker cap. A single runaway child reached 23 GiB in production, so this is the
#: number that decides how much a single bug may take before it dies alone.
DEFAULT_MEMORY_MAX_MB = 6 * 1024
#: No swap for workers: at ``memory.max`` an allocation must be *killed*, not spilled.
#: ``memory.high`` is deliberately not set by default — with swap exhausted it converts a
#: runaway into an indefinite reclaim throttle (the allocating task sleeps instead of dying,
#: and the card neither finishes nor frees the memory). ``HERMES_WORKER_MEMORY_HIGH_MB``
#: re-enables that soft throttle for hosts with swap headroom to spare.
#: Workers die before the gateway. The kernel refuses to *lower* oom_score_adj without
#: CAP_SYS_RESOURCE, so raising the worker's is the direction an unprivileged manager can
#: express; the gateway stays at whatever its unit sets.
DEFAULT_OOM_SCORE_ADJ = 500
#: A worker that has not shown up in its scope by now is not going to (systemd migrates the
#: pid asynchronously, in practice within ~200 ms).
ADOPT_DEADLINE_SECONDS = 3.0

_MIB = 1024 * 1024
_OFF_WORDS = frozenset({"0", "off", "no", "false", "disabled", "none"})


def _positive_int(value: Optional[str]) -> Optional[int]:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _physical_bytes() -> Optional[int]:
    try:
        return int(os.sysconf("SC_PAGE_SIZE")) * int(os.sysconf("SC_PHYS_PAGES"))
    except (OSError, ValueError, AttributeError):
        return None


def own_cgroup_path() -> str:
    """This process's cgroup-v2 path relative to ``/sys/fs/cgroup`` ('' when unreadable)."""
    try:
        for line in Path("/proc/self/cgroup").read_text(encoding="utf-8-sig").splitlines():
            if line.startswith("0::"):
                return line.partition("::")[2].rstrip("/")
    except OSError:
        pass
    return ""


def systemd_user_slice(path: str) -> str:
    """The slice a unit path lives in (``.../app.slice/hermes-gateway.service`` → ``app.slice``)."""
    parts = [part for part in path.split("/") if part]
    return parts[-2] if len(parts) > 1 and parts[-2].endswith(".slice") else ""


def _bus_env() -> dict:
    """Environment that can reach this user's systemd manager.

    A systemd-started gateway does not necessarily carry the session bus variables, so they
    are derived from ``/run/user/<uid>/bus`` exactly as ``tools.process_registry`` does for
    its ``systemd-run`` spawns. ``os.environ`` is left untouched.
    """
    env = dict(os.environ)
    configured = env.get("XDG_RUNTIME_DIR")
    runtime_dir = Path(configured) if configured else Path(f"/run/user/{os.getuid()}")
    try:
        metadata = runtime_dir.lstat()
        bus = runtime_dir / "bus"
        bus_metadata = bus.lstat()
    except OSError:
        return env
    if not (stat.S_ISDIR(metadata.st_mode) and metadata.st_uid == os.getuid()
            and metadata.st_mode & 0o022 == 0 and stat.S_ISSOCK(bus_metadata.st_mode)
            and bus_metadata.st_uid == os.getuid()):
        return env
    env["XDG_RUNTIME_DIR"] = str(runtime_dir)
    env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
    return env


def plan(*, environ: Optional[dict] = None, physical: Optional[int] = None) -> dict:
    """Resolve the caps one worker should run under, or ``{'enabled': False, reason}``.

    ``HERMES_WORKER_CGROUP=0`` disables adoption (a unit-test/desktop escape hatch).
    ``HERMES_WORKER_SLICE``, ``HERMES_WORKER_MEMORY_MAX_MB``, ``HERMES_WORKER_MEMORY_HIGH_MB``
    and ``HERMES_WORKER_OOM_SCORE_ADJ`` tighten/shape the defaults; a request is capped at a
    quarter of physical RAM so the same code cannot reserve 6 GiB on a 4 GiB box.
    """
    env = os.environ if environ is None else environ
    if str(env.get("HERMES_WORKER_CGROUP", "")).strip().lower() in _OFF_WORDS:
        return {"enabled": False, "reason": "disabled by HERMES_WORKER_CGROUP"}

    requested_max = _positive_int(env.get("HERMES_WORKER_MEMORY_MAX_MB"))
    requested_high = _positive_int(env.get("HERMES_WORKER_MEMORY_HIGH_MB"))
    memory_max = (requested_max or DEFAULT_MEMORY_MAX_MB) * _MIB
    host_cap = _physical_bytes() if physical is None else physical
    if host_cap:
        memory_max = min(memory_max, max(512 * _MIB, host_cap // 4))
    memory_high = min(requested_high * _MIB, memory_max) if requested_high else None

    adj = _positive_int(env.get("HERMES_WORKER_OOM_SCORE_ADJ"))
    return {
        "enabled": True,
        "slice": (str(env.get("HERMES_WORKER_SLICE", "")).strip() or DEFAULT_SLICE),
        "memory_max": memory_max,
        "memory_high": memory_high,
        "oom_score_adj": min(DEFAULT_OOM_SCORE_ADJ if adj is None else adj, 1000),
    }


def unit_name(pid: Optional[int] = None) -> str:
    """Transient scope name for one worker: recognisable, unique, collision-free.

    Deliberately not the ``hermes-worker-kanban-<task>-run-<run>`` name the dispatcher uses
    for its client scope: two units cannot share a name, and the client scope is created
    before the worker that would want it.
    """
    return f"hermes-worker-managed-{os.getpid() if pid is None else pid}-{secrets.token_hex(3)}.scope"


def _start_transient_unit_argv(busctl: str, *, unit: str, settings: dict, pid: int) -> list[str]:
    """``busctl`` call that starts *unit* as a transient scope owning *pid*.

    Property encoding is busctl's: the count of structs, then ``name``, ``type``, value per
    property (arrays carry their own length). ``Slice=`` decides the cgroup parent at
    creation; ``CollectMode=inactive-or-failed`` keeps the user manager from accumulating one
    dead scope per turn. ``MemorySwapMax=0`` makes ``MemoryMax`` the kill line rather than a
    spill line, so a runaway allocation dies at its cap instead of pushing host swap further
    into exhaustion (this host runs 7 GiB of swap, permanently full).
    """
    properties: list[tuple[str, str, str]] = [
        ("Description", "s", f"Hermes managed worker {pid}"),
        ("Slice", "s", str(settings["slice"])),
        ("PIDs", "au", str(pid)),
        ("MemoryAccounting", "b", "true"),
        ("MemoryMax", "t", str(int(settings["memory_max"]))),
        ("MemorySwapMax", "t", "0"),
        # Without this, an OOM kill of one tool child marks the scope failed and systemd
        # stops the unit -- taking the worker with it (systemd's default OOMPolicy=stop).
        # ``continue`` is the whole point: the runaway dies, the worker survives to report it.
        ("OOMPolicy", "s", "continue"),
        ("CollectMode", "s", "inactive-or-failed"),
    ]
    if settings.get("memory_high"):
        properties.append(("MemoryHigh", "t", str(int(settings["memory_high"]))))
    flat: list[str] = [str(len(properties))]
    for name, kind, value in properties:
        flat.extend((name, kind))
        if kind == "au":
            flat.extend(("1", value))
        else:
            flat.append(value)
    return [
        busctl, "--user", "call",
        "org.freedesktop.systemd1", "/org/freedesktop/systemd1",
        "org.freedesktop.systemd1.Manager", "StartTransientUnit",
        "ssa(sv)a(sa(sv))", unit, "fail",
        *flat,
        "0",
    ]


def _set_oom_score_adj(value: int) -> Optional[int]:
    """Raise this process's OOM score adjustment; returns the value the kernel kept.

    Raising is always permitted; the kernel only refuses to go *down* without
    CAP_SYS_RESOURCE, so an operator-set value above ours is left alone.
    """
    try:
        current = int(Path("/proc/self/oom_score_adj").read_text(encoding="utf-8-sig").strip() or 0)
        if current >= value:
            return current
        Path("/proc/self/oom_score_adj").write_text(str(int(value)), encoding="utf-8")
        return int(Path("/proc/self/oom_score_adj").read_text(encoding="utf-8-sig").strip() or value)
    except (OSError, ValueError):
        return None


def _wait_for_scope(unit: str, deadline: float) -> str:
    """Poll until this process's cgroup names *unit*; return the cgroup path ('' on timeout)."""
    stop = time.monotonic() + deadline
    while True:
        path = own_cgroup_path()
        if path.endswith("/" + unit):
            return path
        if time.monotonic() >= stop:
            return ""
        time.sleep(0.05)


def _memory_max_of(path: str) -> Optional[int]:
    try:
        raw = (Path("/sys/fs/cgroup") / path.lstrip("/") / "memory.max").read_text(encoding="utf-8-sig")
        value = int(raw.strip())
    except (OSError, ValueError):
        return None
    return value if 0 < value < (1 << 62) else None


#: cgroup-v2 mount point. A module constant so a test can point the reader at a fake hierarchy.
CGROUP_ROOT = Path("/sys/fs/cgroup")
#: The counters ``memory.events`` states about one cgroup. ``oom_kill`` is the one that answers
#: "did memory kill this process"; ``max`` counts the times it sat at its hard cap.
MEMORY_COUNTERS = ("low", "high", "max", "oom", "oom_kill", "oom_group_kill")


def _cgroup_of(pid: Optional[int]) -> str:
    """The unified cgroup path of *pid* (or this process); ``""`` when it cannot be read.

    A process the kernel has killed and nobody has reaped yet is a zombie, and a zombie still
    has ``/proc/<pid>/cgroup`` — which is exactly the moment this is needed.
    """
    source = "/proc/self/cgroup" if pid is None else f"/proc/{int(pid)}/cgroup"
    try:
        for line in Path(source).read_text(encoding="utf-8-sig").splitlines():
            if line.startswith("0::"):
                return line.partition("::")[2].rstrip("/")
    except (OSError, TypeError, ValueError):
        pass
    return ""


def scope_memory(pid: Optional[int] = None, *, root: Optional[Path] = None) -> dict:
    """What a lost worker's own cgroup says about its death — for a run record, not a decision.

    An OOM-killed worker cannot write its own epitaph: the owner sees a closed pipe
    (``EOFError``) and the board books a crash that reads like a transport failure. This
    returns the cgroup's own counters so a loss can be recorded as what it was:
    ``oom_kill=1``, a peak that reached the cap, or (the common case) neither.

    The reading is best-effort by contract — a vanished unit, a v1 host or a permission
    problem returns ``{}`` and costs the evidence, never the caller. ``pid`` may be a zombie
    (that is the usual case: the owner notices the pipe before it reaps the child), and the
    cgroup it names keeps existing while any sibling process is still charged to it.
    """
    path = _cgroup_of(pid)
    if not path:
        return {}
    base = Path(root if root is not None else CGROUP_ROOT) / path.lstrip("/")

    def number(name: str) -> Optional[int]:
        try:
            raw = (base / name).read_text(encoding="utf-8-sig").strip()
        except OSError:
            return None
        return int(raw) if raw.isdigit() else None

    events: dict = {}
    try:
        for line in (base / "memory.events").read_text(encoding="utf-8-sig").splitlines():
            key, _, value = line.partition(" ")
            if key in MEMORY_COUNTERS and value.strip().isdigit():
                events[key] = int(value.strip())
    except OSError:
        pass
    report = {
        "path": path,
        "max": number("memory.max"),
        "current": number("memory.current"),
        "peak": number("memory.peak"),
        "events": events,
    }
    return report if any(report[key] is not None for key in ("max", "current", "peak")) or events else {}


def adopt_worker_cgroup(settings: Optional[dict] = None, *, busctl: Optional[str] = None,
                        deadline: float = ADOPT_DEADLINE_SECONDS) -> dict:
    """Move this process into its own memory-capped transient scope.

    Returns ``{"status": "adopted"|"skipped"|"failed", ...}`` and never raises for a host
    condition: a worker denied isolation must still run its turn (it is only as safe as it
    was before). Every caller logs the status, so a host that silently lost its user bus
    shows up in ``managed-worker.log`` instead of quietly charging the gateway again.
    """
    if sys.platform != "linux":
        return {"status": "skipped", "reason": "not linux"}
    settings = plan() if settings is None else settings
    if not settings.get("enabled"):
        return {"status": "skipped", "reason": settings.get("reason") or "disabled"}

    path = own_cgroup_path()
    if not path:
        return {"status": "skipped", "reason": "no cgroup-v2 hierarchy"}
    unit = path.rsplit("/", 1)[-1]
    if unit.startswith("hermes-worker-"):
        return {"status": "skipped", "reason": f"already in {unit}", "path": path}
    if not unit.endswith(".service"):
        return {"status": "skipped", "reason": f"not inside a systemd service ({unit or 'root cgroup'})",
                "path": path}

    binary = busctl if busctl is not None else shutil.which("busctl")
    if binary is None:
        return {"status": "skipped", "reason": "busctl not found", "path": path}

    name = unit_name()
    argv = _start_transient_unit_argv(binary, unit=name, settings=settings, pid=os.getpid())
    env = _bus_env()
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=10, env=env, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "failed", "reason": f"{type(exc).__name__}: {exc}", "unit": name, "path": path}
    if done.returncode != 0:
        detail = (done.stderr or b"").decode("utf-8", "replace").strip() or f"busctl exited {done.returncode}"
        return {"status": "failed", "reason": detail[:400], "unit": name, "path": path}

    adopted = _wait_for_scope(name, deadline)
    if not adopted:
        return {"status": "failed", "reason": "scope accepted but this pid was never migrated",
                "unit": name, "path": path}
    adj = _set_oom_score_adj(int(settings["oom_score_adj"]))
    return {
        "status": "adopted",
        "unit": name,
        "path": adopted,
        "slice": settings["slice"],
        "memory_max": _memory_max_of(adopted) or int(settings["memory_max"]),
        "memory_high": settings.get("memory_high"),
        "oom_score_adj": adj,
    }
