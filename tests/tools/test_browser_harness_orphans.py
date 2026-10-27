"""A browser-harness daemon must never outlive the Hermes process that owned it.

``browser_harness.admin.ensure_daemon`` starts the daemon DETACHED (``start_new_session=True``) and the
daemon has no idle exit at all, because the next CLI call is supposed to reuse its warm CDP connection.
Nothing in that contract ties the daemon to the Hermes process that owns the session, and Hermes kept its
"which daemons did I drive" bookkeeping in process memory only (``browser_use_cli._driven_daemons``). So
every exit that skipped ``stop_harness_daemons()`` — Ctrl-C, SIGKILL, a crashed gateway, a one-shot
``hermes -p`` run — left its daemons reparented to launchd/init (ppid=1), alive for the life of the
machine, one per BU_NAME, with no record anywhere of who owned them.

These tests exercise the real thing: a real owner process that spawns a real detached daemon process
listening on a real AF_UNIX socket, then dies; and the real ``~/.hermes`` ownership records. The
end-to-end test asserts behaviour (the daemon is gone after the next browser call), so it fails on the
leak itself rather than on a missing symbol.
"""
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psutil
import pytest

import tools.browser_use_cli as bu

# The ownership records live under the profile home, one file per daemon identity (dirs + BU_NAME).
RECORD_DIRNAME = Path("run") / "browser-harness"


def _record_path(identity, pid=None) -> Path:
    """Where ONE owning process keeps its claim on ``identity``'s daemon.

    This pins the on-disk contract: ``<HERMES_HOME>/run/browser-harness/<sha256(identity)[:16]>-<owner
    pid>.json``, one file per (daemon, owner) so two processes claiming the same daemon never race.
    """
    from hermes_constants import get_hermes_home
    digest = hashlib.sha256("\0".join(identity).encode()).hexdigest()[:16]
    return Path(get_hermes_home()) / RECORD_DIRNAME / f"{digest}-{os.getpid() if pid is None else pid}.json"


# ── fixtures ───────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def runtime(tmp_path):
    """A SHORT harness runtime dir: AF_UNIX ``sun_path`` is 104 bytes on macOS and pytest's tmp_path
    alone can exceed it, which would fail the bind for reasons that have nothing to do with the bug."""
    path = Path(tempfile.mkdtemp(dir="/tmp", prefix="hbo-"))  # no-tmp: ok — AF_UNIX sun_path budget
    (path / "runtime").mkdir()
    try:
        yield path / "runtime"
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(autouse=True)
def _fresh_sweep(monkeypatch):
    """The orphan sweep is throttled per process; every test needs it armed."""
    monkeypatch.setattr(bu, "_last_harness_orphan_sweep", 0.0)


@pytest.fixture(autouse=True)
def _fake_supervisor(monkeypatch):
    """browser_exec attaches the vault supervisor to the resolved endpoint; the fake one is no browser."""
    from tools import browser_supervisor

    class _Registry:
        def get_or_start(self, task_id, cdp_url, **kw):
            return None

    monkeypatch.setattr(browser_supervisor, "SUPERVISOR_REGISTRY", _Registry())


@pytest.fixture
def harness_scripts(tmp_path):
    """The three players, each a real process: the harness's path rules, a daemon, and its ``--reload``."""
    (tmp_path / "_fakepaths.py").write_text('''
import os
from pathlib import Path


def runtime_dir():
    """browser_harness.paths.runtime_dir() as browser_harness._ipc resolves it at import."""
    raw = os.environ.get("BH_RUNTIME_DIR") or os.environ.get("BH_TMP_DIR")
    if raw:
        return Path(raw).expanduser().resolve()
    home = os.environ.get("BH_HOME") or os.environ.get("BROWSER_HARNESS_HOME")
    if home:
        return Path(home).expanduser().resolve() / "runtime"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() / "browser-harness" if xdg else Path.home() / ".config" / "browser-harness"
    return base.resolve() / "runtime"


def stem():
    """browser_harness._ipc._runtime_stem(): bare "bu" when the runtime dir isolates one daemon."""
    name = os.environ.get("BU_NAME") or "default"
    if os.environ.get("BH_RUNTIME_DIR") and os.environ.get("BH_RUNTIME_DIR_SHARED") != "1":
        return "bu"
    return "bu-" + name


def endpoints():
    directory = runtime_dir()
    name = stem()
    return str(directory / (name + ".sock")), str(directory / (name + ".pid"))
''', encoding="utf-8")

    daemon = tmp_path / "fake_daemon.py"
    daemon.write_text('''
import json, os, socket, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _fakepaths import endpoints

sock_path, pid_path = endpoints()
os.makedirs(os.path.dirname(sock_path), exist_ok=True)
try:
    os.unlink(sock_path)
except FileNotFoundError:
    pass
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(sock_path)
server.listen(8)
with open(pid_path, "w") as fh:
    fh.write(str(os.getpid()))
while True:
    conn, _ = server.accept()
    try:
        data = b""
        while not data.endswith(b"\\n"):
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
        try:
            request = json.loads(data or b"{}")
        except ValueError:
            request = {}
        if isinstance(request, dict) and request.get("meta") == "shutdown":
            conn.sendall(b'{"ok": true}\\n')
            conn.close()
            for path in (sock_path, pid_path):
                try:
                    os.unlink(path)
                except OSError:
                    pass
            os._exit(0)
        conn.sendall(json.dumps({"pong": True, "pid": os.getpid()}).encode() + b"\\n")
    except OSError:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass
''', encoding="utf-8")

    # browser_harness.admin.restart_daemon() == what `--reload` does: meta:shutdown over the socket,
    # wait for the process, SIGTERM if it hangs, then unlink the socket and pid file.
    cli = tmp_path / "fake_run.py"
    cli.write_text('''
import json, os, socket, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _fakepaths import endpoints

argv = sys.argv[1:]
if argv != ["--reload"]:
    sys.stdin.read()
    print("ran the task")
    sys.exit(0)

sock_path, pid_path = endpoints()

if os.path.exists(sock_path):
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(2.0)
        client.connect(sock_path)
        client.sendall(b'{"meta": "shutdown"}\\n')
        client.recv(65536)
        client.close()
    except OSError:
        pass
# The daemon unlinks its own endpoint on a clean shutdown; wait for that, then clean up after a wedged one.
for _ in range(200):
    if not os.path.exists(pid_path):
        break
    time.sleep(0.005)
for path in (sock_path, pid_path):
    try:
        os.unlink(path)
    except OSError:
        pass
print("daemon stopped — will restart fresh on next call")
''', encoding="utf-8")

    # The Hermes process that owns a daemon and then dies without running any cleanup.
    owner = tmp_path / "fake_owner.py"
    owner.write_text('''
import hashlib, json, os, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _fakepaths import endpoints

record_dir, daemon_script, identity_json = sys.argv[1], sys.argv[2], sys.argv[3]
identity = json.loads(identity_json)
sock_path, pid_path = endpoints()
os.makedirs(os.path.dirname(sock_path), exist_ok=True)
# browser_harness.admin.ensure_daemon(): detached, stdin/out to devnull.
subprocess.Popen([sys.executable, daemon_script], stdin=subprocess.DEVNULL,
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
for _ in range(1000):
    if os.path.exists(pid_path):
        break
    time.sleep(0.01)
os.makedirs(record_dir, exist_ok=True)
digest = hashlib.sha256("\\0".join(identity).encode()).hexdigest()[:16]
record = {"version": 1, "identity": identity, "runtime_dir": os.path.dirname(sock_path),
          "stem": os.path.basename(sock_path)[:-len(".sock")], "owner_pid": os.getpid(),
          "owner_started": None, "updated": time.time()}
with open(os.path.join(record_dir, "%s-%d.json" % (digest, os.getpid())), "w") as fh:
    json.dump(record, fh)
print(os.getpid())
''', encoding="utf-8")
    return {"daemon": daemon, "cli": cli, "owner": owner, "dir": tmp_path}


def _alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def _identity(runtime: Path, name: str, tmp: Path):
    """``browser_harness_dirs.daemon_identity`` for a served-profile-style env (shared dirs)."""
    return [str(runtime), "1", str(tmp), "1", name]


def _age(path: Path, seconds: float) -> None:
    """Backdate ``path``'s mtime: the orphan grace is measured from the endpoint files' last touch."""
    when = time.time() - seconds
    os.utime(path, (when, when))


def _start_daemon(scripts, runtime: Path, name: str):
    """A detached harness daemon for ``name`` under ``runtime``, plus its (socket, pid file) paths."""
    env = dict(os.environ, BH_RUNTIME_DIR=str(runtime), BH_RUNTIME_DIR_SHARED="1",
               BH_TMP_DIR=str(scripts["dir"]), BU_NAME=name)
    daemon = subprocess.Popen([sys.executable, str(scripts["daemon"])], env=env,
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, start_new_session=True)
    sock, pid_file = runtime / f"bu-{name}.sock", runtime / f"bu-{name}.pid"
    for _ in range(1000):
        if sock.exists() and pid_file.exists():
            break
        time.sleep(0.01)
    return daemon, sock, pid_file


def _drive_exec(monkeypatch, scripts, session: str, cdp: str = "http://127.0.0.1:9400"):
    """A browser_exec that runs a fake CLI instead of a real browser.

    The browser config is stubbed through ``_read_browser_cfg`` — browser_use_cli's own seam — and never
    by patching ``hermes_cli.config.read_raw_config``: a module imported for the first time inside that
    patch window (``tools.browser_camofox`` binds the name at import) would keep the stub forever and
    hand its 30s default to every later test in this process.
    """
    monkeypatch.setattr(bu, "_find_cli", lambda: [sys.executable, str(scripts["cli"])])
    monkeypatch.setenv("BROWSER_CDP_URL", cdp)
    monkeypatch.setattr(bu, "_read_browser_cfg", lambda: {"backend": "browser-use"})
    monkeypatch.setattr("tools.browser_tool_cdp._resolve_cdp_override", lambda url: url)
    return json.loads(bu.browser_exec("print('payload')", session=session))


# ── the bug ───────────────────────────────────────────────────────────────────────────────────────
def test_daemon_does_not_outlive_the_process_that_owned_it(runtime, harness_scripts, monkeypatch):
    """The reported leak: a detached daemon whose owner died must be gone after the next browser call.

    Before the fix nothing recorded ownership and nothing swept: browser_exec below happily spawned
    another daemon and the ownerless one kept running until the machine rebooted.
    """
    scripts = harness_scripts
    identity = _identity(runtime, "stale", scripts["dir"])
    record_dir = _record_path(identity).parent

    env = dict(os.environ, BH_RUNTIME_DIR=str(runtime), BH_RUNTIME_DIR_SHARED="1",
               BH_TMP_DIR=str(scripts["dir"]), BU_NAME="stale")
    owner = subprocess.run([sys.executable, str(scripts["owner"]), str(record_dir), str(scripts["daemon"]),
                            json.dumps(identity)],
                           env=env, capture_output=True, text=True, timeout=120, check=True)
    owner_pid = int(owner.stdout.strip())
    record = _record_path(identity, owner_pid)
    pid_file = runtime / "bu-stale.pid"
    daemon_pid = int(pid_file.read_text())
    assert record.exists()
    assert _alive(daemon_pid)
    assert psutil.Process(daemon_pid).ppid() != owner_pid, "the daemon did not detach from its owner"

    result = _drive_exec(monkeypatch, scripts, session="fresh")

    assert result["success"], result
    assert not _alive(daemon_pid), "the browser-harness daemon outlived the Hermes process that owned it"
    assert not (runtime / "bu-stale.sock").exists(), "the dead daemon's socket was left behind"
    assert not pid_file.exists(), "the dead daemon's pid file was left behind"
    assert not record.exists(), "the ownership record should be gone with the daemon"


def test_the_call_that_drives_a_daemon_records_ownership(runtime, harness_scripts, monkeypatch):
    """The other half of the contract: whoever drives a daemon writes down that it owns it.

    ``_driven_daemons`` is memory-only, so this record is the only thing a later process can find.
    """
    scripts = harness_scripts
    monkeypatch.setattr(bu, "_base_subprocess_env",
                        lambda: {"BH_RUNTIME_DIR": str(runtime), "BH_RUNTIME_DIR_SHARED": "1",
                                 "BH_TMP_DIR": str(scripts["dir"]), "BH_TMP_DIR_SHARED": "1"})

    assert _drive_exec(monkeypatch, scripts, session="mine")["success"]

    identity = _identity(runtime, "mine", scripts["dir"])
    record = _record_path(identity)
    assert record.exists(), "driving a daemon left no ownership record"
    payload = json.loads(record.read_text())
    assert payload["identity"] == identity
    assert payload["stem"] == "bu-mine" and payload["runtime_dir"] == str(runtime.resolve())
    assert payload["owner_pid"] == os.getpid(), "the driving process is not recorded as an owner"
    assert payload["owner_started"] is not None, "no PID-reuse fingerprint: a recycled PID would look alive"


def test_a_leaked_daemon_from_before_the_fix_is_adopted_once_it_goes_stale(runtime, harness_scripts,
                                                                          monkeypatch):
    """Daemons leaked before this fix carry no record, so the claim sweep cannot see them. They are
    adopted only once their endpoint files have been untouched past the orphan grace: an upgraded Hermes
    must never kill a daemon a live pre-fix process is still using."""
    scripts = harness_scripts
    monkeypatch.setattr(bu, "_base_subprocess_env", lambda: {
        "BH_RUNTIME_DIR": str(runtime), "BH_RUNTIME_DIR_SHARED": "1",
        "BH_TMP_DIR": str(scripts["dir"]), "BH_TMP_DIR_SHARED": "1"})
    monkeypatch.setattr(bu, "_find_cli", lambda: [sys.executable, str(scripts["cli"])])
    daemon, sock, pid_file = _start_daemon(scripts, runtime, "legacy")
    try:
        assert bu.reap_orphaned_harness_daemons(force=True) == 0, "reaped a daemon that was still fresh"
        assert daemon.poll() is None

        _age(sock, 24 * 3600)
        _age(pid_file, 24 * 3600)
        assert bu.reap_orphaned_harness_daemons(force=True) == 1
        assert not _alive(daemon.pid), "a stale unrecorded daemon was left running"
        assert not sock.exists() and not pid_file.exists()
    finally:
        if daemon.poll() is None:
            daemon.kill()
        daemon.wait(timeout=10)


def test_the_machine_wide_harness_dir_is_never_adopted(runtime, monkeypatch):
    """Without a dir of its own a profile shares ``~/.config/browser-harness/runtime`` with every other
    process AND with the user's own plain ``browser-harness`` runs: nothing there proves ownership, so
    nothing there may be adopted (let alone killed)."""
    monkeypatch.setenv("HOME", str(runtime.parent))
    shared = runtime.parent / ".config" / "browser-harness" / "runtime"
    shared.mkdir(parents=True, exist_ok=True)
    (shared / "bu-someone-elses.pid").write_text("1")

    assert bu._legacy_harness_daemons(set(), {}) == []


# ── safety: never reap a daemon somebody is still using ──────────────────────────────────────────
def _write_record(path: Path, identity, runtime: Path, owner_pid: int, owner_started=None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "identity": list(identity), "runtime_dir": str(runtime),
                                "stem": "bu", "owner_pid": owner_pid,
                                "owner_started": owner_started, "updated": time.time()}), encoding="utf-8")


def test_a_live_owner_keeps_its_daemon(runtime, monkeypatch):
    """Two live Hermes processes can share one daemon; a daemon is only an orphan when ALL its owners are
    gone. Reaping on a single dead claim would kill a running session's browser."""
    identity = _identity(runtime, "shared", runtime)
    _write_record(_record_path(identity), identity, runtime, os.getpid(),
                  bu._process_start_time(os.getpid()))
    _write_record(_record_path(identity, 999999), identity, runtime, 999999)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(runtime / "bu.sock"))
    daemon = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        (runtime / "bu.pid").write_text(str(daemon.pid))
        monkeypatch.setattr(bu, "_find_cli", lambda: pytest.fail("reaped a daemon with a live owner"))

        assert bu.orphaned_harness_daemons() == []
        assert bu.reap_orphaned_harness_daemons(force=True) == 0
        assert _record_path(identity).exists()
        assert daemon.poll() is None, "a daemon with a live owner was killed"
    finally:
        server.close()
        daemon.kill()
        daemon.wait(timeout=10)


def test_a_recycled_pid_is_not_a_live_owner(runtime):
    """A stale claim whose PID number was reused by an unrelated process must still be reaped; the
    start-time fingerprint is what distinguishes them."""
    identity = _identity(runtime, "recycled", runtime)
    _write_record(_record_path(identity), identity, runtime, os.getpid(), 1)  # our PID, not our start

    assert [o["identity"] for o in bu.orphaned_harness_daemons()] == [tuple(identity)]


def test_an_unknown_or_corrupt_record_is_left_alone(runtime):
    """Fail safe: a record we cannot read is not evidence that anything should be killed."""
    identity = _identity(runtime, "garbled", runtime)
    record = _record_path(identity)
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text("{not json", encoding="utf-8")

    assert bu.orphaned_harness_daemons() == []
    assert record.exists()


# ── the leak as measured: a LIVE owner whose daemon never stops ────────────────────────────────────
# Measured on macOS: four ``browser_harness.daemon`` processes, ppid=1, hours old — and the gateway that
# spawned them (named in each daemon's env) was still alive. ppid=1 is the harness's normal shape (the CLI
# that runs ensure_daemon() exits after every call); the leak is that nothing in a LIVE process ever stops
# a daemon it drove. The janitor closed each Chromium session after the inactivity timeout, the daemon
# stayed attached to the dead CDP port, and ``stop_harness_daemons()`` only runs at shutdown or
# ``/browser connect``. One immortal daemon per BU_NAME, stacking for the life of the gateway.
@pytest.fixture
def _no_janitor_thread(monkeypatch):
    """browser_exec arms the janitor thread; the tests drive its tick by hand instead."""
    started = []
    monkeypatch.setattr("tools.browser_tool_lifecycle._start_browser_cleanup_thread", lambda: started.append(1))
    return started


def _served_env(runtime: Path, scripts) -> dict:
    return {"BH_RUNTIME_DIR": str(runtime), "BH_RUNTIME_DIR_SHARED": "1",
            "BH_TMP_DIR": str(scripts["dir"]), "BH_TMP_DIR_SHARED": "1"}


def test_an_idle_daemon_is_stopped_by_the_janitor_while_its_owner_lives(runtime, harness_scripts,
                                                                         monkeypatch, _no_janitor_thread):
    """The owner (this process) stays alive; the daemon it drove must not outlive the idle timeout."""
    from tools import browser_tool_lifecycle as lifecycle
    scripts = harness_scripts
    monkeypatch.setattr(bu, "_driven_daemons", {})
    monkeypatch.setattr(bu, "_base_subprocess_env", lambda: _served_env(runtime, scripts))
    daemon, sock, pid_file = _start_daemon(scripts, runtime, "idle")  # what ensure_daemon() left running
    try:
        assert _drive_exec(monkeypatch, scripts, session="idle")["success"]
        assert _no_janitor_thread, "browser_exec never armed the janitor that stops its idle daemon"

        lifecycle._cleanup_inactive_browser_sessions()  # a janitor tick right after the call: still warm
        assert daemon.poll() is None, "a daemon that was just used was stopped"

        later = time.monotonic() + 24 * 3600  # the gateway lives on; nobody browses for a day
        monkeypatch.setattr(bu, "_monotonic", lambda: later)
        lifecycle._cleanup_inactive_browser_sessions()

        daemon.wait(timeout=10)
        assert not sock.exists() and not pid_file.exists(), "the stopped daemon's endpoint was left behind"
        assert not bu._driven_daemons, "a stopped daemon is still tracked as driven"
    finally:
        if daemon.poll() is None:
            daemon.kill()
            daemon.wait(timeout=10)
            pytest.fail("the browser-harness daemon outlived its idle timeout under a live owner")


def test_a_daemon_mid_call_is_never_idle_stopped(runtime, harness_scripts, monkeypatch, _no_janitor_thread):
    """A browser_exec may run for up to 30 minutes; the janitor must not stop the daemon under it."""
    scripts = harness_scripts
    monkeypatch.setattr(bu, "_driven_daemons", {})
    identity = tuple(_identity(runtime, "busy", scripts["dir"]))
    monkeypatch.setattr(bu, "_reload_daemons", lambda daemons: pytest.fail(f"stopped a busy daemon: {daemons}"))
    with bu._track_daemon_call(identity, "home"):
        assert bu.stop_idle_harness_daemons(0, now=time.monotonic() + 24 * 3600) == []
    assert bu._driven_daemons == {identity: "home"}


def test_an_idle_stop_never_kills_a_daemon_another_live_process_still_owns(runtime, monkeypatch):
    """Two live Hermes processes can drive one daemon (same dirs + BU_NAME). This one going idle drops only
    its own claim; the daemon stays for the other owner, which may be mid-call."""
    identity = tuple(_identity(runtime, "shared-idle", runtime))
    from hermes_constants import hermes_home_key
    monkeypatch.setattr(bu, "_driven_daemons", {identity: hermes_home_key()})
    monkeypatch.setattr(bu, "_daemon_last_used", {identity: 0.0})
    mine = _record_path(identity)
    _write_record(mine, identity, runtime, os.getpid(), bu._process_start_time(os.getpid()))
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        _write_record(_record_path(identity, other.pid), identity, runtime, other.pid,
                      bu._process_start_time(other.pid))
        monkeypatch.setattr(bu, "_reload_daemons", lambda d: pytest.fail("stopped a daemon a live peer owns"))

        assert bu.stop_idle_harness_daemons(60, now=3600.0) == [identity]
        assert not mine.exists(), "this process's claim outlived its idle stop"
        assert _record_path(identity, other.pid).exists(), "the live peer's claim was dropped"
    finally:
        other.kill()
        other.wait(timeout=10)


def test_a_clean_exit_stops_driven_daemons_even_with_no_browser_session(monkeypatch):
    """atexit used to stop harness daemons only inside ``if _active_sessions:``. A CDP-override or BU-cloud
    exec never creates a session there, so a clean ``hermes`` exit left every daemon it drove running."""
    from tools import browser_tool, browser_tool_lifecycle as lifecycle
    identity = ("", "", "", "", "exit-me")
    stopped = []
    monkeypatch.setattr(bu, "_driven_daemons", {identity: "home"})
    monkeypatch.setattr(bu, "_reload_daemons", lambda daemons: stopped.extend(daemons))
    monkeypatch.setattr(browser_tool, "_cleanup_done", False)
    monkeypatch.setattr(browser_tool, "_active_sessions", {})
    monkeypatch.setattr(lifecycle, "_reap_orphaned_browser_sessions", lambda: None)
    monkeypatch.setattr(lifecycle, "_stop_all_lightpanda", lambda: None)
    monkeypatch.setattr(lifecycle._real_profile, "_terminate_real_profile_chrome", lambda: None)

    lifecycle._emergency_cleanup_all_sessions()

    assert stopped == [identity], "a clean exit left a driven harness daemon running"


# ── the idle gate is a DEAD Chrome, not a quiet agent ────────────────────────────────────────────
def _free_dead_port() -> int:
    """A loopback port nothing listens on (bound, read, released)."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def test_a_daemon_whose_chrome_is_still_live_is_never_idle_stopped(runtime, harness_scripts, monkeypatch,
                                                                     _no_janitor_thread):
    """An agent pausing for a long model turn is not done browsing: while the daemon's Chrome still listens,
    no amount of idle time stops it."""
    from tools import browser_tool_lifecycle as lifecycle
    scripts = harness_scripts
    monkeypatch.setattr(bu, "_driven_daemons", {})
    monkeypatch.setattr(bu, "_daemon_cdp", {})
    monkeypatch.setattr(bu, "_base_subprocess_env", lambda: _served_env(runtime, scripts))
    chrome = socket.socket()  # the live Chrome's CDP port
    chrome.bind(("127.0.0.1", 0))
    chrome.listen(8)
    cdp = f"http://127.0.0.1:{chrome.getsockname()[1]}"
    daemon, sock, pid_file = _start_daemon(scripts, runtime, "thinking")
    try:
        assert _drive_exec(monkeypatch, scripts, session="thinking", cdp=cdp)["success"]
        assert cdp in bu._daemon_cdp.values(), "the daemon's Chrome endpoint was not tracked"
        later = time.monotonic() + 6 * 3600  # far past idle_s, still under the 12 h live-Chrome ceiling
        monkeypatch.setattr(bu, "_monotonic", lambda: later)
        lifecycle._cleanup_inactive_browser_sessions()
        assert daemon.poll() is None, "a daemon whose Chrome is still live was stopped for being idle"
        assert sock.exists() and bu._driven_daemons, "a live-Chrome daemon stopped being tracked"
    finally:
        chrome.close()
        if daemon.poll() is None:
            daemon.kill()
        daemon.wait(timeout=10)


def test_a_daemon_whose_chrome_died_is_stopped_once_past_idle(runtime, harness_scripts, monkeypatch,
                                                               _no_janitor_thread):
    """The measured leak: the daemon outlives its Chrome (CDP port dead). Past ``idle_s`` it is stopped;
    before ``idle_s`` it is not (no churn around a Chrome restart)."""
    from tools import browser_tool_lifecycle as lifecycle
    scripts = harness_scripts
    monkeypatch.setattr(bu, "_driven_daemons", {})
    monkeypatch.setattr(bu, "_daemon_cdp", {})
    monkeypatch.setattr(bu, "_base_subprocess_env", lambda: _served_env(runtime, scripts))
    cdp = f"http://127.0.0.1:{_free_dead_port()}"
    daemon, sock, pid_file = _start_daemon(scripts, runtime, "orphaned-chrome")
    try:
        assert _drive_exec(monkeypatch, scripts, session="orphaned-chrome", cdp=cdp)["success"]
        assert bu._cdp_endpoint_alive(cdp) is False
        lifecycle._cleanup_inactive_browser_sessions()
        assert daemon.poll() is None, "stopped before idle_s: a Chrome restart would churn the daemon"

        later = time.monotonic() + 24 * 3600
        monkeypatch.setattr(bu, "_monotonic", lambda: later)
        lifecycle._cleanup_inactive_browser_sessions()
        daemon.wait(timeout=10)
        assert not sock.exists() and not pid_file.exists()
        assert not bu._driven_daemons and not bu._daemon_cdp
    finally:
        if daemon.poll() is None:
            daemon.kill()
            daemon.wait(timeout=10)
            pytest.fail("a daemon whose Chrome died outlived idle_s")


def _idle_driven(monkeypatch, name, cdp, last_used=0.0):
    identity = ("", "", "", "", name)
    monkeypatch.setattr(bu, "_driven_daemons", {identity: ""})
    monkeypatch.setattr(bu, "_daemon_cdp", {identity: cdp})
    monkeypatch.setattr(bu, "_daemon_last_used", {identity: last_used})
    monkeypatch.setattr(bu, "_daemon_calls_in_flight", {})
    monkeypatch.setattr(bu, "_other_live_owner", lambda key: False)
    stopped = []
    monkeypatch.setattr(bu, "_reload_daemons", lambda daemons: (stopped.extend(daemons), [])[1])
    return identity, stopped


def test_a_cdp_connect_timeout_is_unknown_and_never_stops_a_daemon(monkeypatch):
    """A loaded machine answers a CDP connect slowly: a TIMEOUT is not a dead Chrome."""
    import socket as _socket
    identity, stopped = _idle_driven(monkeypatch, "slow-chrome", "http://127.0.0.1:9222")

    def timeout(*_a, **_k):
        raise _socket.timeout("timed out")
    monkeypatch.setattr(_socket, "create_connection", timeout)
    assert bu._cdp_endpoint_alive("http://127.0.0.1:9222") is None
    assert bu.stop_idle_harness_daemons(60, now=3600.0) == []
    assert stopped == [] and identity in bu._driven_daemons

    def other(*_a, **_k):
        raise OSError(65, "No route to host")  # any non-refused OSError: unknown as well
    monkeypatch.setattr(_socket, "create_connection", other)
    assert bu._cdp_endpoint_alive("http://127.0.0.1:9222") is None
    assert bu.stop_idle_harness_daemons(60, now=3600.0) == [] and stopped == []


def test_a_refused_cdp_connect_is_dead_and_stops_the_daemon_past_idle(monkeypatch):
    import socket as _socket
    identity, stopped = _idle_driven(monkeypatch, "gone-chrome", "http://127.0.0.1:9222")

    def refused(*_a, **_k):
        raise ConnectionRefusedError(61, "Connection refused")
    monkeypatch.setattr(_socket, "create_connection", refused)
    assert bu._cdp_endpoint_alive("http://127.0.0.1:9222") is False
    assert bu.stop_idle_harness_daemons(60, now=30.0) == [], "stopped before idle_s"
    assert bu.stop_idle_harness_daemons(60, now=3600.0) == [identity]
    assert stopped == [identity]


def test_a_live_chrome_daemon_is_kept_past_idle_but_stopped_past_the_12h_ceiling(monkeypatch):
    """Alive Chrome: idle_s never stops the daemon, the long ceiling does (the daemon only, not Chrome)."""
    chrome = socket.socket()
    chrome.bind(("127.0.0.1", 0))
    chrome.listen(8)
    try:
        cdp = f"http://127.0.0.1:{chrome.getsockname()[1]}"
        identity, stopped = _idle_driven(monkeypatch, "live-chrome", cdp)
        assert bu._harness_daemon_max_idle_s() == 43200.0
        assert bu._cdp_endpoint_alive(cdp) is True
        assert bu.stop_idle_harness_daemons(60, now=11 * 3600.0) == [], "a live-Chrome daemon stopped early"
        assert stopped == []
        assert bu.stop_idle_harness_daemons(60, now=12 * 3600.0 + 1) == [identity]
        assert stopped == [identity]
        chrome.getsockname()  # Chrome's listener is untouched
    finally:
        chrome.close()


def test_the_live_chrome_ceiling_is_configurable_and_can_be_disabled(monkeypatch):
    chrome = socket.socket()
    chrome.bind(("127.0.0.1", 0))
    chrome.listen(8)
    try:
        cdp = f"http://127.0.0.1:{chrome.getsockname()[1]}"
        identity, stopped = _idle_driven(monkeypatch, "cfg-chrome", cdp)
        import hermes_cli.config as cfg
        monkeypatch.setattr(cfg, "read_raw_config_readonly",
                            lambda: {"browser": {"harness_daemon_max_idle_seconds": 0}})
        assert bu.stop_idle_harness_daemons(60, now=10 ** 7) == [] and stopped == []
        monkeypatch.setattr(cfg, "read_raw_config_readonly",
                            lambda: {"browser": {"harness_daemon_max_idle_seconds": 600}})
        assert bu.stop_idle_harness_daemons(60, now=601.0) == [identity]
    finally:
        chrome.close()


def test_the_live_chrome_ceiling_default_is_in_config_defaults():
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["browser"]["harness_daemon_max_idle_seconds"] == 43200


def test_unreadable_ownership_records_fail_safe_on_exit(runtime, monkeypatch):
    """Exit is claim-aware: if a peer's claim cannot be read (or the record dir cannot be listed), another
    owner cannot be ruled out, so the daemon is left running and only our claim is dropped."""
    identity = tuple(_identity(runtime, "garbled-peer", runtime))
    mine = _record_path(identity)
    _write_record(mine, identity, runtime, os.getpid(), bu._process_start_time(os.getpid()))
    peer = _record_path(identity, 999999)
    peer.write_text("{not json", encoding="utf-8")
    assert bu._other_live_owner(identity) is True

    stopped = []
    monkeypatch.setattr(bu, "_reload_daemons", lambda daemons: stopped.extend(daemons))
    monkeypatch.setattr(bu, "_driven_daemons", {identity: ""})
    bu.stop_harness_daemons(claim_aware=True)
    assert stopped == [] and not mine.exists() and peer.exists()

    peer.unlink()

    def unlistable(self, pattern):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(Path, "glob", unlistable)
    assert bu._other_live_owner(identity) is True


def test_a_killed_daemon_socket_is_waited_out_before_cleanup(tmp_path, monkeypatch):
    """After SIGKILL the socket may still accept for a moment (Darwin): wait (bounded) before unlinking."""
    path = Path(tempfile.mkdtemp(dir="/tmp", prefix="hbw-")) / "s.sock"  # no-tmp: ok — AF_UNIX sun_path
    try:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        listener.listen(8)
        t0 = time.monotonic()
        assert bu._wait_socket_released(path, 0.3) is False  # still held: bounded wait, then give up
        assert 0.25 <= time.monotonic() - t0 < 2.0
        import threading
        threading.Timer(0.2, listener.close).start()
        assert bu._wait_socket_released(path, 2.0) is True  # released mid-wait
        assert bu._wait_socket_released(path.with_name("absent.sock"), 1.0) is True
    finally:
        shutil.rmtree(path.parent, ignore_errors=True)


# ── exit is claim-aware, like the idle stop ──────────────────────────────────────────────────────
def test_exit_leaves_a_daemon_another_live_owner_still_claims(runtime, monkeypatch):
    """A clean exit of THIS process must not take down a daemon a live peer (same dirs + BU_NAME) drives:
    only our claim goes."""
    from hermes_constants import hermes_home_key
    from tools import browser_tool, browser_tool_lifecycle as lifecycle
    identity = tuple(_identity(runtime, "shared-exit", runtime))
    monkeypatch.setattr(bu, "_driven_daemons", {identity: hermes_home_key()})
    mine = _record_path(identity)
    _write_record(mine, identity, runtime, os.getpid(), bu._process_start_time(os.getpid()))
    stopped = []
    monkeypatch.setattr(bu, "_reload_daemons", lambda daemons: stopped.extend(daemons))
    monkeypatch.setattr(browser_tool, "_cleanup_done", False)
    monkeypatch.setattr(browser_tool, "_active_sessions", {})
    monkeypatch.setattr(lifecycle, "_reap_orphaned_browser_sessions", lambda: None)
    monkeypatch.setattr(lifecycle, "_stop_all_lightpanda", lambda: None)
    monkeypatch.setattr(lifecycle._real_profile, "_terminate_real_profile_chrome", lambda: None)
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        _write_record(_record_path(identity, other.pid), identity, runtime, other.pid,
                      bu._process_start_time(other.pid))

        lifecycle._emergency_cleanup_all_sessions()

        assert stopped == [], "exit stopped a daemon a live peer still drives"
        assert not mine.exists(), "this process's claim outlived its exit"
        assert _record_path(identity, other.pid).exists(), "the live peer's claim was dropped"
        assert not bu._driven_daemons
    finally:
        other.kill()
        other.wait(timeout=10)


# ── a wedged daemon is escalated, but only once its pid is proven ────────────────────────────────
_IGNORES_SIGTERM = ("import signal, sys, time\n"
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                    "open(sys.argv[1], 'w').write('ready')\n"
                    "time.sleep(120)\n")


def _wedged_setup(runtime, tmp_path, monkeypatch, name):
    hang = tmp_path / "hang_reload.py"
    hang.write_text("import time\ntime.sleep(120)\n", encoding="utf-8")  # --reload never returns
    monkeypatch.setattr(bu, "_find_cli", lambda: [sys.executable, str(hang)])
    monkeypatch.setattr(bu, "_base_subprocess_env", lambda: {
        "BH_RUNTIME_DIR": str(runtime), "BH_RUNTIME_DIR_SHARED": "1",
        "BH_TMP_DIR": str(tmp_path), "BH_TMP_DIR_SHARED": "1"})
    monkeypatch.setattr(bu, "_RELOAD_TIMEOUT_S", 1)
    monkeypatch.setattr(bu, "_KILL_GRACE_S", 1.0)
    monkeypatch.setattr(bu, "_reload_failures", {})
    sent = []
    real = bu._signal_pid
    monkeypatch.setattr(bu, "_signal_pid", lambda pid, sig: (sent.append((pid, sig)), real(pid, sig)))
    identity = (str(runtime), "1", str(tmp_path), "1", name)
    _write_record(_record_path(identity), identity, runtime, os.getpid(), bu._process_start_time(os.getpid()))
    return identity, runtime / f"bu-{name}.pid", sent


def _stubborn(tmp_path, tag):
    ready = tmp_path / f"{tag}.ready"
    proc = subprocess.Popen([sys.executable, "-c", _IGNORES_SIGTERM, str(ready)], start_new_session=True)
    for _ in range(1000):
        if ready.exists():
            break
        time.sleep(0.01)
    return proc


def test_a_wedged_daemon_is_sigtermed_then_sigkilled_after_two_failed_reloads(runtime, tmp_path, monkeypatch):
    import signal
    identity, pid_file, sent = _wedged_setup(runtime, tmp_path, monkeypatch, "wedged")
    daemon = _stubborn(tmp_path, "wedged")
    try:
        pid_file.write_text(str(daemon.pid))
        assert bu._reload_daemons([identity]) == [identity], "first failure should only be retried"
        assert sent == [] and daemon.poll() is None
        assert _record_path(identity).exists()

        waited = []
        real_wait = bu._wait_socket_released
        monkeypatch.setattr(bu, "_wait_socket_released",
                            lambda sock, budget: (waited.append((sock.name, budget, pid_file.exists())),
                                                  real_wait(sock, budget))[1])
        assert bu._reload_daemons([identity]) == []
        assert sent == [(daemon.pid, signal.SIGTERM), (daemon.pid, signal.SIGKILL)], sent
        assert waited == [("bu-wedged.sock", bu._POST_KILL_SOCKET_WAIT_S, True)], \
            "endpoint files were unlinked without waiting for the killed daemon's socket"
        daemon.wait(timeout=10)
        assert not _record_path(identity).exists(), "the killed daemon's record was kept"
        assert not pid_file.exists()
        assert identity not in bu._reload_failures
    finally:
        if daemon.poll() is None:
            daemon.kill()
        daemon.wait(timeout=10)


def test_a_successful_reload_resets_the_failure_count(runtime, tmp_path, monkeypatch):
    identity, pid_file, sent = _wedged_setup(runtime, tmp_path, monkeypatch, "flaky")
    hang = bu._find_cli()
    assert bu._reload_daemons([identity]) == [identity]
    monkeypatch.setattr(bu, "_find_cli", lambda: [sys.executable, "-c", "pass"])
    assert bu._reload_daemons([identity]) == []
    assert identity not in bu._reload_failures
    _write_record(_record_path(identity), identity, runtime, os.getpid(), bu._process_start_time(os.getpid()))
    monkeypatch.setattr(bu, "_find_cli", lambda: hang)
    assert bu._reload_daemons([identity]) == [identity], "a success did not reset the count"
    assert sent == []


def test_a_wedged_daemon_whose_pid_no_longer_matches_is_never_signalled(runtime, tmp_path, monkeypatch):
    """Between failures the pid file came to name a different process: identity unproven, so the record is
    dropped and NOTHING is signalled — neither the original pid nor the new one."""
    identity, pid_file, sent = _wedged_setup(runtime, tmp_path, monkeypatch, "moved")
    first = _stubborn(tmp_path, "first")
    stranger = _stubborn(tmp_path, "stranger")
    try:
        pid_file.write_text(str(first.pid))
        assert bu._reload_daemons([identity]) == [identity]
        pid_file.write_text(str(stranger.pid))  # e.g. a fresh daemon, or a recycled pid
        assert bu._reload_daemons([identity]) == []
        assert sent == [], f"signalled a pid not proven to be the daemon: {sent}"
        assert first.poll() is None and stranger.poll() is None
        assert not _record_path(identity).exists(), "the unprovable record was kept (retried forever)"
    finally:
        for proc in (first, stranger):
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)
