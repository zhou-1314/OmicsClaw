"""Science payload and worker behavior through actual finite processes."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_file_markers_bind_both_matrix_and_metadata_without_scientific_algorithm_copy(
    tmp_path,
):
    from types import SimpleNamespace

    from omicsclaw.entry.desktop.jobs_manager import SkillApiRunner

    (tmp_path / "expression.csv").write_text("gene,s1,s2\ng1,1,2\ng2,3,4\n")
    (tmp_path / "metadata.csv").write_text("sample,batch\ns1,a\ns2,b\n")

    def entry(data, *, batches):
        return data, batches

    values = SkillApiRunner(SimpleNamespace())._bind_arguments(
        entry,
        {
            "data": {"$file": "expression.csv", "indexColumn": 0},
            "batches": {"$file": "metadata.csv"},
        },
        tmp_path,
    )
    assert list(values["data"].index) == ["g1", "g2"] and list(
        values["batches"]["sample"]
    ) == ["s1", "s2"]


def test_file_marker_refuses_a_workspace_escape_before_loading(tmp_path):
    from types import SimpleNamespace

    from omicsclaw.entry.desktop.jobs_manager import JobFailure, SkillApiRunner

    workspace = tmp_path / "work"
    workspace.mkdir()
    (tmp_path / "outside.csv").write_text("value\n1\n")

    def entry(data):
        return data

    with pytest.raises(JobFailure, match="file marker"):
        SkillApiRunner(SimpleNamespace())._bind_arguments(
            entry, {"data": {"$file": "../outside.csv"}}, workspace
        )


def make_skill(root):
    directory = root / "skills" / "fixture" / "resource"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: resource\ndescription: Read a declared resource\nentry: run\n---\n"
    )
    (directory / "table.txt").write_text("resource-value")
    (directory / "kernel.py").write_text(
        "__all__=['run']\n"
        "def run(output='results/value.json'):\n"
        " '''Read the payload resource.'''\n from pathlib import Path\n import os,json\n"
        " p=Path(output);p.parent.mkdir(parents=True,exist_ok=True)\n"
        " p.write_text(json.dumps({'value':Path(__file__).with_name('table.txt').read_text(),'pid':os.getpid()}))\n"
        " return {'output':str(p)}\n"
    )
    return directory


def test_payload_preserves_resources_and_refuses_changed_installed_bytes(tmp_path):
    from omicsclaw.entry.ssh.science_payload import build_payload, verify_payload

    source = tmp_path / "source"
    skill = make_skill(source)
    destination = tmp_path / "supplied"
    manifest = build_payload(source, destination)
    digest = manifest["sourceFilesSha256"]
    verify_payload(destination, manifest, expected_digest=digest)
    resource = destination / skill.relative_to(source) / "table.txt"
    assert resource.read_text() == "resource-value"
    resource.write_text("resource-drift")
    with pytest.raises(ValueError, match="integrity"):
        verify_payload(destination, manifest, expected_digest=digest)


def test_payload_rejects_symlinks_instead_of_copying_an_external_resource(tmp_path):
    from omicsclaw.entry.ssh.science_payload import build_payload

    source = tmp_path / "source"
    skill = make_skill(source)
    (skill / "outside.txt").symlink_to(tmp_path / "unowned")
    with pytest.raises(ValueError, match="symlink"):
        build_payload(source, tmp_path / "supplied")


def test_worker_calls_original_api_in_selected_process_with_payload_resource(tmp_path):
    from omicsclaw.entry.ssh.science_payload import build_payload

    source = tmp_path / "source"
    make_skill(source)
    payload = tmp_path / "supplied"
    manifest = build_payload(source, payload)
    workspace = tmp_path / "work"
    workspace.mkdir()
    request = workspace / "request.json"
    request.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "jobId": "owned-worker",
                "skill": "fixture/resource",
                "inputs": {},
                "payloadRoot": str(payload),
                "workspace": str(workspace),
                "payloadManifest": manifest,
                "payloadDigest": manifest["sourceFilesSha256"],
            }
        )
    )
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "omicsclaw.entry.ssh.science_worker",
            "--request",
            str(request),
        ],
        cwd=workspace,
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(Path(__file__).parents[2]),
            "PYTHONNOUSERSITE": "1",
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert process.returncode == 0, process.stderr
    result = json.loads((workspace / "results" / "value.json").read_text())
    proof = json.loads((workspace / "_omicsclaw_worker.json").read_text())
    assert (
        result["value"],
        result["pid"] == proof["pid"],
        proof["pid"] != os.getpid(),
        proof["python"],
    ) == ("resource-value", True, True, sys.executable)
    events = [
        json.loads(line)
        for line in (workspace / "_omicsclaw_worker_events.jsonl")
        .read_text()
        .splitlines()
    ]
    assert [event["type"] for event in events] == [
        "tool_started",
        "progress",
        "tool_output_chunk",
    ]


def test_scientific_result_writer_keeps_named_indexes_and_plain_json(tmp_path):
    import numpy as np
    import pandas as pd
    from omicsclaw.entry.ssh.science_worker import write_result

    table = pd.DataFrame({"value": [1]}, index=pd.Index(["gene"], name="id"))
    write_result(table, tmp_path / "table.csv", ".csv")
    assert (tmp_path / "table.csv").read_text() == "id,value\ngene,1\n"
    write_result({"values": np.array([1, 2]), "path": Path("data.csv")}, tmp_path / "value.json", ".json")
    assert json.loads((tmp_path / "value.json").read_text()) == {"values": [1, 2], "path": "data.csv"}


@pytest.mark.parametrize("entry", ["form", "agent"])
def test_form_job_uses_canonical_science_worker_and_original_approval(tmp_path, entry):
    import asyncio
    from types import SimpleNamespace

    from omicsclaw.entry.desktop.job_service import create_job_from_document
    from omicsclaw.entry.desktop.jobs_manager import JobsManager, JobStore
    from omicsclaw.entry.ssh.science_payload import build_payload
    from omicsclaw.entry.ssh.science_runner import ScienceJobRunner
    from omicsclaw.memory.database import Database
    from omicsclaw.remote.bridge import RemoteJobsBridge
    from omicsclaw.remote.local import LocalTargetSpawner
    from omicsclaw.remote.plane import RemotePlaneBinding
    from omicsclaw.skills.loader import load_skills
    from omicsclaw.tools.context import ApprovalDecision

    source = tmp_path / "source"
    make_skill(source)
    payload = tmp_path / "payload"
    manifest = build_payload(source, payload)
    app = SimpleNamespace(
        config=SimpleNamespace(workspace=str(tmp_path), approval_timeout_s=2),
        skills=load_skills(payload / "skills"),
    )
    database = Database(tmp_path / "owned-jobs.db")
    plane = RemotePlaneBinding(
        database, workspace=tmp_path, spawner=LocalTargetSpawner(tmp_path)
    )
    bridge = RemoteJobsBridge(
        plane,
        target_alias="controller",
        target_root=str(tmp_path),
        poll_interval_s=0.01,
    )
    runner = ScienceJobRunner(
        app,
        bridge,
        interpreter=Path(sys.executable),
        component_root=Path(__file__).parents[2],
        payload_root=payload,
        manifest=manifest,
        environment_digest="a" * 64,
    )
    manager = JobsManager(
        app,
        store=JobStore(database),
        remote_runtime=bridge,
        register_default_runners=False,
    )
    manager.register_runner("skill_run", runner)

    async def scenario():
        async def submit(request):
            if entry == "form":
                return await create_job_from_document(manager, request)
            from omicsclaw.entry.ssh.science_runner import RunSkillJobTool

            reply = json.loads(
                await RunSkillJobTool(manager).execute(json.dumps(request))
            )
            return manager.get_job(reply["job_id"])

        request = {
            "skill": "fixture/resource",
            "inputs": {},
            "source_request_id": "form-once",
        }
        first = await submit(request)
        deadline = asyncio.get_running_loop().time() + 5
        approval = None
        while asyncio.get_running_loop().time() < deadline:
            approval = next(
                (
                    payload
                    for _, kind, payload in manager.replay_events(first.id, 0)
                    if kind == "tool_approval_request"
                ),
                None,
            )
            if approval:
                break
            await asyncio.sleep(0.01)
        assert approval is not None
        assert manager.settle_approval(
            first.id, approval["call_id"], ApprovalDecision(approved=True)
        )
        await asyncio.wait_for(manager._live[first.id].task, 10)
        second = await submit(request)
        before = manager.replay_events(first.id, 0)
        await manager.reconcile_remote_jobs()
        assert manager.replay_events(first.id, 0) == before
        artifacts = manager.run_store.list("artifact")
        assert len(artifacts) == 2 and all(
            item.checksum and item.size > 0 for item in artifacts
        )
        assert any(
            item.uri.endswith("/tables/omicsclaw-result.json") for item in artifacts
        )
        result = json.loads(
            (Path(first.target_workdir) / "results" / "value.json").read_text()
        )
        assert (
            first.id,
            second.id,
            manager.get_job(first.id).status,
            result["value"],
            database.run(
                lambda conn: conn.execute(
                    "SELECT COUNT(*) FROM remote_jobs"
                ).fetchone()[0]
            ),
        ) == (first.id, first.id, "succeeded", "resource-value", 1)

    asyncio.run(scenario())


@pytest.mark.parametrize("branch", ["kernel", "forced_r"])
def test_worker_uses_sidecar_and_real_r_in_owned_library(
    tmp_path, branch
):
    import shutil

    from omicsclaw.entry.ssh.science_payload import build_payload

    source = tmp_path / "source"
    directory = make_skill(source)
    body = "__all__=['run']\ndef run():\n from pathlib import Path\n"
    if branch == "forced_r":
        rscript = Path(shutil.which("Rscript") or "")
        assert rscript.is_file(), "the real R worker test needs Rscript"
        (directory / "fixture.R").write_text(
            "args <- commandArgs(trailingOnly=TRUE)\ndir.create('results',showWarnings=FALSE)\nwriteLines(readLines(args[1]), 'results/r-value.txt')\n"
        )
        body += " import os,subprocess\n subprocess.run([os.environ['OMICSCLAW_RSCRIPT'],str(Path(__file__).with_name('fixture.R')),str(Path(__file__).with_name('table.txt'))],check=True)\n"
    body += " return {'summary':{'branch':" + repr(branch) + "},'data':{'resource':Path(__file__).with_name('table.txt').read_text()}}\n"
    (directory / "kernel.py").write_text(body)
    payload = tmp_path / "supplied"
    manifest = build_payload(source, payload)
    work = tmp_path / "work"
    work.mkdir()
    library = tmp_path / "r-library"
    library.mkdir()
    request = work / "request.json"
    request.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "jobId": "public-sdk",
                "skill": "fixture/resource",
                "inputs": {},
                "workspace": str(work),
                "payloadRoot": str(payload),
                "payloadManifest": manifest,
                "payloadDigest": manifest["sourceFilesSha256"],
            }
        )
    )
    environment = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(Path(__file__).parents[2]),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "HOME": str(work),
        "OMICSCLAW_R_LIBS": str(library),
    }
    if branch == "forced_r":
        environment["OMICSCLAW_RSCRIPT"] = str(rscript)
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "omicsclaw.entry.ssh.science_worker",
            "--request",
            str(request),
        ],
        cwd=work,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (
        json.loads((work / "tables" / "omicsclaw-result.json").read_text())["summary"]["branch"]
        == branch
    )
    assert not (work / "lib").exists()
    if branch == "forced_r":
        assert (
            work / "results" / "r-value.txt"
        ).read_text().strip() == "resource-value"
