"""The P2 artifacts plane over the real FastAPI app.

Same composition as ``test_desktop_jobs.py`` — the real
:func:`~omicsclaw.entry.desktop.server.create_desktop_app` over a
scripted backend, a fake skill planted in the workspace's ``skills/``
tree, the jobs manager injected with an in-memory store — plus what P2
adds: the fake skill's ``references/output_contract.md`` declares its
outputs, so the post-run scanner has a real contract to parse, and the
``TestClient`` context-manager form keeps the job's asyncio task alive
to run the scan.

What is pinned here, in order of how likely a regression is to eat it:
the contract the health route publishes, the scan-and-emit path (rows
**and** ``artifact.created`` frames before the terminal event), the
fallback when a contract parses to nothing, idempotency of a re-scan,
the list/detail/lineage routes, ``/files/serve`` compatibility (the
reason there is no ``/artifacts/{id}/content``), and the
``save_artifact`` tool's path fence.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import pathlib

import pytest

pytest.importorskip("fastapi", reason="the Desktop HTTP adapter needs it")
testclient = pytest.importorskip("fastapi.testclient")

from omicsclaw.entry.assembly import WorkspaceArtifactSink  # noqa: E402
from omicsclaw.entry.desktop import create_desktop_app  # noqa: E402
from omicsclaw.entry.desktop.artifacts_manager import (  # noqa: E402
    parse_output_contract,
    register_job_artifacts,
)
from omicsclaw.entry.desktop.jobs_manager import JobStore, JobsManager  # noqa: E402
from omicsclaw.memory.artifacts import ArtifactStore  # noqa: E402
from omicsclaw.memory.database import Database  # noqa: E402
from omicsclaw.entry.session import attach_sessions  # noqa: E402
from omicsclaw.schema import Message, Role  # noqa: E402
from omicsclaw.tools._workspace import Workspace  # noqa: E402
from omicsclaw.tools.builtin.save_artifact import SaveArtifactTool  # noqa: E402
from omicsclaw.tools.context import (  # noqa: E402
    ApprovalDecision,
    ApprovalRequest,
    use_tool_context,
)
from omicsclaw.tools.function_tool import ToolArgumentError  # noqa: E402
from tests.entry.test_desktop_jobs import (  # noqa: E402
    drain_sse,
    wait_for,
)
from tests.entry.test_turn_runner import (  # noqa: E402
    Scripted,  # type: ignore[import-not-found]
    make_app,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"not really a png, but a stable one" * 4

CONTRACT_SKILL_MD = """---
name: artifact-skill
description: Writes the files its output_contract declares.
entry: produce
inputs_schema: |
  {
    "type": "object",
    "properties": {
      "out_dir": {"type": "string"},
      "name": {"type": "string"}
    },
    "required": ["out_dir"],
    "additionalProperties": false
  }
---

# artifact-skill

Writes figures/hello.png and tables/greet.csv under out_dir.
"""

CONTRACT_API_PY = '''"""The contract skill entry surface."""

from pathlib import Path

PNG_BYTES = %r


def produce(out_dir, name="world"):
    root = Path(out_dir)
    (root / "figures").mkdir(parents=True, exist_ok=True)
    (root / "tables").mkdir(parents=True, exist_ok=True)
    (root / "figures" / "hello.png").write_bytes(PNG_BYTES)
    (root / "tables" / "greet.csv").write_text(
        "cell,greeting\\n1,hello " + name + "\\n", encoding="utf-8"
    )
    return "wrote 2 files under " + str(root)
''' % (PNG_BYTES,)

CONTRACT_MD = """# Outputs

The standalone CLI writes `figures/hello.png` and `tables/greet.csv`
under the output directory. `run_info` is prose, `obsm["X"]` is not a
path, and `figures/r_enhanced/` is a directory — none of those parse.
"""

FALLBACK_SKILL_MD = """---
name: fallback-skill
description: Writes figures the scanner must find without a contract.
entry: produce
inputs_schema: |
  {
    "type": "object",
    "properties": {"out_dir": {"type": "string"}},
    "required": ["out_dir"],
    "additionalProperties": false
  }
---

# fallback-skill
"""

FALLBACK_API_PY = '''"""The fallback skill entry surface."""

from pathlib import Path


def produce(out_dir):
    root = Path(out_dir)
    (root / "figures").mkdir(parents=True, exist_ok=True)
    (root / "figures" / "fallback.png").write_bytes(b"fallbackpng")
    return "wrote 1 file"
'''

FALLBACK_CONTRACT_MD = """# Outputs

This contract is prose with no parseable file declaration at all, so the
scan must fall back to the conventional output directories.
"""


def plant_skills(tmp_path: pathlib.Path) -> None:
    contract = tmp_path / "skills" / "demo" / "artifact-skill"
    contract.mkdir(parents=True)
    (contract / "SKILL.md").write_text(CONTRACT_SKILL_MD, encoding="utf-8")
    (contract / "_api.py").write_text(CONTRACT_API_PY, encoding="utf-8")
    (contract / "references").mkdir()
    (contract / "references" / "output_contract.md").write_text(
        CONTRACT_MD, encoding="utf-8"
    )
    fallback = tmp_path / "skills" / "demo" / "fallback-skill"
    fallback.mkdir(parents=True)
    (fallback / "SKILL.md").write_text(FALLBACK_SKILL_MD, encoding="utf-8")
    (fallback / "_api.py").write_text(FALLBACK_API_PY, encoding="utf-8")
    (fallback / "references").mkdir()
    (fallback / "references" / "output_contract.md").write_text(
        FALLBACK_CONTRACT_MD, encoding="utf-8"
    )


def artifacts_client(tmp_path: pathlib.Path):
    """The manager (over an in-memory store shared with its artifacts) and
    a TestClient, pinned to tmp_path as the workspace."""
    plant_skills(tmp_path)
    app = attach_sessions(
        make_app(tmp_path, Scripted(Message(role=Role.ASSISTANT, content="ok"))),
        abandon_grace_s=None,
    )
    database = Database(":memory:")
    manager = JobsManager(app, store=JobStore(database))
    assert manager.artifacts is not None
    return manager, manager.artifacts, testclient.TestClient(
        create_desktop_app(app, jobs_manager=manager)
    )


def run_skill(http, skill: str, out_dir: pathlib.Path, session: str = "art-s1") -> str:
    created = http.post(
        "/jobs",
        json={
            "kind": "skill_run",
            "skill": skill,
            "inputs": {"out_dir": str(out_dir)},
            "session_id": session,
        },
    )
    assert created.status_code == 200, created.text
    job_id = created.json()["job_id"]
    assert wait_for(
        lambda: http.get("/jobs/" + job_id).json()["job"]["status"] == "succeeded"
    ), http.get("/jobs/" + job_id).json()
    return job_id


# ---- the contract and the parser -------------------------------------------


def test_parse_output_contract_takes_paths_and_drops_prose():
    text = (
        "# Outputs\n\n"
        "Writes `figures/mean_usage.png` and `tables/program_weights.csv`.\n"
        "`run_info(table)` names methods; `obsm[\"X_gene_programs\"]` is not a\n"
        "path; `figures/r_enhanced/` is a directory; `--r-enhanced` is a flag.\n\n"
        "```\n"
        "output_directory/\n"
        "├── report.md\n"
        "├── processed.h5ad\n"
        "└── tables/\n"
        "    └── Summary.csv\n"
        "```\n"
    )
    declared = parse_output_contract(text)
    assert declared == [
        "figures/mean_usage.png",
        "tables/program_weights.csv",
        "report.md",
        "processed.h5ad",
        "tables/Summary.csv",
    ]


def test_parse_output_contract_answers_nothing_for_prose_only():
    assert parse_output_contract("No paths here, just words.") == []
    assert parse_output_contract("") == []


# ---- health and the wire contract ------------------------------------------


def test_health_publishes_artifacts_contract(tmp_path):
    _, _, http = artifacts_client(tmp_path)
    with http as client:
        payload = client.get("/health").json()
    contract = payload["contracts"]["desktop_artifacts"]
    assert contract["artifacts_schema_version"] == 1
    assert payload["capabilities"]["artifacts"] is True
    for path in ("/artifacts", "/artifacts/{artifact_id}"):
        assert path in payload["served_paths"]


# ---- the scan: rows, kinds, events ------------------------------------------


def test_job_registers_contract_artifacts_and_emits_events(tmp_path):
    manager, store, http = artifacts_client(tmp_path)
    with http as client:
        job_id = run_skill(client, "demo/artifact-skill", tmp_path / "run1")

        listed = client.get("/artifacts", params={"job_id": job_id}).json()
        artifacts = listed["artifacts"]
        assert len(artifacts) == 2, artifacts
        by_kind = {row["kind"]: row for row in artifacts}
        assert set(by_kind) == {"figure", "table"}

        figure = by_kind["figure"]
        assert figure["path"].endswith("run1/figures/hello.png")
        assert figure["mime"] == "image/png"
        assert figure["sha256"] == hashlib.sha256(PNG_BYTES).hexdigest()
        assert figure["size"] == len(PNG_BYTES)
        assert figure["meta"]["capture"] == "contract"
        assert figure["meta"]["declared"] == "figures/hello.png"
        assert figure["session_id"] == "art-s1"
        assert figure["job_id"] == job_id

        table = by_kind["table"]
        assert table["mime"] == "text/csv"
        assert table["title"] == "greet"

        # the job stream carries artifact.created before the terminal frame
        frames = drain_sse(client, "/jobs/" + job_id + "/events")
    types = [envelope["type"] for _, envelope in frames]
    assert "artifact.created" in types
    assert types.index("artifact.created") < types.index("job.done")
    created_frames = [
        envelope["data"] for _, envelope in frames
        if envelope["type"] == "artifact.created"
    ]
    assert len(created_frames) == 2
    assert {frame["kind"] for frame in created_frames} == {"figure", "table"}
    for frame in created_frames:
        assert frame["artifact_id"] and frame["title"] and frame["path"]
        assert set(frame) == {"artifact_id", "kind", "title", "path"}


def test_unparseable_contract_falls_back_to_convention_scan(tmp_path):
    _, store, http = artifacts_client(tmp_path)
    with http as client:
        job_id = run_skill(client, "demo/fallback-skill", tmp_path / "run2")
        artifacts = client.get(
            "/artifacts", params={"job_id": job_id}
        ).json()["artifacts"]
    assert len(artifacts) == 1
    row = artifacts[0]
    assert row["kind"] == "figure"
    assert row["path"].endswith("run2/figures/fallback.png")
    assert row["meta"]["capture"] == "fallback"
    assert row["produced_by"] == "fallback_scan"


def test_rescan_is_idempotent(tmp_path):
    _, store, _ = artifacts_client(tmp_path)
    skill_directory = tmp_path / "skills" / "demo" / "artifact-skill"
    out_figures = tmp_path / "run3" / "figures"
    out_figures.mkdir(parents=True)
    (out_figures / "hello.png").write_bytes(PNG_BYTES)

    emitted: list[str] = []
    first = register_job_artifacts(
        store,
        job_id="job-x",
        session_id="s",
        skill_directory=skill_directory,
        workspace=tmp_path,
        emit=lambda event_type, payload: emitted.append(event_type),
    )
    assert len(first) == 1
    second = register_job_artifacts(
        store,
        job_id="job-x",
        session_id="s",
        skill_directory=skill_directory,
        workspace=tmp_path,
        emit=lambda event_type, payload: emitted.append(event_type),
    )
    assert second == []
    assert store.count_for_job("job-x") == 1
    assert emitted.count("artifact.created") == 1


# ---- the routes: filters, detail, lineage, content ---------------------------


def test_list_filters_and_detail_lineage(tmp_path):
    manager, store, http = artifacts_client(tmp_path)
    with http as client:
        job_a = run_skill(client, "demo/artifact-skill", tmp_path / "runA", "sess-a")
        job_b = run_skill(client, "demo/artifact-skill", tmp_path / "runB", "sess-b")

        only_figures = client.get(
            "/artifacts", params={"job_id": job_a, "kind": "figure"}
        ).json()["artifacts"]
        assert len(only_figures) == 1
        assert only_figures[0]["kind"] == "figure"

        by_session = client.get(
            "/artifacts", params={"session_id": "sess-b"}
        ).json()["artifacts"]
        assert len(by_session) == 2
        assert all(row["job_id"] == job_b for row in by_session)

        bad_kind = client.get("/artifacts", params={"kind": "nope"})
        assert bad_kind.status_code == 422
        bad_limit = client.get("/artifacts", params={"limit": "zero"})
        assert bad_limit.status_code == 422

        artifact_id = only_figures[0]["artifact_id"]
        detail = client.get("/artifacts/" + artifact_id).json()
        job = detail["lineage"]["job"]
        assert job["job_id"] == job_a
        assert job["skill"] == "demo/artifact-skill"
        assert job["status"] == "succeeded"
        assert job["inputs"]["out_dir"].endswith("runA")
        assert detail["lineage"]["parents"] == []

        missing = client.get("/artifacts/does-not-exist")
        assert missing.status_code == 404


def test_files_serve_serves_artifact_content(tmp_path):
    """The reason there is no ``/artifacts/{id}/content``: ``/files/serve``
    already serves absolute in-workspace paths, and every artifact is one."""
    _, _, http = artifacts_client(tmp_path)
    with http as client:
        job_id = run_skill(client, "demo/artifact-skill", tmp_path / "run4")
        artifacts = client.get("/artifacts", params={"job_id": job_id}).json()[
            "artifacts"
        ]
        figure = next(row for row in artifacts if row["kind"] == "figure")
        served = client.get("/files/serve", params={"path": figure["path"]})
        assert served.status_code == 200, served.text
        assert served.content == PNG_BYTES
        assert served.headers["content-type"].startswith("image/png")


# ---- capture channel ①: the save_artifact tool -------------------------------


def _yes():
    def channel(request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(approved=True)

    return channel


def _run(coroutine):
    return asyncio.run(coroutine)


def test_save_artifact_registers_a_workspace_file(tmp_path):
    target = tmp_path / "results" / "summary.csv"
    target.parent.mkdir()
    target.write_text("a,b\n1,2\n", encoding="utf-8")
    tool = SaveArtifactTool(Workspace(tmp_path), sink=WorkspaceArtifactSink(tmp_path))
    arguments = json.dumps({"path": "results/summary.csv", "title": "Summary"})

    with use_tool_context(approval=_yes(), values={"session_id": "tool-s1"}):
        message = _run(tool.execute(arguments))

    assert "Saved artifact" in message
    store = ArtifactStore(Database(tmp_path / ".omicsclaw" / "jobs.db"))
    rows = store.list_artifacts(session_id="tool-s1")
    assert len(rows) == 1
    row = rows[0]
    assert row.kind == "table"
    assert row.title == "Summary"
    assert row.produced_by == "save_artifact"
    assert row.job_id == ""
    assert row.meta["capture"] == "tool"
    assert row.mime == "text/csv"
    assert row.sha256 == hashlib.sha256(b"a,b\n1,2\n").hexdigest()

    # saving the same file again is the idempotent no-op, not a duplicate
    with use_tool_context(approval=_yes(), values={"session_id": "tool-s1"}):
        again = _run(tool.execute(arguments))
    assert "Already saved" in again
    assert len(store.list_artifacts(session_id="tool-s1")) == 1


def test_save_artifact_refuses_paths_outside_the_workspace(tmp_path):
    outside = tmp_path.parent / "escaped.png"
    outside.write_bytes(b"nope")
    tool = SaveArtifactTool(Workspace(tmp_path))

    for bad in (
        json.dumps({"path": str(outside)}),
        json.dumps({"path": "../../escaped.png"}),
    ):
        with pytest.raises(ValueError):
            with use_tool_context(approval=_yes()):
                _run(tool.execute(bad))
    store = ArtifactStore(Database(tmp_path / ".omicsclaw" / "jobs.db"))
    assert store.list_artifacts(limit=500) == []


def test_save_artifact_refuses_a_file_that_does_not_exist(tmp_path):
    tool = SaveArtifactTool(Workspace(tmp_path))
    with pytest.raises(ToolArgumentError):
        with use_tool_context(approval=_yes()):
            _run(tool.execute(json.dumps({"path": "figures/never.png"})))
    assert not (tmp_path / ".omicsclaw" / "jobs.db").exists()
