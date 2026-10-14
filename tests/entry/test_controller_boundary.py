"""The SSH controller shares assembly without loading the HTTP surface."""

import asyncio
import json
import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

from omicsclaw.entry import AppConfig, build_app
from omicsclaw.provider import ModelLimits


def test_manager_import_does_not_load_http_server():
    output = subprocess.check_output(
        [
            sys.executable,
            "-c",
            (
                "import sys,json; import omicsclaw.entry.desktop.jobs_manager; "
                "print(json.dumps(sorted(sys.modules)))"
            ),
        ],
        text=True,
    )
    modules = json.loads(output)
    assert "omicsclaw.entry.desktop.server" not in modules
    assert "fastapi" not in modules


def test_base_assembly_uses_mac_model_limits_without_kernel(tmp_path, monkeypatch):
    from omicsclaw.entry.assembly import DeploymentCapabilities
    from omicsclaw.provider import ProviderConfig, provider_for

    monkeypatch.setenv("OMICSCLAW_MODEL", "ambient-model")
    config = AppConfig(workspace=tmp_path, model="mac-model", provider="custom")
    provider = provider_for(ProviderConfig(provider="custom", model="mac-model"))
    app = build_app(
        config,
        provider=provider,
        resolved_model="mac-model",
        model_limits=ModelLimits(16384, 1024),
        capabilities=DeploymentCapabilities(notebook=False),
    )
    try:
        assert app.budget.context_tokens == 16384
        assert app.budget.reserve_output_tokens == 1024
        assert app.kernels is None
        assert "python" not in {tool.name for tool in app.tools_snapshot}
        from omicsclaw.entry.desktop.jobs_manager import JobsManager

        manager = JobsManager(app, register_default_runners=False)
        assert manager.runner_kinds() == ()
        assert manager.kernels is None
        manager.store.database.close()
    finally:
        asyncio.run(app.aclose())


def test_notebook_binding_preserves_selected_science_interpreter(tmp_path):
    from omicsclaw.entry.assembly import WorkspaceKernelBinding

    binding = WorkspaceKernelBinding(tmp_path, python="/selected/science/bin/python")
    assert binding.manager.python == "/selected/science/bin/python"
    asyncio.run(binding.aclose())


def test_controller_refuses_a_profile_without_durable_admission(tmp_path, monkeypatch):
    from omicsclaw.entry.ssh import controller as module
    from omicsclaw.entry.ssh.protocol import RpcError

    original = module.build_app

    def disabled(config, **kwargs):
        return original(replace(config, durable_turn_events=False), **kwargs)

    monkeypatch.setattr(module, "build_app", disabled)

    async def run():
        controller = module.Controller(tmp_path, SimpleNamespace(handlers={}))
        try:
            with pytest.raises(RpcError, match="durable"):
                await controller.configure(
                    {
                        "provider_id": "test",
                        "dialect": "openai_chat",
                        "model": "test",
                        "allowed_models": ["test"],
                        "context_tokens": 8192,
                        "output_tokens": 1024,
                    }
                )
        finally:
            await controller.aclose()

    asyncio.run(run())


def test_hello_identity_survives_controller_restart(tmp_path):
    from omicsclaw.entry.ssh.controller import Controller

    async def run():
        first = Controller(tmp_path, SimpleNamespace(handlers={}))
        try:
            initial = await first.hello({"protocolVersion": 1})
        finally:
            await first.aclose()
        second = Controller(tmp_path, SimpleNamespace(handlers={}))
        try:
            restored = await second.hello({"protocolVersion": 1})
            assert restored["workspaceId"] == initial["workspaceId"]
            assert restored["authorityId"] == initial["authorityId"]
            assert restored["controllerInstanceId"] != initial["controllerInstanceId"]
        finally:
            await second.aclose()

    asyncio.run(run())


def test_duplicate_request_cannot_change_permission_profile(tmp_path, monkeypatch):
    from omicsclaw.entry.session import SessionRegistry, SubmissionRefused
    from omicsclaw.entry.ssh.controller import Controller

    async def blocked_pump(_self, _lane):
        await asyncio.Event().wait()

    monkeypatch.setattr(SessionRegistry, "_pump", blocked_pump)

    async def run():
        controller = Controller(tmp_path, SimpleNamespace(handlers={}))
        try:
            await controller.configure(
                {
                    "provider_id": "test",
                    "dialect": "openai_chat",
                    "model": "test",
                    "allowed_models": ["test"],
                    "context_tokens": 8192,
                    "output_tokens": 1024,
                }
            )
            request = {
                "ingress_schema_version": 3,
                "session_id": "same",
                "source_request_id": "1" * 32,
                "content": "once",
                "permission_profile": "default",
            }
            await controller.submit(request)
            with pytest.raises(SubmissionRefused):
                await controller.submit(
                    {**request, "permission_profile": "full_access"}
                )
            assert controller.interactions.permission_profile("same") == "default"
        finally:
            await controller.aclose()

    asyncio.run(run())
