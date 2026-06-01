"""Thread-scoped Ideate listing + formalize for the Bench surface (ADR 0021).

KG is thread-blind (ADR 0021 §1): true per-thread filtering and the cross-study
(跨课题) badge need a thread<->Source association that does not exist yet (the
0019 gap — ``kg_ingest`` records no thread, ``ThreadMemory`` has no source list).
So this v1.5 slice is **workspace-wide**: it lists every Hypothesis page in the
workspace KG and grounds formalize against every workspace Source page. The
``thread_id`` is carried through by the caller and the returned shape keeps a
``cross_study`` field (always False for now) so per-thread scoping can be layered
in later without changing the frontend contract.

All KG access is in-process via ``omicsclaw_kg`` (the same package the desktop
server mounts at ``/kg``), keyed by the resolved KG ``home`` string.
"""

from __future__ import annotations

from typing import Any


def to_frontend_hypothesis(raw: dict[str, Any]) -> dict[str, Any]:
    """Map a KG hypothesis (frontmatter dict or a formalize result) to the shape
    the frontend HypothesisCard renders. Both sources use the same KG field names
    (slug/title/proposed_claim/supported_by/...), so one mapper serves both.
    """
    supported = [str(s) for s in (raw.get("supported_by") or [])]
    return {
        "id": str(raw.get("slug") or raw.get("id") or ""),
        "title": str(raw.get("title") or ""),
        "claim": str(raw.get("proposed_claim") or raw.get("claim") or ""),
        "supported_by": supported,
        "ungrounded": len(supported) == 0,
        "candidate_datasets": [str(d) for d in (raw.get("candidate_datasets") or [])],
        "recommended_skills": [str(s) for s in (raw.get("recommended_skills") or [])],
        # Deferred to 0019 (needs thread<->source association); always False here.
        "cross_study": False,
        "status": str(raw.get("status") or "draft"),
    }


def list_workspace_source_slugs(home: str, *, limit: int = 1000) -> list[str]:
    """Every Source page slug in the workspace KG — the allow-list for formalize."""
    from omicsclaw_kg.mcp_server import tools

    listing = tools.kg_list_pages(page_type="sources", limit=limit, home=home)
    if "error" in listing:
        raise RuntimeError(listing["error"])
    return [str(p["slug"]) for p in listing.get("pages", []) if p.get("slug")]


def list_workspace_hypotheses(home: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """List workspace hypotheses (newest-first) mapped to the frontend shape."""
    from omicsclaw_kg.mcp_server import tools

    listing = tools.kg_list_pages(page_type="hypotheses", limit=limit, home=home)
    if "error" in listing:
        raise RuntimeError(listing["error"])

    out: list[dict[str, Any]] = []
    for page in listing.get("pages", []):
        slug = page.get("slug")
        if not slug:
            continue
        detail = tools.kg_get_page(page_type="hypotheses", slug=slug, home=home)
        if "error" in detail:
            continue
        out.append(to_frontend_hypothesis(detail.get("frontmatter") or {}))
    return out


def formalize_thread_hypothesis(home: str, hunch: str, llm: Any) -> dict[str, Any]:
    """Ground a free-text hunch against every workspace Source, returning the new
    hypothesis in the frontend shape. ``llm`` is a KG ``LLMClient`` (injected so
    tests can pass a stub). May raise ``HypothesisIdeationError`` on a bad draft.
    """
    from omicsclaw_kg import config as kg_config
    from omicsclaw_kg.ideation.formalize import formalize_hypothesis

    cfg = kg_config.resolve(home)
    source_slugs = list_workspace_source_slugs(home)
    result = formalize_hypothesis(cfg, hunch, source_slugs, llm)
    return to_frontend_hypothesis(result)
