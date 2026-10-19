"""GW-23 (#134889): per-profile browser scope edges left after CLI-9.

A multiplexed gateway serves every profile from one process; a live ``/browser connect`` endpoint,
a harness daemon's runtime and tmp dirs, and a browser session belong to ONE profile.
"""
import contextlib
import threading

import pytest

from agent import secret_scope
from gateway.run import _profile_runtime_scope
from hermes_constants import get_hermes_home, hermes_home_key
from tools import browser_tool_cdp as bt_cdp


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    monkeypatch.setattr(bt_cdp, "_live_overrides", {})
    return a, b


def _in_bare_thread(fn):
    """Run ``fn`` on a stdlib thread started WITHOUT the caller's copied context."""
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("v", fn()))
    t.start()
    t.join(5)
    return out["v"]


def test_a_bare_thread_on_a_multiplexed_gateway_never_resolves_the_launch_profiles_override(profiles, monkeypatch):
    """No profile scope bound on a multiplexed gateway: the launch profile's live endpoint is NOT the
    answer (``get_secret`` fails closed the same way). A bound scope still sees its own."""
    launch = get_hermes_home()
    with _profile_runtime_scope(launch, hydrate_secrets=False):
        bt_cdp.set_live_cdp_override("http://127.0.0.1:9401")
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    assert _in_bare_thread(bt_cdp.live_cdp_override) == ""
    assert _in_bare_thread(bt_cdp._get_cdp_override_raw) == ""
    with _profile_runtime_scope(launch, hydrate_secrets=False):
        assert bt_cdp.live_cdp_override() == "http://127.0.0.1:9401"


def test_single_profile_bare_thread_keeps_the_process_override(profiles):
    bt_cdp.set_live_cdp_override("http://127.0.0.1:9402")
    assert _in_bare_thread(bt_cdp.live_cdp_override) == "http://127.0.0.1:9402"
    assert hermes_home_key() in bt_cdp._live_overrides


# --- macOS AF_UNIX budget for a served profile's harness sockets -------------------------------------

_LONGEST_NAME = "n" * 64  # browser_harness/_ipc.py _NAME_RE: [A-Za-z0-9_-]{1,64}


def _sock_len(runtime_dir: str) -> int:
    """browser_harness/_ipc.py ``_sock_path``: ``<resolved BH_RUNTIME_DIR>/bu-<NAME>.sock`` when
    ``BH_RUNTIME_DIR_SHARED=1`` (pinned against the installed package by the test below)."""
    return len(f"{runtime_dir}/bu-{_LONGEST_NAME}.sock".encode())


def test_installed_harness_names_a_shared_dir_socket_bu_name_sock(tmp_path):
    """The length rule rests on the installed harness's stem: resolved dir + ``bu-<NAME>.sock``."""
    import os
    import subprocess
    import sys
    rt = tmp_path / "rt"
    env = {**os.environ, "BH_RUNTIME_DIR": str(rt), "BH_RUNTIME_DIR_SHARED": "1",
           "BH_TMP_DIR": str(tmp_path / "tmp"), "BH_HOME": str(tmp_path / "home")}
    out = subprocess.run([sys.executable, "-c",
                          f"from browser_harness import _ipc; print(_ipc._sock_path({_LONGEST_NAME!r}))"],
                         env=env, capture_output=True, text=True, check=True).stdout.strip()
    assert out == f"{rt.resolve()}/bu-{_LONGEST_NAME}.sock"


def test_a_served_profiles_harness_socket_fits_macos_sun_path_on_a_default_home(profiles, monkeypatch):
    """macOS ``sun_path`` is 104 bytes (103 + NUL). Under ``~/.config/browser-harness/runtime/p-<hash>``
    a served profile's socket left room for only ~31-character session names on a 9-letter account."""
    import os
    import sys
    from tools import browser_use_cli as bu_cli
    a, _b = profiles
    real = os.path.realpath
    # os-marker: ok — computed length on a fixed macOS-shaped HOME; the real branch runs in the macOS test below
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(os.path, "realpath",  # no-tmp: ok — macOS resolves the /tmp alias
                        lambda p, *args, **kw: "/private/tmp" if p == "/tmp" else real(p, *args, **kw))
    claimed = []
    monkeypatch.setattr(bu_cli, "_claim_private_dir", lambda path: claimed.append(path) or True, raising=False)
    env = {"HOME": "/Users/alexander"}
    with _profile_runtime_scope(a, hydrate_secrets=False):
        bu_cli._namespace_harness_daemons(env)
    assert env["BH_RUNTIME_DIR_SHARED"] == "1"
    assert _sock_len(env["BH_RUNTIME_DIR"]) <= 103, env["BH_RUNTIME_DIR"]
    assert claimed == [env["BH_RUNTIME_DIR"]]


@pytest.mark.platforms("macos")
def test_on_macos_a_served_profile_binds_a_64_char_session_socket(profiles, tmp_path):
    """Real branch: the dir is claimed under the real short root and a 64-character name binds."""
    import os
    import socket
    from tools import browser_use_cli as bu_cli
    a, _b = profiles
    env = {"HOME": str(tmp_path / ("u" * 40))}
    with _profile_runtime_scope(a, hydrate_secrets=False):
        bu_cli._namespace_harness_daemons(env)
    path = f"{os.path.realpath(env['BH_RUNTIME_DIR'])}/bu-{_LONGEST_NAME}.sock"
    assert len(path.encode()) <= 103
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.bind(path)
    finally:
        s.close()
        with contextlib.suppress(OSError):
            os.unlink(path)


def test_the_short_socket_dir_is_claimed_only_when_private_and_ours(tmp_path):
    """The short dir sits in a world-writable root: a symlink or another user's/open dir is refused."""
    from tools import browser_use_cli as bu_cli
    fresh = tmp_path / "hbh-fresh"
    assert bu_cli._claim_private_dir(str(fresh)) and (fresh.stat().st_mode & 0o777) == 0o700
    assert bu_cli._claim_private_dir(str(fresh))  # ours, 0700: reused
    open_dir = tmp_path / "hbh-open"
    open_dir.mkdir(mode=0o755)
    open_dir.chmod(0o755)
    assert not bu_cli._claim_private_dir(str(open_dir))
    link = tmp_path / "hbh-link"
    link.symlink_to(fresh)
    assert not bu_cli._claim_private_dir(str(link))


# --- per-profile harness tmp dir (screenshots, debug overlays, daemon logs) ---------------------------


def test_each_served_profile_gets_its_own_harness_tmp_dir(profiles, tmp_path):
    """browser_harness/_ipc.py ``_TMP`` (paths.tmp_dir: BH_TMP_DIR, else <home>/tmp) holds ``shot.png``,
    debug-click overlays and ``bu-<NAME>.log``; one shared dir let profile B read A's screenshots."""
    from tools import browser_use_cli as bu_cli
    a, b = profiles
    seen = {}
    for home in (a, b):
        env = {"BH_HOME": str(tmp_path / "bh")}
        with _profile_runtime_scope(home, hydrate_secrets=False):
            bu_cli._namespace_harness_daemons(env)
        assert env.get("BH_TMP_DIR_SHARED") == "1"  # several BU_NAMEs share it: keep bu-<name>.log stems
        seen[home] = env.get("BH_TMP_DIR")
    assert seen[a] and seen[b] and seen[a] != seen[b]
    assert all(str(p).startswith(str(tmp_path / "bh" / "tmp")) for p in seen.values())
    unscoped = {"BH_HOME": str(tmp_path / "bh")}
    bu_cli._namespace_harness_daemons(unscoped)
    assert unscoped == {"BH_HOME": str(tmp_path / "bh")}  # single-profile: unchanged


def test_a_served_profile_under_an_inherited_tmp_dir_nests_inside_it(profiles, tmp_path):
    from tools import browser_use_cli as bu_cli
    a, _b = profiles
    env = {"BH_TMP_DIR": str(tmp_path / "shared-tmp")}
    with _profile_runtime_scope(a, hydrate_secrets=False):
        bu_cli._namespace_harness_daemons(env)
    assert env["BH_TMP_DIR"].startswith(str(tmp_path / "shared-tmp") + "/p-")
    # The runtime dir still derives from the inherited BH_TMP_DIR (harness fallback), not the new one.
    assert env["BH_RUNTIME_DIR"].startswith(str(tmp_path / "shared-tmp") + "/p-")


def test_an_unscoped_shutdown_reload_reaches_the_daemon_with_its_own_tmp_dir(profiles, monkeypatch):
    """The reaper/shutdown runs outside any profile scope; its ``--reload`` must still carry the
    daemon's own tmp dir, or the harness writes that profile's log into the shared one."""
    from tools import browser_use_cli as bu_cli
    a, _b = profiles
    with _profile_runtime_scope(a, hydrate_secrets=False):
        env = {"HOME": "/home/u"}
        bu_cli._namespace_harness_daemons(env)
    key = (*(env.get(k, "") for k in bu_cli._HARNESS_DIR_KEYS), "s1")
    monkeypatch.setattr(bu_cli, "_driven_daemons", {key: hermes_home_key(a)})
    monkeypatch.setattr(bu_cli, "_find_cli", lambda: ["browser-harness"])
    seen = []
    monkeypatch.setattr(bu_cli, "_run_cli_killing_process_group", lambda cmd, code, e, t: seen.append(e))
    bu_cli.stop_harness_daemons()
    assert [(e["BH_TMP_DIR"], e["BH_TMP_DIR_SHARED"], e["BU_NAME"]) for e in seen] == [
        (env["BH_TMP_DIR"], "1", "s1")]


# --- reap/publish race within one profile -----------------------------------------------------------


def test_a_session_that_resolved_the_old_endpoint_during_a_connect_is_closed_not_kept(profiles, monkeypatch):
    """``/browser connect`` reaps, publishes the new endpoint, then reaps again. A tool call that
    resolved the OLD endpoint before the publish but inserted its session after the second reap kept
    driving the old browser. The real ``_browser_connect`` handler runs mid-creation here."""
    from tools import browser_tool, browser_tool_lifecycle as lifecycle, browser_tool_session as bt_session
    from tui_gateway import server  # binds methods_browser helpers onto server globals

    a, _b = profiles
    sessions = {}
    monkeypatch.setattr(browser_tool, "_active_sessions", sessions)
    monkeypatch.setattr(lifecycle, "_start_browser_cleanup_thread", lambda: None)
    monkeypatch.setattr(bt_cdp, "_ensure_cdp_supervisor", lambda key: None)
    monkeypatch.setattr(server, "_cdp_http_reachable", lambda parsed, timeout=2.0: True)
    closed = []
    monkeypatch.setattr(lifecycle, "_cleanup_single_browser_session",
                        lambda task_id: closed.append(sessions.pop(task_id, {}).get("cdp_url")))
    created = []

    def create(task_id, local):
        url = bt_cdp._get_cdp_override_raw()  # resolved before the connect publishes
        if not created:
            assert server._browser_connect("r1", {"url": "http://127.0.0.1:9502"})["result"]["connected"]
        created.append(url)
        return {"session_name": f"s{len(created)}", "cdp_url": url}
    monkeypatch.setattr(bt_session, "_create_session_for_key", create)

    with _profile_runtime_scope(a, hydrate_secrets=False):
        bt_cdp.set_live_cdp_override("http://127.0.0.1:9501")
        info = bt_session._get_session_info("task-a")
    assert info["cdp_url"] == "http://127.0.0.1:9502"
    assert sessions["task-a"] is info
    assert closed == ["http://127.0.0.1:9501"]


def test_another_profiles_connect_does_not_invalidate_this_profiles_new_session(profiles, monkeypatch):
    from tools import browser_tool, browser_tool_lifecycle as lifecycle, browser_tool_session as bt_session
    a, b = profiles
    sessions = {}
    monkeypatch.setattr(browser_tool, "_active_sessions", sessions)
    monkeypatch.setattr(lifecycle, "_start_browser_cleanup_thread", lambda: None)
    monkeypatch.setattr(bt_cdp, "_ensure_cdp_supervisor", lambda key: None)
    closed = []
    monkeypatch.setattr(lifecycle, "_cleanup_single_browser_session", closed.append)

    def create(task_id, local):
        with _profile_runtime_scope(b, hydrate_secrets=False):
            bt_cdp.set_live_cdp_override("http://127.0.0.1:9601")
        return {"session_name": "s", "cdp_url": "x"}
    monkeypatch.setattr(bt_session, "_create_session_for_key", create)
    with _profile_runtime_scope(a, hydrate_secrets=False):
        info = bt_session._get_session_info("task-a")
    assert sessions == {"task-a": info} and closed == []
