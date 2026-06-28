"""Backend wiring for the Bench Ideate stage (ADR 0021).

Exercises the thread-scoped hypothesis listing + formalize helpers against a real
embedded OmicsClaw-KG workspace with an injected LLM stub. The HTTP endpoints
(``GET/POST /thread/{id}/hypotheses|formalize``) are thin wrappers over these.

v1.5 is workspace-wide (KG is thread-blind); per-thread filtering and the
cross-study badge are deferred to 0019.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

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


def test_to_frontend_hypothesis_surfaces_suggested_verdict() -> None:
    pending = hyp_svc.to_frontend_hypothesis({"slug": "y", "suggested_verdict": "validated"})
    assert pending["suggested_verdict"] == "validated"
    none = hyp_svc.to_frontend_hypothesis({"slug": "x"})
    assert none["suggested_verdict"] is None


def test_confirm_thread_hypothesis_verdict_flips(kg: KGConfig) -> None:
    _seed_hypothesis(kg, "h1", ["s1"])
    out = hyp_svc.confirm_thread_hypothesis_verdict(str(kg.home), "h1", "validated")
    assert out["old_status"] == "draft"
    assert out["new_status"] == "validated"


def test_confirm_thread_hypothesis_verdict_inconclusive_maps_to_submitted(kg: KGConfig) -> None:
    _seed_hypothesis(kg, "h1", ["s1"])
    out = hyp_svc.confirm_thread_hypothesis_verdict(str(kg.home), "h1", "inconclusive")
    assert out["new_status"] == "submitted"


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


# --- route_preview (ADR 0021 §4) ----------------------------------------------
# These exercise the real MemoryClient read contract: route_preview lists the
# thread's dataset subtree (``dataset://<thread_id>/``) via ``get_subtree`` and
# ``recall``, then parses the stored content with the real dataset parser. The
# Router itself is monkeypatched (it has its own tests) and used to capture the
# resolved dataset path it is handed.
from types import SimpleNamespace  # noqa: E402


class _Ref:
    def __init__(self, uri: str) -> None:
        self.uri = uri


class _Rec:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _FakeMemoryClient:
    """Minimal MemoryClient: a thread→[(uri, DatasetMemory|None)] leaf map."""

    def __init__(self, leaves: list[tuple[str, Any]], *, boom: bool = False) -> None:
        self._leaves = leaves
        self._boom = boom

    async def get_subtree(self, uri: str, *, limit: int = 100):
        if self._boom:
            raise RuntimeError("memory backend down")
        assert uri == "dataset://t1"  # no trailing slash (real-client fix)
        return [_Ref(u) for u, _ in self._leaves]

    async def recall(self, uri: str):
        from omicsclaw.memory.compat import _memory_to_content

        for u, mem in self._leaves:
            if u == uri:
                return _Rec(_memory_to_content(mem) if mem is not None else None)
        return None


def _capture_route(monkeypatch, **route_kwargs):
    """Patch the Router to capture its args and return a SimpleNamespace route."""
    captured: dict[str, Any] = {}

    def _fake(query: str, file_path: str = "", domain_hint: str = ""):
        captured["query"] = query
        captured["file_path"] = file_path
        return SimpleNamespace(
            kind=SimpleNamespace(value=route_kwargs.get("kind", "exact_skill")),
            chosen_skill=route_kwargs.get("chosen_skill", "spatial-preprocess"),
            confidence=route_kwargs.get("confidence", 0.9),
            should_search_web=route_kwargs.get("should_search_web", False),
            missing_params=route_kwargs.get("missing_params", []),
            capability_decision=SimpleNamespace(
                reasoning=route_kwargs.get("reasoning", ["matched the spatial domain"])
            ),
        )

    monkeypatch.setattr(hyp_svc, "route_analysis_request", _fake)
    return captured


def test_route_preview_resolves_thread_dataset_and_maps_shape(monkeypatch) -> None:
    import asyncio

    from omicsclaw.memory.compat import DatasetMemory

    ds = DatasetMemory(file_path="data/glioma.h5ad", thread_id="t1")
    client = _FakeMemoryClient(
        [
            ("dataset://t1/", None),  # container node — skipped
            ("dataset://t1/data_glioma.h5ad", ds),  # the real leaf
        ]
    )
    captured = _capture_route(monkeypatch, missing_params=["n_neighbors"])

    out = asyncio.run(hyp_svc.route_preview(client, "t1", "h1", "Test TP53 spatially"))
    # The Router was handed the resolved relative dataset path + the verbatim claim.
    assert captured["file_path"] == "data/glioma.h5ad"
    assert captured["query"] == "Test TP53 spatially"
    # The response shape the frontend card consumes.
    assert out["thread_id"] == "t1"
    assert out["slug"] == "h1"
    assert out["kind"] == "exact_skill"
    assert out["chosen_skill"] == "spatial-preprocess"
    assert out["dataset_path"] == "data/glioma.h5ad"
    assert out["missing_params"] == ["n_neighbors"]
    assert out["reasoning"] == ["matched the spatial domain"]


def test_route_preview_without_dataset_is_soft(monkeypatch) -> None:
    """No bound dataset → the Router still runs claim-only; dataset_path is null."""
    import asyncio

    client = _FakeMemoryClient([])  # empty subtree
    captured = _capture_route(
        monkeypatch, kind="no_skill", chosen_skill="", should_search_web=True
    )

    out = asyncio.run(hyp_svc.route_preview(client, "t1", "h1", "vague hunch"))
    assert captured["file_path"] == ""  # no fabricated path
    assert out["dataset_path"] is None
    assert out["kind"] == "no_skill"
    assert out["should_search_web"] is True


def test_route_preview_survives_memory_failure(monkeypatch) -> None:
    """A memory lookup that raises must not abort the preview — the Router still runs."""
    import asyncio

    client = _FakeMemoryClient([], boom=True)
    captured = _capture_route(monkeypatch, chosen_skill="spatial-de")

    out = asyncio.run(hyp_svc.route_preview(client, "t1", "h1", "claim"))
    assert captured["file_path"] == ""
    assert out["dataset_path"] is None
    assert out["chosen_skill"] == "spatial-de"


# --- 批7: per-thread grounding (thread_source_slugs + cross_study + sources) ---


def test_formalize_thread_scoped_allow_list_excludes_other_thread_sources(kg: KGConfig) -> None:
    """With thread_source_slugs=[s1], a hunch may NOT cite s2 (a workspace source
    outside this thread) — the closed-list validator rejects it."""
    from omicsclaw_kg.ideation.hypotheses import HypothesisIdeationError

    _seed_source(kg, "s1")
    _seed_source(kg, "s2")
    stub = StubLLMClient(
        responses={
            "HUNCH:": {
                "title": "Cross-thread cite",
                "slug": "cross-thread",
                "proposed_claim": "A claim long enough to pass validation comfortably.",
                "supported_by": ["s2"],  # NOT in the thread's allow-list
                "candidate_datasets": [],
                "recommended_skills": [],
            }
        }
    )
    with pytest.raises(HypothesisIdeationError):
        hyp_svc.formalize_thread_hypothesis(str(kg.home), "x", stub, thread_source_slugs=["s1"])


def test_formalize_thread_scoped_grounds_on_thread_source(kg: KGConfig) -> None:
    _seed_source(kg, "s1")
    _seed_source(kg, "s2")
    stub = StubLLMClient(
        responses={
            "HUNCH:": {
                "title": "Within thread",
                "slug": "within-thread",
                "proposed_claim": "A claim long enough to pass validation comfortably.",
                "supported_by": ["s1"],
                "candidate_datasets": [],
                "recommended_skills": [],
            }
        }
    )
    h = hyp_svc.formalize_thread_hypothesis(str(kg.home), "x", stub, thread_source_slugs=["s1"])
    assert h["supported_by"] == ["s1"]
    assert h["ungrounded"] is False


def test_formalize_empty_thread_yields_ungrounded(kg: KGConfig) -> None:
    """A thread with zero sources (empty allow-list) formalizes to an UNGROUNDED
    hypothesis — not a 400. require_support=False permits it."""
    _seed_source(kg, "s1")  # workspace has a source, but the thread has none
    stub = StubLLMClient(
        responses={
            "HUNCH:": {
                "title": "Fresh thread guess",
                "slug": "fresh-thread-guess",
                "proposed_claim": "A speculative claim that is plenty long enough to pass.",
                "supported_by": [],
                "candidate_datasets": [],
                "recommended_skills": [],
            }
        }
    )
    h = hyp_svc.formalize_thread_hypothesis(str(kg.home), "x", stub, thread_source_slugs=[])
    assert h["ungrounded"] is True
    assert h["supported_by"] == []


def test_formalize_none_thread_slugs_is_workspace_wide(kg: KGConfig) -> None:
    """thread_source_slugs=None preserves the legacy workspace-wide grounding."""
    _seed_source(kg, "s1")
    _seed_source(kg, "s2")
    stub = StubLLMClient(
        responses={
            "HUNCH:": {
                "title": "Workspace wide",
                "slug": "workspace-wide",
                "proposed_claim": "A claim long enough to pass validation comfortably.",
                "supported_by": ["s2"],  # allowed because workspace-wide
                "candidate_datasets": [],
                "recommended_skills": [],
            }
        }
    )
    h = hyp_svc.formalize_thread_hypothesis(str(kg.home), "x", stub)  # no thread scoping
    assert h["supported_by"] == ["s2"]


def test_cross_study_badge_guards_none_and_computes_membership() -> None:
    # None thread context → always False (legacy / no thread).
    assert hyp_svc.to_frontend_hypothesis({"slug": "h", "supported_by": ["s1"]})["cross_study"] is False
    # All cited sources inside the thread → False.
    inside = hyp_svc.to_frontend_hypothesis({"slug": "h", "supported_by": ["s1"]}, thread_slugs={"s1", "s2"})
    assert inside["cross_study"] is False
    # A cited source outside the thread → True (跨课题).
    outside = hyp_svc.to_frontend_hypothesis({"slug": "h", "supported_by": ["s1", "s3"]}, thread_slugs={"s1", "s2"})
    assert outside["cross_study"] is True
    # Ungrounded (no citations) → never cross-study.
    ungrounded = hyp_svc.to_frontend_hypothesis({"slug": "h", "supported_by": []}, thread_slugs={"s1"})
    assert ungrounded["cross_study"] is False


def test_list_workspace_hypotheses_computes_cross_study_with_thread_slugs(kg: KGConfig) -> None:
    _seed_source(kg, "s1")
    _seed_source(kg, "s2")
    _seed_hypothesis(kg, "h1", ["s2"])  # cites s2, which is NOT in the thread
    items = hyp_svc.list_workspace_hypotheses(str(kg.home), thread_slugs={"s1"})
    assert len(items) == 1
    assert items[0]["cross_study"] is True


def test_list_thread_sources_enriches_and_drops_stale(kg: KGConfig) -> None:
    _seed_source(kg, "s1")
    _seed_source(kg, "s2")
    # thread references s1 (exists) and s3 (deleted/stale) — s2 is not in the thread.
    out = hyp_svc.list_thread_sources(str(kg.home), ["s1", "s3"])
    assert [s["slug"] for s in out] == ["s1"]
    assert out[0]["title"] == "s1"
    assert "state" in out[0]


# --- 批7: endpoint wiring (per-thread slugs reach the service) ----------------


async def _client_with_thread_source(tmp_path, thread_id: str, slug: str):
    from omicsclaw.memory.compat import ThreadSourceMemory
    from omicsclaw.memory.database import DatabaseManager
    from omicsclaw.memory.engine import MemoryEngine
    from omicsclaw.memory.memory_client import MemoryClient
    from omicsclaw.memory.search import SearchIndexer

    db = DatabaseManager(f"sqlite+aiosqlite:///{tmp_path}/m.db")
    await db.init_db()
    client = MemoryClient(engine=MemoryEngine(db, SearchIndexer(db)), namespace="app/u")
    await client.remember(
        f"thread_source://{thread_id}/{slug}",
        ThreadSourceMemory(thread_id=thread_id, slug=slug).model_dump_json(),
    )
    return db, client


@pytest.mark.asyncio
async def test_thread_sources_endpoint_lists_thread_scoped(kg, tmp_path, monkeypatch):
    from omicsclaw.surfaces.desktop import server

    _seed_source(kg, "s1")
    _seed_source(kg, "s2")  # in workspace but NOT in the thread
    db, client = await _client_with_thread_source(tmp_path, "A", "s1")
    monkeypatch.setattr(server, "_memory_client", client)
    monkeypatch.setattr(server, "_KG_AVAILABLE", True)
    monkeypatch.setattr(server, "_resolve_shared_kg_home", lambda: str(kg.home))
    try:
        res = await server.thread_sources("A")
    finally:
        await db.close()
    assert [s["slug"] for s in res["sources"]] == ["s1"]
    assert res["returned"] == 1 and res["kg_available"] is True


@pytest.mark.asyncio
async def test_thread_formalize_endpoint_passes_thread_slugs(kg, tmp_path, monkeypatch):
    from omicsclaw.surfaces.desktop import hypotheses as hyp_svc
    from omicsclaw.surfaces.desktop import server

    db, client = await _client_with_thread_source(tmp_path, "A", "s1")
    captured: dict = {}

    def fake_formalize(home, hunch, llm, thread_source_slugs=None):
        captured["slugs"] = thread_source_slugs
        return {"id": "h", "title": "t", "claim": "c", "supported_by": [], "ungrounded": True}

    monkeypatch.setattr(hyp_svc, "formalize_thread_hypothesis", fake_formalize)
    monkeypatch.setattr(server, "_memory_client", client)
    monkeypatch.setattr(server, "_KG_AVAILABLE", True)
    monkeypatch.setattr(server, "_resolve_shared_kg_home", lambda: str(kg.home))
    req = server.ThreadFormalizeRequest(hunch="x", stub=True)
    try:
        res = await server.thread_formalize("A", req)
    finally:
        await db.close()
    assert captured["slugs"] == ["s1"]
    assert res["thread_id"] == "A"


@pytest.mark.asyncio
async def test_thread_hypotheses_endpoint_passes_thread_slugs(kg, tmp_path, monkeypatch):
    from omicsclaw.surfaces.desktop import hypotheses as hyp_svc
    from omicsclaw.surfaces.desktop import server

    db, client = await _client_with_thread_source(tmp_path, "A", "s1")
    captured: dict = {}

    def fake_list(home, *, limit=50, thread_slugs=None):
        captured["thread_slugs"] = thread_slugs
        return []

    monkeypatch.setattr(hyp_svc, "list_workspace_hypotheses", fake_list)
    monkeypatch.setattr(server, "_memory_client", client)
    monkeypatch.setattr(server, "_KG_AVAILABLE", True)
    monkeypatch.setattr(server, "_resolve_shared_kg_home", lambda: str(kg.home))
    try:
        res = await server.thread_hypotheses("A")
    finally:
        await db.close()
    assert captured["thread_slugs"] == {"s1"}
    assert res["kg_available"] is True
