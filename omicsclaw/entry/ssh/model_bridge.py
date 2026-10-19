"""Vendor SDKs keep their codecs and use a restricted Mac transport."""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from omicsclaw.provider import ProviderConfig, ProviderDialect, provider_for

from .protocol import JsonPeer, RpcError


@dataclass(frozen=True, slots=True)
class ModelSnapshot:
    provider_id: str
    dialect: ProviderDialect
    model: str
    allowed_models: tuple[str, ...]
    context_tokens: int
    output_tokens: int
    temperature: float = 0.7
    timeout_seconds: float = 120
    revision: int = 1
    provider_name: str = "custom"
    endpoint_hint: str = ""
    max_retries: int = 0
    connect_timeout_seconds: float | None = None

    def __post_init__(self):
        if (
            not isinstance(self.provider_id, str)
            or not self.provider_id
            or self.model not in self.allowed_models
        ):
            raise ValueError("invalid model snapshot")
        if any(
            type(value) is not int
            for value in (self.context_tokens, self.output_tokens, self.revision)
        ):
            raise ValueError("integer budgets and revision required")
        if not 1024 <= self.context_tokens <= 2000000:
            raise ValueError("invalid context budget")
        if not 1 <= self.output_tokens < self.context_tokens:
            raise ValueError("invalid output budget")
        if not 1 <= self.timeout_seconds <= 3600:
            raise ValueError("invalid model deadline")
        if type(self.max_retries) is not int or not 0 <= self.max_retries <= 5:
            raise ValueError("invalid SDK retry policy")
        if (
            self.connect_timeout_seconds is not None
            and not 0 < self.connect_timeout_seconds <= self.timeout_seconds
        ):
            raise ValueError("invalid connect deadline")


class RpcByteStream:
    def __init__(self, peer: JsonPeer, stream_id: str, deadline: float):
        self.peer = peer
        self.stream_id = stream_id
        self.closed = False
        self.deadline = deadline

    async def __aiter__(self):
        try:
            while not self.closed:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("model deadline exceeded")
                result = await self.peer.call(
                    "model.read", {"streamId": self.stream_id}, timeout=remaining
                )
                if result.get("done"):
                    return
                chunk = base64.b64decode(result["data"], validate=True)
                if len(chunk) > 32768:
                    raise RpcError("model chunk exceeds limit")
                yield chunk
        finally:
            await self.aclose()

    async def aclose(self):
        if self.closed:
            return
        self.closed = True
        if not self.peer.closed:
            try:
                await self.peer.call(
                    "model.close", {"streamId": self.stream_id}, timeout=1
                )
            except (TimeoutError, RpcError):
                pass


class RpcTransport:
    def __init__(
        self,
        peer: JsonPeer,
        snapshot: ModelSnapshot,
        *,
        role: str | None = None,
        identity: Callable[[], dict | None] | None = None,
    ):
        self.peer = peer
        self.snapshot = snapshot
        self.role = role
        self.identity = identity

    async def open(self, request):
        import json

        if request.method != "POST" or request.url.host != "bridge.invalid":
            raise RpcError("SDK request outside model capability")
        body = json.loads(await request.aread())
        if body.get("model") not in self.snapshot.allowed_models:
            if (
                self.role in ("subagent", "shadow_review")
                and self.snapshot.model in self.snapshot.allowed_models
            ):
                body["model"] = self.snapshot.model
            else:
                raise RpcError("model outside Mac snapshot")
        caller = None
        if self.role is not None:
            identity = self.identity() if self.identity else None
            if not identity or any(
                not identity.get(key)
                for key in ("controllerInstanceId", "epoch", "requestId", "sessionId")
            ):
                raise RpcError("model caller identity required")
            caller = {**identity, "role": self.role}
        deadline = time.monotonic() + self.snapshot.timeout_seconds
        head = await self.peer.call(
            "model.open",
            {
                "providerId": self.snapshot.provider_id,
                "revision": self.snapshot.revision,
                "dialect": self.snapshot.dialect.value,
                "path": request.url.path,
                "body": body,
                **({"caller": caller} if caller else {}),
            },
            timeout=self.snapshot.timeout_seconds,
        )
        return head, RpcByteStream(self.peer, head["streamId"], deadline)


def sdk_transport(http, bridge: RpcTransport):
    """HTTP libraries supply their public types; the RPC implementation is shared."""

    class ByteStream(http.AsyncByteStream):
        def __init__(self, stream):
            self.stream = stream

        def __aiter__(self):
            return self.stream.__aiter__()

        async def aclose(self):
            await self.stream.aclose()

    class Transport(http.AsyncBaseTransport):
        async def handle_async_request(self, request):
            head, stream = await bridge.open(request)
            return http.Response(
                head["status"],
                headers=head.get("headers", {}),
                stream=ByteStream(stream),
                request=request,
            )

    return Transport()


class SDKClientFactory:
    def __init__(
        self,
        snapshot: ModelSnapshot,
        peer: JsonPeer,
        *,
        role: str | None = None,
        identity: Callable[[], dict | None] | None = None,
    ):
        self.snapshot = snapshot
        self.transport = RpcTransport(peer, snapshot, role=role, identity=identity)
        self.clients: list[Any] = []
        self._by_options: dict[tuple[int, float, float], Any] = {}
        self._closed = False

    def __call__(self, config: ProviderConfig):
        if self._closed:
            raise RpcError("model client factory closed")
        if config.model not in self.snapshot.allowed_models:
            raise RpcError("bound model outside Mac snapshot")
        # Model/output/thinking options stay on the original immutable provider.
        # Only these three fields affect the SDK client constructor. The RPC
        # transport reads caller context for each request, including concurrent
        # binds sharing this client.
        options = (
            config.max_retries,
            config.timeout_seconds,
            config.connect_timeout_seconds,
        )
        if options in self._by_options:
            return self._by_options[options]
        if len(self._by_options) >= 8:
            raise RpcError("model client transport configuration limit")
        if self.snapshot.dialect == ProviderDialect.OPENAI_CHAT:
            import httpx as http
            from openai import AsyncOpenAI as SDKClient
        else:
            import httpx as http
            from anthropic import AsyncAnthropic as SDKClient
        http_client = http.AsyncClient(
            transport=sdk_transport(http, self.transport), trust_env=False
        )
        common = {
            "api_key": "ssh-bridge-placeholder",
            "base_url": (
                "https://bridge.invalid/v1"
                if self.snapshot.dialect == ProviderDialect.OPENAI_CHAT
                else "https://bridge.invalid"
            ),
            "http_client": http_client,
            "max_retries": config.max_retries,
            "timeout": http.Timeout(
                config.timeout_seconds, connect=config.connect_timeout_seconds
            ),
        }
        client = SDKClient(**common)
        self.clients.append(client)
        self._by_options[options] = client
        return client

    async def aclose(self):
        self._closed = True
        clients = tuple(self.clients)
        self.clients.clear()
        self._by_options.clear()
        results = await asyncio.gather(
            *(client.close() for client in clients), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result


def bridged_provider(
    snapshot: ModelSnapshot,
    peer: JsonPeer,
    *,
    role: str | None = None,
    identity: Callable[[], dict | None] | None = None,
):
    factory = SDKClientFactory(snapshot, peer, role=role, identity=identity)
    config = ProviderConfig(
        provider=snapshot.provider_name,
        model=snapshot.model,
        api_key="ssh-bridge-placeholder",
        base_url=snapshot.endpoint_hint,
        max_tokens=snapshot.output_tokens,
        temperature=snapshot.temperature,
        timeout_seconds=snapshot.timeout_seconds,
        max_retries=snapshot.max_retries,
        connect_timeout_seconds=snapshot.connect_timeout_seconds
        or min(10, snapshot.timeout_seconds),
    )
    return (
        provider_for(config, dialect=snapshot.dialect, client_factory=factory),
        factory,
    )
