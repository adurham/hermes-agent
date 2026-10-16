"""#139485 on a real Windows ACL: a mailbox dir that denies FILE_WRITE_ATTRIBUTES to this user makes
``chmod`` raise WinError 5 (as on an Administrators-owned dir), yet the dir stays usable."""
import os
import subprocess

import pytest

from tools import bot_live_delivery as mailbox

pytestmark = pytest.mark.platforms("windows")


def test_mailbox_lock_survives_a_dir_whose_acl_denies_write_attributes(tmp_path):
    root = tmp_path / "runtime" / mailbox.DELIVERY_DIR_NAME
    root.mkdir(parents=True)
    user = subprocess.run(["whoami"], capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run(["icacls", str(root), "/deny", f"{user}:(WA)"], check=True, capture_output=True)
    try:
        with pytest.raises(PermissionError):
            os.chmod(root, 0o700)  # the reporter's condition holds on this dir
        with mailbox._locked(tmp_path) as locked:
            assert locked == root.resolve()
            mailbox._write(locked / ("a" * 32 + ".json"), {"status": "queued"})
        assert mailbox._read(root / ("a" * 32 + ".json")) == {"status": "queued"}
    finally:
        subprocess.run(["icacls", str(root), "/remove:d", user], check=False, capture_output=True)
