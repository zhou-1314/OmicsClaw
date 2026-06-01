"""Backend KG handoff/record agent tools for the Ideate→Analyze loop (ADR 0021 §4/§5/§6).

`kg_build_packet` links a hypothesis to its analysis; `kg_record_result` records the
outcome, which SUGGESTS a verdict (does not flip status). Both soft-fail when KG is
absent. Exercised against a real embedded OmicsClaw-KG workspace.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from omicsclaw.runtime.tools import kg_tools

pytest.importorskip("omicsclaw_kg")

from omicsclaw_kg import paths  # noqa: E402
from omicsclaw_kg.cli.cmd_init import init  # noqa: E402
from omicsclaw_kg.config import KGConfig  # noqa: E402
from omicsclaw_kg.schema.frontmatter import parse_frontmatter  # noqa: E402
from omicsclaw_kg.wiki.reader import parse_page  # noqa: E402


@pytest.fixture
def kg_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> KGConfig:
    cfg = init(tmp_path / "ws" / ".omicsclaw" / "knowledge")
    monkeypatch.setenv("OMICSCLAW_KG_HOME", str(cfg.home))
    monkeypatch.delenv("OMICSCLAW_WORKSPACE", raising=False)
    return cfg


def _seed_source(cfg: KGConfig, slug: str) -> None:
    (paths.wiki_subdir(cfg, "sources") / f"{slug}.md").write_text(
        "---\n"
        f"id: {slug}\ntype: source\ntitle: {slug}\nslug: {slug}\n"
        "created: 2026-04-10T00:00:00Z\nupdated: 2026-04-10T00:00:00Z\n"
        f"graph_node_id: {slug}\nraw_path: raw/papers/x.pdf\n"
        "source_type: paper\ningest_hash: deadbeef0000\n"
        "---\n\n## Notes\n\n",
        encoding="utf-8",
    )


def _seed_hypothesis(cfg: KGConfig, slug: str, supported: list[str]) -> None:
    cites = "".join(f"  - {s}\n" for s in supported)
    (paths.wiki_subdir(cfg, "hypotheses") / f"{slug}.md").write_text(
        "---\n"
        f"id: {slug}\ntype: hypothesis\ntitle: {slug}\nslug: {slug}\n"
        "created: 2026-04-10T00:00:00Z\nupdated: 2026-04-10T00:00:00Z\n"
        f"graph_node_id: {slug}\nknowledge_state: HYPOTHESIS\n"
        "question: q\nproposed_claim: a long enough proposed claim sentence\n"
        f"supported_by:\n{cites}"
        "candidate_datasets: []\nrecommended_skills: [spatial.x]\nstatus: draft\n"
        "---\n\n## Notes\n\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_build_packet_then_record_suggests_verdict(kg_env: KGConfig) -> None:
    _seed_source(kg_env, "src1")
    _seed_hypothesis(kg_env, "h1", ["src1"])

    built = await kg_tools.execute_kg_build_packet({"hypothesis_slug": "h1"})
    assert "Built handoff packet" in built
    m = re.search(r"`(hof-[^`]+)`", built)
    assert m, built
    packet_id = m.group(1)

    recorded = await kg_tools.execute_kg_record_result(
        {
            "packet_id": packet_id,
            "verdict": "validated",
            "summary": "Marker burden tracks chronological age in the visium run.",
        }
    )
    assert "suggested verdict" in recorded
    assert "validated" in recorded

    # The hypothesis page carries the suggestion; status is unchanged (ADR §6).
    fm = parse_frontmatter(parse_page(paths.wiki_subdir(kg_env, "hypotheses") / "h1.md")[0])
    assert fm.suggested_verdict == "validated"
    assert fm.status == "draft"


@pytest.mark.asyncio
async def test_record_refined_creates_refined_page(kg_env: KGConfig) -> None:
    _seed_source(kg_env, "src1")
    _seed_hypothesis(kg_env, "h1", ["src1"])
    built = await kg_tools.execute_kg_build_packet({"hypothesis_slug": "h1"})
    packet_id = re.search(r"`(hof-[^`]+)`", built).group(1)  # type: ignore[union-attr]

    rec = await kg_tools.execute_kg_record_result(
        {
            "packet_id": packet_id,
            "verdict": "refined",
            "summary": "The original was too broad; a sharper claim fits the data.",
            "refined_hypothesis_slug": "h1-refined",
            "refined_proposed_claim": "TP53 inhibition reverses senescence within 7 days in fibroblasts.",
        }
    )
    assert "suggested verdict" in rec

    refined_page = paths.wiki_subdir(kg_env, "hypotheses") / "h1-refined.md"
    assert refined_page.exists()
    rfm = parse_frontmatter(parse_page(refined_page)[0])
    assert rfm.refines == "h1"
    assert "TP53 inhibition" in rfm.proposed_claim


@pytest.mark.asyncio
async def test_build_packet_unknown_hypothesis(kg_env: KGConfig) -> None:
    out = await kg_tools.execute_kg_build_packet({"hypothesis_slug": "does-not-exist"})
    assert "Error building handoff packet" in out


@pytest.mark.asyncio
async def test_build_packet_requires_slug(kg_env: KGConfig) -> None:
    assert "required" in await kg_tools.execute_kg_build_packet({})


@pytest.mark.asyncio
async def test_record_result_rejects_unknown_verdict(kg_env: KGConfig) -> None:
    out = await kg_tools.execute_kg_record_result(
        {"packet_id": "hof-x-h1", "verdict": "bogus", "summary": "x"}
    )
    assert out.startswith("Error: invalid result")  # pydantic rejects the verdict


@pytest.mark.asyncio
async def test_record_result_refined_requires_slug(kg_env: KGConfig) -> None:
    out = await kg_tools.execute_kg_record_result(
        {"packet_id": "hof-x-h1", "verdict": "refined", "summary": "needs work"}
    )
    assert out.startswith("Error: invalid result")  # refined without refined_hypothesis_slug


@pytest.mark.asyncio
async def test_handoff_tools_soft_fail_without_kg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kg_tools, "_import_kg_handoff", lambda: None)
    built = await kg_tools.execute_kg_build_packet({"hypothesis_slug": "h1"})
    recorded = await kg_tools.execute_kg_record_result(
        {"packet_id": "p", "verdict": "validated", "summary": "x"}
    )
    assert "OmicsClaw-KG is not installed" in built
    assert "OmicsClaw-KG is not installed" in recorded
