"""Contracts for the Backend-owned AutoAgent process-tree owner."""

from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess
from typing import Any

import pytest

from omicsclaw.autoagent.process_owner import governed_worker_available


def _ipc_root(process_owner: Any, tmp_path: Path) -> Path:
    state_root = tmp_path / "control-state"
    state_root.mkdir()
    return process_owner.prepare_governed_worker_ipc_root(state_root)


def _worker_authority(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    from omicsclaw.autoagent.output_ownership import preclaim_session_output_root

    session_id = "a" * 32
    output_root = preclaim_session_output_root(
        tmp_path / "session-output",
        claim_id=session_id,
    )
    return output_root, {
        "skill_name": "missing-autoagent-test-skill",
        "method": "missing-method",
        "input_path": "",
        "cwd": str(tmp_path),
        "output_dir": str(output_root),
        "output_claim_id": session_id,
        "max_iterations": 1,
        "fixed_params": {},
        "evolution_goal": "",
        "surface_level": 1,
        "explicit_files": [],
        "auto_promote": False,
        "llm_provider": "",
        "llm_model": "",
        "llm_provider_config": None,
        "demo": True,
    }


async def _connect_worker_protocol(
    process_owner: Any,
    command: list[str],
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, dict[str, Any]]:
    """Perform the child half of the IPC-only challenge handshake."""

    assert "--ipc-nonce" not in command
    socket_address = command[command.index("--ipc-address") + 1]
    reader, writer = await asyncio.open_unix_connection(
        "\0" + socket_address[1:]
    )
    await process_owner.write_worker_frame(
        writer,
        {"version": 1, "kind": "hello"},
    )
    challenge = await process_owner.read_worker_frame(reader, max_bytes=4096)
    assert set(challenge) == {"version", "kind", "nonce"}
    assert challenge["version"] == 1
    assert challenge["kind"] == "challenge"
    await process_owner.write_worker_frame(
        writer,
        {
            "version": 1,
            "kind": "challenge_response",
            "nonce": challenge["nonce"],
        },
        max_bytes=4096,
    )
    request = await process_owner.read_worker_frame(reader)
    return reader, writer, request


def test_worker_protocol_rejects_overflowed_json_numbers() -> None:
    from omicsclaw.autoagent import process_owner

    with pytest.raises(process_owner.GovernedWorkerProtocolError):
        process_owner.decode_worker_frame(b'{"value":1e999}')


def test_unsupported_host_rejects_before_any_worker_spawn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from omicsclaw.autoagent import process_owner

    spawned = False

    async def forbidden_spawn(*_args: object, **_kwargs: object) -> object:
        nonlocal spawned
        spawned = True
        raise AssertionError("unsupported AutoAgent owner attempted a spawn")

    monkeypatch.setattr(process_owner, "governed_process_tree_supported", lambda: False)
    monkeypatch.setattr(process_owner, "adrive_subprocess", forbidden_spawn)
    output_root, worker_request = _worker_authority(tmp_path)

    worker = process_owner.GovernedAutoAgentWorker(
        session_id="a" * 32,
        execution_reference_type=process_owner.LINUX_SYSTEMD_OWNER_REFERENCE_TYPE,
        execution_reference="omicsclaw-run-" + "b" * 24 + ".scope",
        cwd=tmp_path,
        writable_output_root=output_root,
        ipc_root=tmp_path / "not-created",
        request=worker_request,
    )

    with pytest.raises(process_owner.GovernedWorkerUnavailable):
        asyncio.run(worker.run())

    assert spawned is False


def test_success_is_not_exposed_until_the_governed_tree_is_confirmed_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from omicsclaw.autoagent import process_owner

    async def scenario() -> None:
        provider_secret = "autoagent-provider-secret-must-use-ipc"
        monkeypatch.setenv("OPENAI_API_KEY", provider_secret)
        monkeypatch.setenv("LLM_API_KEY", provider_secret)
        terminal_sent = asyncio.Event()
        release_stop_proof = asyncio.Event()

        async def fake_governed_driver(cmd: list[str], **kwargs: object):
            child_env = kwargs["env"]
            assert isinstance(child_env, dict)
            assert kwargs["stdio"] == "devnull"
            assert provider_secret not in "\0".join(cmd)
            assert provider_secret not in child_env.values()
            async def child() -> None:
                reader, writer, request = await _connect_worker_protocol(
                    process_owner,
                    cmd,
                )
                assert request["kind"] == "request"
                payload = request["payload"]
                assert payload["llm_provider_config"]["api_key"] == provider_secret
                await process_owner.write_worker_frame(
                    writer,
                    {
                        "version": 1,
                        "kind": "terminal",
                        "status": "done",
                        "result": {
                            "success": True,
                            "skill": payload["skill_name"],
                            "method": payload["method"],
                            "evolution_goal": payload["evolution_goal"],
                            "output_dir": payload["output_dir"],
                            "score": 1.0,
                        },
                    },
                )
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                terminal_sent.set()

            child_task = asyncio.create_task(child())
            await terminal_sent.wait()
            await release_stop_proof.wait()
            await child_task
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(
            process_owner,
            "governed_process_tree_supported",
            lambda: True,
        )
        monkeypatch.setattr(process_owner, "adrive_subprocess", fake_governed_driver)
        output_root, worker_request = _worker_authority(tmp_path)
        worker_request["llm_provider_config"] = {
            "provider": "test",
            "base_url": "https://provider.invalid/v1",
            "model": "test-model",
            "api_key": provider_secret,
        }
        worker = process_owner.GovernedAutoAgentWorker(
            session_id="a" * 32,
            execution_reference_type=process_owner.LINUX_SYSTEMD_OWNER_REFERENCE_TYPE,
            execution_reference="omicsclaw-run-" + "b" * 24 + ".scope",
            cwd=tmp_path,
            writable_output_root=output_root,
            ipc_root=_ipc_root(process_owner, tmp_path),
            request=worker_request,
        )

        task = asyncio.create_task(worker.run())
        await asyncio.wait_for(terminal_sent.wait(), timeout=2)
        await asyncio.sleep(0)
        assert task.done() is False

        release_stop_proof.set()
        outcome = await asyncio.wait_for(task, timeout=2)
        assert outcome.status == "done"
        assert outcome.result is not None
        assert outcome.result["success"] is True
        assert outcome.result["score"] == 1.0
        assert worker.process_tree_confirmed_empty is True

    asyncio.run(scenario())


def test_cancel_waits_for_stubborn_worker_stop_proof(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from omicsclaw.autoagent import process_owner

    async def scenario() -> None:
        spawned = asyncio.Event()
        termination_started = asyncio.Event()
        release_stop_proof = asyncio.Event()

        async def stubborn_driver(_cmd: list[str], **_kwargs: object):
            spawned.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                termination_started.set()
                await release_stop_proof.wait()
                raise

        monkeypatch.setattr(
            process_owner,
            "governed_process_tree_supported",
            lambda: True,
        )
        monkeypatch.setattr(process_owner, "adrive_subprocess", stubborn_driver)
        output_root, worker_request = _worker_authority(tmp_path)
        worker = process_owner.GovernedAutoAgentWorker(
            session_id="a" * 32,
            execution_reference_type=process_owner.LINUX_SYSTEMD_OWNER_REFERENCE_TYPE,
            execution_reference="omicsclaw-run-" + "b" * 24 + ".scope",
            cwd=tmp_path,
            writable_output_root=output_root,
            ipc_root=_ipc_root(process_owner, tmp_path),
            request=worker_request,
        )
        run_task = asyncio.create_task(worker.run())
        await asyncio.wait_for(spawned.wait(), timeout=2)

        worker.request_cancel()
        await asyncio.wait_for(termination_started.wait(), timeout=2)
        await asyncio.sleep(0)
        assert run_task.done() is False

        release_stop_proof.set()
        outcome = await asyncio.wait_for(run_task, timeout=2)
        assert outcome.status == "cancelled"
        assert outcome.error_code == "cancelled"
        assert worker.process_tree_confirmed_empty is True

    asyncio.run(scenario())


def test_setup_failure_reconciles_owner_before_cleaning_runtime_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from omicsclaw.autoagent import process_owner

    async def scenario() -> None:
        reconciled = False

        async def absent_owner(_reference_type: str, _reference: str) -> bool:
            nonlocal reconciled
            reconciled = True
            return False

        async def broken_server(*_args: object, **_kwargs: object) -> object:
            raise OSError("injected socket setup failure")

        monkeypatch.setattr(
            process_owner,
            "governed_process_tree_supported",
            lambda: True,
        )
        monkeypatch.setattr(
            process_owner,
            "reconcile_governed_process_tree",
            absent_owner,
        )
        monkeypatch.setattr(process_owner.asyncio, "start_unix_server", broken_server)
        output_root, worker_request = _worker_authority(tmp_path)
        ipc_root = _ipc_root(process_owner, tmp_path)
        worker = process_owner.GovernedAutoAgentWorker(
            session_id="a" * 32,
            execution_reference_type=process_owner.LINUX_SYSTEMD_OWNER_REFERENCE_TYPE,
            execution_reference="omicsclaw-run-" + "b" * 24 + ".scope",
            cwd=tmp_path,
            writable_output_root=output_root,
            ipc_root=ipc_root,
            request=worker_request,
        )

        outcome = await worker.run()

        assert outcome.status == "error"
        assert outcome.error_code == "worker_start_failed"
        assert reconciled is True
        assert worker.process_tree_confirmed_empty is True
        assert not process_owner.governed_worker_ipc_directory(
            ipc_root,
            "a" * 32,
        ).exists()

    asyncio.run(scenario())


def test_cumulative_event_byte_budget_stops_the_owned_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from omicsclaw.autoagent import process_owner

    async def scenario() -> None:
        async def noisy_driver(cmd: list[str], **_kwargs: object):
            reader, writer, _request = await _connect_worker_protocol(
                process_owner,
                cmd,
            )
            for _ in range(2):
                await process_owner.write_worker_frame(
                    writer,
                    {
                        "version": 1,
                        "kind": "event",
                        "event_type": "progress",
                        "data": {"message": "x" * 80},
                    },
                )
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                writer.close()
                await writer.wait_closed()
                raise

        monkeypatch.setattr(
            process_owner,
            "governed_process_tree_supported",
            lambda: True,
        )
        monkeypatch.setattr(process_owner, "adrive_subprocess", noisy_driver)
        monkeypatch.setattr(process_owner, "_MAX_TOTAL_EVENT_BYTES", 200)
        output_root, worker_request = _worker_authority(tmp_path)
        worker = process_owner.GovernedAutoAgentWorker(
            session_id="a" * 32,
            execution_reference_type=process_owner.LINUX_SYSTEMD_OWNER_REFERENCE_TYPE,
            execution_reference="omicsclaw-run-" + "b" * 24 + ".scope",
            cwd=tmp_path,
            writable_output_root=output_root,
            ipc_root=_ipc_root(process_owner, tmp_path),
            request=worker_request,
        )

        outcome = await asyncio.wait_for(worker.run(), timeout=2)

        assert outcome.status == "error"
        assert outcome.error_code == "worker_crashed"
        assert worker.process_tree_confirmed_empty is True

    asyncio.run(scenario())


def test_full_harness_entrypoint_runs_only_in_the_child_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from omicsclaw.autoagent import process_owner

    async def scenario() -> None:
        async def local_process_driver(
            cmd: list[str],
            **kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(kwargs["cwd"]),
                env=kwargs["env"],
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            return subprocess.CompletedProcess(
                cmd,
                proc.returncode,
                stdout.decode("utf-8", errors="replace"),
                stderr.decode("utf-8", errors="replace"),
            )

        monkeypatch.setattr(
            process_owner,
            "governed_process_tree_supported",
            lambda: True,
        )
        monkeypatch.setattr(
            process_owner,
            "adrive_subprocess",
            local_process_driver,
        )
        output_root, worker_request = _worker_authority(tmp_path)
        worker = process_owner.GovernedAutoAgentWorker(
            session_id="a" * 32,
            execution_reference_type=process_owner.LINUX_SYSTEMD_OWNER_REFERENCE_TYPE,
            execution_reference="omicsclaw-run-" + "b" * 24 + ".scope",
            cwd=tmp_path,
            writable_output_root=output_root,
            ipc_root=_ipc_root(process_owner, tmp_path),
            request=worker_request,
        )

        outcome = await asyncio.wait_for(worker.run(), timeout=10)
        assert outcome.status == "error"
        assert outcome.error_code == "harness_failed"
        assert worker.process_tree_confirmed_empty is True

    asyncio.run(scenario())


@pytest.mark.skipif(
    not governed_worker_available(),
    reason="no Linux user-systemd+bwrap process owner",
)
def test_native_linux_owner_accepts_userns_peer_and_closes_exact_scope(
    tmp_path: Path,
) -> None:
    from omicsclaw.autoagent import process_owner

    async def scenario() -> None:
        output_root, worker_request = _worker_authority(tmp_path)
        ipc_root = _ipc_root(process_owner, tmp_path)
        reference_type, reference = process_owner.new_governed_worker_reference()
        worker = process_owner.GovernedAutoAgentWorker(
            session_id="a" * 32,
            execution_reference_type=reference_type,
            execution_reference=reference,
            cwd=tmp_path,
            writable_output_root=output_root,
            ipc_root=ipc_root,
            request=worker_request,
        )

        outcome = await asyncio.wait_for(worker.run(), timeout=20)

        assert outcome.status == "error"
        assert outcome.error_code == "harness_failed"
        assert worker.process_tree_confirmed_empty is True
        assert (
            await process_owner.reconcile_governed_worker(
                reference_type,
                reference,
                ipc_root=ipc_root,
                session_id="a" * 32,
            )
            == process_owner.OWNER_STOP_EVIDENCE_CODE
        )
        assert not process_owner.governed_worker_ipc_directory(
            ipc_root,
            "a" * 32,
        ).exists()

    asyncio.run(scenario())
