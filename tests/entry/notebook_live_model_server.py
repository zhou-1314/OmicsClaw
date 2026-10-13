"""Opt-in real-provider Desktop server for synthetic live-app browser acceptance."""

from __future__ import annotations

import argparse
import json
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from omicsclaw.entry import AppConfig, attach_sessions, build_app
from omicsclaw.entry.desktop import create_desktop_app
from omicsclaw.provider import provider_for, resolve_config
from omicsclaw.schema import StreamChunkType
from tests.evals.live.conftest import _environment


class RecordedProvider:
    """Record actual provider traffic without changing messages or completions."""

    def __init__(self, provider, log):
        self.provider = provider
        self.log = log

    @property
    def name(self):
        return self.provider.name

    def record(self, kind, **data):
        with self.log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"kind": kind, **data}, ensure_ascii=False) + "\n")

    def request(self, messages):
        self.record("request", messages=[asdict(message) for message in messages])

    async def generate(self, messages, tools=None):
        self.request(messages)
        result = await self.provider.generate(messages, tools)
        self.record("completion", **asdict(result))
        return result

    async def generate_stream(self, messages, tools=None):
        self.request(messages)
        async for chunk in self.provider.generate_stream(messages, tools):
            if chunk.type is StreamChunkType.DONE:
                self.record(
                    "completion",
                    message=asdict(chunk.message),
                    usage=asdict(chunk.usage),
                )
            yield chunk

    def bind(self, **overrides):
        return RecordedProvider(self.provider.bind(**overrides), self.log)


def main():
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--model-log", required=True, type=Path)
    args = parser.parse_args()
    args.workspace.mkdir(parents=True, exist_ok=True)
    args.model_log.parent.mkdir(parents=True, exist_ok=True)
    config = resolve_config(
        env=_environment(), max_tokens=8192, timeout_seconds=120, max_retries=0
    )
    provider = RecordedProvider(provider_for(config), args.model_log)
    provider.record("model", provider=config.provider, model=config.model)
    app = build_app(
        AppConfig(
            workspace=args.workspace,
            provider=config.provider,
            model=config.model,
            max_turns=10,
        ),
        provider=provider,
    )
    api = create_desktop_app(attach_sessions(app, abandon_grace_s=30))
    desktop_lifespan = api.router.lifespan_context

    @asynccontextmanager
    async def lifespan(server):
        try:
            async with desktop_lifespan(server):
                yield
        finally:
            await app.aclose()

    api.router.lifespan_context = lifespan
    uvicorn.run(api, host="127.0.0.1", port=args.port, log_level="warning", ws="none")


if __name__ == "__main__":
    main()
