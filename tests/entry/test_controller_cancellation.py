"""Cancelled RPCs release their peer work without invalidating other requests."""

import asyncio
import json

import pytest

from omicsclaw.entry.ssh.protocol import JsonPeer, RpcError


@pytest.mark.parametrize("reason", ["timeout", "cancel"])
def test_known_late_response_is_ignored_but_unknown_response_is_refused(reason):
    async def run():
        reader = asyncio.StreamReader()
        frames = []

        async def write(raw):
            frames.append(json.loads(raw))

        peer = JsonPeer(reader, write)
        running = asyncio.create_task(peer.run())
        pending = asyncio.create_task(peer.call("model.open", {}, timeout=0.02))
        try:
            while not frames:
                await asyncio.sleep(0)
            identifier = frames[0]["id"]
            if reason == "cancel":
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            else:
                with pytest.raises(TimeoutError):
                    await pending
            assert frames[-1] == {"kind": "cancel", "id": identifier}
            reader.feed_data(
                json.dumps(
                    {"kind": "response", "id": identifier, "result": {}}
                ).encode()
                + b"\n"
            )
            await asyncio.sleep(0)
            assert not running.done()
            reader.feed_data(
                b'{"kind":"response","id":"actually-unknown","result":{}}\n'
            )
            with pytest.raises(RpcError):
                await running
        finally:
            reader.feed_eof()
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    asyncio.run(run())


def test_model_reads_share_the_open_deadline_instead_of_default_rpc_timeout(
    monkeypatch,
):
    from types import SimpleNamespace

    from omicsclaw.entry.ssh import model_bridge as module
    from omicsclaw.provider import ProviderDialect

    clock = [0.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    calls = []

    class Peer:
        closed = False

        async def call(self, method, params, **kwargs):
            calls.append((method, kwargs["timeout"]))
            if method == "model.open":
                clock[0] = 15.0
                return {"streamId": "stream", "status": 200}
            if method == "model.read" and clock[0] == 15:
                clock[0] = 290.0
                return {"data": "YQ=="}
            return {"done": True}

    class Request:
        method = "POST"
        url = SimpleNamespace(host="bridge.invalid", path="/v1/chat/completions")

        async def aread(self):
            return b'{"model":"primary"}'

    async def run():
        snapshot = module.ModelSnapshot(
            "provider",
            ProviderDialect.OPENAI_CHAT,
            "primary",
            ("primary",),
            8192,
            1024,
            timeout_seconds=300,
        )
        _head, stream = await module.RpcTransport(Peer(), snapshot).open(Request())
        assert [part async for part in stream] == [b"a"]
        assert calls == [
            ("model.open", 300),
            ("model.read", 285),
            ("model.read", 10),
            ("model.close", 1),
        ]

    asyncio.run(run())


def test_retired_requests_are_bounded_and_each_late_response_is_accepted_once():
    async def run():
        reader = asyncio.StreamReader()
        frames = []

        async def write(raw):
            frames.append(json.loads(raw))

        peer = JsonPeer(reader, write)
        running = asyncio.create_task(peer.run())
        try:
            for _ in range(129):
                with pytest.raises(TimeoutError):
                    await peer.call("slow", {}, timeout=0.001)
            oldest, newest = frames[0]["id"], frames[-2]["id"]
            reader.feed_data(
                json.dumps({"kind": "response", "id": newest, "result": {}}).encode()
                + b"\n"
            )
            await asyncio.sleep(0)
            assert not running.done()
            reader.feed_data(
                json.dumps({"kind": "response", "id": oldest, "result": {}}).encode()
                + b"\n"
            )
            with pytest.raises(RpcError):
                await running
        finally:
            reader.feed_eof()
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    asyncio.run(run())
