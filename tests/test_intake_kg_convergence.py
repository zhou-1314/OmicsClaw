"""Intake converges onto the canonical KG ingest (audit §4.2 / D-1 pattern).

The autonomous pipeline's intake stage used to ONLY run its own regex extraction
(geo/organism/tissue/technology) — it never fed the knowledge graph, so an
autonomous research run built zero KG and ideation had nothing to ground on.

Now ``prepare_intake`` persists the paper's full extracted text to
``paper/source.txt`` and ``ingest_intake_paper`` ingests THAT via the shared
in-process bridge ``kg_tools.ingest_source_into_kg`` — so the paper becomes a
citeable Source (wiki/sources + concept/claim graph). The regex metadata stays
for ``research_request.md`` (it feeds the pipeline's downstream agents). Ingest is
best-effort: KG/LLM may be absent → no-op, never breaks the pipeline.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

# Import agent state first so the state↔agent_executors↔kg_tools cycle resolves.
import omicsclaw.runtime.agent.state as _state  # noqa: F401
import omicsclaw.runtime.tools.kg_tools as kg_tools
from omicsclaw.agents import intake as intake_mod
from omicsclaw.agents.intake import IntakeResult, ingest_intake_paper, prepare_intake


def _run(coro):
    return asyncio.run(coro)


# ---- prepare_intake persists the full source text ----


def test_intake_result_has_kg_fields():
    r = IntakeResult()
    assert r.source_text_path == ""
    assert r.kg_source == ""


def test_prepare_intake_mode_a_persists_source_text():
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(b"%PDF-1.4\nTest paper about GSE123456 in human brain\n")
        pdf_path = f.name
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            r = prepare_intake(idea="Test idea", pdf_path=pdf_path, output_dir=tmpdir)
            assert r.source_text_path
            assert Path(r.source_text_path).is_file()
            assert Path(r.source_text_path).name == "source.txt"
            # the persisted text is non-trivial (the extracted paper body)
            assert Path(r.source_text_path).read_text(encoding="utf-8").strip()
    finally:
        Path(pdf_path).unlink(missing_ok=True)


def test_prepare_intake_mode_c_has_no_source_text():
    with tempfile.TemporaryDirectory() as tmpdir:
        r = prepare_intake(idea="idea only", output_dir=tmpdir)
        assert r.input_mode == "C"
        assert r.source_text_path == ""


# ---- ingest_intake_paper dispatches the canonical bridge ----


def test_ingest_intake_paper_returns_slug_on_ingested(monkeypatch, tmp_path):
    src = tmp_path / "source.txt"
    src.write_text("paper body", encoding="utf-8")

    async def fake_ingest(source):
        assert source == str(src)
        return {"status": "ingested", "slug": "paper-2024"}

    monkeypatch.setattr(kg_tools, "ingest_source_into_kg", fake_ingest)
    r = IntakeResult(input_mode="A", source_text_path=str(src))
    assert _run(ingest_intake_paper(r)) == "paper-2024"


def test_ingest_intake_paper_returns_slug_on_cache_hit(monkeypatch, tmp_path):
    src = tmp_path / "source.txt"
    src.write_text("paper body", encoding="utf-8")

    async def fake_ingest(source):
        return {"status": "skipped", "reason": "cache hit", "slug": "paper-2024"}

    monkeypatch.setattr(kg_tools, "ingest_source_into_kg", fake_ingest)
    assert _run(ingest_intake_paper(IntakeResult(source_text_path=str(src)))) == "paper-2024"


def test_ingest_intake_paper_soft_fails_when_kg_absent(monkeypatch, tmp_path):
    src = tmp_path / "source.txt"
    src.write_text("x", encoding="utf-8")

    async def fake_ingest(source):
        return None  # KG/LLM unavailable

    monkeypatch.setattr(kg_tools, "ingest_source_into_kg", fake_ingest)
    assert _run(ingest_intake_paper(IntakeResult(source_text_path=str(src)))) == ""


def test_ingest_intake_paper_noop_without_source_text():
    # Mode C / resume: no persisted text → "" without touching KG.
    assert _run(ingest_intake_paper(IntakeResult(input_mode="C"))) == ""


def test_ingest_intake_paper_nonfatal_on_error(monkeypatch, tmp_path):
    src = tmp_path / "source.txt"
    src.write_text("x", encoding="utf-8")

    async def boom(source):
        raise RuntimeError("kg down")

    monkeypatch.setattr(kg_tools, "ingest_source_into_kg", boom)
    # Must not raise — best-effort.
    assert _run(ingest_intake_paper(IntakeResult(source_text_path=str(src)))) == ""
