"""Chat admission is one shared service for finite RPC and legacy HTTP."""

import asyncio
import sys
import subprocess
from types import SimpleNamespace


def test_chat_service_import_does_not_construct_or_import_http_server():
    subprocess.check_call(
        [
            sys.executable,
            "-c",
            "import sys; from omicsclaw.entry.desktop.chat_service import admit_chat; assert callable(admit_chat); assert 'omicsclaw.entry.desktop.server' not in sys.modules; assert 'fastapi' not in sys.modules",
        ]
    )


def test_shared_admission_does_not_replay_permission_side_effect_on_duplicate(
    tmp_path, monkeypatch
):
    from omicsclaw.entry.desktop.chat_service import admit_chat
    from omicsclaw.entry.session import SessionRegistry
    from omicsclaw.entry.ssh.controller import Controller

    async def blocked_pump(_self, _lane):
        await asyncio.Event().wait()

    monkeypatch.setattr(SessionRegistry, "_pump", blocked_pump)

    async def run():
        controller = Controller(tmp_path, SimpleNamespace(handlers={}))
        await controller.configure(
            {
                "provider_id": "fixture",
                "dialect": "openai_chat",
                "model": "primary",
                "allowed_models": ["primary"],
                "context_tokens": 8192,
                "output_tokens": 1024,
                "revision": 1,
            }
        )
        try:
            app = controller.app
            interactions = controller.interactions
            first = {
                "ingress_schema_version": 3,
                "session_id": "shared-admit",
                "source_request_id": "1" * 32,
                "content": "one",
                "permission_profile": "full_access",
            }
            second = {
                **first,
                "source_request_id": "2" * 32,
                "content": "two",
                "permission_profile": "default",
            }
            one = await admit_chat(app, first, interactions=interactions)
            await admit_chat(app, second, interactions=interactions)
            repeated = await admit_chat(app, first, interactions=interactions)
            assert repeated.handle.turn_id == one.handle.turn_id
            assert not repeated.newly_admitted
            assert interactions.permission_profile("shared-admit") == "default"
        finally:
            await controller.aclose()

    asyncio.run(run())
