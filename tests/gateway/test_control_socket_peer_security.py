"""Control-socket trust boundary: server identity, home writability, pointer mode, fallback root."""
import os
import socket

import pytest

pytestmark = pytest.mark.platforms("linux")  # SO_PEERCRED / POSIX ACL / AF_UNIX semantics


def test_symlinked_home_still_authenticates_same_uid_peers(tmp_path):
    from gateway.control_socket import GatewayControlServer
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    (tmp_path / "link").symlink_to(real)
    ours, _theirs = socket.socketpair(socket.AF_UNIX)

    class Writer:
        def get_extra_info(self, _key):
            return ours
    assert GatewayControlServer(tmp_path / "link")._posix_peer_subject(Writer()) == f"uid:{os.getuid()}"
