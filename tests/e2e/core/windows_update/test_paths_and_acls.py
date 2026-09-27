"""A Windows user profile path with non-ASCII characters and a space, and tool ACLs.

Failure class: paths and ACLs.

* ``C:\\Users\\Jörg Ñúñez`` is an ordinary Windows account. ``install.ps1`` must get
  through the uv ``python-deps`` stage there (#124526), and ``hermes`` must then work and
  update from that profile.
* After ``hermes update`` the managed toolchain under ``%LOCALAPPDATA%\\hermes\\tools``
  must still be executable by a non-elevated process: a logon Scheduled Task or Startup
  entry runs with a standard-user token even when the updating session was elevated
  (#122935). The runner session is elevated, so each tool is launched with a Basic User
  (SAFER_LEVELID_NORMALUSER) token, the same token ``runas /trustlevel:0x20000`` uses.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.e2e.core._pending_fixes import known_gate
from tests.e2e.core.windows_update._machine import (
    REQUIRES_OPT_IN,
    Journey,
    fail_with,
    new_machine,
    one_shot_turn,
)
from tests.fakes.fake_llm_provider import FakeLLMServer

pytestmark = [pytest.mark.platforms("windows"), pytest.mark.integration,
              pytest.mark.live_system_guard_bypass, REQUIRES_OPT_IN]

PROFILE_NAME = "Jörg Ñúñez e2e"
KNOWN = {
    "install": (r"^install\.ps1 failed for a profile path with non-ASCII characters and spaces: .*"
                r"is not recognized as the name of a cmdlet",
                "gated on #124526: a non-ASCII profile path breaks uv Python path resolution in python-deps"),
    "acl": (r"^managed tools are not executable by a non-elevated process after update: .*WinError 5",
            "gated on #122935: tools\\* keep a hardened DACL a standard-user token cannot execute"),
}
_TOOL_NAMES = {"python.exe", "node.exe", "git.exe", "uv.exe", "rg.exe"}


def run_as_standard_user(argv: list[str], timeout: float = 120.0) -> tuple[int | None, str]:
    """``(exit code, "")`` or ``(None, launch error)`` for ``argv`` under a Basic User token."""
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
                    ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                    ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
                    ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                    ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
                    ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                    ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]

    advapi32.SaferCreateLevel.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.HANDLE), ctypes.c_void_p]
    advapi32.SaferComputeTokenFromLevel.argtypes = [wintypes.HANDLE, wintypes.HANDLE,
                                                    ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
                                                    ctypes.c_void_p]
    advapi32.SaferCloseLevel.argtypes = [wintypes.HANDLE]
    advapi32.CreateProcessAsUserW.argtypes = [
        wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL,
        wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(STARTUPINFOW),
        ctypes.POINTER(PROCESS_INFORMATION)]
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    level, token = wintypes.HANDLE(), wintypes.HANDLE()
    # SAFER_SCOPEID_USER=2, SAFER_LEVELID_NORMALUSER=0x20000, SAFER_LEVEL_OPEN=1
    if not advapi32.SaferCreateLevel(2, 0x20000, 1, ctypes.byref(level), None):
        raise OSError(ctypes.get_last_error(), "SaferCreateLevel failed")
    try:
        if not advapi32.SaferComputeTokenFromLevel(level, None, ctypes.byref(token), 0, None):
            raise OSError(ctypes.get_last_error(), "SaferComputeTokenFromLevel failed")
    finally:
        advapi32.SaferCloseLevel(level)
    si, pi = STARTUPINFOW(), PROCESS_INFORMATION()
    si.cb = ctypes.sizeof(si)
    cmdline = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
    try:
        if not advapi32.CreateProcessAsUserW(token, argv[0], cmdline, None, None, False,
                                             0x08000000, None, None, ctypes.byref(si), ctypes.byref(pi)):
            err = ctypes.get_last_error()
            return None, f"WinError {err}: {ctypes.FormatError(err).strip()}"
        try:
            if kernel32.WaitForSingleObject(pi.hProcess, int(timeout * 1000)) != 0:
                kernel32.TerminateProcess(pi.hProcess, 1)
                return None, f"timed out after {timeout}s"
            code = wintypes.DWORD()
            kernel32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
            return int(code.value), ""
        finally:
            kernel32.CloseHandle(pi.hProcess)
            kernel32.CloseHandle(pi.hThread)
    finally:
        kernel32.CloseHandle(token)


def _standard_user_token_is_really_restricted(scratch: Path) -> str:
    """Harness control: the Basic User token runs system binaries and is not an admin."""
    out = scratch / "whoami-groups.txt"
    code, err = run_as_standard_user(
        [r"C:\Windows\System32\cmd.exe", "/d", "/c", f'whoami /groups /fo csv > "{out}"'])
    assert code == 0, f"harness: cannot launch cmd.exe with a Basic User token (code={code}, {err})"
    admins = [ln for ln in out.read_text(encoding="mbcs", errors="replace").splitlines() if "S-1-5-32-544" in ln]
    assert not admins or all("Deny only" in ln or "deny only" in ln.lower() for ln in admins), (
        f"harness: the Basic User token still holds Administrators: {admins}")
    return "ok"


def _managed_tools(hermes_home: Path) -> list[Path]:
    tools = hermes_home / "tools"
    found = []
    for pattern in ("*/*.exe", "*/*/*.exe", "*/*/*/*.exe"):
        found += [p for p in tools.glob(pattern) if p.name.lower() in _TOOL_NAMES]
    return sorted(set(found))


def _tool_runs(hermes_home: Path) -> list[tuple[Path, int | None, str]]:
    return [(exe, *run_as_standard_user([str(exe), "--version"])) for exe in _managed_tools(hermes_home)]


@pytest.fixture(scope="module")
def journey(tmp_path_factory):
    with FakeLLMServer() as srv:
        machine = new_machine(tmp_path_factory.mktemp("paths"), srv.base_url, label="paths",
                              profile_name=PROFILE_NAME)
        j = Journey(machine)
        try:
            install = j.step("install", machine.install)
            if j.ok("install") and install.returncode == 0:
                j.step("version", lambda: machine.hermes("--version"))
                j.step("turn", lambda: one_shot_turn(machine, srv, "turn-unicode-profile"))
                machine.advance()
                j.step("update", machine.update)
                j.step("control", lambda: _standard_user_token_is_really_restricted(machine.root))
                j.step("tool_runs", lambda: _tool_runs(machine.hermes_home))
            yield j
        finally:
            machine.teardown()


def test_install_from_non_ascii_profile_with_spaces(journey: Journey) -> None:
    m, run = journey.machine, journey["install"]
    first_error = next((ln.strip() for ln in run.stdout.splitlines() if "[X]" in ln or "✗" in ln), "<none>")
    with known_gate(KNOWN, "install"):
        assert run.returncode == 0, fail_with(
            m, f"install.ps1 failed for a profile path with non-ASCII characters and spaces: {first_error}", run)


def test_hermes_works_and_updates_from_that_profile(journey: Journey) -> None:
    m = journey.machine
    version, turn, update = journey["version"], journey["turn"], journey["update"]
    assert version.returncode == 0 and "Hermes Agent v" in version.stdout, fail_with(
        m, "hermes --version fails from a non-ASCII profile", version)
    assert turn.ok, fail_with(
        m, f"a turn fails from a non-ASCII profile (reply printed={turn.reply_id in turn.run.stdout}, "
           f"prompt reached provider={turn.reached_wire})", turn.run)
    assert update.returncode == 0 and m.installed_head() == m.next, fail_with(
        m, f"hermes update from a non-ASCII profile exited {update.returncode}, checkout at {m.installed_head()}",
        update)


def test_managed_tools_stay_executable_for_standard_user(journey: Journey) -> None:
    m, runs = journey.machine, journey["tool_runs"]
    journey["control"]
    journey["update"]
    assert any(exe.name.lower() == "python.exe" for exe, _, _ in runs), fail_with(
        m, f"no managed python.exe under {m.hermes_home / 'tools'}: {[str(e) for e, _, _ in runs]}")
    broken = [f"{exe.relative_to(m.hermes_home)}: {err or f'exit {code}'}" for exe, code, err in runs if code != 0]
    with known_gate(KNOWN, "acl"):
        assert not broken, fail_with(
            m, f"managed tools are not executable by a non-elevated process after update: {'; '.join(broken)}")
