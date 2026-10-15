"""Public SDK clients remain bounded across completed production binds."""

import asyncio
import base64
import contextvars
import json
from types import SimpleNamespace

import pytest


class ModelPeer:
    closed = False

    def __init__(self, dialect):
        self.dialect = dialect
        self.streams = {}
        self.callers = []

    async def call(self, method, document, **kwargs):
        if method == "model.open":
            self.callers.append(document["caller"])
            identifier = str(len(self.callers))
            if self.dialect == "openai_chat":
                payload = {
                    "id": "synthetic",
                    "object": "chat.completion",
                    "created": 1,
                    "model": document["body"]["model"],
                    "error": None,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "Synthetic title",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                }
            else:
                payload = {
                    "id": "synthetic",
                    "type": "message",
                    "role": "assistant",
                    "model": document["body"]["model"],
                    "error": None,
                    "content": [{"type": "text", "text": "Synthetic title"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }
            self.streams[identifier] = json.dumps(payload).encode()
            return {
                "streamId": identifier,
                "status": 200,
                "headers": {"content-type": "application/json"},
            }
        if method == "model.read":
            value = self.streams.pop(document["streamId"], None)
            return (
                {"done": True}
                if value is None
                else {"done": False, "data": base64.b64encode(value).decode()}
            )
        if method == "model.close":
            self.streams.pop(document["streamId"], None)
            return {"closed": True}
        raise AssertionError(method)


@pytest.mark.parametrize("dialect", ["openai_chat", "anthropic_messages"])
def test_completed_shared_title_binds_do_not_retain_one_client_per_title(dialect):
    pytest.importorskip("openai")
    pytest.importorskip("anthropic")
    from omicsclaw.entry.desktop.title import generate_title
    from omicsclaw.entry.ssh.model_bridge import ModelSnapshot, bridged_provider
    from omicsclaw.provider import ProviderDialect

    async def run():
        peer = ModelPeer(dialect)
        current = contextvars.ContextVar("title_identity")
        provider, factory = bridged_provider(
            ModelSnapshot("custom", ProviderDialect(dialect), "m", ("m",), 8192, 1024),
            peer,
            role="title",
            identity=current.get,
        )
        app = SimpleNamespace(provider_for_role=lambda role: provider)
        try:
            for turn in range(100):
                identifier = f"{turn:032x}"
                current.set(
                    {
                        "controllerInstanceId": "controller",
                        "epoch": "epoch",
                        "requestId": identifier,
                        "sessionId": "session",
                        "turnId": identifier,
                    }
                )
                status, result = await generate_title(
                    app,
                    {
                        "schema_version": 1,
                        "source_request_id": identifier,
                        "user_text": "Synthetic first message",
                    },
                )
                assert status == 200 and result["title"] == "Synthetic title"
            assert len(factory.clients) == 1
            assert len({row["requestId"] for row in peer.callers}) == 100
            assert not peer.streams
        finally:
            await factory.aclose()
        assert not factory.clients

    asyncio.run(run())


@pytest.mark.parametrize("dialect", ["openai_chat", "anthropic_messages"])
def test_constructor_option_variants_remain_distinct_bounded_and_close_together(
    dialect,
):
    pytest.importorskip("openai")
    pytest.importorskip("anthropic")
    from omicsclaw.entry.ssh.model_bridge import ModelSnapshot, SDKClientFactory
    from omicsclaw.entry.ssh.protocol import RpcError
    from omicsclaw.provider import ProviderConfig, ProviderDialect

    async def run():
        factory = SDKClientFactory(
            ModelSnapshot("custom", ProviderDialect(dialect), "m", ("m",), 8192, 1024),
            ModelPeer(dialect),
        )
        original = ProviderConfig(
            provider="custom",
            model="m",
            max_retries=0,
            timeout_seconds=120,
            connect_timeout_seconds=10,
        )
        clients = [
            factory(original.with_overrides(timeout_seconds=120 + index))
            for index in range(8)
        ]
        assert len(set(map(id, clients))) == 8
        assert (
            factory(original.with_overrides(max_tokens=64, temperature=0.1))
            is clients[0]
        )
        with pytest.raises(RpcError, match="configuration limit"):
            factory(original.with_overrides(max_retries=1))
        assert not any(client.is_closed() for client in clients)
        await factory.aclose()
        assert all(client.is_closed() for client in clients) and not factory.clients
        with pytest.raises(RpcError, match="closed"):
            factory(original)
        await factory.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("dialect", ["openai_chat", "anthropic_messages"])
def test_cancel_one_bound_call_leaves_shared_sdk_and_other_request_live(dialect):
    pytest.importorskip("openai")
    pytest.importorskip("anthropic")
    from omicsclaw.entry.ssh.model_bridge import ModelSnapshot, bridged_provider
    from omicsclaw.provider import ProviderDialect
    from omicsclaw.schema import Message, Role

    async def run():
        entered = asyncio.Event()

        class Peer(ModelPeer):
            async def call(self, method, document, **kwargs):
                if method == "model.read" and document["streamId"] == "1":
                    entered.set()
                    await asyncio.Event().wait()
                return await super().call(method, document, **kwargs)

        peer = Peer(dialect)
        current = contextvars.ContextVar("cancellation_identity")
        provider, factory = bridged_provider(
            ModelSnapshot("custom", ProviderDialect(dialect), "m", ("m",), 8192, 1024),
            peer,
            role="title",
            identity=current.get,
        )

        async def call(index):
            current.set(
                {
                    "controllerInstanceId": "controller",
                    "epoch": "epoch",
                    "requestId": str(index),
                    "sessionId": "session",
                    "turnId": str(index),
                }
            )
            return await provider.bind(max_tokens=64 + index).generate(
                [Message(role=Role.USER, content="synthetic")]
            )

        try:
            first = asyncio.create_task(call(1))
            await entered.wait()
            second = asyncio.create_task(call(2))
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert (await second).message.content == "Synthetic title"
            assert not peer.streams
            assert len(factory.clients) == 1 and not factory.clients[0].is_closed()
        finally:
            await factory.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("dialect", ["openai_chat", "anthropic_messages"])
@pytest.mark.parametrize(
    "role", ["main", "summary", "subagent", "title", "shadow_review", "provider_test"]
)
def test_shared_client_keeps_concurrent_bind_and_caller_context(dialect, role):
    pytest.importorskip("openai")
    pytest.importorskip("anthropic")
    from omicsclaw.entry.ssh.model_bridge import ModelSnapshot, bridged_provider
    from omicsclaw.provider import ProviderDialect
    from omicsclaw.schema import Message, Role

    async def run():
        peer = ModelPeer(dialect)
        current = contextvars.ContextVar("caller_identity")
        provider, factory = bridged_provider(
            ModelSnapshot(
                "custom", ProviderDialect(dialect), "m", ("m", "other"), 8192, 1024
            ),
            peer,
            role=role,
            identity=current.get,
        )

        async def call(index):
            identifier = f"{index:032x}"
            current.set(
                {
                    "controllerInstanceId": "controller",
                    "epoch": "epoch",
                    "requestId": identifier,
                    "sessionId": "session",
                    "turnId": identifier,
                }
            )
            bound = provider.bind(
                model="m" if index % 2 else "other",
                max_tokens=64 + index,
                temperature=index / 10,
            )
            result = await bound.generate(
                [Message(role=Role.USER, content="synthetic")]
            )
            assert result.message.content == "Synthetic title"

        try:
            await asyncio.gather(*(call(index) for index in range(10)))
            assert len(factory.clients) == 1
            assert {row["role"] for row in peer.callers} == {role}
            assert len({row["requestId"] for row in peer.callers}) == 10
            assert not peer.streams
        finally:
            await factory.aclose()

    asyncio.run(run())
