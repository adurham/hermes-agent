"""The viewer RPC client keeps an owner refusal's structured verdict (code + bounded data)."""
import asyncio
import json

import pytest

from hermes_cli.gateway_client import GatewayClient, GatewayRPCError


class _Socket:
    """One scripted owner: answers each request with the frame ``reply(request)`` builds."""

    def __init__(self, reply):
        self.reply = reply
        self.inbox = asyncio.Queue()

    async def send(self, raw):
        frame = self.reply(json.loads(raw))
        if frame is not None:
            await self.inbox.put(json.dumps(frame))

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.inbox.get()


@pytest.mark.asyncio
async def test_rpc_error_keeps_numeric_code_and_bounded_data():
    errors = {
        'session.mutate': {'code': 4001, 'message': 'invalid_params', 'data': {
            'reason': 'invalid_params', 'fields': ['payload.title'],
            'trace': {'nested': 'remote diagnostics'}, 'blob': 'x' * 5000}},
        'nope': {'code': -32601, 'message': 'unknown method: nope', 'data': {'reason': 'unknown_method'}},
    }
    socket = _Socket(lambda req: {'jsonrpc': '2.0', 'id': req['id'], 'error': errors[req['method']]})
    async with GatewayClient(socket) as client:
        with pytest.raises(GatewayRPCError) as refused:
            await client.rpc('session.mutate', session_id='s')
        assert str(refused.value) == 'invalid_params' and refused.value.code == 4001
        # Scalar facts survive; nested objects and oversized strings never reach the caller.
        assert refused.value.data == {'reason': 'invalid_params', 'fields': ['payload.title']}
        with pytest.raises(GatewayRPCError) as unknown:
            await client.rpc('nope')
        # Free-text messages stay bounded: the owner's reason code, not the raw sentence.
        assert str(unknown.value) == 'unknown_method' and unknown.value.code == -32601
