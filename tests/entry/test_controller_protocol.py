"""Bounded stdio framing and the public SDK transport contract."""

import asyncio
import base64
import json

import pytest


def test_duplicate_json_keys_are_refused():
    from omicsclaw.entry.ssh.protocol import decode_frame

    with pytest.raises(ValueError):
        decode_frame(b'{"kind":"request","kind":"response"}\n')


def test_sdk_clients_use_bridge_transport_and_keep_binding():
    pytest.importorskip("openai")
    pytest.importorskip("anthropic")
    from omicsclaw.entry.ssh.model_bridge import ModelSnapshot, bridged_provider
    from omicsclaw.provider import ProviderDialect, ProviderError

    class Peer:
        closed = False

        async def call(self, method, params, **kwargs):
            if method == "model.read":
                return {"done": True}
            if method == "model.close":
                return {}
            assert method == "model.open"
            assert params["path"] in {"/v1/chat/completions", "/v1/messages"}
            assert params["body"]["model"] == "summary"
            assert "authorization" not in params
            return {"streamId": "s", "status": 401, "headers": {}}

    async def run():
        for dialect in ProviderDialect:
            snapshot = ModelSnapshot(
                provider_id="mac-provider",
                dialect=dialect,
                model="primary",
                allowed_models=("primary", "summary"),
                context_tokens=16384,
                output_tokens=1024,
            )
            provider, factory = bridged_provider(snapshot, Peer())
            child = provider.bind(model="summary")
            with pytest.raises(ProviderError, match="401"):
                await child.generate([])
            await factory.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("dialect", ["openai_chat", "anthropic_messages"])
def test_real_sdk_stream_codec_and_close(dialect):
    pytest.importorskip("openai")
    pytest.importorskip("anthropic")
    from omicsclaw.entry.ssh.model_bridge import ModelSnapshot, bridged_provider
    from omicsclaw.provider import ProviderDialect
    from omicsclaw.schema import Message, Role

    if dialect == "openai_chat":
        events = [
            {
                "id": "m",
                "choices": [
                    {"index": 0, "delta": {"content": "hello"}, "finish_reason": None}
                ],
            },
            {
                "id": "m",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        ]
        encoded = (
            "".join("data: " + json.dumps(event) + "\n\n" for event in events)
            + "data: [DONE]\n\n"
        )
    else:
        events = [
            {
                "type": "message_start",
                "message": {
                    "id": "m",
                    "type": "message",
                    "role": "assistant",
                    "model": "summary",
                    "content": [],
                    "usage": {"input_tokens": 10, "output_tokens": 0},
                },
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "hello"},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 5},
            },
            {"type": "message_stop"},
        ]
        encoded = "".join(
            "event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n"
            for event in events
        )

    class Peer:
        closed = False
        consumed = False
        stream_closed = False

        async def call(self, method, params, **kwargs):
            if method == "model.open":
                assert params["body"]["model"] == "summary"
                return {
                    "streamId": "s",
                    "status": 200,
                    "headers": {"content-type": "text/event-stream"},
                }
            if method == "model.close":
                self.stream_closed = True
                return {}
            assert method == "model.read"
            if self.consumed:
                return {"done": True}
            self.consumed = True
            return {"done": False, "data": base64.b64encode(encoded.encode()).decode()}

    async def run():
        peer = Peer()
        snapshot = ModelSnapshot(
            provider_id="mac-provider",
            dialect=ProviderDialect(dialect),
            model="primary",
            allowed_models=("primary", "summary"),
            context_tokens=16384,
            output_tokens=1024,
        )
        provider, factory = bridged_provider(snapshot, peer)
        chunks = [
            chunk
            async for chunk in provider.bind(model="summary").generate_stream(
                [Message(role=Role.USER, content="hello")]
            )
        ]
        assert "".join(chunk.delta for chunk in chunks) == "hello"
        assert any(chunk.usage and chunk.usage.output_tokens == 5 for chunk in chunks)
        assert peer.stream_closed
        await factory.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("dialect", ["openai_chat", "anthropic_messages"])
@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_real_sdk_retains_status_classification_with_safe_error_document(
    dialect, status
):
    pytest.importorskip("openai")
    pytest.importorskip("anthropic")
    from omicsclaw.entry.ssh.model_bridge import ModelSnapshot, SDKClientFactory
    from omicsclaw.provider import ProviderConfig, ProviderDialect

    class Peer:
        closed = False
        read = False

        async def call(self, method, params, **kwargs):
            if method == "model.open":
                return {
                    "streamId": "safe",
                    "status": status,
                    "headers": {"content-type": "application/json", "retry-after": "0"},
                }
            if method == "model.close":
                return {}
            if self.read:
                return {"done": True}
            self.read = True
            document = json.dumps(
                {
                    "error": {
                        "type": "api_error",
                        "message": f"Model request failed (HTTP {status}).",
                    }
                }
            ).encode()
            return {"data": base64.b64encode(document).decode()}

    async def run():
        snapshot = ModelSnapshot(
            "mac", ProviderDialect(dialect), "primary", ("primary",), 16384, 1024
        )
        factory = SDKClientFactory(snapshot, Peer())
        client = factory(
            ProviderConfig(provider="custom", model="primary", max_retries=0)
        )
        try:
            with pytest.raises(Exception) as caught:
                if dialect == "openai_chat":
                    await client.chat.completions.create(
                        model="primary", messages=[{"role": "user", "content": "hello"}]
                    )
                else:
                    await client.messages.create(
                        model="primary",
                        max_tokens=64,
                        messages=[{"role": "user", "content": "hello"}],
                    )
            assert caught.value.status_code == status
        finally:
            await factory.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("dialect", ["openai_chat", "anthropic_messages"])
def test_public_factory_retains_configured_single_layer_retry(dialect):
    pytest.importorskip("openai")
    pytest.importorskip("anthropic")
    from omicsclaw.entry.ssh.model_bridge import ModelSnapshot, SDKClientFactory
    from omicsclaw.provider import ProviderConfig, ProviderDialect

    class Peer:
        closed = False
        calls = 0
        consumed = False

        async def call(self, method, params, **kwargs):
            if method == "model.close":
                return {}
            if method == "model.open":
                self.calls += 1
                self.consumed = False
                return {
                    "streamId": str(self.calls),
                    "status": 429 if self.calls == 1 else 200,
                    "headers": {"content-type": "application/json", "retry-after": "0"},
                }
            if self.consumed:
                return {"done": True}
            self.consumed = True
            if self.calls == 1:
                body = {
                    "error": {
                        "type": "rate_limit_error",
                        "message": "Safe rate limit diagnostic",
                    }
                }
            elif dialect == "openai_chat":
                body = {
                    "id": "m",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "primary",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                }
            else:
                body = {
                    "id": "m",
                    "type": "message",
                    "role": "assistant",
                    "model": "primary",
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }
            return {"data": base64.b64encode(json.dumps(body).encode()).decode()}

    async def run():
        peer = Peer()
        snapshot = ModelSnapshot(
            "mac", ProviderDialect(dialect), "primary", ("primary",), 16384, 1024
        )
        factory = SDKClientFactory(snapshot, peer)
        client = factory(
            ProviderConfig(provider="custom", model="primary", max_retries=1)
        )
        try:
            if dialect == "openai_chat":
                response = await client.chat.completions.create(
                    model="primary", messages=[{"role": "user", "content": "hello"}]
                )
                assert response.choices[0].message.content == "ok"
            else:
                response = await client.messages.create(
                    model="primary",
                    max_tokens=64,
                    messages=[{"role": "user", "content": "hello"}],
                )
                assert response.content[0].text == "ok"
            assert peer.calls == 2
        finally:
            await factory.aclose()

    asyncio.run(run())
