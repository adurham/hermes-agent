"""#139485 on a real Windows ACL: a mailbox dir that denies FILE_WRITE_ATTRIBUTES makes ``chmod``
raise WinError 5 (as on an Administrators-owned dir seen by a non-elevated user), yet the dir stays usable."""
import os
import subprocess

import pytest

from tools import bot_live_delivery as mailbox

pytestmark = pytest.mark.platforms("windows")


def _out(*cmd):
    return subprocess.run(list(cmd), capture_output=True, text=True, check=False).stdout


def test_mailbox_lock_survives_a_dir_whose_acl_denies_write_attributes(tmp_path):
    root = tmp_path / "runtime" / mailbox.DELIVERY_DIR_NAME
    root.mkdir(parents=True)
    # Deny write-attributes to Everyone (S-1-1-0): no group or name form of this token escapes it.
    subprocess.run(["icacls", str(root), "/deny", "*S-1-1-0:(WA)"], check=True, capture_output=True)
    try:
        try:
            os.chmod(root, 0o700)
        except PermissionError:
            pass  # the reporter's condition holds on this dir
        else:
            pytest.fail("a deny-WA ACE did not make chmod fail on this runner:\n"
                        f"{_out('icacls', str(root))}\n{_out('whoami', '/priv')}\n{_out('whoami', '/groups')}")
        with mailbox._locked(tmp_path) as locked:
            assert locked == root.resolve()
            mailbox._write(locked / ("a" * 32 + ".json"), {"status": "queued"})
        assert mailbox._read(root / ("a" * 32 + ".json")) == {"status": "queued"}
    finally:
        subprocess.run(["icacls", str(root), "/remove:d", "*S-1-1-0"], check=False, capture_output=True)
