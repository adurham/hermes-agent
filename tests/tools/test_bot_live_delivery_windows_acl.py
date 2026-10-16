"""#139485 on a real Windows ACL: a mailbox dir that denies FILE_WRITE_ATTRIBUTES makes ``chmod``
raise WinError 5 (as on an Administrators-owned dir seen by a non-elevated user), yet the dir stays usable.

CPython's Windows ``Path.chmod`` opens the dir with FILE_FLAG_BACKUP_SEMANTICS, so a token with
SeBackupPrivilege/SeRestorePrivilege enabled (an elevated CI runner) bypasses the deny ACE. The
reporter's non-elevated token has neither. The probe runs in a child that drops both privileges from
its own token first, then exercises the production lock.
"""
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.platforms("windows")

_REPO = Path(__file__).resolve().parents[2]

_CHILD = textwrap.dedent(r"""
    import ctypes, ctypes.wintypes as w, json, subprocess, sys
    from pathlib import Path

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", w.DWORD), ("HighPart", w.LONG)]

    class LUID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Luid", LUID), ("Attributes", w.DWORD)]

    class TOKEN_PRIVILEGES(ctypes.Structure):
        _fields_ = [("PrivilegeCount", w.DWORD), ("Privileges", LUID_AND_ATTRIBUTES * 1)]

    kernel.GetCurrentProcess.restype = w.HANDLE
    advapi.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    advapi.LookupPrivilegeValueW.argtypes = [w.LPCWSTR, w.LPCWSTR, ctypes.POINTER(LUID)]
    advapi.AdjustTokenPrivileges.argtypes = [w.HANDLE, w.BOOL, ctypes.POINTER(TOKEN_PRIVILEGES),
                                             w.DWORD, ctypes.c_void_p, ctypes.c_void_p]
    token = w.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0020 | 0x0008, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    for name in ("SeBackupPrivilege", "SeRestorePrivilege"):
        luid = LUID()
        if not advapi.LookupPrivilegeValueW(None, name, ctypes.byref(luid)):
            raise ctypes.WinError(ctypes.get_last_error())
        # SE_PRIVILEGE_REMOVED: gone from this process's token for good, as in a non-elevated token.
        tp = TOKEN_PRIVILEGES(1, (LUID_AND_ATTRIBUTES * 1)(LUID_AND_ATTRIBUTES(luid, 0x00000004)))
        if not advapi.AdjustTokenPrivileges(token, False, ctypes.byref(tp), 0, None, None):
            raise ctypes.WinError(ctypes.get_last_error())

    from tools import bot_live_delivery as mailbox

    home = Path(sys.argv[1])
    root = home / "runtime" / mailbox.DELIVERY_DIR_NAME
    try:
        root.chmod(0o700)
        denied = False
    except PermissionError:
        denied = True
    if not denied:
        priv = subprocess.run(["whoami", "/priv"], capture_output=True, text=True, check=False).stdout
        print(json.dumps({"denied": False, "priv": priv}))
        sys.exit(0)
    with mailbox._locked(home) as locked:
        mailbox._write(locked / ("a" * 32 + ".json"), {"status": "queued"})
    print(json.dumps({"denied": True, "read": mailbox._read(root / ("a" * 32 + ".json")),
                      "locked": str(locked)}))
""")


def test_mailbox_lock_survives_a_dir_whose_acl_denies_write_attributes(tmp_path):
    root = tmp_path / "runtime" / "bot_live_delivery"
    root.mkdir(parents=True)
    # Deny write-attributes to Everyone (S-1-1-0): no group or name form of the token escapes it.
    subprocess.run(["icacls", str(root), "/deny", "*S-1-1-0:(WA)"], check=True, capture_output=True)
    try:
        child = subprocess.run([sys.executable, "-c", _CHILD, str(tmp_path)], cwd=_REPO,
                               capture_output=True, text=True, check=False, timeout=120)
        assert child.returncode == 0, f"the production lock failed under the reporter's ACL:\n{child.stderr}"
        result = json.loads(child.stdout.strip().splitlines()[-1])
        assert result["denied"], f"chmod still succeeded without backup/restore privileges:\n{result['priv']}"
        assert Path(result["locked"]) == root.resolve()
        assert result["read"] == {"status": "queued"}
    finally:
        subprocess.run(["icacls", str(root), "/remove:d", "*S-1-1-0"], check=False, capture_output=True)
