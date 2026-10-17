"""The assembled SSH application can chat before a Notebook is bound."""

import asyncio
import json
import os
import signal
from types import SimpleNamespace

from omicsclaw.entry.ssh.controller import Controller
from omicsclaw.entry.notebook.security import same_process
from omicsclaw.schema import Message, Role
from tests.engine.test_augmentor import Provider
from tests.entry.test_controller_durable_failure import Peer


def test_real_controller_plain_chat_calls_provider_without_starting_notebook(tmp_path, monkeypatch):
    async def run():
        provider = Provider(Message(role=Role.ASSISTANT, content="Hello from the provider."))

        async def close():
            pass

        monkeypatch.setattr(
            "omicsclaw.entry.ssh.controller.bridged_provider",
            lambda *args, **kwargs: (provider, SimpleNamespace(aclose=close)),
        )
        peer = Peer()
        controller = Controller(tmp_path, peer)
        try:
            await controller.configure({
                "provider_id": "fixture", "dialect": "openai_chat", "model": "fixture",
                "allowed_models": ["fixture"], "context_tokens": 8192, "output_tokens": 1024,
            })
            await controller.submit({
                "ingress_schema_version": 3, "session_id": "plain-chat",
                "source_request_id": "a" * 32, "content": "Hello",
                "permission_profile": "default",
            })
            while controller.observers:
                await asyncio.wait_for(asyncio.gather(*list(controller.observers.values())), 10)
            events = [json.loads(frame["params"]["frame"].split("data: ", 1)[1])
                      for frame in peer.frames if frame.get("method") == "chat.frame"]
            assert provider.seen, events
            assert not [event for event in events if event["type"] == "error"], events
            assert [event for event in events if event["type"] == "done"], events
            health = await controller.notebook_client.attach()
            owner = {k: health["owner"][k] for k in ("ownerId", "ownerEpoch")}
            assert not (await controller.notebook_client.call("list", {}, expected_owner=owner))["items"]
        finally:
            await controller.aclose()
            health = controller.notebook_client.health if controller.notebook_client else None
            if health and same_process(health["owner"]["process"]):
                os.kill(health["owner"]["process"]["pid"], signal.SIGTERM)
                for _ in range(100):
                    if not same_process(health["owner"]["process"]):
                        break
                    await asyncio.sleep(0.05)
                assert not same_process(health["owner"]["process"])

    asyncio.run(run())
