"""Tests for workspace project settings, Project Context prompt assembly, and artifact archiving."""

from __future__ import annotations

import hashlib
import io
import logging
import pathlib
import zipfile

import pytest

from omicsclaw.entry.assembly import (
    LOGGER_NAME,
    PROJECT_CONTEXT_SECTION_HEADING,
    PROJECT_CONTEXT_SECTION_KEY,
    RecordingPromptSource,
    build_prompt,
    default_sections,
)
from omicsclaw.entry.config import AppConfig
from omicsclaw.entry.desktop.files import archive_workspace_artifacts
from omicsclaw.entry.desktop.turn_submission import (
    DesktopIngressError,
    decode_chat_stream_request,
)
from omicsclaw.entry.memory import MEMORY_SECTION_KEY, open_memory
from omicsclaw.entry.project_settings import (
    PROJECT_CONTEXT_PREAMBLE,
    load_project_settings,
    resolve_project_instructions,
    save_project_settings,
)
from omicsclaw.entry.session_options import SessionOptionsState
from omicsclaw.tools import use_tool_context


def _config(workspace: pathlib.Path, **overrides: object) -> AppConfig:
    return AppConfig(workspace=workspace, **overrides)


def test_empty_project_instructions_omits_heading_and_section_stats(tmp_path: pathlib.Path):
    """When no project instructions exist, ``## Project Context`` is completely absent."""
    config = _config(tmp_path)
    prompt = build_prompt(
        default_sections(config, project_instructions=True)
    ).render()

    assert PROJECT_CONTEXT_SECTION_HEADING not in prompt.system_prompt
    assert PROJECT_CONTEXT_SECTION_KEY not in dict(prompt.section_stats)
    assert PROJECT_CONTEXT_SECTION_KEY not in [s.key for s in prompt.sections]


def test_whitespace_only_project_instructions_omits_heading(tmp_path: pathlib.Path):
    save_project_settings(
        tmp_path,
        name="Spatial Atlas",
        description="Visium HD mouse brain study",
        instructions="   \n\t  ",
    )
    config = _config(tmp_path)
    prompt = build_prompt(
        default_sections(config, project_instructions=True)
    ).render()

    assert PROJECT_CONTEXT_SECTION_HEADING not in prompt.system_prompt
    assert PROJECT_CONTEXT_SECTION_KEY not in dict(prompt.section_stats)


def test_non_empty_project_instructions_renders_between_environment_and_memory(
    tmp_path: pathlib.Path,
):
    """``project_instructions`` renders strictly after ``environment`` and before ``memory``."""
    save_project_settings(
        tmp_path,
        name="Visium Study",
        description="UI-only description that must never enter prompt",
        instructions="Use SI units, cite a source for every claim, and report weak evidence.",
    )
    config = _config(tmp_path)
    memory = open_memory(config)
    assert memory is not None
    try:
        import asyncio
        from omicsclaw.memory import MemoryEntry

        asyncio.run(
            memory.store.add(
                MemoryEntry(title="t", content="Saved long-term observation.", importance=9)
            )
        )
        asyncio.run(memory.precis.regenerate())
        sections = default_sections(
            config,
            memory=memory,
            precis_in_prompt=True,
            project_instructions=True,
        )
        unrendered_keys = [s.key for s in sections]
        env_idx = unrendered_keys.index("environment")
        proj_idx = unrendered_keys.index(PROJECT_CONTEXT_SECTION_KEY)
        mem_idx = unrendered_keys.index(MEMORY_SECTION_KEY)
        assert env_idx < proj_idx < mem_idx

        rendered = build_prompt(sections).render()
    finally:
        memory.close()

    rendered_keys = [s.key for s in rendered.sections]
    assert rendered_keys.index("environment") < rendered_keys.index(PROJECT_CONTEXT_SECTION_KEY)
    assert rendered_keys.index(PROJECT_CONTEXT_SECTION_KEY) < rendered_keys.index(MEMORY_SECTION_KEY)

    assert PROJECT_CONTEXT_SECTION_HEADING in rendered.system_prompt
    assert PROJECT_CONTEXT_PREAMBLE in rendered.system_prompt
    assert "Use SI units, cite a source for every claim, and report weak evidence." in rendered.system_prompt
    assert "UI-only description that must never enter prompt" not in rendered.system_prompt
    assert "Visium Study" not in rendered.system_prompt


def test_updating_name_and_description_never_changes_system_prompt_or_hash(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
):
    """Changing ``name`` or ``description`` leaves ``system_prompt`` and ``RecordingPromptSource`` hash identical."""
    save_project_settings(
        tmp_path,
        name="Initial Name",
        description="Initial description for UI list",
        instructions="Always normalize counts with sc.pp.normalize_total(adata, target_sum=1e4).",
    )
    config = _config(tmp_path)
    source = RecordingPromptSource(
        build_prompt(default_sections(config, project_instructions=True))
    )

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        first = source.render()
        first_hash = hashlib.sha256(first.system_prompt.encode("utf-8")).hexdigest()[:16]

        save_project_settings(
            tmp_path,
            name="Renamed Project Title",
            description="Completely rewritten description that should never leak into prompt!",
        )
        second = source.render()
        second_hash = hashlib.sha256(second.system_prompt.encode("utf-8")).hexdigest()[:16]

    assert first.system_prompt == second.system_prompt
    assert first_hash == second_hash
    assert "Renamed Project Title" not in second.system_prompt
    assert "Completely rewritten description" not in second.system_prompt

    change_logs = [
        rec.message for rec in caplog.records if "prompt.render changed" in rec.message
    ]
    assert len(change_logs) == 1


def test_session_options_and_turn_submission_round_trip_project_instructions(
    tmp_path: pathlib.Path,
):
    """``SessionOptionsState`` and ``decode_chat_stream_request`` carry ``project_instructions``."""
    req = decode_chat_stream_request(
        {
            "ingress_schema_version": 3,
            "content": "Run QC on sample_01.h5ad",
            "session_id": "sess_proj_1",
            "source_request_id": "a" * 32,
            "project_instructions": "  Enforce mitochondrial cutoff < 15%.  ",
        }
    )
    assert req.values["project_instructions"] == "Enforce mitochondrial cutoff < 15%."

    opts = SessionOptionsState.from_mapping(req.values)
    assert opts.project_instructions == "Enforce mitochondrial cutoff < 15%."
    assert not opts.is_empty()
    assert opts.to_dict()["project_instructions"] == "Enforce mitochondrial cutoff < 15%."

    config = _config(tmp_path)
    assembler = build_prompt(default_sections(config, project_instructions=True))
    with use_tool_context(values=req.values):
        rendered = assembler.render()

    assert PROJECT_CONTEXT_SECTION_HEADING in rendered.system_prompt
    assert "Enforce mitochondrial cutoff < 15%." in rendered.system_prompt

    with pytest.raises(DesktopIngressError) as exc_info:
        decode_chat_stream_request(
            {
                "ingress_schema_version": 3,
                "content": "hi",
                "session_id": "sess_proj_1",
                "source_request_id": "b" * 32,
                "project_instructions": 12345,
            }
        )
    assert exc_info.value.code == "invalid_project_instructions"


def test_instructions_md_file_combines_deterministically(tmp_path: pathlib.Path):
    save_project_settings(
        tmp_path,
        name="Multi-Omics",
        instructions="Rule 1: Use GRCh38 reference.",
    )
    md_path = tmp_path / ".omicsclaw" / "instructions.md"
    md_path.write_text("Rule 2: Export figures at 300 DPI.", encoding="utf-8")

    resolved = resolve_project_instructions(tmp_path)
    assert "Rule 1: Use GRCh38 reference." in resolved
    assert "Rule 2: Export figures at 300 DPI." in resolved
    assert resolved.index("Rule 1") < resolved.index("Rule 2")

    loaded = load_project_settings(tmp_path)
    assert loaded.name == "Multi-Omics"
    assert loaded.instructions == "Rule 1: Use GRCh38 reference."


def test_archive_workspace_artifacts_creates_valid_zip_excluding_ignored_dirs(
    tmp_path: pathlib.Path,
):
    """``archive_workspace_artifacts`` packs deliverables and skips internal/cache folders."""
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "markers.csv").write_text("gene,pval\nCD3D,1e-9\n", encoding="utf-8")
    (tmp_path / "report.md").write_text("# Analysis Summary\n", encoding="utf-8")

    # Ignored / hidden paths that must be excluded
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "mod.cpython-311.pyc").write_bytes(b"\x00\x01")
    save_project_settings(tmp_path, name="Test", instructions="Secret internal config")

    raw_zip = archive_workspace_artifacts(tmp_path)
    assert zipfile.is_zipfile(io.BytesIO(raw_zip))

    with zipfile.ZipFile(io.BytesIO(raw_zip), "r") as zf:
        names = set(zf.namelist())
        assert "report.md" in names
        assert "results/markers.csv" in names
        assert not any(n.startswith(".git") for n in names)
        assert not any(n.startswith("__pycache__") for n in names)
        assert not any(n.startswith(".omicsclaw") for n in names)
        assert zf.read("results/markers.csv").decode("utf-8") == "gene,pval\nCD3D,1e-9\n"


def test_desktop_project_settings_and_archive_routes(tmp_path: pathlib.Path):
    pytest.importorskip("fastapi", reason="Desktop HTTP routes require fastapi")
    testclient = pytest.importorskip("fastapi.testclient")
    from omicsclaw.entry.desktop import create_desktop_app
    from omicsclaw.entry.session import attach_sessions
    from tests.entry.test_turn_runner import Scripted, make_app

    (tmp_path / "figure1.svg").write_text("<svg></svg>", encoding="utf-8")
    app = attach_sessions(make_app(tmp_path, Scripted(), tools=()), abandon_grace_s=None)
    client = testclient.TestClient(create_desktop_app(app))

    get_resp = client.get("/project/settings")
    assert get_resp.status_code == 200
    assert get_resp.json()["name"] == ""
    assert get_resp.json()["instructions"] == ""

    put_resp = client.put(
        "/project/settings",
        json={
            "name": "Spatial Transcriptomics",
            "description": "Mouse brain Visium HD",
            "instructions": "Use Leiden resolution 0.8 by default.",
        },
    )
    assert put_resp.status_code == 200
    saved = put_resp.json()
    assert saved["name"] == "Spatial Transcriptomics"
    assert saved["description"] == "Mouse brain Visium HD"
    assert saved["instructions"] == "Use Leiden resolution 0.8 by default."

    archive_resp = client.get("/files/archive")
    assert archive_resp.status_code == 200
    assert archive_resp.headers["content-type"] == "application/zip"
    assert "attachment;" in archive_resp.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(archive_resp.content), "r") as zf:
        assert "figure1.svg" in zf.namelist()
        assert ".omicsclaw/project.json" not in zf.namelist()
