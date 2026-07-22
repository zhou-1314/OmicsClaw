"""End-to-end: async driver + real MemoryProjectionWriter land projections in
Project-scoped graph Memory over a real control.db and a real MemoryEngine."""

import hashlib

import pytest
import pytest_asyncio

from omicsclaw.control import (
    ControlStateRepository,
    ProjectionIntentInput,
    RunAcceptanceIntent,
    RunReport,
)
from omicsclaw.memory.database import DatabaseManager
from omicsclaw.memory.engine import MemoryEngine
from omicsclaw.memory.memory_client import MemoryClient
from omicsclaw.memory.projection_driver import adrive_pending_projections
from omicsclaw.memory.projection_writer import (
    MemoryProjectionWriter,
    project_scope_namespace,
    projection_memory_uri,
)
from omicsclaw.memory.search import SearchIndexer


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _reader_for(content):
    def _read(*, source_store, source_ref):
        return content

    return _read


def _record_run_projection(
    repo, *, content: bytes, submission: str = "sub-1", fp: str = "e"
):
    project = repo.create_project(f"Study {submission}")
    accepted = repo.accept_run(
        RunAcceptanceIntent(
            run_submission_id=submission,
            fingerprint_version=1,
            fingerprint_sha256=fp * 64,
            run_kind="skill",
            scope_kind="project",
            project_id=project.project_id,
            manifest_ref="run-store://manifest/1",
        )
    )
    assignment = repo.assign_run(accepted.run_id, executor_kind="local")
    repo.apply_run_report(
        RunReport(
            run_id=accepted.run_id,
            assignment_id=assignment.assignment_id,
            terminal_status="succeeded",
            projections=(
                ProjectionIntentInput(
                    projection_kind="analysis_lineage",
                    source_store="run",
                    source_ref="run-store://completion/1",
                    content_sha256=_digest(content),
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


def _control(tmp_path):
    # Reuse pytest's 0700 tmp_path directly (ControlStateRepository requires an
    # owner-private dir); control.db and mem.db coexist without cross-checks.
    return ControlStateRepository(tmp_path)


def _factory(engine):
    return lambda namespace: MemoryClient(engine=engine, namespace=namespace)


@pytest.mark.asyncio
async def test_driver_lands_projection_in_project_namespace(engine, tmp_path):
    content = b'{"lineage": "pca->leiden"}'
    writer = MemoryProjectionWriter(_factory(engine))
    with _control(tmp_path) as repo:
        project, intent = _record_run_projection(repo, content=content)

        summary = await adrive_pending_projections(
            repo, read_source=_reader_for(content), write_projection=writer
        )

        assert summary.applied == 1
        assert repo.list_projection_intents(project.project_id)[0].state == "applied"

    client = MemoryClient(
        engine=engine, namespace=project_scope_namespace(project.project_id)
    )
    record = await client.recall(projection_memory_uri(intent))
    assert record is not None
    assert record.content == content.decode()


@pytest.mark.asyncio
async def test_projection_absent_from_transport_namespace(engine, tmp_path):
    # ADR 0064: the projection is Project-scoped, not owned by a transport
    # identity — a legacy platform/user_id namespace must not see it.
    content = b'{"insight": "x"}'
    writer = MemoryProjectionWriter(_factory(engine))
    with _control(tmp_path) as repo:
        _, intent = _record_run_projection(repo, content=content)
        await adrive_pending_projections(
            repo, read_source=_reader_for(content), write_projection=writer
        )

    transport = MemoryClient(engine=engine, namespace="telegram/u1")
    assert await transport.recall(projection_memory_uri(intent)) is None


@pytest.mark.asyncio
async def test_lands_after_archive(engine, tmp_path):
    # The keystone property, now proven to actually WRITE into Memory after the
    # Project is archived — completion of already-accepted work.
    content = b'{"insight": "kept"}'
    writer = MemoryProjectionWriter(_factory(engine))
    with _control(tmp_path) as repo:
        project, intent = _record_run_projection(repo, content=content)
        repo.archive_project(project.project_id)

        summary = await adrive_pending_projections(
            repo, read_source=_reader_for(content), write_projection=writer
        )
        assert summary.applied == 1

    client = MemoryClient(
        engine=engine, namespace=project_scope_namespace(project.project_id)
    )
    record = await client.recall(projection_memory_uri(intent))
    assert record is not None
    assert record.content == content.decode()


@pytest.mark.asyncio
async def test_reapply_is_idempotent_overwrite(engine, tmp_path):
    # projection:// is overwrite-mode: the same Intent ID URI rewrites in place
    # rather than forking a version, satisfying "idempotent by Intent ID".
    writer = MemoryProjectionWriter(_factory(engine))
    with _control(tmp_path) as repo:
        project, intent = _record_run_projection(repo, content=b"v1")

    await writer(intent=intent, content=b"v1")
    await writer(intent=intent, content=b"v2")

    client = MemoryClient(
        engine=engine, namespace=project_scope_namespace(project.project_id)
    )
    record = await client.recall(projection_memory_uri(intent))
    assert record is not None
    assert record.content == "v2"

    # Prove a true overwrite (a single Memory row), not a latest-wins version
    # chain — recall alone would pass either way.
    import sqlalchemy as sa

    from omicsclaw.memory.models import Edge, Memory, Path

    async with engine.db.session() as session:
        path = (
            await session.execute(
                sa.select(Path).where(
                    Path.namespace == project_scope_namespace(project.project_id),
                    Path.domain == "projection",
                    Path.path == projection_memory_uri(intent).split("://", 1)[1],
                )
            )
        ).scalar_one()
        edge = await session.get(Edge, path.edge_id)
        memories = (
            await session.execute(
                sa.select(Memory).where(Memory.node_uuid == edge.child_uuid)
            )
        ).scalars().all()
    assert len(memories) == 1


@pytest.mark.asyncio
async def test_digest_mismatch_writes_nothing(engine, tmp_path):
    writer = MemoryProjectionWriter(_factory(engine))
    with _control(tmp_path) as repo:
        project, intent = _record_run_projection(repo, content=b"original")

        summary = await adrive_pending_projections(
            repo, read_source=_reader_for(b"tampered"), write_projection=writer
        )
        assert summary.failed == 1
        assert repo.list_projection_intents(project.project_id)[0].state == "failed"

    client = MemoryClient(
        engine=engine, namespace=project_scope_namespace(project.project_id)
    )
    assert await client.recall(projection_memory_uri(intent)) is None
