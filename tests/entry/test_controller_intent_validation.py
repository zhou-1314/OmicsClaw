"""Ordinary duplicate intent uses the same canonical receipt as admission."""

import asyncio
from types import SimpleNamespace

import pytest

from omicsclaw.entry.session import SessionRegistry
from omicsclaw.entry.ssh.controller import Controller
from omicsclaw.entry.ssh.protocol import RpcError


def test_read_validation_preserves_complete_intent_and_restart_receipt(
    tmp_path, monkeypatch
):
    calls = []

    async def blocked(self, handle):
        calls.append(handle.turn_id)
        await asyncio.Event().wait()

    monkeypatch.setattr(SessionRegistry, "_run", blocked)

    async def run():
        role = {
            "provider_id": "test",
            "dialect": "openai_chat",
            "model": "test",
            "allowed_models": ["test"],
            "context_tokens": 8192,
            "output_tokens": 1024,
        }
        configuration = {
            "schemaVersion": 2,
            "installationId": "a" * 32,
            "roles": {
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
        }
        request = {
            "ingress_schema_version": 3,
            "installation_id": "a" * 32,
            "session_id": "same",
            "source_request_id": "1" * 32,
            "content": "once",
            "permission_profile": "default",
            "module_review_requested": False,
        }
        controller = Controller(tmp_path, SimpleNamespace(handlers={}))
        await controller.configure(configuration)
        accepted = await controller.submit(request)
        await asyncio.sleep(0)
        try:
            assert accepted["firstRequest"] is True
            for changed in (
                {"permission_profile": "full_access"},
                {"module_review_requested": True},
                {"content": "/compact"},
                {"content": "different"},
            ):
                assert await controller.validate_intent({**request, **changed}) == {
                    "valid": False,
                    "code": "request_payload_conflict",
                }
            checked = await controller.validate_intent(
                dict(reversed(list(request.items())))
            )
            assert checked["valid"] and checked["turnId"] == accepted["turnId"]
            assert len(calls) == 1
            assert controller.interactions.permission_profile("same") == "default"
            with pytest.raises(RpcError):
                await controller.validate_intent(
                    {**request, "installation_id": "b" * 32}
                )
        finally:
            await controller.aclose()
        restarted = Controller(tmp_path, SimpleNamespace(handlers={}))
        await restarted.configure(configuration)
        try:
            assert (await restarted.validate_intent(request))["turnId"] == accepted[
                "turnId"
            ]
            assert len(calls) == 1 and not restarted.observers
        finally:
            await restarted.aclose()

    asyncio.run(run())
