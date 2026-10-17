"""The original LocalTarget Job transport runs a contained scientific worker."""

import asyncio
import json
import os
import secrets
import signal
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from omicsclaw.entry.environment_coordination import (
    EnvironmentCoordinator,
    root_evidence,
)
from omicsclaw.entry.environment_jobs import ScienceEnvironmentGuard
from omicsclaw.entry.environment_runtime import ProcessEnvironmentGuard
from omicsclaw.entry.notebook.security import process_evidence, same_process
from omicsclaw.entry.process_containment import contained_exit
from omicsclaw.entry.ssh.science_payload import build_payload
from omicsclaw.remote.jobs import KIND_NOHUP, build_job_scripts
from omicsclaw.remote.local import LocalTargetSpawner
from omicsclaw.remote.ssh import build_ssh_argv, shell_quote
from tests.entry.test_science_worker import make_skill


@pytest.mark.parametrize("termination", ["normal", "cancel", "supervisor_crash"])
def test_native_localtarget_worker_releases_scope_and_kills_detached_child(
    tmp_path, termination
):
    if not sys.platform.startswith("linux"):
        pytest.skip("Linux subreaper required")

    async def run():
        prefix = tmp_path / "venv"
        created = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "venv",
            "--without-pip",
            "--system-site-packages",
            str(prefix),
        )
        assert await created.wait() == 0
        interpreter = prefix / "bin/python"
        source = tmp_path / "source"
        skill = make_skill(source)
        (skill / "kernel.py").write_text(
            "__all__=['run']\n"
            "def run():\n"
            " '''Spawn a detached child under the owned supervisor.'''\n"
            " import os,time,json\n from pathlib import Path\n"
            " p=os.fork()\n"
            " if p==0:\n"
            "  os.setsid(); p=os.fork()\n"
            "  if p: os._exit(0)\n"
            "  from omicsclaw.entry.notebook.security import process_evidence\n"
            "  host=int(Path('/proc/self').resolve().name)\n"
            "  Path('descendant.tmp').write_text(json.dumps(process_evidence(host))); os.replace('descendant.tmp','descendant.json')\n"
            "  time.sleep(60); os._exit(0)\n"
            " for i in range(200):\n"
            "  if Path('descendant.json').exists(): break\n"
            "  time.sleep(.01)\n"
            + (" time.sleep(60)\n" if termination != "normal" else "")
            + " return {'value':42}\n"
        )
        payload = tmp_path / "payload"
        manifest = build_payload(source, payload)
        workspace = tmp_path / "job"
        workspace.mkdir()
        coordinator = EnvironmentCoordinator(tmp_path / "global")
        guard = ScienceEnvironmentGuard(
            ProcessEnvironmentGuard(tmp_path, coordinator=coordinator),
            interpreter=interpreter,
        )
        science = {
            "python": str(interpreter),
            "payloadRoot": str(payload),
            "payloadDigest": manifest["sourceFilesSha256"],
        }
        record = SimpleNamespace(
            id="native-job",
            skill="fixture/resource",
            inputs={"_science": science},
            target_workdir=str(workspace),
        )
        await guard.reserve(record)
        guard.committed(record)
        claim = guard.request(record)
        guard.dispatching(record)
        request = {
            "schemaVersion": 1,
            "jobId": record.id,
            "skill": record.skill,
            "inputs": {},
            "payloadRoot": str(payload),
            "workspace": str(workspace),
            "payloadManifest": manifest,
            "payloadDigest": manifest["sourceFilesSha256"],
            "environmentReservation": claim,
        }
        request_path = workspace / "request.json"
        request_path.write_text(json.dumps(request))
        bootstrap = tmp_path / "test-worker.py"
        bootstrap.write_text(
            "from pathlib import Path\nfrom omicsclaw.entry import environment_jobs\n"
            f"environment_jobs.coordinator_directory=lambda:Path({str(coordinator.directory)!r})\n"
            "from omicsclaw.entry.ssh.science_worker import main\nmain()\n"
        )
        component = Path(__file__).resolve().parents[2]
        command = f"cd {shell_quote(str(workspace))} && env PYTHONPATH={shell_quote(str(component))} {shell_quote(str(interpreter))} {shell_quote(str(bootstrap))} --request {shell_quote(str(request_path))}"
        if termination != "normal":
            await cancelled_job(
                command,
                workspace,
                coordinator,
                guard,
                record,
                prefix,
                kill_supervisor=termination == "supervisor_crash",
            )
            return
        outcome = await LocalTargetSpawner(tmp_path).spawn(
            build_ssh_argv("controller", command), timeout=20
        )
        assert outcome.exit_code == 0, outcome.output
        guard.finished(record)
        row = coordinator.get(guard.key(record.id))
        proof = json.loads(row["containment_json"] or "null")
        assert proof and row["state"] == "closed" and row["uncertain_exit"] == 0
        assert not same_process(proof["root"]) and not same_process(proof["child"])
        descendant = json.loads((workspace / "descendant.json").read_text())
        assert descendant and not same_process(descendant)
        assert coordinator.impact([root_evidence(prefix, role="install_write")]) == []
        assert (
            json.loads((workspace / "_omicsclaw_worker.json").read_text())[
                "payloadDigest"
            ]
            == manifest["sourceFilesSha256"]
        )

    asyncio.run(run())


async def cancelled_job(
    command, workspace, coordinator, guard, record, prefix, *, kill_supervisor
):
    for name, content in build_job_scripts(command, KIND_NOHUP).items():
        (workspace / name).write_text(content)
    (workspace / "_omicsclaw_nonce").write_text(secrets.token_hex(16))
    launcher = await asyncio.create_subprocess_exec(
        "/bin/bash",
        "./_omicsclaw_launch.sh",
        cwd=workspace,
        start_new_session=True,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    birth = process_evidence(launcher.pid)
    proof, descendant = None, None
    try:
        for _ in range(500):
            if (workspace / "descendant.json").exists():
                break
            await asyncio.sleep(0.01)
        descendant = json.loads((workspace / "descendant.json").read_text())
        row = coordinator.get(guard.key(record.id))
        proof = json.loads(row["containment_json"])
        assert row["state"] == "active" and row["dispatch_state"] == "started"
        assert same_process(proof["root"]) and same_process(descendant)
        if kill_supervisor:
            os.kill(proof["root"]["pid"], signal.SIGKILL)
        else:
            assert same_process(birth) and os.getpgid(launcher.pid) == launcher.pid
            os.killpg(launcher.pid, signal.SIGTERM)
        await asyncio.wait_for(launcher.wait(), 10)
        if kill_supervisor:
            assert not contained_exit(proof, proof["root"])
            assert same_process(descendant)
        else:
            assert (
                workspace / "_omicsclaw_exit_code"
            ).read_text().strip() == "__OMICSCLAW_CANCELED__"
            for _ in range(400):
                if contained_exit(proof, proof["root"]):
                    break
                await asyncio.sleep(0.01)
            assert contained_exit(proof, proof["root"])
            assert not same_process(proof["child"]) and not same_process(descendant)
        # The observer has no live guard or worker state from this interpreter.
        observer = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import json,sys; from pathlib import Path; "
            "from omicsclaw.entry.environment_coordination import EnvironmentCoordinator,root_evidence; "
            "c=EnvironmentCoordinator(Path(sys.argv[1])); "
            "c.reap_dead_controls(); c.reap_unsent_jobs(); c.reap_finished_jobs(); c.reap_contained(); "
            "print(json.dumps({'row':c.get(sys.argv[2]),'impact':c.impact([root_evidence(sys.argv[3],role='install_write')])}))",
            str(coordinator.directory),
            guard.key(record.id),
            str(prefix),
            cwd=Path(__file__).resolve().parents[2],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        output, errors = await asyncio.wait_for(observer.communicate(), 10)
        assert observer.returncode == 0, errors.decode()
        result = json.loads(output)
        if kill_supervisor:
            assert result["row"]["state"] == "active"
            assert any(
                item["reservation_id"] == guard.key(record.id)
                for item in result["impact"]
            )
        else:
            assert result["row"]["state"] == "closed"
            assert result["row"]["dispatch_state"] == "terminal"
            assert result["row"]["uncertain_exit"] == 0
            assert result["impact"] == []
    finally:
        if same_process(birth):
            os.killpg(birth["pid"], signal.SIGTERM)
        await asyncio.wait_for(launcher.wait(), 10)
        owned = [
            descendant,
            proof.get("child") if proof else None,
            proof.get("root") if proof else None,
        ]
        for evidence in owned:
            if same_process(evidence):
                os.kill(evidence["pid"], signal.SIGKILL)
        for _ in range(300):
            if not any(same_process(evidence) for evidence in owned):
                break
            await asyncio.sleep(0.01)
        assert not any(same_process(evidence) for evidence in owned)
