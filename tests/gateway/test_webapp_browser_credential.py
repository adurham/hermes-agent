"""The Webapp's browser credential file stays private to the gateway's OS user.

The gateway keeps the browser credential in ``webapp/session-token`` inside its home so open tabs
survive a restart. On POSIX the folder is user-only (0700) and the file owner-read/write (0600). A
folder found open to other users is closed again before the credential is used, and a credential
file other users could read is treated as exposed: it is replaced, which signs the open tabs out,
rather than reused. Windows homes keep their own ACL, so this applies to POSIX only.
"""
import os
import stat

import pytest

from gateway import webapp_listener as webapp

posix_only = pytest.mark.platforms("posix")
STORED = "s" * 43


def _store(home, *, folder_mode, file_mode):
    path = home / webapp.TOKEN_PATH
    path.parent.mkdir(parents=True)
    path.write_text(STORED + "\n", encoding="utf-8")
    os.chmod(path, file_mode)
    os.chmod(path.parent, folder_mode)
    return path


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


@posix_only
@pytest.mark.parametrize("file_mode", [0o644, 0o640, 0o604])
def test_a_credential_other_users_could_read_is_replaced(tmp_path, file_mode):
    path = _store(tmp_path, folder_mode=0o700, file_mode=file_mode)
    token = webapp.browser_session_token(tmp_path)
    assert token != STORED
    assert path.read_text(encoding="utf-8").strip() == token
    assert _mode(path) == 0o600


@posix_only
def test_a_private_credential_in_an_open_folder_is_kept_and_the_folder_closed(tmp_path):
    path = _store(tmp_path, folder_mode=0o755, file_mode=0o600)
    assert webapp.browser_session_token(tmp_path) == STORED
    assert _mode(path.parent) == 0o700
    assert _mode(path) == 0o600


@posix_only
def test_signing_out_closes_an_open_credential_folder_too(tmp_path):
    path = _store(tmp_path, folder_mode=0o755, file_mode=0o600)
    assert webapp.browser_session_token(tmp_path, rotate=True) != STORED
    assert (_mode(path.parent), _mode(path)) == (0o700, 0o600)


def test_a_private_credential_is_reused(tmp_path):
    first = webapp.browser_session_token(tmp_path)
    assert webapp.browser_session_token(tmp_path) == first
