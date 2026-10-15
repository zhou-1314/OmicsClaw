"""Production SDK calls carry live caller and durable admission identity."""

import asyncio
import json
from types import SimpleNamespace
import pytest


def test_transport_requires_production_call_identity():
    from omicsclaw.entry.ssh.model_bridge import ModelSnapshot, RpcTransport
    from omicsclaw.provider import ProviderDialect

    snapshot = ModelSnapshot(
        "mac", ProviderDialect.OPENAI_CHAT, "m", ("m",), 8192, 1024
    )
    peer = SimpleNamespace()
    bridge = RpcTransport(peer, snapshot, role="main", identity=lambda: None)
    request = SimpleNamespace(
        method="POST",
        url=SimpleNamespace(host="bridge.invalid", path="/v1/chat/completions"),
    )

    async def aread():
        return json.dumps({"model": "m"}).encode()

    request.aread = aread
    with pytest.raises(Exception, match="caller identity"):
        asyncio.run(bridge.open(request))


def test_transport_attaches_identity_without_forwarding_auth_or_endpoint():
    from omicsclaw.entry.ssh.model_bridge import ModelSnapshot, RpcTransport
    from omicsclaw.provider import ProviderDialect

    snapshot = ModelSnapshot(
        "mac", ProviderDialect.OPENAI_CHAT, "m", ("m",), 8192, 1024, max_retries=2
    )
    calls = []

    class Peer:
        async def call(self, method, params, **kwargs):
            calls.append((method, params))
            return {"streamId": "s", "status": 200, "headers": {}}

    identity = {
        "controllerInstanceId": "c",
        "epoch": "e",
        "requestId": "r",
        "sessionId": "ss",
        "turnId": "t",
    }
    bridge = RpcTransport(Peer(), snapshot, role="summary", identity=lambda: identity)
    request = SimpleNamespace(
        method="POST",
        url=SimpleNamespace(host="bridge.invalid", path="/v1/chat/completions"),
    )

    async def aread():
        return json.dumps({"model": "m"}).encode()

    request.aread = aread
    asyncio.run(bridge.open(request))
    assert calls[0][1]["caller"] == {**identity, "role": "summary"}
    assert set(calls[0][1]) == {
        "caller",
        "providerId",
        "revision",
        "dialect",
        "path",
        "body",
    }
