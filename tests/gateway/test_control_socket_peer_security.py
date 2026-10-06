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


def test_restart_heals_a_world_readable_pointer_left_by_an_older_gateway(tmp_path):
    import asyncio
    from gateway.control_socket import GatewayControlServer
    from hermes_cli.gateway_runtime_discovery import _socket_path
    home = tmp_path / ("p" * 90) / ".hermes"
    home.mkdir(parents=True, mode=0o700)
    pointer = home / "gateway.sock.path"
    pointer.write_text("/old/control.sock")
    pointer.chmod(0o644)  # main wrote it with write_text under umask 022; a crash leaves it

    async def run():
        server = GatewayControlServer(home)
        assert await server.start()
        try:
            assert _socket_path(home).name == "control.sock"
        finally:
            await server.stop()
    asyncio.run(run())
