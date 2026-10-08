"""Named connector accounts over the real RPC dispatch.

- ``connectors.connect`` with an alias opens an operation for that one account and mints it
- ``connectors.accounts.rename`` answers ALIAS_TAKEN when another account holds the name
"""

import threading

import pytest

from tools.connectors import live
from tools.connectors.gateway.errors import IdempotencyConflict
from tui_gateway import server


@pytest.fixture(autouse=True)
def _gate(monkeypatch):
    live.reset_for_tests()
    monkeypatch.setattr("tools.connectors.connectors_available", lambda: True)
    yield
    live.reset_for_tests()


class _Transport:
    """Collects the frames a long handler writes back instead of returning."""

    def __init__(self):
        self.frames = []
        self.arrived = threading.Event()

    def write(self, obj):
        self.frames.append(obj)
        self.arrived.set()
        return True

    def close(self):
        pass


def _rpc(method, **params):
    transport = _Transport()
    reply = server.dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, transport)
    if reply is not None:
        return reply
    assert transport.arrived.wait(5), "no reply"
    return next(frame for frame in transport.frames if frame.get("id") == 1)


def test_account_connect_with_an_alias_mints_that_account(monkeypatch):
    mints = []
    minted = threading.Event()

    class Client:
        def connections(self, names, *, reinitiate=False, alias=None, **_):
            mints.append((tuple(names), reinitiate, alias))
            minted.set()
            return {"results": [{"connector": n, "status": "initiated", "connection_id": "ca_1",
                                 "connect_url": "https://connect.example/1"} for n in names]}

        def account_status(self, connection_id, *, timeout=None):
            return None

    monkeypatch.setattr("tools.connectors.managed.managed_client", Client)
    reply = _rpc("connectors.connect", owner={"type": "account"}, connectors=["gmail"], alias="work")
    assert "result" in reply, reply
    assert minted.wait(2)
    assert mints == [(("gmail",), False, "work")]
    assert [(t["name"], t.get("alias")) for t in reply["result"]["targets"]] == [("gmail", "work")]


def test_rename_to_a_name_another_account_holds_is_alias_taken(monkeypatch):
    def taken(self, connection_id, alias):
        raise IdempotencyConflict("alias taken", code="alias_taken", status=409)

    monkeypatch.setattr("tools.connectors.portal.client.PortalConnectorClient.rename_account", taken)
    reply = _rpc("connectors.accounts.rename", connection_id="ca_1", alias="home")
    assert reply["error"]["data"]["reason"] == "ALIAS_TAKEN"
