"""Production RPC identities map to the shared Desktop ingress namespace."""

import asyncio
from types import SimpleNamespace
import pytest
from omicsclaw.entry.session import SessionRegistry
from omicsclaw.entry.ssh.controller import Controller
from omicsclaw.entry.desktop.turn_submission import ingress_identity


def test_receipt_and_caller_stay_in_configured_installation(tmp_path, monkeypatch):
    async def blocked(self, handle):
        await asyncio.Event().wait()

    monkeypatch.setattr(SessionRegistry, "_run", blocked)

    async def run():
        controller = Controller(tmp_path, SimpleNamespace(handlers={}))
        role = dict(
            provider_id="test",
            dialect="openai_chat",
            model="test",
            allowed_models=["test"],
            context_tokens=8192,
            output_tokens=1024,
        )
        installation = "a" * 32
        request = dict(
            ingress_schema_version=3,
            installation_id=installation,
            session_id="same",
            source_request_id="1" * 32,
            content="once",
        )
        try:
            await controller.configure(
                dict(
                    schemaVersion=2,
                    installationId=installation,
                    roles={
                        name: role
                        for name in (
                            "main",
                            "summary",
                            "subagent",
                            "title",
                            "shadow_review",
                            "provider_test",
                        )
                    },
                )
            )
            admitted = await controller.submit(request)
            await asyncio.sleep(0)
            receipt = (
                await controller.lookup(
                    dict(sessionId="same", sourceRequestId="1" * 32)
                )
            )["receipt"]
            assert receipt["turn_id"] == admitted["turnId"]
            assert (
                receipt["source_request_id"]
                == f"{ingress_identity(installation)[2]}/{'1' * 32}"
            )
            assert (
                controller.caller_request_id(receipt["source_request_id"]) == "1" * 32
            )
            assert (await controller.session_live({"sessionId": "same"}))[
                "source_request_id"
            ] == "1" * 32
            with pytest.raises(Exception):
                await controller.submit({**request, "installation_id": "b" * 32})
            with pytest.raises(Exception):
                controller.caller_request_id(f"{ingress_identity('b'*32)[2]}/{'1'*32}")
        finally:
            await controller.aclose()

    asyncio.run(run())
