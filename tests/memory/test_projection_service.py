"""Integration tests for the background MemoryProjectionService (ADR 0064)."""

import asyncio

import pytest
import pytest_asyncio

from omicsclaw.control import (
    ControlStateRepository,
    ProjectionIntentInput,
    RunAcceptanceIntent,
    RunReport,
)
from omicsclaw.control.projection_payload import analysis_lineage_bytes
from omicsclaw.memory.database import DatabaseManager
from omicsclaw.memory.engine import MemoryEngine
from omicsclaw.memory.memory_client import MemoryClient
from omicsclaw.memory.projection_service import MemoryProjectionService
from omicsclaw.memory.projection_source import RunManifestSourceReader
from omicsclaw.memory.projection_writer import (
    project_scope_namespace,
    projection_memory_uri,
)
from omicsclaw.memory.search import SearchIndexer


def _manifest(project_id: str):
    return {
        "header": {
            "run_id": "a" * 32,
            "inputs": {"skill_id": "sc-de"},
            "scope": {"project_id": project_id},
            "parameters": {"resolution": 1.0},
            "skill_revision": {"skill_content_sha256": "c" * 64},
        },
        "completion": {
            "kind": "succeeded",
            "result_envelope_sha256": "e" * 64,
            "artifacts": [{"path": "result.json"}],
        },
    }


def _record_run_projection(repo, *, manifest, source_ref="run-store://manifest/1"):
    project = repo.create_project("Serviced study")
    accepted = repo.accept_run(
        RunAcceptanceIntent(
            run_submission_id="s" * 32,
            fingerprint_version=1,
            fingerprint_sha256="e" * 64,
            run_kind="skill",
            scope_kind="project",
            project_id=project.project_id,
            manifest_ref="run-store://manifest/1",
        )
    )
    assignment = repo.assign_run(accepted.run_id, executor_kind="local")
    import hashlib

    repo.apply_run_report(
        RunReport(
            run_id=accepted.run_id,
            assignment_id=assignment.assignment_id,
            terminal_status="succeeded",
            projections=(
                ProjectionIntentInput(
                    projection_kind="analysis_lineage",
                    source_store="run",
                    source_ref=source_ref,
                    content_sha256=hashlib.sha256(analysis_lineage_bytes(manifest)).hexdigest(),
                ),
            ),
        )
    )
    return project, repo.list_projection_intents(project.project_id)[0]


@pytest_asyncio.fixture
async def engine(tmp_path):
    db = DatabaseManager(f"sqlite+aiosqlite:///{tmp_path}/mem.db")
    await db.init_db()
    mem_engine = MemoryEngine(db, SearchIndexer(db))
    yield mem_engine
    await db.close()


def _service(repo, engine, manifest, *, source_ref="run-store://manifest/1", interval=0.01):
    # A source reader keyed to return our manifest for the recorded source_ref.
    reader = RunManifestSourceReader(lambda ref: manifest if ref == source_ref else {})
    return MemoryProjectionService(
        repository=repo,
        read_source=reader,
        client_factory=lambda ns: MemoryClient(engine=engine, namespace=ns),
        interval_seconds=interval,
    )


@pytest.mark.asyncio
async def test_run_once_lands_pending_intent(engine, tmp_path):
    with ControlStateRepository(tmp_path) as repo:
        manifest = _manifest("p1")
        project, intent = _record_run_projection(repo, manifest=manifest)
        service = _service(repo, engine, manifest)

        summary = await service.run_once()

        assert summary.applied == 1
        assert repo.list_projection_intents(project.project_id)[0].state == "applied"

    client = MemoryClient(
        engine=engine, namespace=project_scope_namespace(project.project_id)
    )
    record = await client.recall(projection_memory_uri(intent))
    assert record is not None
    assert record.content == analysis_lineage_bytes(manifest).decode()


@pytest.mark.asyncio
async def test_background_loop_applies_then_stops_cleanly(engine, tmp_path):
    with ControlStateRepository(tmp_path) as repo:
        manifest = _manifest("p2")
        project, intent = _record_run_projection(repo, manifest=manifest)
        service = _service(repo, engine, manifest)

        service.start()
        service.start()  # idempotent — must not spawn a second task
        # Wait for the sweep to apply the pending intent.
        for _ in range(200):
            if repo.list_projection_intents(project.project_id)[0].state == "applied":
                break
            await asyncio.sleep(0.01)
        assert repo.list_projection_intents(project.project_id)[0].state == "applied"

        await service.close()  # cancels + awaits the task cleanly
        await service.close()  # idempotent second close is a no-op

    client = MemoryClient(
        engine=engine, namespace=project_scope_namespace(project.project_id)
    )
    assert (await client.recall(projection_memory_uri(intent))) is not None


@pytest.mark.asyncio
async def test_run_once_is_noop_when_nothing_pending(engine, tmp_path):
    with ControlStateRepository(tmp_path) as repo:
        service = _service(repo, engine, _manifest("p3"))
        summary = await service.run_once()
        assert (summary.processed, summary.applied, summary.failed) == (0, 0, 0)
