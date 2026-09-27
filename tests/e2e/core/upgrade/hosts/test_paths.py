"""Install, update, gateway and a turn on homes whose paths are not plain ASCII.

Failure class: path handling. Users run Hermes from homes like ``/home/José Müller`` or a
CJK-named directory, and keep ``~/.hermes`` (or just its ``tools`` store) on another disk behind a
symlink. Every path the installer and updater write (clone target, uv/python/node store, PM
generation, the ``~/.local/bin/hermes`` launcher, the rc-file PATH line, sys.path) must survive a
space, non-ASCII bytes and a symlink hop, and must stay stable from one launch to the next.

Two real installs through HEAD's ``scripts/install.sh``:

* ``odd_home``: the whole sandbox (HOME, HERMES_HOME, TMPDIR) lives under
  ``José Müller 漢字 dir``. Then a one-shot turn, a new login shell resolving ``hermes``, an
  upstream release and ``hermes update``, a turn on the new commit and ``hermes gateway run``.
* ``linked_home``: ``~/.hermes`` is a symlink to a directory on "another volume" (a path with a
  space and a non-ASCII char); plain launches through it must not re-run the source-update
  completion. An orchestrator-style per-task HERMES_HOME reaches the same ``tools``/``installs`` through
  symlinks, which must not change the PM runtime's identity either (gated on #123798).
"""

from __future__ import annotations

import os
import shutil

import pytest

from tests.e2e.core._pending_fixes import known_failure
from tests.e2e.core.upgrade import _helpers as H
from tests.e2e.core.upgrade import _install_helpers as I
from tests.e2e.core.upgrade.hosts import _hosts as X
from tests.fakes.fake_llm_provider import FakeLLMServer

pytestmark = [
    pytest.mark.platforms("linux"),
    pytest.mark.live_system_guard_bypass,
    pytest.mark.skipif(H.sandbox_required_reason() is not None, reason=str(H.sandbox_required_reason())),
    pytest.mark.skipif(shutil.which("git") is None, reason="git required"),
    pytest.mark.skipif(I.real_uv() is None, reason="uv required"),
]

ODD_DIR = "José Müller 漢字 dir"
VOLUME_DIR = "external volume ü"


@pytest.fixture(scope="module")
def provider():
    with FakeLLMServer(default_text="fake reply for the host-paths suite") as srv:
        yield srv


@pytest.fixture(scope="module")
def odd_home(tmp_path_factory, provider):
    root = tmp_path_factory.mktemp("paths-odd")
    origin = X.make_origin(root)
    sb = X.new_sandbox(root / ODD_DIR, origin)
    (sb.home / ".bashrc").write_text("# ~/.bashrc\ncase $- in *i*) ;; *) return;; esac\n", encoding="utf-8")
    (sb.home / ".profile").write_text('if [ -f "$HOME/.bashrc" ]; then . "$HOME/.bashrc"; fi\n', encoding="utf-8")
    first = I.run_installer(sb)
    return {"sb": sb, "origin": origin, "root": root, "install": first}


@pytest.fixture(scope="module")
def linked_home(tmp_path_factory, provider):
    root = tmp_path_factory.mktemp("paths-linked")
    origin = X.make_origin(root)
    sb = X.new_sandbox(root / "sb", origin)
    real = root / "sb" / VOLUME_DIR / "hermes-data"
    real.mkdir(parents=True)
    os.symlink(real, sb.hermes_home)
    first = I.run_installer(sb)
    return {"sb": sb, "origin": origin, "root": root, "real": real, "install": first}


def _pending_markers(sb: I.Sandbox) -> list[str]:
    return sorted(str(p) for p in (sb.hermes_home / "installs").glob("*/source-completion-pending"))


def test_install_update_gateway_and_turn_under_a_non_ascii_spaced_home(odd_home, provider):
    sb, first = odd_home["sb"], odd_home["install"]
    assert ODD_DIR in str(sb.home), "harness: sandbox HOME is not under the odd directory"
    assert first.returncode == 0, "install.sh failed under a non-ASCII, spaced HOME:\n" + I.describe(first)
    assert I.git("rev-parse", "HEAD", cwd=sb.checkout) == I.head_sha(), "installed checkout is not the published commit"
    X.configure(sb, provider)
    t1 = X.turn(sb, provider, "first turn under a non-ASCII home")
    assert not X.reran_completion(t1), "the first launch after install re-ran the completion:\n" + I.describe(t1)

    # A new login shell finds the launcher through the rc line the installer wrote.
    probe = X.login_shell(sb, "command -v hermes; hermes --version >/dev/null && echo LAUNCH-OK")
    assert probe.returncode == 0, H.describe(probe)
    lines = probe.stdout.strip().splitlines()
    assert lines and lines[0] == sb.hermes and "LAUNCH-OK" in lines, (
        f"a new login shell does not run the installed launcher (want {sb.hermes}):\n" + H.describe(probe))

    # Every sys.path entry of the selected interpreter names a real location.
    seen = X.interpreter_paths(sb)
    assert not X.missing_entries(seen["sys_path"]), f"sys.path names locations that do not exist: {X.missing_entries(seen['sys_path'])}"
    assert os.path.exists(seen["executable"]), f"sys.executable does not exist: {seen['executable']!r}"

    target = I.publish_commit(odd_home["origin"], odd_home["root"], "release: host-paths bump",
                              {"docs/e2e-host-paths-marker.txt": "release 1\n"})
    up = sb.cli("update", "--yes", "--branch", "main", timeout=X.UPDATE_TIMEOUT)
    assert up.returncode == 0 and I.TRACEBACK not in up.stdout + up.stderr, (
        "hermes update failed under a non-ASCII, spaced HOME:\n" + I.describe(up))
    assert I.git("rev-parse", "HEAD", cwd=sb.checkout) == target, "update exited 0 but HEAD is not the new release"
    t2 = X.turn(sb, provider, "turn after updating under a non-ASCII home")
    assert not X.reran_completion(t2), "the first launch after a finished update re-ran the completion:\n" + I.describe(t2)
    seen = X.interpreter_paths(sb)
    assert not X.missing_entries(seen["sys_path"]), f"after update, sys.path names missing locations: {X.missing_entries(seen['sys_path'])}"

    gw = X.Gateway(sb)
    try:
        st = gw.start()
        assert st.get("pid") and gw.proc.poll() is None, f"gateway state says running but the process is gone: {st}\n{gw.tail()}"
    finally:
        gw.stop()


def test_per_task_home_sharing_the_tools_store_by_symlink_is_current(linked_home, provider):
    """An orchestrator's per-task HERMES_HOME whose ``tools``/``installs`` link back to the main home.

    Precondition (not gated): the main home is itself a symlink, and plain launches through it are
    current; only the per-task home's launch is the #123798 gap.
    """
    sb, first = linked_home["sb"], linked_home["install"]
    assert sb.hermes_home.is_symlink(), "harness: ~/.hermes is not a symlink"
    assert first.returncode == 0, "install.sh failed with ~/.hermes behind a symlink:\n" + I.describe(first)
    assert (linked_home["real"] / "hermes-agent" / ".git").exists(), "install did not land on the symlink's target"
    X.configure(sb, provider)
    alt = linked_home["root"] / "per-task home ü"
    alt.mkdir()
    for name in ("tools", "installs"):
        os.symlink(sb.hermes_home / name, alt / name)
    for name in ("config.yaml", ".env"):
        shutil.copy(sb.hermes_home / name, alt / name)
    launches = [X.turn(sb, provider, f"turn {i} through the symlinked main home") for i in range(2)]
    reruns = [i for i, cp in enumerate(launches) if X.reran_completion(cp)]
    assert not reruns, (f"plain launches {reruns} through a symlinked ~/.hermes re-ran the source-update completion:\n"
                        + I.describe(launches[reruns[0]]))
    assert not _pending_markers(sb), f"a plain launch left source-completion-pending markers: {_pending_markers(sb)}"
    task = X.turn(sb, provider, "turn in a per-task home", env=dict(sb.env, HERMES_HOME=str(alt)))
    with known_failure(r"per-task HERMES_HOME .* re-ran the source-update completion",
                       "gated on #123798: store_root() returns the unresolved $HERMES_HOME/tools, so the same "
                       "store through a symlink is a different PM runtime identity"):
        assert not X.reran_completion(task), (
            "a per-task HERMES_HOME whose tools/installs symlink to the main home re-ran the source-update "
            "completion on a current install:\n" + I.describe(task))
