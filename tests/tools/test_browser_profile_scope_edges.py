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
