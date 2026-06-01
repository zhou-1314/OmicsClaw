"""Backend wiring for the Bench Ideate stage (ADR 0021).

Exercises the thread-scoped hypothesis listing + formalize helpers against a real
embedded OmicsClaw-KG workspace with an injected LLM stub. The HTTP endpoints
(``GET/POST /thread/{id}/hypotheses|formalize``) are thin wrappers over these.

v1.5 is workspace-wide (KG is thread-blind); per-thread filtering and the
cross-study badge are deferred to 0019.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omicsclaw.runtime.tools.registry import _READ_STAGE_TOOLS, STAGE_TO_TOOL_SUBSETS
from omicsclaw.surfaces.desktop import hypotheses as hyp_svc

pytest.importorskip("omicsclaw_kg")

from omicsclaw_kg import paths  # noqa: E402
from omicsclaw_kg.cli.cmd_init import init  # noqa: E402
from omicsclaw_kg.config import KGConfig  # noqa: E402
from omicsclaw_kg.llm.stub import StubLLMClient  # noqa: E402


@pytest.fixture
def kg(tmp_path: Path) -> KGConfig:
    return init(tmp_path / "ws" / ".omicsclaw" / "knowledge")


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
    cites = "".join(f"  - {s}\n" for s in supported) or "[]\n"
    sb = "supported_by:\n" + cites if supported else "supported_by: []\n"
    (paths.wiki_subdir(cfg, "hypotheses") / f"{slug}.md").write_text(
        "---\n"
        f"id: {slug}\ntype: hypothesis\ntitle: {slug}\nslug: {slug}\n"
        "created: 2026-04-10T00:00:00Z\nupdated: 2026-04-10T00:00:00Z\n"
        f"graph_node_id: {slug}\nknowledge_state: HYPOTHESIS\n"
        "question: q\nproposed_claim: a long enough proposed claim sentence\n"
        f"{sb}"
        "candidate_datasets: []\nrecommended_skills: [spatial.x]\nstatus: draft\n"
        "---\n\n## Notes\n\n",
        encoding="utf-8",
    )


def test_list_workspace_source_slugs(kg: KGConfig) -> None:
    _seed_source(kg, "s1")
    _seed_source(kg, "s2")
    assert set(hyp_svc.list_workspace_source_slugs(str(kg.home))) == {"s1", "s2"}


def test_list_workspace_hypotheses_maps_shape(kg: KGConfig) -> None:
    _seed_source(kg, "s1")
    _seed_hypothesis(kg, "h1", ["s1"])
    items = hyp_svc.list_workspace_hypotheses(str(kg.home))
    assert len(items) == 1
    h = items[0]
    assert h["id"] == "h1"
    assert h["supported_by"] == ["s1"]
    assert h["ungrounded"] is False
    assert h["cross_study"] is False
    assert h["claim"]


def test_to_frontend_hypothesis_ungrounded_inference() -> None:
    grounded = hyp_svc.to_frontend_hypothesis({"slug": "y", "supported_by": ["s1"]})
    assert grounded["ungrounded"] is False
    ungrounded = hyp_svc.to_frontend_hypothesis({"slug": "x", "supported_by": []})
    assert ungrounded["ungrounded"] is True
    assert ungrounded["cross_study"] is False


def test_formalize_grounds_against_workspace_sources(kg: KGConfig) -> None:
    _seed_source(kg, "s1")
    _seed_source(kg, "s2")
    stub = StubLLMClient(
        responses={
            "HUNCH:": {
                "title": "TP53 reverses fibroblast senescence",
                "slug": "tp53-reverses-senescence",
                "proposed_claim": "TP53 inhibition reverses senescence in fibroblasts within 7 days.",
                "supported_by": ["s1"],
                "candidate_datasets": [],
                "recommended_skills": ["spatial.cell_type_annotation"],
            }
        }
    )
    h = hyp_svc.formalize_thread_hypothesis(str(kg.home), "TP53 drives senescence", stub)
    assert h["id"] == "tp53-reverses-senescence"
    assert h["supported_by"] == ["s1"]
    assert h["ungrounded"] is False
    # The new page is now listable.
    assert any(x["id"] == "tp53-reverses-senescence" for x in hyp_svc.list_workspace_hypotheses(str(kg.home)))


def test_formalize_allows_ungrounded(kg: KGConfig) -> None:
    _seed_source(kg, "s1")
    stub = StubLLMClient(
        responses={
            "HUNCH:": {
                "title": "A wild speculative hypothesis",
                "slug": "a-wild-guess",
                "proposed_claim": "Something speculative that is long enough to pass.",
                "supported_by": [],
                "candidate_datasets": [],
                "recommended_skills": [],
            }
        }
    )
    h = hyp_svc.formalize_thread_hypothesis(str(kg.home), "a wild guess", stub)
    assert h["ungrounded"] is True
    assert h["supported_by"] == []


def test_ideate_stage_tool_subset_mirrors_read() -> None:
    assert STAGE_TO_TOOL_SUBSETS.get("ideate") == _READ_STAGE_TOOLS
    assert "kg_search" in STAGE_TO_TOOL_SUBSETS["ideate"]
