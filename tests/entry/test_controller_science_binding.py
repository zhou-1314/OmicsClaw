"""Production configuration mounts the original external scientific runner."""

import asyncio
import hashlib
import json
import platform
import sys
import sysconfig
from pathlib import Path
from types import SimpleNamespace

import pytest

from omicsclaw.entry.env_supply import PinnedEnvironmentSupply
from omicsclaw.entry.ssh.controller import Controller
from omicsclaw.entry.ssh.protocol import RpcError
from omicsclaw.entry.ssh.science_payload import build_payload


def configured_science(tmp_path):
    source = tmp_path / "source/skills/fixture/minimal"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(
        "---\nname: minimal\ndescription: Original worker fixture\nentry: run\n---\n"
    )
    (source / "_api.py").write_text("def run(value='fixture'):\n return value\n")
    payload = tmp_path / "payload"
    manifest = build_payload(tmp_path / "source", payload)
    lock = tmp_path / "science.lock"
    lock.write_text("")
    environment = asyncio.run(
        PinnedEnvironmentSupply(tmp_path / "envs").supply(
            {
                "schemaVersion": 1,
                "role": "science",
                "pythonVersion": platform.python_version(),
                "platform": sys.platform,
                "machine": platform.machine(),
                "abi": sysconfig.get_config_var("SOABI"),
                "lockSha256": hashlib.sha256(b"").hexdigest(),
                "imports": [],
            },
            lock=lock,
            interpreter=Path(sys.executable),
        )
    )
    return {
        "schemaVersion": 1,
        "environmentRoot": environment["root"],
        "environmentDigest": environment["environmentDigest"],
        "payloadRoot": str(payload),
        "payloadDigest": manifest["sourceFilesSha256"],
        "payloadManifestSha256": hashlib.sha256(
            (payload / "science-manifest.json").read_bytes()
        ).hexdigest(),
        "r": None,
    }


def production_params(science):
    model = {
        "provider_id": "test",
        "dialect": "openai_chat",
        "model": "test",
        "allowed_models": ["test"],
        "context_tokens": 8192,
        "output_tokens": 1024,
    }
    return {
        "schemaVersion": 2,
        "installationId": "a" * 32,
        "roles": {
            role: model
            for role in (
                "main",
                "summary",
                "subagent",
                "title",
                "shadow_review",
                "provider_test",
            )
        },
        "science": science,
    }


def test_production_science_configuration_mounts_one_shared_runner_and_agent_tool(
    tmp_path,
):
    selected = configured_science(tmp_path)

    async def run():
        controller = Controller(tmp_path, SimpleNamespace(handlers={}))
        try:
            await controller.configure(production_params(selected))
            health = await controller.desktop_request(
                {"method": "GET", "path": "/health"}
            )
            assert (
                controller.jobs.runner_kinds() == ("skill_run",)
                and controller.jobs.runner_for("skill_run").default_hard_timeout_s
                == 3600.0
                and controller.app.registry.get("run_skill_job") is not None
                and "run_skill_job"
                in {item.name for item in controller.app.tools_snapshot}
                and health["payload"]["capabilities"]["science_runner"] is True
                and health["payload"]["skill_python_executable"]
                == selected["environmentRoot"] + "/venv/bin/python"
            )
        finally:
            await controller.aclose()

    asyncio.run(run())


def test_production_science_missing_ready_does_not_mount_or_import_science(tmp_path):
    selected = configured_science(tmp_path)
    (Path(selected["environmentRoot"]) / "ready.json").unlink()

    async def run():
        controller = Controller(tmp_path, SimpleNamespace(handlers={}))
        try:
            with pytest.raises(RpcError, match="science_environment_not_ready"):
                await controller.configure(production_params(selected))
            assert controller.app is None and controller.jobs is None
        finally:
            await controller.aclose()

    asyncio.run(run())


def test_production_science_payload_pin_cannot_be_replaced_by_remote_self_claim(
    tmp_path,
):
    selected = configured_science(tmp_path)
    manifest = Path(selected["payloadRoot"]) / "science-manifest.json"
    value = json.loads(manifest.read_text())
    value["files"][0]["sha256"] = "0" * 64
    manifest.write_text(json.dumps(value))

    async def run():
        controller = Controller(tmp_path, SimpleNamespace(handlers={}))
        try:
            with pytest.raises(RpcError, match="science_payload_manifest_conflict"):
                await controller.configure(production_params(selected))
        finally:
            await controller.aclose()

    asyncio.run(run())


def test_production_job_approval_uses_the_original_job_gate_once(tmp_path):
    selected = configured_science(tmp_path)

    async def run():
        controller = Controller(tmp_path, SimpleNamespace(handlers={}))
        try:
            await controller.configure(production_params(selected))
            accepted = await controller.desktop_request(
                {
                    "method": "POST",
                    "path": "/jobs",
                    "document": {
                        "skill": "fixture/minimal",
                        "source_request_id": "form-production-once",
                        "inputs": {"value": "fixture"},
                    },
                }
            )
            job_id = accepted["payload"]["job_id"]
            approval = None
            for _ in range(200):
                approval = next(
                    (
                        value
                        for _, kind, value in controller.jobs.replay_events(job_id, 0)
                        if kind == "tool_approval_request"
                    ),
                    None,
                )
                if approval:
                    break
                await asyncio.sleep(0.01)
            assert approval is not None
            assert json.loads(approval["arguments"])["hard_timeout_s"] == 3600.0
            request = {
                "method": "POST",
                "path": f"/jobs/{job_id}/approval/{approval['call_id']}",
                "document": {"decision": "deny"},
            }
            first = await controller.desktop_request(request)
            second = await controller.desktop_request(request)
            assert first["status"] == 200 and second["status"] == 404
        finally:
            await controller.aclose()

    asyncio.run(run())


def test_production_skill_submission_requires_a_durable_request_identity(tmp_path):
    selected = configured_science(tmp_path)

    async def run():
        controller = Controller(tmp_path, SimpleNamespace(handlers={}))
        try:
            await controller.configure(production_params(selected))
            answer = await controller.desktop_request(
                {
                    "method": "POST",
                    "path": "/jobs",
                    "document": {"skill": "fixture/minimal", "inputs": {}},
                }
            )
            assert answer["status"] == 422 and not controller.jobs.list_jobs()
        finally:
            await controller.aclose()

    asyncio.run(run())


def test_concurrent_science_configuration_cannot_cross_the_immutable_snapshot_fence(
    tmp_path,
    monkeypatch,
):
    import threading

    from omicsclaw.entry.ssh import controller as module

    entered, release = threading.Event(), threading.Event()
    calls = []

    def verify(document):
        calls.append(document)
        entered.set()
        assert release.wait(2)
        raise RpcError("owned verifier fixture completed")

    monkeypatch.setattr(module, "verified_science_deployment", verify)

    async def run():
        controller = Controller(tmp_path, SimpleNamespace(handlers={}))
        first = asyncio.create_task(controller.configure(production_params({})))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            second = asyncio.create_task(controller.configure(production_params({})))
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(first, second, return_exceptions=True)
            assert len(calls) == 1
        finally:
            release.set()
            await asyncio.gather(first, return_exceptions=True)
            await controller.aclose()

    asyncio.run(run())
